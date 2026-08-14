//! `wrekker_engine` — PyO3 extension module.
//!
//! Exposes `NativeEngine`: audio callback runs entirely in Rust/CPAL.

mod audio;
mod eq;
mod fx;
mod lufs;
mod phase_sync;
mod shared;
mod time_stretch;

use std::sync::atomic::Ordering;
use std::sync::Arc;

use numpy::PyReadonlyArray2;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
#[allow(unused_imports)]
use pyo3::Python;

use audio::DeckAudioState;
use eq::{ChannelFilter, ThreeBandEQ};
use fx::{FxProcessor, FxShared};
use phase_sync::PhaseSync;
use shared::{update_live_buf, AudioBuffers, DeckShared, MixerShared, N_STEMS};
use time_stretch::{StretchMode, WrekkerTimeStretch};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

// ── NativeEngine ─────────────────────────────────────────────────────────────

#[pyclass]
pub struct NativePhaseSync {
    inner: PhaseSync,
}

unsafe impl Send for NativePhaseSync {}

#[pymethods]
impl NativePhaseSync {
    #[new]
    #[pyo3(signature = (kp = 0.35, dead_zone_beats = 0.02, max_correction_rate = 2.0))]
    fn new(kp: f64, dead_zone_beats: f64, max_correction_rate: f64) -> Self {
        Self {
            inner: PhaseSync::new(kp, dead_zone_beats, max_correction_rate),
        }
    }

    fn on_master_beat(&mut self, master_beat_time: f64, master_bpm: f64) {
        self.inner.on_master_beat(master_beat_time, master_bpm);
    }

    fn on_slave_beat(&mut self, slave_beat_time: f64, slave_bpm: f64) {
        self.inner.on_slave_beat(slave_beat_time, slave_bpm);
    }

    fn compute_correction(&self) -> f64 {
        self.inner.compute_correction()
    }

    fn update_phase_error(
        &mut self,
        slave_minus_master_beats: f64,
        master_bpm: f64,
        slave_bpm: f64,
        dt_seconds: f64,
    ) -> f64 {
        self.inner
            .update_phase_error(slave_minus_master_beats, master_bpm, slave_bpm, dt_seconds)
    }

    fn snap_to_grid(&mut self) -> f64 {
        self.inner.snap_to_grid()
    }

    fn pull_in(&mut self, beats_to_converge: u32) {
        self.inner.pull_in(beats_to_converge);
    }

    fn reset(&mut self) {
        self.inner.reset();
    }

    #[getter]
    fn is_locked(&self) -> bool {
        self.inner.is_locked()
    }

    #[getter]
    fn phase_error_ms(&self) -> f64 {
        self.inner.phase_error_ms()
    }

    #[getter]
    fn phase_error_beats(&self) -> f64 {
        self.inner.phase_error_beats()
    }

    #[getter]
    fn current_ratio(&self) -> f64 {
        self.inner.current_ratio()
    }
}

#[pyclass]
pub struct NativeTimeStretch {
    inner: WrekkerTimeStretch,
}

unsafe impl Send for NativeTimeStretch {}

#[pymethods]
impl NativeTimeStretch {
    #[new]
    #[pyo3(signature = (sample_rate, channels, mode = "faster", max_latency_ms = 10.0))]
    fn new(sample_rate: u32, channels: usize, mode: &str, max_latency_ms: f32) -> PyResult<Self> {
        let stretch_mode = match mode {
            "finer" | "Finer" | "offline" => StretchMode::Finer,
            "faster" | "Faster" | "realtime" | "real_time" => StretchMode::Faster {
                max_latency_ms: max_latency_ms.max(1.0),
            },
            other => {
                return Err(PyRuntimeError::new_err(format!(
                    "invalid stretch mode '{other}', expected 'faster' or 'finer'"
                )))
            }
        };
        Ok(Self {
            inner: WrekkerTimeStretch::new(sample_rate, channels, stretch_mode),
        })
    }

    fn set_time_ratio(&mut self, ratio: f64) {
        self.inner.set_time_ratio(ratio);
    }

    fn set_pitch_semitones(&mut self, semitones: f64) {
        self.inner.set_pitch_semitones(semitones);
    }

    fn set_formant_preservation(&mut self, enabled: bool) {
        self.inner.set_formant_preservation(enabled);
    }

    fn process(&mut self, input: Vec<f32>) -> Vec<f32> {
        self.inner.process(&input)
    }

    fn process_final(&mut self, input: Vec<f32>) -> Vec<f32> {
        self.inner.process_final(&input)
    }

    fn reset(&mut self) {
        self.inner.reset();
    }

    #[getter]
    fn rubberband_active(&self) -> bool {
        self.inner.is_rubberband_active()
    }

    #[getter]
    fn sample_rate(&self) -> u32 {
        self.inner.sample_rate()
    }

    #[getter]
    fn channels(&self) -> usize {
        self.inner.channels()
    }

    #[getter]
    fn mode(&self) -> &'static str {
        match self.inner.mode() {
            StretchMode::Finer => "finer",
            StretchMode::Faster { .. } => "faster",
        }
    }

    #[getter]
    fn time_ratio(&self) -> f64 {
        self.inner.time_ratio()
    }

    #[getter]
    fn pitch_semitones(&self) -> f64 {
        self.inner.pitch_semitones()
    }

    #[getter]
    fn formant_preservation(&self) -> bool {
        self.inner.formant_preservation()
    }
}

#[pyclass]
pub struct NativeEngine {
    shared_a: Arc<DeckShared>,
    shared_b: Arc<DeckShared>,
    shared_mixer: Arc<MixerShared>,
    shared_fx: Arc<FxShared>,
    /// Master output → system default device (PipeWire)
    stream: Option<cpal::Stream>,
    /// Headphone CUE → DDJ-FLX4 ch 2-3 (None when FLX4 not present)
    hp_stream: Option<cpal::Stream>,
    sr: u32,
}

unsafe impl Send for NativeEngine {}

#[pymethods]
impl NativeEngine {
    #[new]
    fn new(_sample_rate: u32, _blocksize: usize) -> PyResult<Self> {
        Ok(Self {
            shared_a: DeckShared::new(),
            shared_b: DeckShared::new(),
            shared_mixer: MixerShared::new(),
            shared_fx: FxShared::new(),
            stream: None,
            hp_stream: None,
            sr: _sample_rate,
        })
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    fn start(&mut self) -> PyResult<()> {
        let host = cpal::default_host();

        // ── Master stream → system default device (PipeWire / laptop speakers) ──
        // Always 2-ch; FLX4 main RCA jacks are NOT used for master so that the
        // user's system audio routing (PipeWire) receives the mix as normal.
        let master_dev = host
            .default_output_device()
            .ok_or_else(|| PyRuntimeError::new_err("No audio output device"))?;
        eprintln!("[wrekker-engine] Master → {:?} (2-ch)", master_dev.name().unwrap_or_default());

        let sr = self.sr;
        let config2 = cpal::StreamConfig {
            channels: 2,
            sample_rate: cpal::SampleRate(sr),
            buffer_size: cpal::BufferSize::Default,
        };

        let sa = Arc::clone(&self.shared_a);
        let sb = Arc::clone(&self.shared_b);
        let sm = Arc::clone(&self.shared_mixer);
        let sf = Arc::clone(&self.shared_fx);

        let mut deck_a = DeckAudioState::new(Arc::clone(&sa), Arc::clone(&sf), sr, 0, fx::TARGET_A);
        let mut deck_b = DeckAudioState::new(Arc::clone(&sb), Arc::clone(&sf), sr, 0, fx::TARGET_B);
        let mut eq_a = ThreeBandEQ::new(sr as f32);
        let mut eq_b = ThreeBandEQ::new(sr as f32);
        let mut cfx_a = ChannelFilter::new(sr as f32);
        let mut cfx_b = ChannelFilter::new(sr as f32);
        let mut fx_proc = FxProcessor::new(Arc::clone(&sf), sr);

        let mut buf_a: Vec<f32> = Vec::new();
        let mut buf_b: Vec<f32> = Vec::new();
        let mut master_lufs = lufs::KWeightedLUFS::new(sr, 256);
        let fs = sr as f32;
        let mut cf_prev    = 0.5_f32;
        let mut master_prev = 1.0_f32;
        let mut ch_a_prev  = 1.0_f32;
        let mut ch_b_prev  = 1.0_f32;
        let mut pg_a_prev  = 1.0_f32;
        let mut pg_b_prev  = 1.0_f32;
        let mut limiter_gain = 1.0_f32;

        let master_stream = master_dev
            .build_output_stream::<f32, _, _>(
                &config2,
                move |data: &mut [f32], _: &cpal::OutputCallbackInfo| {
                    let frames = (data.len() / 2).max(1);
                    let stereo_n = frames * 2;

                    if buf_a.len() != stereo_n {
                        buf_a.resize(stereo_n, 0.0);
                        buf_b.resize(stereo_n, 0.0);
                    }

                    // Render each deck: EQ → channel filter → FX
                    eq_a.apply_pending(&sa);
                    eq_b.apply_pending(&sb);
                    deck_a.fill(&mut buf_a, fs);
                    deck_b.fill(&mut buf_b, fs);
                    eq_a.process(&mut buf_a);
                    eq_b.process(&mut buf_b);
                    cfx_a.process(&sa, &mut buf_a);
                    cfx_b.process(&sb, &mut buf_b);
                    fx_proc.process_block(&mut buf_a, &mut buf_b);

                    // Publish pre-crossfader audio so HP stream can read it for CUE.
                    // live_seq marks the absolute frame after the stored tail.
                    if update_live_buf(&sa.live_buf, &buf_a) {
                        sa.live_seq.fetch_add(frames as u64, Ordering::Release);
                    }
                    if update_live_buf(&sb.live_buf, &buf_b) {
                        sb.live_seq.fetch_add(frames as u64, Ordering::Release);
                    }

                    let cf_target  = sm.crossfader.load(Ordering::Relaxed);
                    let mst_target = sm.master_gain.load(Ordering::Relaxed);
                    let cga_target = sa.channel_gain.load(Ordering::Relaxed);
                    let pga_target = sa.pregain.load(Ordering::Relaxed);
                    let cgb_target = sb.channel_gain.load(Ordering::Relaxed);
                    let pgb_target = sb.pregain.load(Ordering::Relaxed);
                    let inv_frames = 1.0_f32 / frames as f32;

                    let (mut peak_l, mut peak_r) = (0.0_f32, 0.0_f32);
                    let mut sum_ab = 0.0_f64;
                    let mut sum_aa = 0.0_f64;
                    let mut sum_bb = 0.0_f64;
                    let mut block_peak = 0.0_f32;

                    for i in 0..frames {
                        let t = (i + 1) as f32 * inv_frames;
                        let cf  = cf_prev   + (cf_target  - cf_prev)   * t;
                        let mst = master_prev + (mst_target - master_prev) * t;
                        let cga = ch_a_prev + (cga_target - ch_a_prev) * t;
                        let pga = pg_a_prev + (pga_target - pg_a_prev) * t;
                        let cgb = ch_b_prev + (cgb_target - ch_b_prev) * t;
                        let pgb = pg_b_prev + (pgb_target - pg_b_prev) * t;

                        let angle = cf * (std::f32::consts::PI * 0.5);
                        let gain_a = angle.cos();
                        let gain_b = angle.sin();

                        let al = buf_a[i * 2]     * cga * pga;
                        let ar = buf_a[i * 2 + 1] * cga * pga;
                        let bl = buf_b[i * 2]     * cgb * pgb;
                        let br = buf_b[i * 2 + 1] * cgb * pgb;

                        let l = (al * gain_a + bl * gain_b) * mst;
                        let r = (ar * gain_a + br * gain_b) * mst;

                        data[i * 2]     = l;
                        data[i * 2 + 1] = r;

                        peak_l = peak_l.max(l.abs());
                        peak_r = peak_r.max(r.abs());
                        block_peak = block_peak.max(l.abs()).max(r.abs());

                        let am = (al + ar) * 0.5_f32;
                        let bm = (bl + br) * 0.5_f32;
                        sum_ab += (am * bm) as f64;
                        sum_aa += (am * am) as f64;
                        sum_bb += (bm * bm) as f64;
                    }

                    cf_prev    = cf_target;
                    master_prev = mst_target;
                    ch_a_prev  = cga_target;
                    ch_b_prev  = cgb_target;
                    pg_a_prev  = pga_target;
                    pg_b_prev  = pgb_target;

                    // Safety limiter on master output
                    let limit_target = if block_peak > 0.98 {
                        (0.98 / block_peak).clamp(0.05, 1.0)
                    } else { 1.0 };
                    let attack  = 1.0 - (-((frames as f32) / (fs * 0.0015))).exp();
                    let release = 1.0 - (-((frames as f32) / (fs * 0.180))).exp();
                    if limit_target < limiter_gain {
                        limiter_gain += attack  * (limit_target - limiter_gain);
                    } else {
                        limiter_gain += release * (limit_target - limiter_gain);
                    }
                    if limiter_gain < 0.9995 {
                        for s in data.iter_mut() { *s *= limiter_gain; }
                        peak_l *= limiter_gain;
                        peak_r *= limiter_gain;
                    }

                    // Metering
                    const DECAY: f32 = 0.85;
                    let pl = sm.peak_l.load(Ordering::Relaxed);
                    let pr = sm.peak_r.load(Ordering::Relaxed);
                    sm.peak_l.store((pl * DECAY).max(peak_l), Ordering::Relaxed);
                    sm.peak_r.store((pr * DECAY).max(peak_r), Ordering::Relaxed);
                    if peak_l >= 1.0 { sm.clip_l.store(true, Ordering::Relaxed); }
                    if peak_r >= 1.0 { sm.clip_r.store(true, Ordering::Relaxed); }

                    let denom = (sum_aa * sum_bb).sqrt();
                    let corr = if denom > 1e-10 {
                        ((sum_ab / denom) as f32).clamp(-1.0, 1.0)
                    } else { 0.0 };
                    sm.phase_corr.store(corr, Ordering::Relaxed);

                    // Master loudness (K-weighted, measured on the limited output)
                    let (lm, lst) = master_lufs.process(data);
                    sm.lufs_momentary.store(lm, Ordering::Relaxed);
                    sm.lufs_shortterm.store(lst, Ordering::Relaxed);

                    // Publish master mix for MST CUE / hp_mix blending in HP stream.
                    if update_live_buf(&sm.live_buf, data) {
                        sm.live_seq.fetch_add(frames as u64, Ordering::Release);
                    }
                },
                move |err| eprintln!("[wrekker-engine] CPAL stream error: {err}"),
                None,
            )
            .map_err(|e| PyRuntimeError::new_err(format!("CPAL build_output_stream: {e}")))?;

        master_stream
            .play()
            .map_err(|e| PyRuntimeError::new_err(format!("CPAL stream.play: {e}")))?;
        self.stream = Some(master_stream);

        // ── Headphone CUE stream → DDJ-FLX4 ch 2-3 ──────────────────────────
        self.hp_stream = build_hp_stream(
            &host,
            Arc::clone(&self.shared_a),
            Arc::clone(&self.shared_b),
            Arc::clone(&self.shared_mixer),
            sr,
        );
        if self.hp_stream.is_none() {
            eprintln!("[wrekker-engine] FLX4 not found — headphone CUE disabled");
        }

        Ok(())
    }

    fn stop(&mut self) {
        self.hp_stream = None;
        self.stream = None;
    }

    // ── Transport ─────────────────────────────────────────────────────────────

    fn set_playing(&self, deck_id: &str, playing: bool) {
        self.deck(deck_id).playing.store(playing, Ordering::Relaxed);
    }

    fn seek(&self, deck_id: &str, position_s: f64) {
        let frame = (position_s * self.sr as f64) as i64;
        self.deck(deck_id)
            .pending_seek
            .store(frame, Ordering::Release);
    }

    fn get_position_s(&self, deck_id: &str) -> f64 {
        let frames = self.deck(deck_id).position.load(Ordering::Relaxed);
        frames as f64 / self.sr as f64
    }

    // ── Stem gains ────────────────────────────────────────────────────────────

    fn set_stem_gain(&self, deck_id: &str, stem_idx: usize, gain: f32) {
        if stem_idx < N_STEMS {
            self.deck(deck_id).stem_targets[stem_idx]
                .store(gain.clamp(0.0, 2.0), Ordering::Relaxed);
        }
    }

    fn set_master_gain(&self, gain: f32) {
        self.shared_mixer
            .master_gain
            .store(gain.clamp(0.0, 2.0), Ordering::Relaxed);
    }

    fn get_master_gain(&self) -> f32 {
        self.shared_mixer.master_gain.load(Ordering::Relaxed)
    }

    fn set_crossfader(&self, value: f32) {
        self.shared_mixer
            .crossfader
            .store(value.clamp(0.0, 1.0), Ordering::Relaxed);
    }

    // ── Headphone / CUE bus ───────────────────────────────────────────────────

    fn set_cue_a(&self, enabled: bool) {
        self.shared_mixer.cue_a.store(enabled, Ordering::Relaxed);
    }

    fn set_cue_b(&self, enabled: bool) {
        self.shared_mixer.cue_b.store(enabled, Ordering::Relaxed);
    }

    fn set_cue_master(&self, enabled: bool) {
        self.shared_mixer.cue_master.store(enabled, Ordering::Relaxed);
    }

    /// 0.0 = full CUE signal, 1.0 = full master mix.
    fn set_hp_mix(&self, value: f32) {
        self.shared_mixer
            .hp_mix
            .store(value.clamp(0.0, 1.0), Ordering::Relaxed);
    }

    /// Headphone output gain: 0.0–2.0, 1.0 = unity.
    fn set_hp_level(&self, value: f32) {
        self.shared_mixer
            .hp_level
            .store(value.clamp(0.0, 2.0), Ordering::Relaxed);
    }

    // ── Playback rate (tempo / pitch) ─────────────────────────────────────────

    /// Set playback rate: 1.0 = normal, 1.05 = +5% tempo, 0.95 = -5%.
    fn set_playback_rate(&self, deck_id: &str, rate: f32) {
        self.deck(deck_id)
            .playback_rate
            .store(rate.clamp(0.5, 2.0), Ordering::Relaxed);
    }

    fn get_playback_rate(&self, deck_id: &str) -> f32 {
        self.deck(deck_id).playback_rate.load(Ordering::Relaxed)
    }

    fn set_channel_gain(&self, deck_id: &str, v: f32) {
        self.deck(deck_id)
            .channel_gain
            .store(v.clamp(0.0, 2.0), Ordering::Relaxed);
    }

    fn set_pregain(&self, deck_id: &str, v: f32) {
        self.deck(deck_id)
            .pregain
            .store(v.clamp(0.0, 4.0), Ordering::Relaxed);
    }

    fn set_channel_filter(&self, deck_id: &str, v: f32) {
        self.deck(deck_id)
            .channel_filter
            .store(v.clamp(-1.0, 1.0), Ordering::Relaxed);
    }

    fn get_channel_filter(&self, deck_id: &str) -> f32 {
        self.deck(deck_id).channel_filter.load(Ordering::Relaxed)
    }

    // ── EQ ───────────────────────────────────────────────────────────────────

    fn set_eq_low(&self, deck_id: &str, gain_db: f32) {
        let s = self.deck(deck_id);
        s.eq_low_db
            .store(gain_db.clamp(-12.0, 12.0), Ordering::Relaxed);
        s.eq_dirty.store(true, Ordering::Release);
    }

    fn set_eq_mid(&self, deck_id: &str, gain_db: f32) {
        let s = self.deck(deck_id);
        s.eq_mid_db
            .store(gain_db.clamp(-12.0, 12.0), Ordering::Relaxed);
        s.eq_dirty.store(true, Ordering::Release);
    }

    fn set_eq_high(&self, deck_id: &str, gain_db: f32) {
        let s = self.deck(deck_id);
        s.eq_high_db
            .store(gain_db.clamp(-12.0, 12.0), Ordering::Relaxed);
        s.eq_dirty.store(true, Ordering::Release);
    }

    fn get_eq_low(&self, deck_id: &str) -> f32 {
        self.deck(deck_id).eq_low_db.load(Ordering::Relaxed)
    }

    fn get_eq_mid(&self, deck_id: &str) -> f32 {
        self.deck(deck_id).eq_mid_db.load(Ordering::Relaxed)
    }

    fn get_eq_high(&self, deck_id: &str) -> f32 {
        self.deck(deck_id).eq_high_db.load(Ordering::Relaxed)
    }

    // ── Nudge / pitch-bend (side jog) ─────────────────────────────────────────

    /// Set temporary rate offset for side-jog nudge. Audio callback decays this to 0.
    fn set_nudge(&self, deck_id: &str, offset: f32) {
        self.deck(deck_id)
            .nudge_target
            .store(offset, Ordering::Relaxed);
    }

    // ── Scratch ───────────────────────────────────────────────────────────────

    /// Enable vinyl scratch mode. Saves `target_rate` for the release ramp.
    fn scratch_enable(&self, deck_id: &str, target_rate: f32) {
        let s = self.deck(deck_id);
        s.scratch.target_rate.store(target_rate, Ordering::Relaxed);
        s.scratch.scratch_active.store(true, Ordering::Release);
    }

    /// Disable scratch mode — audio thread starts exponential ramp to target_rate.
    fn scratch_disable(&self, deck_id: &str) {
        self.deck(deck_id)
            .scratch
            .scratch_active
            .store(false, Ordering::Release);
    }

    /// Accumulate jog ticks (signed: positive=forward, negative=backward).
    fn scratch_tick(&self, deck_id: &str, delta: i32) {
        self.deck(deck_id)
            .scratch
            .pending_ticks
            .fetch_add(delta, Ordering::AcqRel);
    }

    // ── Loop / cue ────────────────────────────────────────────────────────────

    fn set_loop(&self, deck_id: &str, active: bool, start_s: f64, end_s: f64) {
        let s = self.deck(deck_id);
        let sr = self.sr as f64;
        s.loop_start.store((start_s * sr) as u64, Ordering::Relaxed);
        s.loop_end.store((end_s * sr) as u64, Ordering::Relaxed);
        s.loop_active.store(active, Ordering::Release);
    }

    fn set_cue_point(&self, deck_id: &str, position_s: f64) {
        self.deck(deck_id)
            .cue_point
            .store((position_s * self.sr as f64) as u64, Ordering::Relaxed);
    }

    fn get_cue_point_s(&self, deck_id: &str) -> f64 {
        self.deck(deck_id).cue_point.load(Ordering::Relaxed) as f64 / self.sr as f64
    }

    // ── Metering ──────────────────────────────────────────────────────────────

    fn get_peak_levels(&self, deck_id: &str) -> (f32, f32) {
        let s = self.deck(deck_id);
        (
            s.peak_l.load(Ordering::Relaxed),
            s.peak_r.load(Ordering::Relaxed),
        )
    }

    fn get_master_peak(&self) -> (f32, f32) {
        (
            self.shared_mixer.peak_l.load(Ordering::Relaxed),
            self.shared_mixer.peak_r.load(Ordering::Relaxed),
        )
    }

    fn get_clip_flags(&self, deck_id: &str) -> (bool, bool) {
        let s = self.deck(deck_id);
        (
            s.clip_l.load(Ordering::Relaxed),
            s.clip_r.load(Ordering::Relaxed),
        )
    }

    fn get_master_clip(&self) -> (bool, bool) {
        (
            self.shared_mixer.clip_l.load(Ordering::Relaxed),
            self.shared_mixer.clip_r.load(Ordering::Relaxed),
        )
    }

    fn reset_clip(&self, deck_id: &str) {
        let s = self.deck(deck_id);
        s.clip_l.store(false, Ordering::Relaxed);
        s.clip_r.store(false, Ordering::Relaxed);
    }

    fn reset_master_clip(&self) {
        self.shared_mixer.clip_l.store(false, Ordering::Relaxed);
        self.shared_mixer.clip_r.store(false, Ordering::Relaxed);
    }

    fn get_lufs_momentary(&self, deck_id: &str) -> f32 {
        self.deck(deck_id).lufs_momentary.load(Ordering::Relaxed)
    }

    fn get_lufs_shortterm(&self, deck_id: &str) -> f32 {
        self.deck(deck_id).lufs_shortterm.load(Ordering::Relaxed)
    }

    fn get_spectrum(&self, deck_id: &str) -> Vec<f32> {
        self.deck(deck_id)
            .spectrum
            .iter()
            .map(|a| a.load(Ordering::Relaxed))
            .collect()
    }

    fn get_phase_correlation(&self) -> f32 {
        self.shared_mixer.phase_corr.load(Ordering::Relaxed)
    }

    /// Master-bus K-weighted loudness → (momentary_LUFS, short_term_LUFS).
    fn get_master_lufs(&self) -> (f32, f32) {
        (
            self.shared_mixer.lufs_momentary.load(Ordering::Relaxed),
            self.shared_mixer.lufs_shortterm.load(Ordering::Relaxed),
        )
    }

    /// Per-stem peak (decayed). 0.0–2.0 range.
    fn get_stem_peak(&self, deck_id: &str, stem_idx: usize) -> f32 {
        if stem_idx < N_STEMS {
            self.deck(deck_id).stem_peaks[stem_idx].load(Ordering::Relaxed)
        } else {
            0.0
        }
    }

    /// Per-stem K-weighted loudness → (momentary_LUFS, short_term_LUFS).
    /// NEG_INFINITY when no stems are loaded or the stem is silent.
    fn get_stem_lufs(&self, deck_id: &str, stem_idx: usize) -> (f32, f32) {
        if stem_idx < N_STEMS {
            let d = self.deck(deck_id);
            (
                d.stem_lufs_momentary[stem_idx].load(Ordering::Relaxed),
                d.stem_lufs_shortterm[stem_idx].load(Ordering::Relaxed),
            )
        } else {
            (f32::NEG_INFINITY, f32::NEG_INFINITY)
        }
    }

    // ── Live audio snapshots (for oscilloscope display) ───────────────────────

    /// Returns the last ~1024 interleaved stereo frames as a flat f32 list.
    fn get_live_audio(&self, deck_id: &str) -> Vec<f32> {
        match self.deck(deck_id).live_buf.lock() {
            Ok(guard) => guard.clone(),
            Err(_) => vec![0.0f32; shared::LIVE_BUF_FRAMES * 2],
        }
    }

    fn get_master_live_audio(&self) -> Vec<f32> {
        match self.shared_mixer.live_buf.lock() {
            Ok(guard) => guard.clone(),
            Err(_) => vec![0.0f32; shared::LIVE_BUF_FRAMES * 2],
        }
    }

    // ── Buffer loading ────────────────────────────────────────────────────────

    fn load_original(&self, deck_id: &str, samples: PyReadonlyArray2<f32>) {
        let arr = samples.as_array();
        let n_frames = arr.shape()[0];
        let n_ch = arr.shape()[1];
        let mut data = vec![0.0f32; n_frames * 2];
        for i in 0..n_frames {
            data[i * 2] = arr[[i, 0]];
            data[i * 2 + 1] = if n_ch > 1 { arr[[i, 1]] } else { arr[[i, 0]] };
        }
        self.store_buffer(
            deck_id,
            AudioBuffers {
                stems: None,
                original: Some(data),
                n_frames,
                start_position: Some(0),
            },
        );
    }

    // ── FX control ────────────────────────────────────────────────────────────

    fn fx_set_enabled(&self, v: bool) {
        self.shared_fx.enabled.store(v, Ordering::Relaxed);
    }
    fn fx_get_enabled(&self) -> bool {
        self.shared_fx.enabled.load(Ordering::Relaxed)
    }

    fn fx_set_type(&self, v: u32) {
        self.shared_fx.fx_type.store(v, Ordering::Relaxed);
    }
    fn fx_get_type(&self) -> u32 {
        self.shared_fx.fx_type.load(Ordering::Relaxed)
    }

    fn fx_set_target(&self, v: u32) {
        self.shared_fx.target.store(v, Ordering::Relaxed);
    }
    fn fx_get_target(&self) -> u32 {
        self.shared_fx.target.load(Ordering::Relaxed)
    }

    fn fx_set_wet(&self, v: f32) {
        self.shared_fx
            .wet
            .store(v.clamp(0.0, 1.0), Ordering::Relaxed);
    }
    fn fx_get_wet(&self) -> f32 {
        self.shared_fx.wet.load(Ordering::Relaxed)
    }

    fn fx_set_depth(&self, v: f32) {
        self.shared_fx
            .depth
            .store(v.clamp(0.0, 1.0), Ordering::Relaxed);
    }
    fn fx_get_depth(&self) -> f32 {
        self.shared_fx.depth.load(Ordering::Relaxed)
    }

    fn fx_set_feedback(&self, v: f32) {
        self.shared_fx
            .feedback
            .store(v.clamp(0.0, 0.95), Ordering::Relaxed);
    }
    fn fx_get_feedback(&self) -> f32 {
        self.shared_fx.feedback.load(Ordering::Relaxed)
    }

    fn fx_set_time_division(&self, v: f32) {
        self.shared_fx
            .time_division
            .store(v.clamp(0.0625, 4.0), Ordering::Relaxed);
    }
    fn fx_get_time_division(&self) -> f32 {
        self.shared_fx.time_division.load(Ordering::Relaxed)
    }

    fn fx_set_color(&self, v: f32) {
        self.shared_fx
            .color
            .store(v.clamp(-1.0, 1.0), Ordering::Relaxed);
    }
    fn fx_get_color(&self) -> f32 {
        self.shared_fx.color.load(Ordering::Relaxed)
    }

    fn fx_set_bpm(&self, v: f32) {
        self.shared_fx
            .bpm
            .store(v.clamp(20.0, 300.0), Ordering::Relaxed);
    }

    fn wrekk_fx_set_enabled(&self, v: bool) {
        self.shared_fx.wrekk_enabled.store(v, Ordering::Relaxed);
    }
    fn wrekk_fx_get_enabled(&self) -> bool {
        self.shared_fx.wrekk_enabled.load(Ordering::Relaxed)
    }

    fn wrekk_fx_set_type(&self, v: u32) {
        self.shared_fx.wrekk_type.store(v, Ordering::Relaxed);
    }
    fn wrekk_fx_get_type(&self) -> u32 {
        self.shared_fx.wrekk_type.load(Ordering::Relaxed)
    }

    fn wrekk_fx_set_target(&self, v: u32) {
        self.shared_fx.wrekk_target.store(v, Ordering::Relaxed);
    }
    fn wrekk_fx_get_target(&self) -> u32 {
        self.shared_fx.wrekk_target.load(Ordering::Relaxed)
    }

    fn wrekk_fx_set_stem_target(&self, v: u32) {
        self.shared_fx.wrekk_stem_target.store(v, Ordering::Relaxed);
    }
    fn wrekk_fx_get_stem_target(&self) -> u32 {
        self.shared_fx.wrekk_stem_target.load(Ordering::Relaxed)
    }

    fn wrekk_fx_set_wet(&self, v: f32) {
        self.shared_fx
            .wrekk_wet
            .store(v.clamp(0.0, 1.0), Ordering::Relaxed);
    }
    fn wrekk_fx_get_wet(&self) -> f32 {
        self.shared_fx.wrekk_wet.load(Ordering::Relaxed)
    }

    fn wrekk_fx_set_depth(&self, v: f32) {
        self.shared_fx
            .wrekk_depth
            .store(v.clamp(0.0, 1.0), Ordering::Relaxed);
    }
    fn wrekk_fx_get_depth(&self) -> f32 {
        self.shared_fx.wrekk_depth.load(Ordering::Relaxed)
    }

    fn wrekk_fx_set_feedback(&self, v: f32) {
        self.shared_fx
            .wrekk_feedback
            .store(v.clamp(0.0, 0.95), Ordering::Relaxed);
    }
    fn wrekk_fx_get_feedback(&self) -> f32 {
        self.shared_fx.wrekk_feedback.load(Ordering::Relaxed)
    }

    fn wrekk_fx_set_time_division(&self, v: f32) {
        self.shared_fx
            .wrekk_time_division
            .store(v.clamp(0.0625, 4.0), Ordering::Relaxed);
    }
    fn wrekk_fx_get_time_division(&self) -> f32 {
        self.shared_fx.wrekk_time_division.load(Ordering::Relaxed)
    }

    fn wrekk_fx_set_color(&self, v: f32) {
        self.shared_fx
            .wrekk_color
            .store(v.clamp(-1.0, 1.0), Ordering::Relaxed);
    }
    fn wrekk_fx_get_color(&self) -> f32 {
        self.shared_fx.wrekk_color.load(Ordering::Relaxed)
    }

    fn wrekk_fx_set_bpm(&self, v: f32) {
        self.shared_fx
            .wrekk_bpm
            .store(v.clamp(20.0, 300.0), Ordering::Relaxed);
    }

    // ── Buffer loading ────────────────────────────────────────────────────────

    fn load_stems(
        &self,
        deck_id: &str,
        vocals: PyReadonlyArray2<f32>,
        drums: PyReadonlyArray2<f32>,
        bass: PyReadonlyArray2<f32>,
        other: PyReadonlyArray2<f32>,
    ) {
        fn interleave(arr: &PyReadonlyArray2<f32>) -> Vec<f32> {
            let a = arr.as_array();
            let n_frames = a.shape()[0];
            let n_ch = a.shape()[1];
            let mut data = vec![0.0f32; n_frames * 2];
            for i in 0..n_frames {
                data[i * 2] = a[[i, 0]];
                data[i * 2 + 1] = if n_ch > 1 { a[[i, 1]] } else { a[[i, 0]] };
            }
            data
        }

        let n_frames = vocals.as_array().shape()[0];
        let stems = [
            interleave(&vocals),
            interleave(&drums),
            interleave(&bass),
            interleave(&other),
        ];
        self.store_buffer(
            deck_id,
            AudioBuffers {
                stems: Some(stems),
                original: None,
                n_frames,
                start_position: None,
            },
        );
    }
}

impl NativeEngine {
    fn deck(&self, id: &str) -> &Arc<DeckShared> {
        match id {
            "A" | "a" => &self.shared_a,
            _ => &self.shared_b,
        }
    }

    fn store_buffer(&self, deck_id: &str, bufs: AudioBuffers) {
        let s = self.deck(deck_id);
        *s.buffer.write().unwrap() = Some(Arc::new(bufs));
        s.buffer_epoch.fetch_add(1, Ordering::Release);
    }
}

// ── Headphone CUE stream (DDJ-FLX4, ch 2-3) ─────────────────────────────────
//
// Reads pre-crossfader deck audio from DeckShared.live_buf (already written by
// the master callback) and master mix from MixerShared.live_buf.  Uses locally
// pre-allocated snapshot buffers so try_lock failure just repeats the previous
// block — no silence, no heap allocation in the hot path.
//
// ch 0-1 are written as 0.0 (master comes from the PipeWire stream).
// ch 2-3 carry the headphone CUE mix.

fn build_hp_stream(
    host: &cpal::Host,
    sa: Arc<shared::DeckShared>,
    sb: Arc<shared::DeckShared>,
    sm: Arc<shared::MixerShared>,
    sr: u32,
) -> Option<cpal::Stream> {
    let dev = find_hp_device(host)?;

    let dev_name = dev.name().unwrap_or_default();
    let supports_4ch = dev
        .supported_output_configs()
        .ok()
        .map(|mut configs| configs.any(|c| c.channels() >= 4))
        .unwrap_or(false);
    if !supports_4ch {
        eprintln!(
            "[wrekker-engine] CUE warning: found {dev_name:?}, but CPAL did not report a 4+ channel output config; trying anyway"
        );
    }

    let supported_cfg = dev
        .supported_output_configs()
        .ok()
        .and_then(|configs| {
            configs
                .filter(|c| c.channels() >= 4)
                .find(|c| c.min_sample_rate().0 <= sr && sr <= c.max_sample_rate().0)
                .map(|c| c.with_sample_rate(cpal::SampleRate(sr)).config())
        });
    if supported_cfg.is_none() {
        eprintln!(
            "[wrekker-engine] CUE warning: {dev_name:?} does not report native {sr} Hz 4-ch support; CPAL/backend may resample"
        );
    }

    let mut attempts = vec![
        (
            "4ch Fixed(512)",
            cpal::StreamConfig {
                channels: 4,
                sample_rate: cpal::SampleRate(sr),
                buffer_size: cpal::BufferSize::Fixed(512),
            },
        ),
        (
            "4ch Default",
            cpal::StreamConfig {
                channels: 4,
                sample_rate: cpal::SampleRate(sr),
                buffer_size: cpal::BufferSize::Default,
            },
        ),
    ];
    if let Some(mut cfg) = supported_cfg {
        cfg.buffer_size = cpal::BufferSize::Default;
        attempts.push(("reported 4ch Default", cfg));
    }

    let mut stream_and_label = None;
    for (label, cfg) in attempts {
        let channels = cfg.channels as usize;
        match dev.build_output_stream::<f32, _, _>(
            &cfg,
            make_hp_cb(
                Arc::clone(&sa),
                Arc::clone(&sb),
                Arc::clone(&sm),
                channels,
            ),
            |e| eprintln!("[wrekker-engine] CUE stream error: {e}"),
            None,
        ) {
            Ok(stream) => {
                stream_and_label = Some((stream, label, channels));
                break;
            }
            Err(err) => {
                eprintln!("[wrekker-engine] CUE {dev_name:?} failed {label}: {err}");
            }
        }
    }

    let (stream, label, channels) = stream_and_label?;

    eprintln!("[wrekker-engine] CUE → {dev_name:?} ch 2-3 ({channels}-ch, {label})");
    stream.play().ok()?;
    Some(stream)
}

fn find_hp_device(default_host: &cpal::Host) -> Option<cpal::Device> {
    let mut candidates = scan_hp_devices(default_host, "default");
    for host_id in cpal::available_hosts() {
        if let Ok(host) = cpal::host_from_id(host_id) {
            candidates.extend(scan_hp_devices(&host, &format!("{host_id:?}")));
        }
    }

    candidates.sort_by(|a, b| b.score.cmp(&a.score).then_with(|| b.channels.cmp(&a.channels)));
    candidates.dedup_by(|a, b| a.name == b.name && a.host_label == b.host_label);

    if candidates.is_empty() {
        eprintln!("[wrekker-engine] CUE device scan: no output devices reported by CPAL");
        return None;
    }

    eprintln!("[wrekker-engine] CUE device scan:");
    for c in &candidates {
        eprintln!(
            "[wrekker-engine]   host={} score={} channels={} name={:?}",
            c.host_label, c.score, c.channels, c.name
        );
    }

    if let Some(c) = candidates.iter().find(|c| c.score >= 100) {
        eprintln!(
            "[wrekker-engine] CUE selected explicit FLX4 candidate: {:?} ({})",
            c.name, c.host_label
        );
        return Some(c.device.clone());
    }

    let fallback: Vec<&HpDeviceCandidate> = candidates.iter().filter(|c| c.score >= 30).collect();
    if fallback.len() == 1 {
        let c = fallback[0];
        eprintln!(
            "[wrekker-engine] CUE selected unique multichannel fallback: {:?} ({})",
            c.name, c.host_label
        );
        return Some(c.device.clone());
    }

    None
}

struct HpDeviceCandidate {
    device: cpal::Device,
    name: String,
    host_label: String,
    channels: u16,
    score: i32,
}

fn scan_hp_devices(host: &cpal::Host, host_label: &str) -> Vec<HpDeviceCandidate> {
    let mut out = Vec::new();
    let Ok(devices) = host.output_devices() else {
        return out;
    };
    for device in devices {
        let name = device.name().unwrap_or_else(|_| "<unnamed>".to_string());
        let channels = max_output_channels(&device);
        let score = hp_device_score(&name, channels);
        out.push(HpDeviceCandidate {
            device,
            name,
            host_label: host_label.to_string(),
            channels,
            score,
        });
    }
    out
}

fn max_output_channels(device: &cpal::Device) -> u16 {
    device
        .supported_output_configs()
        .ok()
        .and_then(|configs| configs.map(|c| c.channels()).max())
        .or_else(|| device.default_output_config().ok().map(|c| c.channels()))
        .unwrap_or(0)
}

fn hp_device_score(name: &str, channels: u16) -> i32 {
    let compact: String = name
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .flat_map(|c| c.to_lowercase())
        .collect();
    if compact.contains("ddjflx4")
        || (compact.contains("ddj") && compact.contains("flx4"))
        || (compact.contains("pioneer") && compact.contains("flx4"))
    {
        return 100 + channels as i32;
    }
    if channels < 4 {
        return 0;
    }
    if compact == "default"
        || compact.contains("pipewire")
        || compact.contains("pulse")
        || compact.contains("hdmi")
        || compact.contains("monitor")
    {
        return 0;
    }
    if compact.contains("usb") || compact.contains("pioneer") {
        return 50 + channels as i32;
    }
    30 + channels as i32
}

fn make_hp_cb(
    sa: Arc<shared::DeckShared>,
    sb: Arc<shared::DeckShared>,
    sm: Arc<shared::MixerShared>,
    channels: usize,
) -> impl FnMut(&mut [f32], &cpal::OutputCallbackInfo) + Send + 'static {
    const LBF: usize = shared::LIVE_BUF_FRAMES;
    const TARGET_LATENCY_FRAMES: f64 = 2048.0;
    const MAX_DRIFT_CORRECTION: f64 = 0.08;
    let mut snap_a = vec![0.0f32; LBF * 2];
    let mut snap_b = vec![0.0f32; LBF * 2];
    let mut snap_m = vec![0.0f32; LBF * 2];
    let mut read_pos: Option<f64> = None;
    let mut rate_smooth = 1.0_f64;
    let mut hp_limit_gain = 1.0_f32;

    move |data: &mut [f32], _: &cpal::OutputCallbackInfo| {
        let frames = data.len() / channels;

        // Non-blocking snapshot refresh — repeats last block on contention
        if let Ok(g) = sa.live_buf.try_lock() { snap_a.copy_from_slice(&*g); }
        if let Ok(g) = sb.live_buf.try_lock() { snap_b.copy_from_slice(&*g); }
        if let Ok(g) = sm.live_buf.try_lock() { snap_m.copy_from_slice(&*g); }
        let cue_master_en = sm.cue_master.load(Ordering::Relaxed);
        let cue_a_en = sm.cue_a.load(Ordering::Relaxed);
        let cue_b_en = sm.cue_b.load(Ordering::Relaxed);
        let hp_mix   = sm.hp_mix.load(Ordering::Relaxed);
        let hp_level = sm.hp_level.load(Ordering::Relaxed);
        let seq_a = sa.live_seq.load(Ordering::Acquire);
        let seq_b = sb.live_seq.load(Ordering::Acquire);
        let seq_m = sm.live_seq.load(Ordering::Acquire);
        let master_audible = cue_master_en || hp_mix > 0.001;
        let mut seq_end = u64::MAX;
        let mut have_source = false;
        if cue_a_en {
            seq_end = seq_end.min(seq_a);
            have_source = true;
        }
        if cue_b_en {
            seq_end = seq_end.min(seq_b);
            have_source = true;
        }
        if master_audible {
            seq_end = seq_end.min(seq_m);
            have_source = true;
        }
        if !have_source || seq_end == u64::MAX {
            seq_end = seq_m.max(seq_a).max(seq_b);
        }
        let oldest = seq_end.saturating_sub(LBF as u64);

        let target_read = (seq_end as f64 - TARGET_LATENCY_FRAMES).max(oldest as f64);
        let mut pos = match read_pos {
            Some(p)
                if p >= oldest as f64
                    && p + frames as f64 <= seq_end as f64
                    && seq_end > 0 =>
            {
                p
            }
            _ => target_read,
        };
        let fill = seq_end as f64 - pos;
        let correction = ((fill - TARGET_LATENCY_FRAMES) / TARGET_LATENCY_FRAMES * 0.020)
            .clamp(-MAX_DRIFT_CORRECTION, MAX_DRIFT_CORRECTION);
        let target_rate = (1.0 + correction).clamp(0.92, 1.08);
        rate_smooth += (target_rate - rate_smooth) * 0.02;

        let cue_count = (cue_a_en as u8 + cue_b_en as u8).max(1) as f32;
        let cue_sum_gain = 1.0 / cue_count.sqrt();
        let master_gain = if cue_master_en { 1.0 } else { hp_mix };
        let cue_gain = if cue_master_en { 1.0 } else { 1.0 - hp_mix };
        let mut block_peak = 0.0_f32;

        for i in 0..frames {
            let out = i * channels;
            for ch in 0..channels {
                data[out + ch] = 0.0;
            }
            let (al, ar) = read_live_stereo_interp(&snap_a, seq_a, pos);
            let (bl, br) = read_live_stereo_interp(&snap_b, seq_b, pos);
            let (ml, mr) = read_live_stereo_interp(&snap_m, seq_m, pos);

            let hl = ((if cue_a_en { al } else { 0.0 }) + if cue_b_en { bl } else { 0.0 }) * cue_sum_gain;
            let hr = ((if cue_a_en { ar } else { 0.0 }) + if cue_b_en { br } else { 0.0 }) * cue_sum_gain;

            // hp_mix blends master into the monitor. MST CUE adds master to
            // headphones without disabling active deck CUE buttons.
            let l = (hl * cue_gain + ml * master_gain) * hp_level;
            let r = (hr * cue_gain + mr * master_gain) * hp_level;
            data[out + 2] = l;
            data[out + 3] = r;
            block_peak = block_peak.max(l.abs()).max(r.abs());
            pos += rate_smooth;
        }

        let limit_target = if block_peak > 0.98 {
            (0.98 / block_peak).clamp(0.2, 1.0)
        } else {
            1.0
        };
        let attack = 0.35_f32;
        let release = 0.03_f32;
        if limit_target < hp_limit_gain {
            hp_limit_gain += attack * (limit_target - hp_limit_gain);
        } else {
            hp_limit_gain += release * (limit_target - hp_limit_gain);
        }
        if hp_limit_gain < 0.9995 {
            for frame in data.chunks_exact_mut(channels) {
                frame[2] *= hp_limit_gain;
                frame[3] *= hp_limit_gain;
            }
        }
        read_pos = Some(pos);
    }
}

fn read_live_stereo_interp(buf: &[f32], seq_end: u64, frame_pos: f64) -> (f32, f32) {
    if !frame_pos.is_finite() || seq_end == 0 {
        return (0.0, 0.0);
    }
    let base = frame_pos.floor();
    let frac = (frame_pos - base) as f32;
    let a = read_live_stereo_nearest(buf, seq_end, base as u64);
    let b = read_live_stereo_nearest(buf, seq_end, base as u64 + 1);
    (a.0 + (b.0 - a.0) * frac, a.1 + (b.1 - a.1) * frac)
}

fn read_live_stereo_nearest(buf: &[f32], seq_end: u64, frame_seq: u64) -> (f32, f32) {
    const LBF: u64 = shared::LIVE_BUF_FRAMES as u64;
    let age = match seq_end.checked_sub(frame_seq) {
        Some(v) if v > 0 && v <= LBF => v,
        _ => return (0.0, 0.0),
    };
    let idx = (LBF - age) as usize * 2;
    (buf[idx], buf[idx + 1])
}

// ── Module registration ───────────────────────────────────────────────────────

#[pymodule]
fn wrekker_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NativeEngine>()?;
    m.add_class::<NativePhaseSync>()?;
    m.add_class::<NativeTimeStretch>()?;
    Ok(())
}
