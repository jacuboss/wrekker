//! Per-deck audio-thread state: variable-rate playback, loop, metering.
//! Nothing here is ever called with the Python GIL held.

use std::sync::atomic::Ordering;
use std::sync::Arc;

use crate::fx::{
    FxShared, TARGET_BOTH, WREKK_FX_BASS_LOCK, WREKK_FX_DECONSTRUCT,
    WREKK_FX_DRUM_CRUSH, WREKK_FX_REBUILD, WREKK_FX_RHYTHM_GATE, WREKK_FX_STEM_ROLL,
    WREKK_FX_TOP_WASH, WREKK_FX_VOCAL_GHOST, WREKK_STEM_BSS, WREKK_STEM_DRM,
    WREKK_STEM_OTH, WREKK_STEM_RHYTHM, WREKK_STEM_TOP, WREKK_STEM_VOC,
};
use crate::lufs::KWeightedLUFS;
use crate::shared::{AudioBuffers, DeckShared, N_SPECTRUM, N_STEMS};

// ── Spectrum analysis (Goertzel per log-band) ────────────────────────────────

const BAND_FREQS: [f32; N_SPECTRUM] = [
    31.0, 50.0, 80.0, 125.0, 200.0, 315.0, 500.0, 800.0, 1250.0, 2000.0, 3150.0, 5000.0, 8000.0,
    10000.0, 14000.0, 18000.0,
];

const MAX_WREKK_DELAY_FRAMES: usize = 5 * 48_001;
const MAX_WREKK_ROLL_FRAMES: usize = 2 * 48_001;

struct WrekkFxDeckState {
    delay_buf: [[Vec<f32>; 2]; N_STEMS],
    delay_write: [usize; N_STEMS],
    roll_buf: [[Vec<f32>; 2]; N_STEMS],
    roll_write: [usize; N_STEMS],
    roll_read: [usize; N_STEMS],
    roll_start: [usize; N_STEMS],
    roll_len: usize,
    gate_phase: f32,
    crush_hold: [[f32; 2]; N_STEMS],
    crush_count: [usize; N_STEMS],
    wet_smooth: f32,
    last_type: u32,
}

impl WrekkFxDeckState {
    fn new() -> Self {
        Self {
            delay_buf: std::array::from_fn(|_| {
                std::array::from_fn(|_| vec![0.0; MAX_WREKK_DELAY_FRAMES])
            }),
            delay_write: [0; N_STEMS],
            roll_buf: std::array::from_fn(|_| {
                std::array::from_fn(|_| vec![0.0; MAX_WREKK_ROLL_FRAMES])
            }),
            roll_write: [0; N_STEMS],
            roll_read: [0; N_STEMS],
            roll_start: [0; N_STEMS],
            roll_len: 1,
            gate_phase: 0.0,
            crush_hold: [[0.0; 2]; N_STEMS],
            crush_count: [0; N_STEMS],
            wet_smooth: 0.0,
            last_type: u32::MAX,
        }
    }

    fn reset_for_type(&mut self, fx_type: u32) {
        for stem in 0..N_STEMS {
            self.delay_buf[stem][0].fill(0.0);
            self.delay_buf[stem][1].fill(0.0);
            self.roll_buf[stem][0].fill(0.0);
            self.roll_buf[stem][1].fill(0.0);
            self.delay_write[stem] = 0;
            self.roll_write[stem] = 0;
            self.roll_read[stem] = 0;
            self.roll_start[stem] = 0;
            self.crush_hold[stem] = [0.0; 2];
            self.crush_count[stem] = 0;
        }
        self.roll_len = 1;
        self.gate_phase = 0.0;
        self.last_type = fx_type;
    }

    #[inline(always)]
    fn delay_tail(
        &mut self,
        stem: usize,
        l: f32,
        r: f32,
        feedback: f32,
        td: f32,
        bpm: f32,
        sr: f32,
    ) -> (f32, f32) {
        let delay_s = (60.0 / bpm * td).clamp(0.001, 4.9);
        let delay_fr = ((delay_s * sr) as usize).min(MAX_WREKK_DELAY_FRAMES - 1).max(1);
        let wr = self.delay_write[stem];
        let rd = (wr + MAX_WREKK_DELAY_FRAMES - delay_fr) % MAX_WREKK_DELAY_FRAMES;
        let dl = self.delay_buf[stem][0][rd];
        let dr = self.delay_buf[stem][1][rd];
        let fb = feedback.clamp(0.0, 0.95);
        self.delay_buf[stem][0][wr] = l + dl * fb;
        self.delay_buf[stem][1][wr] = r + dr * fb;
        self.delay_write[stem] = (wr + 1) % MAX_WREKK_DELAY_FRAMES;
        (dl, dr)
    }

    #[inline(always)]
    fn crush(&mut self, stem: usize, l: f32, r: f32, depth: f32) -> (f32, f32) {
        let bits = 15.0 - depth * 11.0;
        let levels = 2.0_f32.powf(bits - 1.0);
        let hold = (1 + (depth * 18.0) as usize).max(1);
        if self.crush_count[stem] == 0 {
            self.crush_hold[stem][0] = (l * levels).round() / levels;
            self.crush_hold[stem][1] = (r * levels).round() / levels;
        }
        self.crush_count[stem] = (self.crush_count[stem] + 1) % hold;
        (self.crush_hold[stem][0], self.crush_hold[stem][1])
    }

    #[inline(always)]
    fn roll(&mut self, stem: usize, l: f32, r: f32, td: f32, bpm: f32, sr: f32) -> (f32, f32) {
        let wr = self.roll_write[stem];
        self.roll_buf[stem][0][wr] = l;
        self.roll_buf[stem][1][wr] = r;
        self.roll_write[stem] = (wr + 1) % MAX_WREKK_ROLL_FRAMES;

        let loop_s = (60.0 / bpm * td).clamp(0.001, (MAX_WREKK_ROLL_FRAMES - 2) as f32 / sr);
        let loop_len = (loop_s * sr) as usize;
        let loop_len = loop_len.clamp(1, MAX_WREKK_ROLL_FRAMES - 1);
        if self.roll_len != loop_len {
            self.roll_len = loop_len;
            for s in 0..N_STEMS {
                self.roll_start[s] = (self.roll_write[s] + MAX_WREKK_ROLL_FRAMES - loop_len) % MAX_WREKK_ROLL_FRAMES;
                self.roll_read[s] = self.roll_start[s];
            }
        }

        let out_l = self.roll_buf[stem][0][self.roll_read[stem]];
        let out_r = self.roll_buf[stem][1][self.roll_read[stem]];
        self.roll_read[stem] = (self.roll_read[stem] + 1) % MAX_WREKK_ROLL_FRAMES;
        let offset = (self.roll_read[stem] + MAX_WREKK_ROLL_FRAMES - self.roll_start[stem]) % MAX_WREKK_ROLL_FRAMES;
        if offset >= self.roll_len {
            self.roll_read[stem] = self.roll_start[stem];
        }
        (out_l, out_r)
    }
}

fn goertzel_power(mono: &[f32], freq: f32, sr: f32) -> f32 {
    let n = mono.len() as f32;
    let k = (n * freq / sr).round() as usize;
    let omega = 2.0 * std::f32::consts::PI * k as f32 / n;
    let coeff = 2.0 * omega.cos();
    let (mut s1, mut s2) = (0.0_f32, 0.0_f32);
    for &x in mono {
        let s0 = x + coeff * s1 - s2;
        s2 = s1;
        s1 = s0;
    }
    let raw = s1 * s1 + s2 * s2 - coeff * s1 * s2;
    raw.max(0.0) / (n * n)
}

// ── Smooth per-stem gain ─────────────────────────────────────────────────────

struct StemSmoother {
    current: f32,
    alpha: f32, // per-sample coefficient
}

impl StemSmoother {
    fn new(tau_s: f32, sr: f32) -> Self {
        let tau_frames = (tau_s * sr).max(1.0);
        let alpha = 1.0 - (-1.0_f32 / tau_frames).exp();
        Self {
            current: 1.0,
            alpha,
        }
    }

    /// Closed-form block advance: returns gain at START of block, updates current.
    #[inline]
    fn advance(&mut self, target: f32, n: usize) -> f32 {
        let decay = (1.0 - self.alpha).powi(n as i32);
        self.current = self.current * decay + target * (1.0 - decay);
        self.current
    }

    fn reset(&mut self, target: f32) {
        self.current = target;
    }
}

// ── Scratch engine (audio-thread side) ───────────────────────────────────────

struct ScratchEngine {
    /// Samples per jog tick: sr * 60 / (33.333 rpm * 720 ticks/rev)
    frames_per_tick: f64,
    /// Release ramp time constant in frames: sr * 0.15
    rel_tau_frames: f64,
    /// Whether scratch was active on the previous block (for edge detection).
    active_prev: bool,
    /// True while ramping from scratch rate back to normal rate.
    releasing: bool,
    /// Rate at the end of the last active-scratch block (release ramp start).
    last_rate: f64,
    /// Smoothed scratch rate to avoid block-to-block zipper artifacts.
    current_rate: f64,
    /// Scratch-rate smoothing time constant in frames.
    rate_tau_frames: f64,
}

impl ScratchEngine {
    fn new(sr: f32) -> Self {
        Self {
            frames_per_tick: sr as f64 * 60.0 / (33.333 * 720.0),
            rel_tau_frames: sr as f64 * 0.15,
            active_prev: false,
            releasing: false,
            last_rate: 1.0,
            current_rate: 0.0,
            rate_tau_frames: sr as f64 * 0.012,
        }
    }

    #[inline]
    fn smooth_rate(&mut self, target: f64, n_out: usize) -> f64 {
        let alpha = 1.0 - (-(n_out as f64) / self.rate_tau_frames).exp();
        self.current_rate += alpha * (target - self.current_rate);
        self.current_rate
    }
}

// ── Nudge engine (side jog / pitch bend, audio-thread side) ──────────────────

struct NudgeEngine {
    current: f64,
    tau_frames: f64, // smoothing time constant (frames)
}

impl NudgeEngine {
    fn new(sr: f32) -> Self {
        Self {
            current: 0.0,
            tau_frames: sr as f64 * 0.06, // 60 ms — responsive, not jumpy
        }
    }

    /// Advance toward `target` using one-pole LP; returns the new current value.
    #[inline]
    fn advance(&mut self, target: f64, n_out: usize) -> f64 {
        let alpha = 1.0 - (-(n_out as f64) / self.tau_frames).exp();
        self.current += alpha * (target - self.current);
        if self.current.abs() < 1e-5 {
            self.current = 0.0;
        }
        self.current
    }
}

// ── Spectrum accumulator ─────────────────────────────────────────────────────

const SPEC_WIN: usize = 1024;

struct SpectrumAccum {
    buf: Vec<f32>,
}

impl SpectrumAccum {
    fn new(_sr: f32) -> Self {
        Self {
            buf: Vec::with_capacity(SPEC_WIN),
        }
    }

    fn push_block(&mut self, block: &[f32]) -> Option<Vec<f32>> {
        self.buf.extend_from_slice(block);
        if self.buf.len() >= SPEC_WIN {
            let window: Vec<f32> = self.buf.drain(..SPEC_WIN).collect();
            let n = window.len() as f32;
            let windowed: Vec<f32> = window
                .iter()
                .enumerate()
                .map(|(i, &x)| {
                    let w = 0.5 * (1.0 - (2.0 * std::f32::consts::PI * i as f32 / (n - 1.0)).cos());
                    x * w
                })
                .collect();
            Some(windowed)
        } else {
            None
        }
    }
}

// ── 4-point Hermite (Catmull-Rom) interpolation ───────────────────────────────
// Significantly better high-frequency behaviour at non-unity rates vs. linear,
// especially during slow scratch (rate < 0.7×).  Cost: ~12 MACs vs. ~4 — still
// negligible inside a 256-frame callback.

#[inline(always)]
fn hermite_frame(data: &[f32], frame_i: usize, frac: f32, n_frames: usize) -> (f32, f32) {
    if frame_i >= n_frames {
        return (0.0, 0.0);
    }
    // Clamp neighbours to valid frame range
    let f0 = frame_i.saturating_sub(1);
    let f1 = frame_i;
    let f2 = (frame_i + 1).min(n_frames - 1);
    let f3 = (frame_i + 2).min(n_frames - 1);

    let t = frac;
    let t2 = t * t;
    let t3 = t2 * t;

    // Hermite basis (matches standard cubic-spline basis)
    let h00 = 2.0 * t3 - 3.0 * t2 + 1.0; // weight for p1
    let h10 = t3 - 2.0 * t2 + t; // weight for tangent m1
    let h01 = -2.0 * t3 + 3.0 * t2; // weight for p2
    let h11 = t3 - t2; // weight for tangent m2

    let sample = |frame: usize, ch: usize| -> f32 {
        let idx = frame * 2 + ch;
        if idx < data.len() {
            data[idx]
        } else {
            0.0
        }
    };

    let interp = |ch: usize| -> f32 {
        let p0 = sample(f0, ch);
        let p1 = sample(f1, ch);
        let p2 = sample(f2, ch);
        let p3 = sample(f3, ch);
        // Catmull-Rom tangents: m = 0.5 * (p_next - p_prev)
        let m1 = 0.5 * (p2 - p0);
        let m2 = 0.5 * (p3 - p1);
        h00 * p1 + h10 * m1 + h01 * p2 + h11 * m2
    };

    (interp(0), interp(1))
}

// ── Per-deck audio-thread state ───────────────────────────────────────────────

pub struct DeckAudioState {
    pub shared: Arc<DeckShared>,
    fx_shared: Arc<FxShared>,
    fx_target_id: u32,
    buffers: Option<Arc<AudioBuffers>>,
    epoch_seen: u64,
    position: usize, // integer frame (for display / epoch bookkeeping)
    pos_f: f64,      // fractional position (used for playback stepping)

    smoothers: [StemSmoother; N_STEMS],
    lufs: KWeightedLUFS,
    spec_accum: SpectrumAccum,
    scratch: ScratchEngine,
    nudge: NudgeEngine,
    wrekk_fx: WrekkFxDeckState,
}

impl DeckAudioState {
    pub fn new(shared: Arc<DeckShared>, fx_shared: Arc<FxShared>, sr: u32, _blocksize: usize, fx_target_id: u32) -> Self {
        let fs = sr as f32;
        Self {
            shared: shared.clone(),
            fx_shared,
            fx_target_id,
            buffers: None,
            epoch_seen: u64::MAX,
            position: 0,
            pos_f: 0.0,
            smoothers: std::array::from_fn(|_| StemSmoother::new(0.15, fs)),
            lufs: KWeightedLUFS::new(sr, 256),
            spec_accum: SpectrumAccum::new(fs),
            scratch: ScratchEngine::new(fs),
            nudge: NudgeEngine::new(fs),
            wrekk_fx: WrekkFxDeckState::new(),
        }
    }

    // ── Called once per CPAL callback ────────────────────────────────────────

    pub fn fill(&mut self, buf: &mut [f32], sr: f32) {
        use Ordering::{AcqRel, Acquire, Relaxed};

        // ── Check for new buffer ──────────────────────────────────────────
        let epoch = self.shared.buffer_epoch.load(Acquire);
        if epoch != self.epoch_seen {
            if let Ok(guard) = self.shared.buffer.try_read() {
                let new_bufs = guard.clone();
                if let Some(ref b) = new_bufs {
                    if let Some(start) = b.start_position {
                        self.position = start;
                        self.pos_f = start as f64;
                        self.lufs.reset();
                        for (i, sm) in self.smoothers.iter_mut().enumerate() {
                            sm.reset(self.shared.stem_targets[i].load(Relaxed));
                        }
                    }
                }
                self.buffers = new_bufs;
                self.epoch_seen = epoch;
            }
        }

        // ── Handle pending seek ───────────────────────────────────────────
        let seek = self.shared.pending_seek.swap(-1, AcqRel);
        if seek >= 0 {
            self.position = seek as usize;
            self.pos_f = seek as f64;
        }

        buf.fill(0.0);

        // ── Scratch state (read before playing guard to detect transitions) ──
        let sc_active = self.shared.scratch.scratch_active.load(Relaxed);
        let sc_ticks = self.shared.scratch.pending_ticks.swap(0, AcqRel);
        let sc_target = self.shared.scratch.target_rate.load(Relaxed) as f64;

        // Detect scratch-off edge: active last block, not active now → start ramp
        if self.scratch.active_prev && !sc_active {
            self.scratch.releasing = true;
        }
        self.scratch.active_prev = sc_active;
        let bypass_stop = sc_active || self.scratch.releasing;

        // ── Peak decay always fires, even when stopped ────────────────────
        const PEAK_DECAY: f32 = 0.85;
        if !self.shared.playing.load(Relaxed) && !bypass_stop {
            let pl = self.shared.peak_l.load(Relaxed);
            let pr = self.shared.peak_r.load(Relaxed);
            self.shared.peak_l.store(pl * PEAK_DECAY, Relaxed);
            self.shared.peak_r.store(pr * PEAK_DECAY, Relaxed);
            for i in 0..N_STEMS {
                let p = self.shared.stem_peaks[i].load(Relaxed);
                self.shared.stem_peaks[i].store(p * PEAK_DECAY, Relaxed);
            }
            self.shared.position.store(self.position as u64, Relaxed);
            return;
        }

        let buffers = match self.buffers.as_ref() {
            Some(b) => Arc::clone(b),
            None => {
                // No buffer loaded — still decay peaks
                let pl = self.shared.peak_l.load(Relaxed);
                let pr = self.shared.peak_r.load(Relaxed);
                self.shared.peak_l.store(pl * PEAK_DECAY, Relaxed);
                self.shared.peak_r.store(pr * PEAK_DECAY, Relaxed);
                for i in 0..N_STEMS {
                    let p = self.shared.stem_peaks[i].load(Relaxed);
                    self.shared.stem_peaks[i].store(p * PEAK_DECAY, Relaxed);
                }
                self.shared.position.store(self.position as u64, Relaxed);
                return;
            }
        };

        let n_frames = buffers.n_frames;
        let n_out = buf.len() / 2; // output stereo frames

        // ── Nudge offset (side jog / pitch bend) — always advance so it decays ──
        let nudge_target = self.shared.nudge_target.load(Relaxed) as f64;
        let nudge_current = self.nudge.advance(nudge_target, n_out);

        // ── Effective playback rate: scratch > release ramp > normal+nudge ────
        let rate = if sc_active {
            let target = sc_ticks as f64 * self.scratch.frames_per_tick / n_out as f64;
            let r = self.scratch.smooth_rate(target.clamp(-8.0, 8.0), n_out);
            self.scratch.last_rate = r;
            r
        } else if self.scratch.releasing {
            let alpha = 1.0 - (-(n_out as f64) / self.scratch.rel_tau_frames).exp();
            let new_rate = self.scratch.last_rate + alpha * (sc_target - self.scratch.last_rate);
            if (new_rate - sc_target).abs() < 0.02 {
                self.scratch.releasing = false;
                self.scratch.last_rate = sc_target;
                self.scratch.current_rate = sc_target;
                sc_target
            } else {
                self.scratch.last_rate = new_rate;
                self.scratch.current_rate = new_rate;
                new_rate
            }
        } else {
            // Normal play: base rate + temporary nudge offset
            (self.shared.playback_rate.load(Relaxed) as f64 + nudge_current).max(0.0)
        };

        // ── Loop parameters ───────────────────────────────────────────────
        let loop_active = self.shared.loop_active.load(Relaxed);
        let loop_start = self.shared.loop_start.load(Relaxed) as usize;
        let loop_end = self.shared.loop_end.load(Relaxed) as usize;
        let loop_valid = loop_active && loop_end > loop_start && loop_end <= n_frames;

        // ── Clamp position into loop range (handles seeks past loop_end) ──
        if loop_valid && self.pos_f >= loop_end as f64 {
            let span = (loop_end - loop_start) as f64;
            let overshoot = self.pos_f - loop_start as f64;
            self.pos_f = loop_start as f64 + overshoot % span.max(1.0);
            self.position = self.pos_f as usize;
        }

        // ── Block-level smoother advance (per OUTPUT frame count) ─────────
        let mut gains = [0.0f32; N_STEMS];
        for (i, sm) in self.smoothers.iter_mut().enumerate() {
            gains[i] = sm.advance(self.shared.stem_targets[i].load(Relaxed), n_out);
        }

        let wrekk_enabled = self.fx_shared.wrekk_enabled.load(Relaxed);
        let wrekk_target = self.fx_shared.wrekk_target.load(Relaxed);
        let wrekk_applies = wrekk_enabled && (wrekk_target == self.fx_target_id || wrekk_target == TARGET_BOTH);
        let wrekk_type = self.fx_shared.wrekk_type.load(Relaxed);
        if self.wrekk_fx.last_type != wrekk_type {
            self.wrekk_fx.reset_for_type(wrekk_type);
        }
        let wrekk_wet = self.fx_shared.wrekk_wet.load(Relaxed).clamp(0.0, 1.0);
        let wrekk_depth = self.fx_shared.wrekk_depth.load(Relaxed).clamp(0.0, 1.0);
        let wrekk_feedback = self.fx_shared.wrekk_feedback.load(Relaxed).clamp(0.0, 0.95);
        let wrekk_td = self.fx_shared.wrekk_time_division.load(Relaxed).clamp(0.0625, 4.0);
        let wrekk_color = self.fx_shared.wrekk_color.load(Relaxed).clamp(-1.0, 1.0);
        let wrekk_bpm = self.fx_shared.wrekk_bpm.load(Relaxed).clamp(20.0, 300.0);
        let wrekk_stem_target = self.fx_shared.wrekk_stem_target.load(Relaxed);
        let wet_target = if wrekk_applies { wrekk_wet } else { 0.0 };
        let wet_coeff = 1.0 - (-1.0_f32 / (0.020 * sr)).exp();

        // ── Per-stem peak tracking ────────────────────────────────────────
        let mut stem_peak = [0.0f32; N_STEMS];

        // ── Main render loop (per output frame) ───────────────────────────
        let mut frames_filled: usize = 0;

        for out_i in 0..n_out {
            // Guard: backward scratch past start of track
            if self.pos_f < 0.0 {
                self.pos_f = 0.0;
                break;
            }

            let src_i = self.pos_f as usize;
            let frac = (self.pos_f - src_i as f64) as f32;

            // End-of-track (no loop)
            if src_i >= n_frames {
                break;
            }

            if let Some(stems) = &buffers.stems {
                self.wrekk_fx.wet_smooth += wet_coeff * (wet_target - self.wrekk_fx.wet_smooth);
                let wrekk_w = self.wrekk_fx.wet_smooth;
                if wrekk_type == WREKK_FX_RHYTHM_GATE {
                    let gate_hz = (wrekk_bpm * wrekk_td / 60.0).max(0.01);
                    self.wrekk_fx.gate_phase += gate_hz / sr;
                    if self.wrekk_fx.gate_phase >= 1.0 {
                        self.wrekk_fx.gate_phase -= 1.0;
                    }
                }
                for stem_idx in 0..N_STEMS {
                    let gain = gains[stem_idx];
                    if gain < 1e-5 {
                        continue;
                    }
                    let (l, r) = hermite_frame(&stems[stem_idx], src_i, frac, n_frames);
                    let (pl, pr) = self.process_wrekk_stem(
                        stem_idx,
                        l * gain,
                        r * gain,
                        wrekk_type,
                        wrekk_w,
                        wrekk_depth,
                        wrekk_feedback,
                        wrekk_td,
                        wrekk_color,
                        wrekk_bpm,
                        wrekk_stem_target,
                        sr,
                    );
                    let gl = pl;
                    let gr = pr;
                    buf[out_i * 2] += gl;
                    buf[out_i * 2 + 1] += gr;
                    let pk = gl.abs().max(gr.abs());
                    if pk > stem_peak[stem_idx] {
                        stem_peak[stem_idx] = pk;
                    }
                }
            } else if let Some(orig) = &buffers.original {
                let (l, r) = hermite_frame(orig, src_i, frac, n_frames);
                buf[out_i * 2] = l;
                buf[out_i * 2 + 1] = r;
                // For original (no stems), attribute to stem 0 for peak tracking
                let pk = l.abs().max(r.abs());
                if pk > stem_peak[0] {
                    stem_peak[0] = pk;
                }
            }

            frames_filled = out_i + 1;

            // Advance fractional position by playback rate
            self.pos_f += rate;

            // Loop wrap (handles loops shorter than one output buffer)
            if loop_valid && self.pos_f >= loop_end as f64 {
                let span = (loop_end - loop_start) as f64;
                let overshoot = self.pos_f - loop_start as f64;
                self.pos_f = loop_start as f64 + overshoot % span.max(1.0);
            }
        }

        // Update display position
        self.position = self.pos_f as usize;
        self.shared.position.store(self.position as u64, Relaxed);

        if frames_filled == 0 {
            // End of track (no loop active) — decay peaks so meters fall
            let pl = self.shared.peak_l.load(Relaxed);
            let pr = self.shared.peak_r.load(Relaxed);
            self.shared.peak_l.store(pl * PEAK_DECAY, Relaxed);
            self.shared.peak_r.store(pr * PEAK_DECAY, Relaxed);
            for i in 0..N_STEMS {
                let p = self.shared.stem_peaks[i].load(Relaxed);
                self.shared.stem_peaks[i].store(p * PEAK_DECAY, Relaxed);
            }
            return;
        }

        // ── Metering ──────────────────────────────────────────────────────
        let filled = frames_filled * 2;

        // Per-stem peaks with decay
        const STEM_DECAY: f32 = 0.90;
        for i in 0..N_STEMS {
            let prev = self.shared.stem_peaks[i].load(Relaxed);
            self.shared.stem_peaks[i].store((prev * STEM_DECAY).max(stem_peak[i]), Relaxed);
        }

        // Overall deck peaks
        self.update_peaks(&buf[..filled]);

        // LUFS
        let (m, st) = self.lufs.process(&buf[..filled]);
        self.shared.lufs_momentary.store(m, Relaxed);
        self.shared.lufs_shortterm.store(st, Relaxed);

        // Spectrum
        let mono: Vec<f32> = buf[..filled]
            .chunks_exact(2)
            .map(|fr| (fr[0] + fr[1]) * 0.5)
            .collect();
        if let Some(windowed) = self.spec_accum.push_block(&mono) {
            self.compute_spectrum(&windowed, sr);
        }
    }

    #[allow(clippy::too_many_arguments)]
    #[inline(always)]
    fn process_wrekk_stem(
        &mut self,
        stem_idx: usize,
        l: f32,
        r: f32,
        fx_type: u32,
        wet: f32,
        depth: f32,
        feedback: f32,
        td: f32,
        color: f32,
        bpm: f32,
        stem_target: u32,
        sr: f32,
    ) -> (f32, f32) {
        let w = wet.clamp(0.0, 1.0);
        let d = depth.clamp(0.0, 1.0);
        if w < 1e-6 {
            if fx_type == WREKK_FX_STEM_ROLL && Self::stem_matches_target(stem_idx, stem_target) {
                let _ = self.wrekk_fx.roll(stem_idx, l, r, td, bpm, sr);
            }
            return (l, r);
        }

        match fx_type {
            WREKK_FX_VOCAL_GHOST if stem_idx == 0 => {
                let (tail_l, tail_r) = self.wrekk_fx.delay_tail(stem_idx, l, r, feedback, td, bpm, sr);
                let tone = 0.65 + (color + 1.0) * 0.25;
                let dry = 1.0 - w * d;
                (l * dry + tail_l * w * tone, r * dry + tail_r * w * tone)
            }
            WREKK_FX_TOP_WASH if stem_idx == 0 || stem_idx == 3 => {
                let (tail_l, tail_r) = self.wrekk_fx.delay_tail(stem_idx, l, r, feedback, td, bpm, sr);
                let wash = 0.6 + d * 0.9;
                let dry = 1.0 - w * d * 0.45;
                (l * dry + tail_l * w * wash, r * dry + tail_r * w * wash)
            }
            WREKK_FX_DRUM_CRUSH if stem_idx == 1 => {
                let (cl, cr) = self.wrekk_fx.crush(stem_idx, l, r, d);
                let tone = if color < 0.0 { 1.0 + color * 0.35 } else { 1.0 + color * 0.20 };
                (l + w * (cl * tone - l), r + w * (cr * tone - r))
            }
            WREKK_FX_RHYTHM_GATE if stem_idx == 1 || stem_idx == 2 => {
                let hard = ((color + 1.0) * 0.5).clamp(0.0, 1.0);
                let gate = if self.wrekk_fx.gate_phase < 0.5 {
                    1.0
                } else {
                    1.0 - d * (0.55 + hard * 0.45)
                };
                let amp = 1.0 + w * (gate - 1.0);
                (l * amp, r * amp)
            }
            WREKK_FX_STEM_ROLL if Self::stem_matches_target(stem_idx, stem_target) => {
                let (rl, rr) = self.wrekk_fx.roll(stem_idx, l, r, td, bpm, sr);
                let suppress = 1.0 - d * 0.35;
                (l * (1.0 - w) * suppress + rl * w, r * (1.0 - w) * suppress + rr * w)
            }
            WREKK_FX_BASS_LOCK => {
                let mult = if stem_idx == 2 {
                    1.0 + w * d * 0.25
                } else if stem_idx == 1 {
                    1.0 - w * d * 0.35
                } else {
                    1.0 - w * d * 0.85
                };
                (l * mult, r * mult)
            }
            WREKK_FX_DECONSTRUCT => {
                let mult = Self::deconstruct_multiplier(stem_idx, d, color);
                let amp = 1.0 + w * (mult - 1.0);
                (l * amp, r * amp)
            }
            WREKK_FX_REBUILD => {
                let mult = Self::rebuild_multiplier(stem_idx, d, color);
                let amp = 1.0 + w * (mult - 1.0);
                (l * amp, r * amp)
            }
            _ => (l, r),
        }
    }

    #[inline(always)]
    fn stem_matches_target(stem_idx: usize, target: u32) -> bool {
        match target {
            WREKK_STEM_VOC => stem_idx == 0,
            WREKK_STEM_DRM => stem_idx == 1,
            WREKK_STEM_BSS => stem_idx == 2,
            WREKK_STEM_OTH => stem_idx == 3,
            WREKK_STEM_TOP => stem_idx == 0 || stem_idx == 3,
            WREKK_STEM_RHYTHM => stem_idx == 1 || stem_idx == 2,
            _ => false,
        }
    }

    #[inline(always)]
    fn fade_out(progress: f32, start: f32, end: f32) -> f32 {
        if progress <= start {
            1.0
        } else if progress >= end {
            0.0
        } else {
            1.0 - (progress - start) / (end - start)
        }
    }

    #[inline(always)]
    fn fade_in(progress: f32, start: f32, end: f32) -> f32 {
        if progress <= start {
            0.0
        } else if progress >= end {
            1.0
        } else {
            (progress - start) / (end - start)
        }
    }

    #[inline(always)]
    fn deconstruct_multiplier(stem_idx: usize, depth: f32, color: f32) -> f32 {
        if color > 0.45 {
            return match stem_idx {
                1 | 2 => Self::fade_out(depth, 0.66, 1.0),
                0 | 3 => Self::fade_out(depth, 0.0, 0.66),
                _ => 1.0,
            };
        }
        if color < -0.45 {
            return match stem_idx {
                0 | 3 => Self::fade_out(depth, 0.66, 1.0),
                1 | 2 => Self::fade_out(depth, 0.0, 0.66),
                _ => 1.0,
            };
        }
        match stem_idx {
            0 => Self::fade_out(depth, 0.00, 0.33),
            3 => Self::fade_out(depth, 0.33, 0.66),
            2 => Self::fade_out(depth, 0.66, 0.92),
            1 => Self::fade_out(depth, 0.82, 1.00),
            _ => 1.0,
        }
    }

    #[inline(always)]
    fn rebuild_multiplier(stem_idx: usize, depth: f32, color: f32) -> f32 {
        if color > 0.45 {
            return match stem_idx {
                0 | 3 => Self::fade_in(depth, 0.0, 0.55),
                1 | 2 => Self::fade_in(depth, 0.45, 1.0),
                _ => 1.0,
            };
        }
        if color < -0.45 {
            return match stem_idx {
                1 | 2 => Self::fade_in(depth, 0.0, 0.55),
                0 | 3 => Self::fade_in(depth, 0.45, 1.0),
                _ => 1.0,
            };
        }
        match stem_idx {
            1 => Self::fade_in(depth, 0.00, 0.33),
            2 => Self::fade_in(depth, 0.22, 0.55),
            3 => Self::fade_in(depth, 0.45, 0.75),
            0 => Self::fade_in(depth, 0.66, 1.00),
            _ => 1.0,
        }
    }

    fn update_peaks(&self, buf: &[f32]) {
        use Ordering::Relaxed;
        const DECAY: f32 = 0.85;
        let n = buf.len() / 2;
        let (mut pl, mut pr) = (0.0_f32, 0.0_f32);
        for i in 0..n {
            pl = pl.max(buf[i * 2].abs());
            pr = pr.max(buf[i * 2 + 1].abs());
        }
        let prev_l = self.shared.peak_l.load(Relaxed);
        let prev_r = self.shared.peak_r.load(Relaxed);
        self.shared.peak_l.store((prev_l * DECAY).max(pl), Relaxed);
        self.shared.peak_r.store((prev_r * DECAY).max(pr), Relaxed);
        if pl >= 1.0 {
            self.shared.clip_l.store(true, Relaxed);
        }
        if pr >= 1.0 {
            self.shared.clip_r.store(true, Relaxed);
        }
    }

    fn compute_spectrum(&self, windowed_mono: &[f32], sr: f32) {
        use Ordering::Relaxed;
        for (band, &freq) in BAND_FREQS.iter().enumerate() {
            let power = goertzel_power(windowed_mono, freq, sr);
            let db = if power > 1e-12 {
                (10.0 * power.log10() as f64) as f32
            } else {
                -96.0
            };
            self.shared.spectrum[band].store(db.max(-96.0), Relaxed);
        }
    }
}
