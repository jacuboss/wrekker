//! 3-band parametric EQ per deck (low-shelf + peak-mid + high-shelf).
//! Filter state lives exclusively on the audio thread; coefficients are
//! refreshed from pending atomics at the start of each callback.

use crate::shared::DeckShared;
use std::f32::consts::PI;
use std::sync::atomic::Ordering;
use std::sync::Arc;

// ── Single biquad filter ─────────────────────────────────────────────────────

struct Biquad {
    b: [f32; 3],      // [b0, b1, b2]
    a: [f32; 2],      // [a1, a2]  (a0 normalised out)
    z: [[f32; 2]; 2], // delay state per channel: z[ch][stage]
}

impl Biquad {
    fn low_shelf(fc: f32, gain_db: f32, fs: f32) -> Self {
        let a = 10.0_f32.powf(gain_db / 40.0);
        let w0 = 2.0 * PI * fc / fs;
        let cos_w0 = w0.cos();
        let sin_w0 = w0.sin();
        let alpha = sin_w0 / 2.0 * 2.0_f32.sqrt() * a.sqrt();

        let b0 = a * ((a + 1.0) - (a - 1.0) * cos_w0 + 2.0 * a.sqrt() * alpha);
        let b1 = 2.0 * a * ((a - 1.0) - (a + 1.0) * cos_w0);
        let b2 = a * ((a + 1.0) - (a - 1.0) * cos_w0 - 2.0 * a.sqrt() * alpha);
        let a0 = (a + 1.0) + (a - 1.0) * cos_w0 + 2.0 * a.sqrt() * alpha;
        let a1 = -2.0 * ((a - 1.0) + (a + 1.0) * cos_w0);
        let a2 = (a + 1.0) + (a - 1.0) * cos_w0 - 2.0 * a.sqrt() * alpha;
        Self::from_coeffs(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)
    }

    fn high_shelf(fc: f32, gain_db: f32, fs: f32) -> Self {
        let a = 10.0_f32.powf(gain_db / 40.0);
        let w0 = 2.0 * PI * fc / fs;
        let cos_w0 = w0.cos();
        let sin_w0 = w0.sin();
        let alpha = sin_w0 / 2.0 * 2.0_f32.sqrt() * a.sqrt();

        let b0 = a * ((a + 1.0) + (a - 1.0) * cos_w0 + 2.0 * a.sqrt() * alpha);
        let b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * cos_w0);
        let b2 = a * ((a + 1.0) + (a - 1.0) * cos_w0 - 2.0 * a.sqrt() * alpha);
        let a0 = (a + 1.0) - (a - 1.0) * cos_w0 + 2.0 * a.sqrt() * alpha;
        let a1 = 2.0 * ((a - 1.0) - (a + 1.0) * cos_w0);
        let a2 = (a + 1.0) - (a - 1.0) * cos_w0 - 2.0 * a.sqrt() * alpha;
        Self::from_coeffs(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)
    }

    fn peak_eq(fc: f32, gain_db: f32, q: f32, fs: f32) -> Self {
        let a = 10.0_f32.powf(gain_db / 40.0);
        let w0 = 2.0 * PI * fc / fs;
        let cos_w0 = w0.cos();
        let alpha = w0.sin() / (2.0 * q);

        let b0 = 1.0 + alpha * a;
        let b1 = -2.0 * cos_w0;
        let b2 = 1.0 - alpha * a;
        let a0 = 1.0 + alpha / a;
        let a1 = -2.0 * cos_w0;
        let a2 = 1.0 - alpha / a;
        Self::from_coeffs(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)
    }

    fn from_coeffs(b0: f32, b1: f32, b2: f32, a1: f32, a2: f32) -> Self {
        Self {
            b: [b0, b1, b2],
            a: [a1, a2],
            z: [[0.0; 2]; 2],
        }
    }

    #[inline(always)]
    fn process(&mut self, x: f32, ch: usize) -> f32 {
        let y = self.b[0] * x + self.z[ch][0];
        self.z[ch][0] = self.b[1] * x - self.a[0] * y + self.z[ch][1];
        self.z[ch][1] = self.b[2] * x - self.a[1] * y;
        y
    }

    fn update_coeffs(&mut self, other: &Biquad) {
        self.b = other.b;
        self.a = other.a;
        // z kept to avoid a click on coefficient update
    }
}

// ── Public 3-band EQ ─────────────────────────────────────────────────────────

pub struct ThreeBandEQ {
    low: Biquad,
    mid: Biquad,
    high: Biquad,
    low_db: f32,
    mid_db: f32,
    high_db: f32,
    fs: f32,
}

// ── Bipolar channel filter for CFX knobs ─────────────────────────────────────

pub struct ChannelFilter {
    value: f32,
    lp_z: [f32; 2],
    fs: f32,
}

impl ChannelFilter {
    pub fn new(fs: f32) -> Self {
        Self {
            value: 0.0,
            lp_z: [0.0; 2],
            fs,
        }
    }

    #[inline]
    fn coeff(&self, cutoff: f32) -> f32 {
        let x = (-2.0 * PI * cutoff.clamp(20.0, self.fs * 0.45) / self.fs).exp();
        1.0 - x
    }

    #[inline]
    fn curve(v: f32) -> f32 {
        let a = v.abs();
        if a < 0.025 {
            0.0
        } else {
            ((a - 0.025) / 0.975).clamp(0.0, 1.0)
        }
    }

    pub fn process(&mut self, shared: &Arc<DeckShared>, data: &mut [f32]) {
        use std::sync::atomic::Ordering::Relaxed;
        let target = shared.channel_filter.load(Relaxed).clamp(-1.0, 1.0);
        let n_frames = (data.len() / 2).max(1) as f32;
        let smooth = 1.0 - (-n_frames / (self.fs * 0.020)).exp();
        self.value += (target - self.value) * smooth;

        let amount = Self::curve(self.value);
        if amount <= 0.0 {
            return;
        }

        if self.value < 0.0 {
            // Left: low-pass, cutoff falls from open to closed.
            let cutoff = 18_000.0 * (1.0 - amount) + 220.0 * amount;
            let a = self.coeff(cutoff);
            for frame in data.chunks_exact_mut(2) {
                for ch in 0..2 {
                    self.lp_z[ch] += a * (frame[ch] - self.lp_z[ch]);
                    frame[ch] = self.lp_z[ch];
                }
            }
        } else {
            // Right: high-pass, cutoff rises from subsonic to aggressive.
            let cutoff = 30.0 * (1.0 - amount) + 5_500.0 * amount;
            let a = self.coeff(cutoff);
            for frame in data.chunks_exact_mut(2) {
                for ch in 0..2 {
                    self.lp_z[ch] += a * (frame[ch] - self.lp_z[ch]);
                    frame[ch] = frame[ch] - self.lp_z[ch];
                }
            }
        }
    }
}

impl ThreeBandEQ {
    pub fn new(fs: f32) -> Self {
        Self {
            low: Biquad::low_shelf(200.0, 0.0, fs),
            mid: Biquad::peak_eq(1000.0, 0.0, 0.9, fs),
            high: Biquad::high_shelf(8000.0, 0.0, fs),
            low_db: 0.0,
            mid_db: 0.0,
            high_db: 0.0,
            fs,
        }
    }

    /// Poll shared state for pending EQ changes. Called at start of each audio callback.
    pub fn apply_pending(&mut self, shared: &Arc<DeckShared>) {
        use std::sync::atomic::Ordering::Relaxed;
        if !shared.eq_dirty.swap(false, Ordering::AcqRel) {
            return;
        }
        let lo = shared.eq_low_db.load(Relaxed);
        let mi = shared.eq_mid_db.load(Relaxed);
        let hi = shared.eq_high_db.load(Relaxed);

        if (lo - self.low_db).abs() > 1e-4 {
            self.low
                .update_coeffs(&Biquad::low_shelf(200.0, lo, self.fs));
            self.low_db = lo;
        }
        if (mi - self.mid_db).abs() > 1e-4 {
            self.mid
                .update_coeffs(&Biquad::peak_eq(1000.0, mi, 0.9, self.fs));
            self.mid_db = mi;
        }
        if (hi - self.high_db).abs() > 1e-4 {
            self.high
                .update_coeffs(&Biquad::high_shelf(8000.0, hi, self.fs));
            self.high_db = hi;
        }
    }

    /// Process interleaved stereo buffer in-place. Skips bands at 0 dB.
    #[inline]
    pub fn process(&mut self, data: &mut [f32]) {
        for frame in data.chunks_exact_mut(2) {
            for ch in 0..2 {
                let mut s = frame[ch];
                if self.low_db.abs() > 1e-4 {
                    s = self.low.process(s, ch);
                }
                if self.mid_db.abs() > 1e-4 {
                    s = self.mid.process(s, ch);
                }
                if self.high_db.abs() > 1e-4 {
                    s = self.high.process(s, ch);
                }
                frame[ch] = s;
            }
        }
    }
}
