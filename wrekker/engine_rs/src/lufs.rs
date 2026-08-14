//! ITU-R BS.1770-4 K-weighted LUFS metering.
//!
//! Two biquad stages per channel (pre-filter + RLB high-pass), then a sliding
//! mean-square ring buffer for momentary (400 ms) and short-term (3 s) windows.
//! Processing one block at a time: the ring buffer stores one f32 per callback,
//! not per sample, so the buffers stay tiny.

use std::f32::consts::PI;

struct Biquad {
    b: [f32; 3],
    a: [f32; 2],
    z: [[f32; 2]; 2], // z[ch][delay_line_stage]
}

impl Biquad {
    fn new(b: [f32; 3], a: [f32; 2]) -> Self {
        Self {
            b,
            a,
            z: [[0.0; 2]; 2],
        }
    }

    #[inline(always)]
    fn step(&mut self, x: f32, ch: usize) -> f32 {
        let y = self.b[0] * x + self.z[ch][0];
        self.z[ch][0] = self.b[1] * x - self.a[0] * y + self.z[ch][1];
        self.z[ch][1] = self.b[2] * x - self.a[1] * y;
        y
    }

    fn reset(&mut self) {
        self.z = [[0.0; 2]; 2];
    }
}

fn k_weighting_coeffs(sr: f32) -> ([f32; 3], [f32; 2], [f32; 3], [f32; 2]) {
    // Stage 1: high-shelf pre-filter (ITU-R BS.1770-4 §2.2)
    let f0 = 1681.974450955533_f32;
    let g = 3.999843853973347_f32;
    let q = 0.7071752369554196_f32;
    let k = (PI * f0 / sr).tan();
    let vh = 10.0_f32.powf(g / 20.0);
    let vb = vh.powf(0.4996667741545416_f32);
    let a0 = 1.0 + k / q + k * k;
    let pre_b = [
        (vh + vb * k / q + k * k) / a0,
        2.0 * (k * k - vh) / a0,
        (vh - vb * k / q + k * k) / a0,
    ];
    let pre_a = [2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0];

    // Stage 2: high-pass RLB weighting
    let f1 = 38.13547087602444_f32;
    let q1 = 0.5003270373238773_f32;
    let k1 = (PI * f1 / sr).tan();
    let a0 = 1.0 + k1 / q1 + k1 * k1;
    let rlb_b = [1.0 / a0, -2.0 / a0, 1.0 / a0];
    let rlb_a = [2.0 * (k1 * k1 - 1.0) / a0, (1.0 - k1 / q1 + k1 * k1) / a0];

    (pre_b, pre_a, rlb_b, rlb_a)
}

const N_BINS: usize = 30;         // 30 × 100 ms = 3 s short-term window
const MOMENTARY_BINS: usize = 4;  //  4 × 100 ms = 400 ms momentary window

/// Sliding mean-square over fixed 100 ms bins counted in *frames*, so the
/// window durations hold regardless of the host's callback size (PipeWire
/// quanta routinely differ from the requested blocksize).
struct MsRing {
    sums: [f64; N_BINS],
    counts: [u32; N_BINS],
    head: usize,
    filled: usize,
    cur_sum: f64,
    cur_n: u32,
    bin_frames: u32,
}

impl MsRing {
    fn new(sr: u32) -> Self {
        Self {
            sums: [0.0; N_BINS],
            counts: [0; N_BINS],
            head: 0,
            filled: 0,
            cur_sum: 0.0,
            cur_n: 0,
            bin_frames: (sr / 10).max(1),
        }
    }

    #[inline(always)]
    fn push(&mut self, ms: f32) {
        self.cur_sum += ms as f64;
        self.cur_n += 1;
        if self.cur_n >= self.bin_frames {
            self.sums[self.head] = self.cur_sum;
            self.counts[self.head] = self.cur_n;
            self.head = (self.head + 1) % N_BINS;
            if self.filled < N_BINS {
                self.filled += 1;
            }
            self.cur_sum = 0.0;
            self.cur_n = 0;
        }
    }

    /// Mean square over the newest `k` complete bins plus the partial bin.
    fn mean_last(&self, k: usize) -> f64 {
        let mut sum = self.cur_sum;
        let mut n = self.cur_n as u64;
        let take = k.min(self.filled);
        for i in 0..take {
            let idx = (self.head + N_BINS - 1 - i) % N_BINS;
            sum += self.sums[idx];
            n += self.counts[idx] as u64;
        }
        if n == 0 {
            0.0
        } else {
            sum / n as f64
        }
    }

    fn reset(&mut self) {
        self.sums = [0.0; N_BINS];
        self.counts = [0; N_BINS];
        self.head = 0;
        self.filled = 0;
        self.cur_sum = 0.0;
        self.cur_n = 0;
    }
}

pub struct KWeightedLUFS {
    pre: Biquad,
    rlb: Biquad,
    ring: MsRing,
}

impl KWeightedLUFS {
    pub fn new(sr: u32, _blocksize: usize) -> Self {
        let (pre_b, pre_a, rlb_b, rlb_a) = k_weighting_coeffs(sr as f32);
        Self {
            pre: Biquad::new(pre_b, pre_a),
            rlb: Biquad::new(rlb_b, rlb_a),
            ring: MsRing::new(sr),
        }
    }

    /// Process one interleaved stereo block; returns (momentary_LUFS, short_term_LUFS).
    pub fn process(&mut self, buf: &[f32]) -> (f32, f32) {
        let n = buf.len() / 2;

        for i in 0..n {
            let mut ms = 0.0_f32;
            for ch in 0..2 {
                let x = buf[i * 2 + ch];
                let y1 = self.pre.step(x, ch);
                let y2 = self.rlb.step(y1, ch);
                ms += y2 * y2;
            }
            // BS.1770-4 §4: stereo loudness sums the per-channel mean squares
            // (G = 1.0 for L/R); averaging here would read 3 dB low.
            self.ring.push(ms);
        }

        fn to_lufs(ms: f64) -> f32 {
            if ms < 1e-10 {
                f32::NEG_INFINITY
            } else {
                (-0.691 + 10.0 * ms.log10()) as f32
            }
        }

        (
            to_lufs(self.ring.mean_last(MOMENTARY_BINS)),
            to_lufs(self.ring.mean_last(N_BINS)),
        )
    }

    pub fn reset(&mut self) {
        self.pre.reset();
        self.rlb.reset();
        self.ring.reset();
    }
}
