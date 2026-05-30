//! Beat-to-beat phase-locked loop for deck sync.
//!
//! The controller keeps the sync math in Rust. Python may still decide which
//! deck follows which, but rate correction and lock-state logic live here.

#[derive(Debug, Clone)]
pub struct PhaseSync {
    master_bpm: f64,
    slave_bpm: f64,
    phase_error: f64, // slave phase - master phase, in beats
    kp: f64,
    dead_zone_beats: f64,
    max_correction_rate: f64, // semitones / second
    last_master_beat_time: Option<f64>,
    last_slave_beat_time: Option<f64>,
    current_ratio: f64,
    target_pull_in_beats: u32,
}

impl PhaseSync {
    pub fn new(kp: f64, dead_zone_beats: f64, max_correction_rate: f64) -> Self {
        Self {
            master_bpm: 120.0,
            slave_bpm: 120.0,
            phase_error: 0.0,
            kp: kp.clamp(0.0, 2.0),
            dead_zone_beats: dead_zone_beats.abs().clamp(0.0, 0.25),
            max_correction_rate: max_correction_rate.abs().clamp(0.01, 24.0),
            last_master_beat_time: None,
            last_slave_beat_time: None,
            current_ratio: 1.0,
            target_pull_in_beats: 0,
        }
    }

    pub fn on_master_beat(&mut self, master_beat_time: f64, master_bpm: f64) {
        self.master_bpm = sanitize_bpm(master_bpm, self.master_bpm);
        self.last_master_beat_time = Some(master_beat_time);
        self.update_error_from_beat_times();
    }

    pub fn on_slave_beat(&mut self, slave_beat_time: f64, slave_bpm: f64) {
        self.slave_bpm = sanitize_bpm(slave_bpm, self.slave_bpm);
        self.last_slave_beat_time = Some(slave_beat_time);
        self.update_error_from_beat_times();
    }

    pub fn compute_correction(&self) -> f64 {
        self.target_ratio_for_error(self.phase_error)
    }

    /// Audio/control callback helper: update the measured phase error and slew
    /// the correction ratio by `max_correction_rate`.
    pub fn update_phase_error(
        &mut self,
        slave_minus_master_beats: f64,
        master_bpm: f64,
        slave_bpm: f64,
        dt_seconds: f64,
    ) -> f64 {
        self.master_bpm = sanitize_bpm(master_bpm, self.master_bpm);
        self.slave_bpm = sanitize_bpm(slave_bpm, self.slave_bpm);
        self.phase_error = wrap_phase_error(slave_minus_master_beats);

        let target = self.compute_correction();
        let dt = dt_seconds.max(0.0);
        let max_semitone_delta = self.max_correction_rate * dt;
        let max_ratio_delta = 2.0_f64.powf(max_semitone_delta / 12.0) - 1.0;
        let delta = (target - self.current_ratio).clamp(-max_ratio_delta, max_ratio_delta);
        self.current_ratio = (self.current_ratio + delta).clamp(0.5, 2.0);
        self.current_ratio
    }

    pub fn snap_to_grid(&mut self) -> f64 {
        let Some(master_time) = self.last_master_beat_time else {
            return 0.0;
        };
        let Some(slave_time) = self.last_slave_beat_time else {
            return 0.0;
        };
        let master_period = 60.0 / sanitize_bpm(self.master_bpm, 120.0);
        let slave_period = 60.0 / sanitize_bpm(self.slave_bpm, 120.0);
        let offset = (master_time + master_period) - (slave_time + slave_period);
        self.phase_error = 0.0;
        self.current_ratio = 1.0;
        offset
    }

    pub fn pull_in(&mut self, beats_to_converge: u32) {
        self.target_pull_in_beats = beats_to_converge.max(1);
        self.kp = (1.0 / self.target_pull_in_beats as f64).clamp(0.05, 1.0);
    }

    pub fn is_locked(&self) -> bool {
        self.phase_error.abs() < self.dead_zone_beats
    }

    pub fn phase_error_ms(&self) -> f64 {
        let period_ms = 60_000.0 / sanitize_bpm(self.master_bpm, 120.0);
        self.phase_error * period_ms
    }

    pub fn phase_error_beats(&self) -> f64 {
        self.phase_error
    }

    pub fn current_ratio(&self) -> f64 {
        self.current_ratio
    }

    pub fn reset(&mut self) {
        self.phase_error = 0.0;
        self.current_ratio = 1.0;
        self.last_master_beat_time = None;
        self.last_slave_beat_time = None;
    }

    fn target_ratio_for_error(&self, phase_error: f64) -> f64 {
        if phase_error.abs() < self.dead_zone_beats {
            return 1.0;
        }

        // Positive error means the slave is ahead of the master, so slow it
        // down. Negative error means the slave is behind, so speed it up.
        let raw = 1.0 - self.kp * phase_error;
        let max_offset = 2.0_f64.powf(self.max_correction_rate / 12.0) - 1.0;
        raw.clamp(1.0 - max_offset, 1.0 + max_offset)
            .clamp(0.5, 2.0)
    }

    fn update_error_from_beat_times(&mut self) {
        let (Some(master_time), Some(slave_time)) =
            (self.last_master_beat_time, self.last_slave_beat_time)
        else {
            return;
        };
        let master_period = 60.0 / sanitize_bpm(self.master_bpm, 120.0);
        self.phase_error = wrap_phase_error((slave_time - master_time) / master_period);
    }
}

fn sanitize_bpm(value: f64, fallback: f64) -> f64 {
    if value.is_finite() && (20.0..=300.0).contains(&value) {
        value
    } else {
        fallback.clamp(20.0, 300.0)
    }
}

fn wrap_phase_error(value: f64) -> f64 {
    let mut err = value - value.round();
    if err <= -0.5 {
        err += 1.0;
    } else if err > 0.5 {
        err -= 1.0;
    }
    err
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pll_convergence() {
        let mut pll = PhaseSync::new(0.35, 0.02, 2.0);
        let master_bpm = 128.0;
        let slave_bpm = 128.5;
        let mut error = 0.20;

        for _ in 0..32 {
            let ratio = pll.update_phase_error(error, master_bpm, slave_bpm, 0.125);
            error += (ratio - 1.0) * 0.5;
            error = wrap_phase_error(error);
        }

        assert!(error.abs() < 0.02, "PLL did not converge: {error}");
    }

    #[test]
    fn test_dead_zone() {
        let mut pll = PhaseSync::new(0.5, 0.02, 2.0);
        let ratio = pll.update_phase_error(0.01, 128.0, 128.0, 0.1);
        assert!((ratio - 1.0).abs() < 1e-9);
    }

    #[test]
    fn test_snap_to_grid() {
        let mut pll = PhaseSync::new(0.5, 0.02, 2.0);
        pll.on_master_beat(10.0, 120.0);
        pll.on_slave_beat(10.25, 120.0);
        let offset = pll.snap_to_grid();
        assert!((offset + 0.25).abs() < 1e-9);
        assert!(pll.is_locked());
    }
}
