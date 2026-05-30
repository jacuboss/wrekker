//! Real-time FX processor for Wrekker.
//!
//! FxProcessor runs inside the CPAL callback — zero heap allocation, no locks.
//! Parameters are communicated via FxShared (Arc of atomics), same as DeckShared.

use atomic_float::AtomicF32;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;

// ── FX type constants ─────────────────────────────────────────────────────────

pub const FX_FILTER: u32 = 0;
pub const FX_ECHO: u32 = 1;
pub const FX_DELAY: u32 = 2;
pub const FX_REVERB: u32 = 3;
pub const FX_FLANGER: u32 = 4;
pub const FX_PHASER: u32 = 5;
pub const FX_BITCRUSHER: u32 = 6;
pub const FX_ROLL: u32 = 7;
pub const FX_TRANS: u32 = 8;
pub const FX_NOISE: u32 = 9;

pub const WREKK_FX_VOCAL_GHOST: u32 = 0;
pub const WREKK_FX_TOP_WASH: u32 = 1;
pub const WREKK_FX_DRUM_CRUSH: u32 = 2;
pub const WREKK_FX_RHYTHM_GATE: u32 = 3;
pub const WREKK_FX_STEM_ROLL: u32 = 4;
pub const WREKK_FX_BASS_LOCK: u32 = 5;
pub const WREKK_FX_DECONSTRUCT: u32 = 6;
pub const WREKK_FX_REBUILD: u32 = 7;

pub const WREKK_STEM_VOC: u32 = 0;
pub const WREKK_STEM_DRM: u32 = 1;
pub const WREKK_STEM_BSS: u32 = 2;
pub const WREKK_STEM_OTH: u32 = 3;
pub const WREKK_STEM_TOP: u32 = 4;
pub const WREKK_STEM_RHYTHM: u32 = 5;

pub const TARGET_A: u32 = 0;
pub const TARGET_B: u32 = 1;
pub const TARGET_BOTH: u32 = 2;

// ── Buffer constants ──────────────────────────────────────────────────────────

const MAX_DELAY_FRAMES: usize = 5 * 48_001; // 5 s @ 48 kHz
const MAX_ROLL_FRAMES: usize = 48_001; // 1 beat @ 60 BPM

// Freeverb delay lengths (tuned for 44100 Hz; scaled by actual SR at init)
const COMB_TUNING_44K: [usize; 4] = [1557, 1617, 1491, 1422];
const COMB_STEREO_OFFSET: usize = 23;
const ALLPASS_TUNING_44K: [usize; 2] = [225, 556];

// ── FxShared — Python ↔ CPAL atomics ─────────────────────────────────────────

pub struct FxShared {
    pub enabled: AtomicBool,
    pub fx_type: AtomicU32,
    pub target: AtomicU32,
    pub wet: AtomicF32,
    pub depth: AtomicF32,
    pub feedback: AtomicF32,
    pub time_division: AtomicF32, // beat fraction: 0.25=1/4, 0.5=1/2, 1.0=1, 2.0=2
    pub color: AtomicF32,         // −1.0..+1.0  (LP→HP, LFO rate, etc.)
    pub bpm: AtomicF32,

    pub wrekk_enabled: AtomicBool,
    pub wrekk_type: AtomicU32,
    pub wrekk_target: AtomicU32,
    pub wrekk_stem_target: AtomicU32,
    pub wrekk_wet: AtomicF32,
    pub wrekk_depth: AtomicF32,
    pub wrekk_feedback: AtomicF32,
    pub wrekk_time_division: AtomicF32,
    pub wrekk_color: AtomicF32,
    pub wrekk_bpm: AtomicF32,
}

impl FxShared {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            enabled: AtomicBool::new(false),
            fx_type: AtomicU32::new(FX_FILTER),
            target: AtomicU32::new(TARGET_A),
            wet: AtomicF32::new(0.8),
            depth: AtomicF32::new(0.5),
            feedback: AtomicF32::new(0.5),
            time_division: AtomicF32::new(0.5),
            color: AtomicF32::new(0.0),
            bpm: AtomicF32::new(120.0),
            wrekk_enabled: AtomicBool::new(false),
            wrekk_type: AtomicU32::new(WREKK_FX_VOCAL_GHOST),
            wrekk_target: AtomicU32::new(TARGET_A),
            wrekk_stem_target: AtomicU32::new(WREKK_STEM_DRM),
            wrekk_wet: AtomicF32::new(0.8),
            wrekk_depth: AtomicF32::new(0.5),
            wrekk_feedback: AtomicF32::new(0.5),
            wrekk_time_division: AtomicF32::new(0.5),
            wrekk_color: AtomicF32::new(0.0),
            wrekk_bpm: AtomicF32::new(120.0),
        })
    }
}

// ── Biquad filter (Direct Form I) ────────────────────────────────────────────

#[derive(Clone, Default)]
struct Biquad {
    b0: f32,
    b1: f32,
    b2: f32,
    a1: f32,
    a2: f32,
    x1: f32,
    x2: f32,
    y1: f32,
    y2: f32,
}

impl Biquad {
    fn set_lowpass(&mut self, freq: f32, q: f32, sr: f32) {
        let w0 = (2.0 * std::f32::consts::PI * freq / sr).clamp(1e-4, std::f32::consts::PI - 1e-4);
        let (sin_w, cos_w) = w0.sin_cos();
        let alpha = sin_w / (2.0 * q.max(0.1));
        let a0_inv = 1.0 / (1.0 + alpha);
        self.b0 = (1.0 - cos_w) * 0.5 * a0_inv;
        self.b1 = (1.0 - cos_w) * a0_inv;
        self.b2 = self.b0;
        self.a1 = -2.0 * cos_w * a0_inv;
        self.a2 = (1.0 - alpha) * a0_inv;
    }

    fn set_highpass(&mut self, freq: f32, q: f32, sr: f32) {
        let w0 = (2.0 * std::f32::consts::PI * freq / sr).clamp(1e-4, std::f32::consts::PI - 1e-4);
        let (sin_w, cos_w) = w0.sin_cos();
        let alpha = sin_w / (2.0 * q.max(0.1));
        let a0_inv = 1.0 / (1.0 + alpha);
        self.b0 = (1.0 + cos_w) * 0.5 * a0_inv;
        self.b1 = -(1.0 + cos_w) * a0_inv;
        self.b2 = self.b0;
        self.a1 = -2.0 * cos_w * a0_inv;
        self.a2 = (1.0 - alpha) * a0_inv;
    }

    #[inline(always)]
    fn process(&mut self, x: f32) -> f32 {
        let y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2
            - self.a1 * self.y1
            - self.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = x;
        self.y2 = self.y1;
        self.y1 = y;
        if y.is_finite() {
            y
        } else {
            self.reset();
            0.0
        }
    }

    fn reset(&mut self) {
        self.x1 = 0.0;
        self.x2 = 0.0;
        self.y1 = 0.0;
        self.y2 = 0.0;
    }
}

// ── Freeverb comb filter ──────────────────────────────────────────────────────

struct CombFilter {
    buf: Vec<f32>,
    idx: usize,
    feedback: f32,
    filterstore: f32,
    damp: f32,
}

impl CombFilter {
    fn new(size: usize) -> Self {
        Self {
            buf: vec![0.0; size.max(1)],
            idx: 0,
            feedback: 0.5,
            filterstore: 0.0,
            damp: 0.5,
        }
    }

    #[inline(always)]
    fn process(&mut self, x: f32) -> f32 {
        let out = self.buf[self.idx];
        self.filterstore = out * (1.0 - self.damp) + self.filterstore * self.damp;
        self.buf[self.idx] = x + self.filterstore * self.feedback;
        self.idx += 1;
        if self.idx >= self.buf.len() {
            self.idx = 0;
        }
        out
    }

    fn clear(&mut self) {
        self.buf.fill(0.0);
        self.filterstore = 0.0;
        self.idx = 0;
    }
}

// ── Freeverb all-pass filter ──────────────────────────────────────────────────

struct AllpassFilter {
    buf: Vec<f32>,
    idx: usize,
}

impl AllpassFilter {
    fn new(size: usize) -> Self {
        Self {
            buf: vec![0.0; size.max(1)],
            idx: 0,
        }
    }

    #[inline(always)]
    fn process(&mut self, x: f32) -> f32 {
        let buf_out = self.buf[self.idx];
        let out = -x + buf_out;
        self.buf[self.idx] = x + buf_out * 0.5;
        self.idx += 1;
        if self.idx >= self.buf.len() {
            self.idx = 0;
        }
        out
    }

    fn clear(&mut self) {
        self.buf.fill(0.0);
        self.idx = 0;
    }
}

// ── Per-channel DSP state ─────────────────────────────────────────────────────

struct ChannelFxState {
    // Filter
    biquad: [Biquad; 2], // [L, R]
    filter_cutoff_smooth: f32,

    // Echo / delay / flanger (shared delay line; only one active at a time)
    delay_buf: [Vec<f32>; 2], // [L, R], MAX_DELAY_FRAMES each
    delay_write: usize,

    // Reverb (Freeverb Schroeder)
    comb_l: [CombFilter; 4],
    comb_r: [CombFilter; 4],
    allpass_l: [AllpassFilter; 2],
    allpass_r: [AllpassFilter; 2],

    // LFO phase (flanger / phaser)
    lfo_phase: f32,

    // Phaser all-pass state
    phaser_ap_l: [f32; 4],
    phaser_ap_r: [f32; 4],

    // Bitcrusher sample-and-hold
    crush_hold_l: f32,
    crush_hold_r: f32,
    crush_hold_count: usize,

    // Roll
    roll_buf: [Vec<f32>; 2], // [L, R], MAX_ROLL_FRAMES each
    roll_write: usize,
    roll_loop_start: usize,
    roll_loop_len: usize,
    roll_read: usize,
    roll_active: bool,

    // Trans/gate oscillator
    trans_phase: f32,

    // Noise LCG
    noise_state: u32,

    // One-pole wet smoother (per-sample)
    wet_smooth: f32,

    // Last active FX type — reset state on type change
    last_type: u32,
}

impl ChannelFxState {
    fn new(sr: f32) -> Self {
        let scale = sr / 44100.0;

        let comb_l: [CombFilter; 4] = std::array::from_fn(|i| {
            CombFilter::new(((COMB_TUNING_44K[i] as f32 * scale) as usize).max(1))
        });
        let comb_r: [CombFilter; 4] = std::array::from_fn(|i| {
            CombFilter::new(
                (((COMB_TUNING_44K[i] + COMB_STEREO_OFFSET) as f32 * scale) as usize).max(1),
            )
        });
        let allpass_l: [AllpassFilter; 2] = std::array::from_fn(|i| {
            AllpassFilter::new(((ALLPASS_TUNING_44K[i] as f32 * scale) as usize).max(1))
        });
        let allpass_r: [AllpassFilter; 2] = std::array::from_fn(|i| {
            AllpassFilter::new(
                (((ALLPASS_TUNING_44K[i] + COMB_STEREO_OFFSET) as f32 * scale) as usize).max(1),
            )
        });

        Self {
            biquad: [Biquad::default(), Biquad::default()],
            filter_cutoff_smooth: 1000.0,
            delay_buf: [vec![0.0; MAX_DELAY_FRAMES], vec![0.0; MAX_DELAY_FRAMES]],
            delay_write: 0,
            comb_l,
            comb_r,
            allpass_l,
            allpass_r,
            lfo_phase: 0.0,
            phaser_ap_l: [0.0; 4],
            phaser_ap_r: [0.0; 4],
            crush_hold_l: 0.0,
            crush_hold_r: 0.0,
            crush_hold_count: 0,
            roll_buf: [vec![0.0; MAX_ROLL_FRAMES], vec![0.0; MAX_ROLL_FRAMES]],
            roll_write: 0,
            roll_loop_start: 0,
            roll_loop_len: 0,
            roll_read: 0,
            roll_active: false,
            trans_phase: 0.0,
            noise_state: 0x12345678,
            wet_smooth: 0.0,
            last_type: u32::MAX,
        }
    }

    fn reset_for_type(&mut self, fx_type: u32) {
        for b in &mut self.biquad {
            b.reset();
        }
        self.filter_cutoff_smooth = 1000.0;
        for buf in &mut self.delay_buf {
            buf.fill(0.0);
        }
        self.delay_write = 0;
        for c in &mut self.comb_l {
            c.clear();
        }
        for c in &mut self.comb_r {
            c.clear();
        }
        for a in &mut self.allpass_l {
            a.clear();
        }
        for a in &mut self.allpass_r {
            a.clear();
        }
        self.lfo_phase = 0.0;
        self.phaser_ap_l = [0.0; 4];
        self.phaser_ap_r = [0.0; 4];
        self.crush_hold_l = 0.0;
        self.crush_hold_r = 0.0;
        self.crush_hold_count = 0;
        for buf in &mut self.roll_buf {
            buf.fill(0.0);
        }
        self.roll_write = 0;
        self.roll_loop_start = 0;
        self.roll_loop_len = 0;
        self.roll_read = 0;
        self.roll_active = false;
        self.trans_phase = 0.0;
        self.noise_state = 0x12345678;
        self.last_type = fx_type;
        // wet_smooth is intentionally kept so the ramp-out is smooth
    }
}

// ── FxProcessor ──────────────────────────────────────────────────────────────

pub struct FxProcessor {
    shared: Arc<FxShared>,
    sr: f32,
    state_a: ChannelFxState,
    state_b: ChannelFxState,
}

impl FxProcessor {
    pub fn new(shared: Arc<FxShared>, sr: u32) -> Self {
        let sr_f = sr as f32;
        Self {
            shared,
            sr: sr_f,
            state_a: ChannelFxState::new(sr_f),
            state_b: ChannelFxState::new(sr_f),
        }
    }

    /// Process both deck buffers in-place. Called after EQ, before live_buf update.
    pub fn process_block(&mut self, buf_a: &mut [f32], buf_b: &mut [f32]) {
        let fx_type = self.shared.fx_type.load(Ordering::Relaxed);
        let enabled = self.shared.enabled.load(Ordering::Relaxed);
        let target = self.shared.target.load(Ordering::Relaxed);
        let wet = self.shared.wet.load(Ordering::Relaxed);
        let depth = self.shared.depth.load(Ordering::Relaxed);
        let feedback = self.shared.feedback.load(Ordering::Relaxed);
        let td = self.shared.time_division.load(Ordering::Relaxed);
        let color = self.shared.color.load(Ordering::Relaxed);
        let bpm = self.shared.bpm.load(Ordering::Relaxed).clamp(20.0, 300.0);

        let apply_a = target == TARGET_A || target == TARGET_BOTH;
        let apply_b = target == TARGET_B || target == TARGET_BOTH;

        if apply_a {
            if self.state_a.last_type != fx_type {
                self.state_a.reset_for_type(fx_type);
            }
            Self::process_channel(
                &mut self.state_a,
                buf_a,
                enabled,
                fx_type,
                wet,
                depth,
                feedback,
                td,
                color,
                bpm,
                self.sr,
            );
        } else {
            Self::decay_wet(&mut self.state_a, buf_a.len() / 2, self.sr);
        }

        if apply_b {
            if self.state_b.last_type != fx_type {
                self.state_b.reset_for_type(fx_type);
            }
            Self::process_channel(
                &mut self.state_b,
                buf_b,
                enabled,
                fx_type,
                wet,
                depth,
                feedback,
                td,
                color,
                bpm,
                self.sr,
            );
        } else {
            Self::decay_wet(&mut self.state_b, buf_b.len() / 2, self.sr);
        }
    }

    /// Analytically decay wet_smooth toward zero when this deck is not targeted.
    fn decay_wet(state: &mut ChannelFxState, n_frames: usize, sr: f32) {
        if state.wet_smooth < 1e-6 {
            return;
        }
        // After n_frames steps of one-pole filter toward 0, with tau=20ms:
        // w_final = w0 * (1 - coeff)^n
        let coeff = 1.0 - (-1.0_f32 / (0.02 * sr)).exp();
        let decay = (1.0 - coeff).powi(n_frames as i32);
        state.wet_smooth *= decay;
    }

    fn process_channel(
        state: &mut ChannelFxState,
        buf: &mut [f32],
        enabled: bool,
        fx_type: u32,
        wet: f32,
        depth: f32,
        feedback: f32,
        td: f32,
        color: f32,
        bpm: f32,
        sr: f32,
    ) {
        let target_wet = if enabled { wet.clamp(0.0, 1.0) } else { 0.0 };
        let n_frames = buf.len() / 2;
        if n_frames == 0 {
            return;
        }

        // One-pole coefficient for 20 ms wet smoothing
        let smooth_coeff = 1.0 - (-1.0_f32 / (0.02 * sr)).exp();

        // Filter: compute biquad coefficients once per block (avoids per-sample trig)
        if fx_type == FX_FILTER {
            Self::update_filter_coeffs(state, depth, color, n_frames, sr);
        }

        for i in 0..n_frames {
            state.wet_smooth += smooth_coeff * (target_wet - state.wet_smooth);
            let w = state.wet_smooth;

            // Fast path on release: nearly silent, just maintain roll capture
            if w < 1e-6 && target_wet < 1e-6 {
                if fx_type == FX_ROLL {
                    let wr = state.roll_write;
                    state.roll_buf[0][wr] = buf[i * 2];
                    state.roll_buf[1][wr] = buf[i * 2 + 1];
                    state.roll_write = (wr + 1) % MAX_ROLL_FRAMES;
                }
                continue;
            }

            let dry_l = buf[i * 2];
            let dry_r = buf[i * 2 + 1];

            let (fx_l, fx_r) = match fx_type {
                FX_FILTER => (
                    state.biquad[0].process(dry_l),
                    state.biquad[1].process(dry_r),
                ),
                FX_ECHO => Self::tick_echo(state, dry_l, dry_r, feedback, td, bpm, sr),
                FX_DELAY => Self::tick_echo(state, dry_l, dry_r, feedback * 0.5, td, bpm, sr),
                FX_REVERB => Self::tick_reverb(state, dry_l, dry_r, depth),
                FX_FLANGER => Self::tick_flanger(state, dry_l, dry_r, depth, feedback, color, sr),
                FX_PHASER => Self::tick_phaser(state, dry_l, dry_r, depth, color, sr),
                FX_BITCRUSHER => Self::tick_bitcrusher(state, dry_l, dry_r, depth),
                FX_ROLL => Self::tick_roll(state, dry_l, dry_r, td, bpm, sr),
                FX_TRANS => Self::tick_trans(state, dry_l, dry_r, td, bpm, sr),
                FX_NOISE => Self::tick_noise(state, dry_l, dry_r, depth, sr),
                _ => (dry_l, dry_r),
            };

            buf[i * 2] = dry_l + w * (fx_l - dry_l);
            buf[i * 2 + 1] = dry_r + w * (fx_r - dry_r);
        }
    }

    // ── Block-level parameter setup ───────────────────────────────────────────

    fn update_filter_coeffs(
        state: &mut ChannelFxState,
        depth: f32,
        color: f32,
        n_frames: usize,
        sr: f32,
    ) {
        const MIN_FREQ: f32 = 40.0;
        const CTR_FREQ: f32 = 800.0;
        const MAX_FREQ: f32 = 18_000.0;

        let raw_target = if color <= 0.0 {
            let t = (color + 1.0).clamp(0.0, 1.0);
            MIN_FREQ + t * (CTR_FREQ - MIN_FREQ)
        } else {
            CTR_FREQ + color * (MAX_FREQ - CTR_FREQ)
        };
        let target = CTR_FREQ + (raw_target - CTR_FREQ) * (0.2 + depth * 0.8);

        // One-pole smooth over the block duration (20 ms tau)
        let dt = n_frames as f32 / sr;
        let coeff = 1.0 - (-dt / 0.020).exp();
        state.filter_cutoff_smooth += coeff * (target - state.filter_cutoff_smooth);
        let freq = state.filter_cutoff_smooth.clamp(MIN_FREQ, MAX_FREQ);
        let q = 0.7 + depth * 2.5; // 0.7 (Butterworth) → 3.2 (resonant)

        if color <= 0.0 {
            state.biquad[0].set_lowpass(freq, q, sr);
            state.biquad[1].set_lowpass(freq, q, sr);
        } else {
            state.biquad[0].set_highpass(freq, q, sr);
            state.biquad[1].set_highpass(freq, q, sr);
        }
    }

    // ── Per-sample effect ticks ───────────────────────────────────────────────

    #[inline(always)]
    fn tick_echo(
        state: &mut ChannelFxState,
        l: f32,
        r: f32,
        feedback: f32,
        td: f32,
        bpm: f32,
        sr: f32,
    ) -> (f32, f32) {
        let delay_s = (60.0 / bpm * td).clamp(0.001, 4.9);
        let delay_fr = ((delay_s * sr) as usize).min(MAX_DELAY_FRAMES - 1).max(1);
        let wr = state.delay_write;
        let rd = (wr + MAX_DELAY_FRAMES - delay_fr) % MAX_DELAY_FRAMES;

        let echo_l = state.delay_buf[0][rd];
        let echo_r = state.delay_buf[1][rd];
        let fb = feedback.clamp(0.0, 0.95);

        state.delay_buf[0][wr] = l + echo_l * fb;
        state.delay_buf[1][wr] = r + echo_r * fb;
        state.delay_write = (wr + 1) % MAX_DELAY_FRAMES;

        // Return dry + echo so wet blend produces: at wet=1 → dry + echo
        (l + echo_l, r + echo_r)
    }

    #[inline(always)]
    fn tick_reverb(state: &mut ChannelFxState, l: f32, r: f32, depth: f32) -> (f32, f32) {
        let room = 0.5 + depth * 0.45; // 0.50..0.95
        let damp = 1.0 - depth * 0.5; // 1.0..0.50
        for c in &mut state.comb_l {
            c.feedback = room;
            c.damp = damp;
        }
        for c in &mut state.comb_r {
            c.feedback = room;
            c.damp = damp;
        }

        let gain = 0.015;
        let mut rev_l = 0.0_f32;
        let mut rev_r = 0.0_f32;
        for c in &mut state.comb_l {
            rev_l += c.process(l * gain);
        }
        for c in &mut state.comb_r {
            rev_r += c.process(r * gain);
        }
        for a in &mut state.allpass_l {
            rev_l = a.process(rev_l);
        }
        for a in &mut state.allpass_r {
            rev_r = a.process(rev_r);
        }

        // Soft limit to prevent runaway feedback; return dry + reverb tail
        let rev_l = if rev_l.is_finite() { rev_l.tanh() } else { 0.0 };
        let rev_r = if rev_r.is_finite() { rev_r.tanh() } else { 0.0 };
        (l + rev_l, r + rev_r)
    }

    #[inline(always)]
    fn tick_flanger(
        state: &mut ChannelFxState,
        l: f32,
        r: f32,
        depth: f32,
        feedback: f32,
        color: f32,
        sr: f32,
    ) -> (f32, f32) {
        let lfo_hz = 0.1 + (color + 1.0) * 0.5 * 1.6; // 0.1..1.7 Hz
        let max_ms = 0.5 + depth * 11.5; // 0.5..12 ms delay swing
        let lfo = state.lfo_phase.sin();
        state.lfo_phase += std::f32::consts::TAU * lfo_hz / sr;
        if state.lfo_phase >= std::f32::consts::TAU {
            state.lfo_phase -= std::f32::consts::TAU;
        }

        let delay_fr = ((0.5 + (lfo + 1.0) * 0.5 * max_ms) * 0.001 * sr) as usize;
        let delay_fr = delay_fr.clamp(1, MAX_DELAY_FRAMES - 1);
        let wr = state.delay_write;
        let rd = (wr + MAX_DELAY_FRAMES - delay_fr) % MAX_DELAY_FRAMES;

        let dly_l = state.delay_buf[0][rd];
        let dly_r = state.delay_buf[1][rd];
        let fb = feedback.clamp(0.0, 0.9);

        state.delay_buf[0][wr] = l + dly_l * fb;
        state.delay_buf[1][wr] = r + dly_r * fb;
        state.delay_write = (wr + 1) % MAX_DELAY_FRAMES;

        (l + dly_l, r + dly_r)
    }

    #[inline(always)]
    fn tick_phaser(
        state: &mut ChannelFxState,
        l: f32,
        r: f32,
        depth: f32,
        color: f32,
        sr: f32,
    ) -> (f32, f32) {
        let lfo_hz = 0.3 + depth * 1.2; // 0.3..1.5 Hz
        state.lfo_phase += std::f32::consts::TAU * lfo_hz / sr;
        if state.lfo_phase >= std::f32::consts::TAU {
            state.lfo_phase -= std::f32::consts::TAU;
        }
        let lfo = state.lfo_phase.sin();

        let center = 300.0 + (color + 1.0) * 0.5 * 3700.0; // 300..4000 Hz
        let freq = (center * (1.0 + lfo * 0.8)).clamp(20.0, 18_000.0);
        // First-order all-pass coefficient
        let tan_val = (std::f32::consts::PI * freq / sr).tan();
        let k = (tan_val - 1.0) / (tan_val + 1.0);

        let mut xl = l;
        for z in &mut state.phaser_ap_l {
            let y = k * xl + *z;
            *z = xl - k * y;
            xl = y;
        }
        let mut xr = r;
        for z in &mut state.phaser_ap_r {
            let y = k * xr + *z;
            *z = xr - k * y;
            xr = y;
        }

        (l + xl * 0.7, r + xr * 0.7)
    }

    #[inline(always)]
    fn tick_bitcrusher(state: &mut ChannelFxState, l: f32, r: f32, depth: f32) -> (f32, f32) {
        let bits = 16.0 - depth * 12.0; // 16..4 bits
        let levels = 2.0_f32.powf(bits - 1.0);
        let hold = (1 + (depth * 15.0) as usize).max(1); // 1..16 samples

        if state.crush_hold_count == 0 {
            state.crush_hold_l = (l * levels).round() / levels;
            state.crush_hold_r = (r * levels).round() / levels;
        }
        state.crush_hold_count = (state.crush_hold_count + 1) % hold;
        (state.crush_hold_l, state.crush_hold_r)
    }

    #[inline(always)]
    fn tick_roll(
        state: &mut ChannelFxState,
        l: f32,
        r: f32,
        td: f32,
        bpm: f32,
        sr: f32,
    ) -> (f32, f32) {
        // Always capture to roll_buf so the loop is fresh when enabled
        let wr = state.roll_write;
        state.roll_buf[0][wr] = l;
        state.roll_buf[1][wr] = r;
        state.roll_write = (wr + 1) % MAX_ROLL_FRAMES;

        let loop_s = (60.0 / bpm * td).clamp(0.001, (MAX_ROLL_FRAMES - 2) as f32 / sr);
        let loop_len = (loop_s * sr) as usize;
        let loop_len = loop_len.clamp(1, MAX_ROLL_FRAMES - 1);

        if state.roll_loop_len != loop_len || !state.roll_active {
            state.roll_loop_len = loop_len;
            // Loop start = loop_len frames behind current write
            state.roll_loop_start =
                (state.roll_write + MAX_ROLL_FRAMES - loop_len) % MAX_ROLL_FRAMES;
            state.roll_read = state.roll_loop_start;
            state.roll_active = true;
        }

        let out_l = state.roll_buf[0][state.roll_read];
        let out_r = state.roll_buf[1][state.roll_read];

        state.roll_read = (state.roll_read + 1) % MAX_ROLL_FRAMES;
        let offset = (state.roll_read + MAX_ROLL_FRAMES - state.roll_loop_start) % MAX_ROLL_FRAMES;
        if offset >= loop_len {
            state.roll_read = state.roll_loop_start;
        }

        (out_l, out_r)
    }

    #[inline(always)]
    fn tick_trans(
        state: &mut ChannelFxState,
        l: f32,
        r: f32,
        td: f32,
        bpm: f32,
        sr: f32,
    ) -> (f32, f32) {
        // Square-wave gate at bpm*td Hz rate, 50 % duty cycle
        let gate_hz = (bpm * td / 60.0).max(0.01);
        state.trans_phase += gate_hz / sr;
        if state.trans_phase >= 1.0 {
            state.trans_phase -= 1.0;
        }
        let amp = if state.trans_phase < 0.5 {
            1.0_f32
        } else {
            0.0_f32
        };
        (l * amp, r * amp)
    }

    #[inline(always)]
    fn tick_noise(state: &mut ChannelFxState, l: f32, r: f32, depth: f32, sr: f32) -> (f32, f32) {
        // LCG white noise generator
        state.noise_state = state
            .noise_state
            .wrapping_mul(1_664_525)
            .wrapping_add(1_013_904_223);
        let noise_raw = (state.noise_state as i32) as f32 / i32::MAX as f32;

        // LP-filtered noise via biquad[0] (biquad[1] unused for noise; both see same noise here)
        let noise_freq = (2_000.0 + depth * 8_000.0).min(sr * 0.4);
        state.biquad[0].set_lowpass(noise_freq, 0.7, sr);
        let filtered = state.biquad[0].process(noise_raw);

        let level = depth * 0.12;
        (l + filtered * level, r + filtered * level)
    }
}
