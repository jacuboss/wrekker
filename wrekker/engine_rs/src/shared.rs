//! Lock-free state shared between the Python control thread and the CPAL audio thread.

use atomic_float::AtomicF32;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicI64, AtomicU64};
use std::sync::{Arc, Mutex, RwLock};

pub const N_STEMS: usize = 4;
pub const N_SPECTRUM: usize = 16;
pub const LIVE_BUF_FRAMES: usize = 8192; // ~186ms at 44100 Hz for cross-device monitor drift

// ── Per-deck scratch shared state ────────────────────────────────────────────

pub struct ScratchShared {
    /// Jog ticks accumulated since last audio block. MIDI adds, audio drains.
    pub pending_ticks: AtomicI32,
    /// True while user is touching the platter (vinyl scratch active).
    pub scratch_active: AtomicBool,
    /// Normal playback rate to ramp back to when touch is released.
    pub target_rate: AtomicF32,
}

impl ScratchShared {
    fn new() -> Self {
        Self {
            pending_ticks: AtomicI32::new(0),
            scratch_active: AtomicBool::new(false),
            target_rate: AtomicF32::new(1.0),
        }
    }
}

// ── Per-deck shared state ────────────────────────────────────────────────────

pub struct DeckShared {
    // Control (Python writes, audio reads)
    pub playing: AtomicBool,
    /// -1 = no seek pending; ≥ 0 = seek to this sample frame on next callback
    pub pending_seek: AtomicI64,
    pub stem_targets: [AtomicF32; N_STEMS],
    pub loop_active: AtomicBool,
    pub loop_start: AtomicU64, // sample-frame index
    pub loop_end: AtomicU64,   // sample-frame index (exclusive)
    pub cue_point: AtomicU64,  // sample-frame index

    /// Playback rate relative to 1.0 (e.g. 1.05 = +5% tempo, 0.95 = -5%).
    /// Changes pitch along with tempo (vinyl-style). Default: 1.0.
    pub playback_rate: AtomicF32,

    // EQ (Python writes target, audio picks up on next callback)
    pub eq_low_db: AtomicF32,
    pub eq_mid_db: AtomicF32,
    pub eq_high_db: AtomicF32,
    pub eq_dirty: AtomicBool,

    pub channel_gain: AtomicF32,
    pub pregain: AtomicF32,
    /// Bipolar channel filter: -1 = low-pass, 0 = neutral, +1 = high-pass.
    pub channel_filter: AtomicF32,

    /// Scratch engine state shared with the MIDI thread.
    pub scratch: ScratchShared,

    /// Temporary pitch-bend offset from side jog wheel. Python writes, audio adds to rate.
    pub nudge_target: AtomicF32,

    // Output (audio writes, Python polls)
    pub position: AtomicU64, // current playback frame (integer)
    pub peak_l: AtomicF32,
    pub peak_r: AtomicF32,
    pub clip_l: AtomicBool,
    pub clip_r: AtomicBool,
    pub lufs_momentary: AtomicF32,
    pub lufs_shortterm: AtomicF32,
    pub spectrum: [AtomicF32; N_SPECTRUM],

    /// Per-stem RMS-like peak (decayed). Audio writes, Python polls.
    pub stem_peaks: [AtomicF32; N_STEMS],

    /// Recent post-EQ audio: LIVE_BUF_FRAMES frames, interleaved stereo f32.
    /// Audio thread writes with try_lock (skips if contended).
    pub live_buf: Mutex<Vec<f32>>,
    /// Absolute frame count for the tail stored in live_buf.
    pub live_seq: AtomicU64,

    // Buffer loading: epoch bumped by Python on each load; audio detects the change
    pub buffer_epoch: AtomicU64,
    pub buffer: RwLock<Option<Arc<AudioBuffers>>>,
}

impl DeckShared {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            playing: AtomicBool::new(false),
            pending_seek: AtomicI64::new(-1),
            stem_targets: std::array::from_fn(|_| AtomicF32::new(1.0)),
            loop_active: AtomicBool::new(false),
            loop_start: AtomicU64::new(0),
            loop_end: AtomicU64::new(0),
            cue_point: AtomicU64::new(0),
            playback_rate: AtomicF32::new(1.0),
            eq_low_db: AtomicF32::new(0.0),
            eq_mid_db: AtomicF32::new(0.0),
            eq_high_db: AtomicF32::new(0.0),
            eq_dirty: AtomicBool::new(false),
            channel_gain: AtomicF32::new(1.0),
            pregain: AtomicF32::new(1.0),
            channel_filter: AtomicF32::new(0.0),
            scratch: ScratchShared::new(),
            nudge_target: AtomicF32::new(0.0),
            position: AtomicU64::new(0),
            peak_l: AtomicF32::new(0.0),
            peak_r: AtomicF32::new(0.0),
            clip_l: AtomicBool::new(false),
            clip_r: AtomicBool::new(false),
            lufs_momentary: AtomicF32::new(f32::NEG_INFINITY),
            lufs_shortterm: AtomicF32::new(f32::NEG_INFINITY),
            spectrum: std::array::from_fn(|_| AtomicF32::new(-96.0)),
            stem_peaks: std::array::from_fn(|_| AtomicF32::new(0.0)),
            live_buf: Mutex::new(vec![0.0f32; LIVE_BUF_FRAMES * 2]),
            live_seq: AtomicU64::new(0),
            buffer_epoch: AtomicU64::new(0),
            buffer: RwLock::new(None),
        })
    }
}

// ── Master mixer shared state ────────────────────────────────────────────────

pub struct MixerShared {
    pub crossfader: AtomicF32,
    pub master_gain: AtomicF32,
    pub peak_l: AtomicF32,
    pub peak_r: AtomicF32,
    pub clip_l: AtomicBool,
    pub clip_r: AtomicBool,
    pub phase_corr: AtomicF32,

    /// Recent master output: LIVE_BUF_FRAMES frames, interleaved stereo f32.
    pub live_buf: Mutex<Vec<f32>>,
    /// Absolute frame count for the tail stored in live_buf.
    pub live_seq: AtomicU64,

    // Headphone / CUE bus
    pub cue_a: AtomicBool,
    pub cue_b: AtomicBool,
    pub cue_master: AtomicBool,
    /// 0.0 = full CUE signal, 1.0 = full master mix.
    pub hp_mix: AtomicF32,
    /// Output gain for the headphone bus (0.0–2.0, 1.0 = unity).
    pub hp_level: AtomicF32,

}

impl MixerShared {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            crossfader: AtomicF32::new(0.5),
            master_gain: AtomicF32::new(1.0),
            peak_l: AtomicF32::new(0.0),
            peak_r: AtomicF32::new(0.0),
            clip_l: AtomicBool::new(false),
            clip_r: AtomicBool::new(false),
            phase_corr: AtomicF32::new(0.0),
            live_buf: Mutex::new(vec![0.0f32; LIVE_BUF_FRAMES * 2]),
            live_seq: AtomicU64::new(0),
            cue_a: AtomicBool::new(false),
            cue_b: AtomicBool::new(false),
            cue_master: AtomicBool::new(false),
            hp_mix: AtomicF32::new(0.0),
            hp_level: AtomicF32::new(1.0),
        })
    }
}

// ── Audio sample buffers (immutable once constructed) ────────────────────────

pub struct AudioBuffers {
    /// Per-stem interleaved stereo f32: `stems[i][frame * 2 + ch]`
    /// Order: 0=vocals 1=drums 2=bass 3=other
    pub stems: Option<[Vec<f32>; N_STEMS]>,
    /// Fallback when no stems: interleaved stereo f32
    pub original: Option<Vec<f32>>,
    pub n_frames: usize,
    /// Some(0) = reset to start on load; None = keep current position (stem update)
    pub start_position: Option<usize>,
}

// ── Live buffer update helper ─────────────────────────────────────────────────

/// Shift `new_data` into `live_buf`, keeping the most-recent samples. Non-blocking.
pub fn update_live_buf(live_buf: &Mutex<Vec<f32>>, new_data: &[f32]) -> bool {
    if let Ok(mut guard) = live_buf.try_lock() {
        let live_len = guard.len();
        let data_len = new_data.len();
        if data_len == 0 || live_len == 0 {
            return false;
        }

        if data_len >= live_len {
            // New data fills (or overfills) the buffer — keep only the last live_len samples
            guard.copy_from_slice(&new_data[data_len - live_len..]);
        } else {
            // Shift old samples left, append new_data at the end
            guard.copy_within(data_len.., 0);
            let start = live_len - data_len;
            guard[start..].copy_from_slice(new_data);
        }
        return true;
    }
    false
}
