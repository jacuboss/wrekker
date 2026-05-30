//! Rubber Band based time-stretch / pitch-shift wrapper.
//!
//! This module is intentionally independent from the deck render loop for now:
//! it gives Wrekker one Rust-side API for offline Finer rendering and realtime
//! Faster processing. PhaseSync can drive this API without involving Python.

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum StretchMode {
    Finer,
    Faster { max_latency_ms: f32 },
}

enum Backend {
    #[cfg(wrekker_has_rubberband)]
    RubberBand(rubberband_ffi::RubberBandBackend),
    Passthrough,
}

pub struct WrekkerTimeStretch {
    backend: Backend,
    mode: StretchMode,
    sample_rate: u32,
    channels: usize,
    time_ratio: f64,
    pitch_semitones: f64,
    formant_preservation: bool,
}

impl WrekkerTimeStretch {
    pub fn new(sample_rate: u32, channels: usize, mode: StretchMode) -> Self {
        let channels = channels.max(1);
        let mut this = Self {
            backend: Backend::Passthrough,
            mode,
            sample_rate,
            channels,
            time_ratio: 1.0,
            pitch_semitones: 0.0,
            formant_preservation: false,
        };

        #[cfg(wrekker_has_rubberband)]
        {
            if let Some(rb) = rubberband_ffi::RubberBandBackend::new(sample_rate, channels, mode) {
                this.backend = Backend::RubberBand(rb);
            }
        }

        this
    }

    pub fn set_time_ratio(&mut self, ratio: f64) {
        self.time_ratio = ratio.clamp(0.5, 2.0);
        #[cfg(wrekker_has_rubberband)]
        if let Backend::RubberBand(rb) = &mut self.backend {
            rb.set_time_ratio(self.time_ratio);
        }
    }

    pub fn set_pitch_semitones(&mut self, semitones: f64) {
        self.pitch_semitones = semitones.clamp(-12.0, 12.0);
        let scale = 2.0_f64.powf(self.pitch_semitones / 12.0);
        #[cfg(wrekker_has_rubberband)]
        if let Backend::RubberBand(rb) = &mut self.backend {
            rb.set_pitch_scale(scale);
        }
    }

    pub fn set_formant_preservation(&mut self, enabled: bool) {
        self.formant_preservation = enabled;
        #[cfg(wrekker_has_rubberband)]
        if let Backend::RubberBand(rb) = &mut self.backend {
            rb.set_formant_preservation(enabled);
        }
    }

    pub fn process(&mut self, input: &[f32]) -> Vec<f32> {
        if input.is_empty() {
            return Vec::new();
        }
        let usable = input.len() - (input.len() % self.channels);
        if usable == 0 {
            return Vec::new();
        }
        match &mut self.backend {
            #[cfg(wrekker_has_rubberband)]
            Backend::RubberBand(rb) => rb.process(&input[..usable], self.channels, false),
            Backend::Passthrough => input[..usable].to_vec(),
        }
    }

    pub fn process_final(&mut self, input: &[f32]) -> Vec<f32> {
        if input.is_empty() {
            match &mut self.backend {
                #[cfg(wrekker_has_rubberband)]
                Backend::RubberBand(rb) => rb.process(&[], self.channels, true),
                Backend::Passthrough => Vec::new(),
            }
        } else {
            let usable = input.len() - (input.len() % self.channels);
            match &mut self.backend {
                #[cfg(wrekker_has_rubberband)]
                Backend::RubberBand(rb) => rb.process(&input[..usable], self.channels, true),
                Backend::Passthrough => input[..usable].to_vec(),
            }
        }
    }

    pub fn reset(&mut self) {
        #[cfg(wrekker_has_rubberband)]
        if let Backend::RubberBand(rb) = &mut self.backend {
            rb.reset();
        }
    }

    pub fn is_rubberband_active(&self) -> bool {
        #[cfg(wrekker_has_rubberband)]
        {
            matches!(self.backend, Backend::RubberBand(_))
        }
        #[cfg(not(wrekker_has_rubberband))]
        {
            false
        }
    }

    pub fn mode(&self) -> StretchMode {
        self.mode
    }

    pub fn sample_rate(&self) -> u32 {
        self.sample_rate
    }

    pub fn channels(&self) -> usize {
        self.channels
    }

    pub fn time_ratio(&self) -> f64 {
        self.time_ratio
    }

    pub fn pitch_semitones(&self) -> f64 {
        self.pitch_semitones
    }

    pub fn formant_preservation(&self) -> bool {
        self.formant_preservation
    }
}

#[cfg(wrekker_has_rubberband)]
mod rubberband_ffi {
    use super::StretchMode;
    use std::ffi::c_void;

    type RubberBandState = *mut c_void;
    type RubberBandOptions = i32;

    const RUBBERBAND_OPTION_PROCESS_OFFLINE: RubberBandOptions = 0x0000_0000;
    const RUBBERBAND_OPTION_PROCESS_REALTIME: RubberBandOptions = 0x0000_0001;
    const RUBBERBAND_OPTION_TRANSIENTS_CRISP: RubberBandOptions = 0x0000_0000;
    const RUBBERBAND_OPTION_DETECTOR_COMPOUND: RubberBandOptions = 0x0000_0000;
    const RUBBERBAND_OPTION_PHASE_LAMINAR: RubberBandOptions = 0x0000_0000;
    const RUBBERBAND_OPTION_THREADING_NEVER: RubberBandOptions = 0x0001_0000;
    const RUBBERBAND_OPTION_WINDOW_SHORT: RubberBandOptions = 0x0010_0000;
    const RUBBERBAND_OPTION_WINDOW_LONG: RubberBandOptions = 0x0020_0000;
    const RUBBERBAND_OPTION_FORMANT_SHIFTED: RubberBandOptions = 0x0000_0000;
    const RUBBERBAND_OPTION_FORMANT_PRESERVED: RubberBandOptions = 0x0100_0000;
    const RUBBERBAND_OPTION_PITCH_HIGH_SPEED: RubberBandOptions = 0x0000_0000;
    const RUBBERBAND_OPTION_PITCH_HIGH_QUALITY: RubberBandOptions = 0x0200_0000;
    const RUBBERBAND_OPTION_CHANNELS_TOGETHER: RubberBandOptions = 0x1000_0000;
    const RUBBERBAND_OPTION_ENGINE_FASTER: RubberBandOptions = 0x0000_0000;
    const RUBBERBAND_OPTION_ENGINE_FINER: RubberBandOptions = 0x2000_0000;

    extern "C" {
        fn rubberband_new(
            sample_rate: u32,
            channels: u32,
            options: RubberBandOptions,
            initial_time_ratio: f64,
            initial_pitch_scale: f64,
        ) -> RubberBandState;
        fn rubberband_delete(state: RubberBandState);
        fn rubberband_reset(state: RubberBandState);
        fn rubberband_set_time_ratio(state: RubberBandState, ratio: f64);
        fn rubberband_set_pitch_scale(state: RubberBandState, scale: f64);
        fn rubberband_set_formant_option(state: RubberBandState, options: RubberBandOptions);
        fn rubberband_set_max_process_size(state: RubberBandState, samples: u32);
        fn rubberband_process(
            state: RubberBandState,
            input: *const *const f32,
            samples: u32,
            final_block: i32,
        );
        fn rubberband_available(state: RubberBandState) -> i32;
        fn rubberband_retrieve(
            state: RubberBandState,
            output: *const *mut f32,
            samples: u32,
        ) -> u32;
    }

    pub struct RubberBandBackend {
        state: RubberBandState,
        planar_in: Vec<Vec<f32>>,
        planar_out: Vec<Vec<f32>>,
    }

    unsafe impl Send for RubberBandBackend {}

    impl RubberBandBackend {
        pub fn new(sample_rate: u32, channels: usize, mode: StretchMode) -> Option<Self> {
            let mode_options = match mode {
                StretchMode::Finer => {
                    RUBBERBAND_OPTION_PROCESS_OFFLINE
                        | RUBBERBAND_OPTION_ENGINE_FINER
                        | RUBBERBAND_OPTION_WINDOW_LONG
                        | RUBBERBAND_OPTION_PITCH_HIGH_QUALITY
                }
                StretchMode::Faster { .. } => {
                    RUBBERBAND_OPTION_PROCESS_REALTIME
                        | RUBBERBAND_OPTION_ENGINE_FASTER
                        | RUBBERBAND_OPTION_WINDOW_SHORT
                        | RUBBERBAND_OPTION_PITCH_HIGH_SPEED
                }
            };
            let options = mode_options
                | RUBBERBAND_OPTION_TRANSIENTS_CRISP
                | RUBBERBAND_OPTION_DETECTOR_COMPOUND
                | RUBBERBAND_OPTION_PHASE_LAMINAR
                | RUBBERBAND_OPTION_THREADING_NEVER
                | RUBBERBAND_OPTION_FORMANT_SHIFTED
                | RUBBERBAND_OPTION_CHANNELS_TOGETHER;
            let state = unsafe { rubberband_new(sample_rate, channels as u32, options, 1.0, 1.0) };
            if state.is_null() {
                return None;
            }
            if let StretchMode::Faster { max_latency_ms } = mode {
                let max_frames =
                    ((sample_rate as f32 * max_latency_ms / 1000.0).ceil() as u32).max(64);
                unsafe { rubberband_set_max_process_size(state, max_frames) };
            }
            Some(Self {
                state,
                planar_in: vec![Vec::new(); channels],
                planar_out: vec![Vec::new(); channels],
            })
        }

        pub fn set_time_ratio(&mut self, ratio: f64) {
            unsafe { rubberband_set_time_ratio(self.state, ratio) };
        }

        pub fn set_pitch_scale(&mut self, scale: f64) {
            unsafe { rubberband_set_pitch_scale(self.state, scale) };
        }

        pub fn set_formant_preservation(&mut self, enabled: bool) {
            let opt = if enabled {
                RUBBERBAND_OPTION_FORMANT_PRESERVED
            } else {
                RUBBERBAND_OPTION_FORMANT_SHIFTED
            };
            unsafe { rubberband_set_formant_option(self.state, opt) };
        }

        pub fn reset(&mut self) {
            unsafe { rubberband_reset(self.state) };
        }

        pub fn process(&mut self, input: &[f32], channels: usize, final_block: bool) -> Vec<f32> {
            let frames = input.len() / channels;
            self.ensure_capacity(channels, frames);

            for ch in 0..channels {
                self.planar_in[ch].clear();
                self.planar_in[ch].extend((0..frames).map(|i| input[i * channels + ch]));
            }

            let ptrs: Vec<*const f32> = self.planar_in.iter().map(|ch| ch.as_ptr()).collect();
            unsafe {
                rubberband_process(
                    self.state,
                    ptrs.as_ptr(),
                    frames as u32,
                    i32::from(final_block),
                );
            }

            let available = unsafe { rubberband_available(self.state).max(0) as usize };
            if available == 0 {
                return Vec::new();
            }
            self.ensure_capacity(channels, available);
            for ch in 0..channels {
                self.planar_out[ch].resize(available, 0.0);
            }
            let out_ptrs: Vec<*mut f32> = self
                .planar_out
                .iter_mut()
                .map(|ch| ch.as_mut_ptr())
                .collect();
            let got = unsafe {
                rubberband_retrieve(self.state, out_ptrs.as_ptr(), available as u32) as usize
            };

            let mut interleaved = vec![0.0_f32; got * channels];
            for i in 0..got {
                for ch in 0..channels {
                    interleaved[i * channels + ch] = self.planar_out[ch][i];
                }
            }
            interleaved
        }

        fn ensure_capacity(&mut self, channels: usize, frames: usize) {
            if self.planar_in.len() != channels {
                self.planar_in = vec![Vec::new(); channels];
                self.planar_out = vec![Vec::new(); channels];
            }
            for ch in 0..channels {
                let in_capacity = self.planar_in[ch].capacity();
                let out_capacity = self.planar_out[ch].capacity();
                self.planar_in[ch].reserve(frames.saturating_sub(in_capacity));
                self.planar_out[ch].reserve(frames.saturating_sub(out_capacity));
            }
        }
    }

    impl Drop for RubberBandBackend {
        fn drop(&mut self) {
            unsafe { rubberband_delete(self.state) };
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn passthrough_or_rubberband_constructs() {
        let ts = WrekkerTimeStretch::new(
            44100,
            2,
            StretchMode::Faster {
                max_latency_ms: 10.0,
            },
        );
        assert_eq!(ts.sample_rate(), 44100);
        assert_eq!(ts.channels(), 2);
    }

    #[test]
    fn process_keeps_interleaved_shape_valid() {
        let mut ts = WrekkerTimeStretch::new(
            44100,
            2,
            StretchMode::Faster {
                max_latency_ms: 10.0,
            },
        );
        ts.set_time_ratio(1.0);
        ts.set_pitch_semitones(0.0);
        let input = vec![0.0_f32; 512];
        let out = ts.process(&input);
        assert_eq!(out.len() % 2, 0);
    }
}
