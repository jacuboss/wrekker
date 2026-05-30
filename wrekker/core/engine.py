"""
Wrekker audio engine.

AudioEngine manages two DeckPlayback objects and a sounddevice output stream.
The audio callback mixes stems from both decks, applies the crossfader, updates
metering, and writes to the output.

Architecture constraints (audio callback):
  - No I/O, no Python allocations, no GC-triggering code
  - All buffers pre-allocated in __init__
  - Stem gains communicated via float64 arrays (atomic on x86/ARM)
  - Metrics snapshot dispatched via a threading.Event → UI reads at ~10 Hz

DeckPlayback is the mutable, audio-thread-facing mirror of DeckState.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from wrekker.core.deck import (
    STEM_NAMES,
    STEM_GAIN_MAX,
    DeckID,
    DeckMetrics,
    DeckState,
    DeckStatus,
    LoudnessMeasure,
    SpectralBands,
    StemState,
    TrackInfo,
    WaveformData,
)
from wrekker.core.metering import LUFSMeter, PhaseCorrelator, SpectralMeter
from wrekker.core.eq import DeckEQ
from wrekker.stems.models import StemResult

__all__ = ["AudioEngine", "DeckPlayback"]

_NEG_INF = float("-inf")
_FADE_MS  = 500.0     # default stem crossfade duration


# ─── per-stem crossfade gains ─────────────────────────────────────────────────

class _StemGains:
    """
    Per-stem smooth gain engine. Equal-power curve; no allocations in hot path.
    current and target are float64 arrays — writes from main thread are
    effectively atomic for individual elements on x86/ARM.
    """

    FADE_CURVE = "equal_power"   # sin/cos pair

    def __init__(self, sr: int, fade_ms: float = _FADE_MS) -> None:
        self._fade_n   = max(1, int(sr * fade_ms / 1000.0))
        self._current  = np.ones(len(STEM_NAMES), dtype=np.float64)
        self._target   = np.ones(len(STEM_NAMES), dtype=np.float64)
        self._faded    = np.zeros(len(STEM_NAMES), dtype=np.int64)

    def set_target(self, idx: int, gain: float) -> None:
        """Main thread — set target gain for stem `idx`."""
        self._target[idx] = max(0.0, min(STEM_GAIN_MAX, gain))
        self._faded [idx] = 0

    def ramp(self, idx: int, frames: int) -> np.ndarray:
        """Audio thread — return gain ramp (frames,) for stem `idx`."""
        cur = self._current[idx]
        tgt = self._target [idx]
        if cur == tgt:
            return np.full(frames, cur, dtype=np.float32)

        rem      = self._fade_n - self._faded[idx]
        fade_now = min(frames, rem)
        t        = np.linspace(0.0, float(fade_now) / self._fade_n,
                               fade_now, dtype=np.float32)

        if tgt > cur:   # activating → sin curve
            curve = np.sin(t * (math.pi / 2.0)) * (tgt - cur) + cur
        else:           # deactivating → cos curve
            curve = (0.5 + 0.5 * np.cos(t * math.pi)) * (cur - tgt) + tgt

        ramp = np.empty(frames, dtype=np.float32)
        ramp[:fade_now] = curve
        if fade_now < frames:
            ramp[fade_now:] = tgt

        self._faded  [idx] += fade_now
        self._current[idx]  = float(ramp[-1])
        if self._faded[idx] >= self._fade_n:
            self._current[idx] = tgt

        return ramp


# ─── deck playback ────────────────────────────────────────────────────────────

class DeckPlayback:
    """
    Mutable per-deck audio state owned by the audio engine.
    All attributes accessed from the audio callback — no locking allowed.

    Main thread communicates via:
      - stem_gains._target   (float64 array, element writes are atomic)
      - playing / pitch_factor  (simple assignments)
    """

    def __init__(self, deck_id: DeckID, sr: int, blocksize: int = 1024) -> None:
        # Target ~80 ms metrics update rate regardless of blocksize
        self._METRICS_EVERY = max(1, int(0.08 * sr / blocksize))
        self.deck_id      = deck_id
        self.sr           = sr
        self.original:    Optional[np.ndarray] = None   # (samples, 2) float32
        self.stems:       Optional[StemResult] = None
        self.read_pos:    int  = 0
        self.playing:     bool = False
        self.pitch_factor: float = 1.0   # playback rate multiplier

        # Per-stem gains with crossfade
        self.stem_gains = _StemGains(sr)

        # Metering (updated in callback, snapshotted for UI)
        self._lufs    = LUFSMeter(sr, 2)
        self._stem_lufs:     list[LUFSMeter]   = [LUFSMeter(sr, 2) for _ in STEM_NAMES]
        self._stem_spectral: list[SpectralMeter] = [SpectralMeter(sr) for _ in STEM_NAMES]
        self._deck_spectral  = SpectralMeter(sr)

        # Metrics snapshot — replaced atomically
        self._metrics: DeckMetrics | None = None
        self._metrics_lock = threading.Lock()
        self._cb_count = 0

        # Stem metering runs every _STEM_METER_STRIDE callbacks (4x cost reduction)
        self._stem_meter_count = 0
        self._STEM_METER_STRIDE = 4

        # Pre-allocated mix buffer (resized on blocksize change)
        self._mix_buf: Optional[np.ndarray] = None

        # Loop state — written by main thread (atomic on CPython), read by callback
        self._loop_active: bool  = False
        self._loop_start:  int   = 0   # sample index
        self._loop_end:    int   = 0   # sample index (exclusive)

        # Cue point (sample index)
        self._cue_point: int = 0

        # Waveform visualization data — set on load, beats patched by analysis thread
        self.waveform: Optional["WaveformData"] = None
        self.waveform_seq: int = 0  # bumped whenever waveform changes

    # ── loop / cue control (main thread) ─────────────────────────────────────

    def set_loop(self, active: bool, start_s: float, end_s: float) -> None:
        self._loop_start  = int(start_s * self.sr)
        self._loop_end    = int(end_s   * self.sr)
        self._loop_active = active

    def set_cue_point(self, position_s: float) -> None:
        self._cue_point = max(0, int(position_s * self.sr))

    @property
    def cue_point_s(self) -> float:
        return self._cue_point / self.sr

    # ── audio-thread API ──────────────────────────────────────────────────────

    def mix_frame(self, frames: int) -> np.ndarray:
        """
        Produce one frame of mixed audio. Called ONLY from audio callback.
        Returns (frames, 2) float32. Never allocates after warm-up.
        """
        if self._mix_buf is None or len(self._mix_buf) != frames:
            self._mix_buf = np.zeros((frames, 2), dtype=np.float32)
        else:
            self._mix_buf[:] = 0.0

        if not self.playing:
            return self._mix_buf

        # Snapshot loop state (avoid multiple attribute lookups)
        loop_active = self._loop_active
        loop_start  = self._loop_start
        loop_end    = self._loop_end
        loop_valid  = loop_active and loop_end > loop_start

        pos = self.read_pos

        # When looping, clip the read window to the loop boundary so we don't
        # read past loop_end in a single callback; the remainder wraps around.
        read_end = min(pos + frames, loop_end) if loop_valid else pos + frames

        if self.stems is not None:
            # ── stem-based mixing ────────────────────────────────────────
            for i, name in enumerate(STEM_NAMES):
                stem = self.stems[name]          # (2, total_samples)
                end  = min(read_end, stem.shape[-1])
                n    = end - pos
                if n <= 0:
                    continue

                chunk = stem[:, pos:end].T       # (n, 2)
                if chunk.shape[1] == 1:
                    chunk = np.repeat(chunk, 2, axis=1)

                gain = self.stem_gains.ramp(i, n)
                weighted = chunk * gain[:, np.newaxis]
                self._mix_buf[:n] += weighted

                # Per-stem metering — strided to reduce callback load
                if self._stem_meter_count == 0:
                    self._stem_lufs    [i].process(weighted)
                    self._stem_spectral[i].process(weighted)

        elif self.original is not None:
            # ── fallback: original audio ─────────────────────────────────
            end = min(read_end, self.original.shape[0])
            n   = end - pos
            if n > 0:
                self._mix_buf[:n] = self.original[pos:end]

        # Advance read head
        self.read_pos = pos + frames

        # ── loop enforcement (audio thread) ──────────────────────────────
        if loop_valid and self.read_pos >= loop_end:
            loop_len = loop_end - loop_start
            overflow = (self.read_pos - loop_start) % loop_len
            self.read_pos = loop_start + overflow

        # LUFS metering on the pre-EQ stem mix (loudness of the source material)
        self._lufs.process(self._mix_buf)
        self._stem_meter_count = (self._stem_meter_count + 1) % self._STEM_METER_STRIDE
        # Note: _deck_spectral is fed post-EQ from AudioEngine._callback so the
        # spectrum display reflects both stem faders AND EQ band kills.

        # Periodically snapshot metrics
        self._cb_count += 1
        if self._cb_count >= self._METRICS_EVERY:
            self._cb_count = 0
            self._snapshot_metrics()

        return self._mix_buf

    def _snapshot_metrics(self) -> None:
        """Build and publish DeckMetrics snapshot. Called from audio callback."""
        lufs     = self._lufs.snapshot()
        spec     = self._deck_spectral.snapshot()
        spec_raw = self._deck_spectral.snapshot_raw()
        m        = DeckMetrics(
            lufs       = lufs,
            spectral   = spec,
            spectrum   = spec_raw,
            phase_corr = None,   # filled by engine after both decks mix
        )
        with self._metrics_lock:
            self._metrics = m

    # ── main-thread API ───────────────────────────────────────────────────────

    def get_metrics(self) -> DeckMetrics | None:
        with self._metrics_lock:
            return self._metrics

    def get_stem_meters(self, name: str) -> tuple[LoudnessMeasure, SpectralBands]:
        i    = STEM_NAMES.index(name)
        lufs = self._stem_lufs    [i].snapshot()
        spec = self._stem_spectral[i].snapshot()
        return lufs, spec

    def load_audio(
        self,
        original: np.ndarray,   # (samples, channels) float32
        stems:    Optional[StemResult] = None,
    ) -> None:
        """Load audio for playback. Called from main thread before playing."""
        # Ensure 2 channels
        if original.ndim == 1 or original.shape[1] == 1:
            original = np.repeat(
                original.reshape(-1, 1) if original.ndim == 1 else original,
                2, axis=1,
            )
        self.original = original
        self.stems    = stems
        self.read_pos = 0
        self._lufs.reset()
        self._deck_spectral.reset()
        for m in self._stem_lufs:     m.reset()
        for m in self._stem_spectral: m.reset()

    def set_stems(self, stems: StemResult) -> None:
        self.stems = stems

    def seek(self, position_s: float) -> None:
        self.read_pos = max(0, int(position_s * self.sr))

    @property
    def position_s(self) -> float:
        return self.read_pos / self.sr


# ─── audio engine ─────────────────────────────────────────────────────────────

class AudioEngine:
    """
    Core audio engine: two decks, crossfader, master output.

    Usage:
        engine = AudioEngine()
        engine.start()
        engine.deck_a.load_audio(audio, stems)
        engine.play("A")
        engine.set_crossfader(0.5)

    Metrics are available via engine.get_deck_state("A") / engine.get_metrics().
    Register a state_callback to be notified of changes (~10 Hz).
    """

    def __init__(self, sr: int = 44100, blocksize: int = 256) -> None:
        self._sr        = sr
        self._blocksize = blocksize

        self.deck_a = DeckPlayback(DeckID.A, sr, blocksize)
        self.deck_b = DeckPlayback(DeckID.B, sr, blocksize)

        # Crossfader: 0.0 = full A, 0.5 = center, 1.0 = full B
        self._crossfader  = 0.5
        self._master_gain = 1.0

        self._phase_corr = PhaseCorrelator(sr)
        self._last_phase: float = 0.0

        self._stream:   Optional[sd.OutputStream] = None
        self._running   = False

        # State snapshots (DeckState) — built by transport.py, read by UI
        self._states: dict[str, DeckState] = {
            "A": DeckState.empty(DeckID.A),
            "B": DeckState.empty(DeckID.B),
        }
        self._state_lock = threading.Lock()

        # Registered callbacks — called from a dispatch thread ~10 Hz
        self._state_callbacks: list[Callable[[dict[str, DeckState]], None]] = []
        self._dispatch_event  = threading.Event()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="engine-dispatch",
        )

        # Per-deck EQ
        self._eq_a = DeckEQ(sr)
        self._eq_b = DeckEQ(sr)

        # Peak level tracking (written by audio callback, read by UI)
        # float64 for effective atomic reads/writes under CPython GIL
        self._peaks_a = np.zeros(2, dtype=np.float64)    # [L, R] 0.0–1.0+
        self._peaks_b = np.zeros(2, dtype=np.float64)
        self._peaks_m = np.zeros(2, dtype=np.float64)    # master out
        self._clip_a  = [False, False]                   # latched clip flags
        self._clip_b  = [False, False]
        self._clip_m  = [False, False]

        # Pre-allocated buffers for the callback
        self._buf_a    = np.zeros((blocksize, 2), dtype=np.float32)
        self._buf_b    = np.zeros((blocksize, 2), dtype=np.float32)
        self._buf_out  = np.zeros((blocksize, 2), dtype=np.float32)

        # Debug / profiling (enabled by AudioEngine.enable_debug())
        self._debug      = False
        self._dbg_times: list[float] = []   # callback durations in ms
        self._dbg_print_every = int(sr / blocksize * 5)  # every ~5 s

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stream = sd.OutputStream(
            samplerate = self._sr,
            channels   = 2,
            dtype      = "float32",
            blocksize  = self._blocksize,
            callback   = self._callback,
            latency    = "low",
        )
        self._running = True
        self._dispatch_thread.start()
        self._stream.start()

    def stop(self) -> None:
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def enable_debug(self) -> None:
        """Print callback timing stats every 5 s. Call before engine.start()."""
        self._debug = True

    # ── audio callback ────────────────────────────────────────────────────────

    def _callback(
        self,
        outdata:   np.ndarray,
        frames:    int,
        time_info,
        status,
    ) -> None:
        _t0 = time.perf_counter() if self._debug else 0.0

        # Get mixed audio from each deck
        np.copyto(self._buf_a[:frames], self.deck_a.mix_frame(frames))
        np.copyto(self._buf_b[:frames], self.deck_b.mix_frame(frames))

        # Apply per-deck 3-band EQ
        self._eq_a.process(self._buf_a[:frames])
        self._eq_b.process(self._buf_b[:frames])

        # Spectral analysis on the post-EQ output so the spectrum widget
        # responds correctly to both stem fader changes and EQ band kills.
        self.deck_a._deck_spectral.process(self._buf_a[:frames])
        self.deck_b._deck_spectral.process(self._buf_b[:frames])

        # Peak detection (decay factor ~0.85 per callback ≈ ~14 dB/s at 256 samples/44100)
        _DECAY = 0.85
        for ch in range(2):
            pa = float(np.max(np.abs(self._buf_a[:frames, ch])))
            pb = float(np.max(np.abs(self._buf_b[:frames, ch])))
            self._peaks_a[ch] = max(self._peaks_a[ch] * _DECAY, pa)
            self._peaks_b[ch] = max(self._peaks_b[ch] * _DECAY, pb)
            if pa >= 1.0: self._clip_a[ch] = True
            if pb >= 1.0: self._clip_b[ch] = True

        # Equal-power crossfader
        x      = self._crossfader * (math.pi * 0.5)
        gain_a = math.cos(x)
        gain_b = math.sin(x)

        np.multiply(self._buf_a[:frames], gain_a,  out=self._buf_out[:frames])
        np.add(self._buf_out[:frames],
               self._buf_b[:frames] * gain_b,
               out=self._buf_out[:frames])

        # Master gain
        np.multiply(self._buf_out[:frames], self._master_gain,
                    out=self._buf_out[:frames])

        outdata[:frames] = self._buf_out[:frames]
        if frames < outdata.shape[0]:
            outdata[frames:] = 0.0

        # Master peak
        for ch in range(2):
            pm = float(np.max(np.abs(outdata[:frames, ch])))
            self._peaks_m[ch] = max(self._peaks_m[ch] * _DECAY, pm)
            if pm >= 1.0: self._clip_m[ch] = True

        # Phase correlation between the two decks
        self._phase_corr.update(
            self._buf_a[:frames],
            self._buf_b[:frames],
        )

        if self._debug:
            elapsed_ms = (time.perf_counter() - _t0) * 1000.0
            self._dbg_times.append(elapsed_ms)
            if len(self._dbg_times) >= self._dbg_print_every:
                budget_ms = frames / self._sr * 1000.0
                avg  = sum(self._dbg_times) / len(self._dbg_times)
                peak = max(self._dbg_times)
                over = sum(1 for t in self._dbg_times if t > budget_ms)
                print(
                    f"[audio] avg={avg:.2f}ms  peak={peak:.2f}ms  "
                    f"budget={budget_ms:.1f}ms  overruns={over}/{len(self._dbg_times)}"
                )
                self._dbg_times.clear()

    # ── transport controls (called from transport.py) ─────────────────────────

    def play(self, deck_id: str) -> None:
        self._get_deck(deck_id).playing = True

    def pause(self, deck_id: str) -> None:
        self._get_deck(deck_id).playing = False

    def seek(self, deck_id: str, position_s: float) -> None:
        self._get_deck(deck_id).seek(position_s)

    def set_stem_gain(self, deck_id: str, stem_name: str, gain: float) -> None:
        pb  = self._get_deck(deck_id)
        idx = STEM_NAMES.index(stem_name)
        pb.stem_gains.set_target(idx, gain)

    def set_crossfader(self, value: float) -> None:
        """value: 0.0 (deck A) – 0.5 (center) – 1.0 (deck B)"""
        self._crossfader = max(0.0, min(1.0, value))

    def set_master_gain(self, gain: float) -> None:
        self._master_gain = max(0.0, min(2.0, gain))

    def get_master_gain(self) -> float:
        return self._master_gain

    def load_track(
        self,
        deck_id:  str,
        original: np.ndarray,
        stems:    Optional[StemResult] = None,
    ) -> None:
        self._get_deck(deck_id).load_audio(original, stems)

    def update_stems(self, deck_id: str, stems: StemResult) -> None:
        self._get_deck(deck_id).set_stems(stems)

    # ── state / metrics ───────────────────────────────────────────────────────

    def register_state_callback(
        self,
        cb: Callable[[dict[str, DeckState]], None],
    ) -> None:
        self._state_callbacks.append(cb)

    def publish_state(self, states: dict[str, DeckState]) -> None:
        """Called by Transport when it rebuilds DeckState snapshots."""
        with self._state_lock:
            self._states = dict(states)
        self._dispatch_event.set()

    def get_state(self, deck_id: str) -> DeckState:
        with self._state_lock:
            return self._states[deck_id]

    def get_phase_correlation(self) -> float:
        return self._phase_corr.correlation()

    def get_deck_metrics(self, deck_id: str) -> DeckMetrics | None:
        pb  = self._get_deck(deck_id)
        raw = pb.get_metrics()
        if raw is None:
            return None
        return DeckMetrics(
            lufs       = raw.lufs,
            spectral   = raw.spectral,
            spectrum   = raw.spectrum,
            phase_corr = self.get_phase_correlation(),
        )

    def get_stem_meters(
        self, deck_id: str, stem_name: str,
    ) -> tuple[LoudnessMeasure, SpectralBands]:
        return self._get_deck(deck_id).get_stem_meters(stem_name)

    # ── loop / cue ───────────────────────────────────────────────────────────

    def set_loop(self, deck_id: str, active: bool, start_s: float, end_s: float) -> None:
        self._get_deck(deck_id).set_loop(active, start_s, end_s)

    def set_cue_point(self, deck_id: str, position_s: float) -> None:
        self._get_deck(deck_id).set_cue_point(position_s)

    def get_cue_point(self, deck_id: str) -> float:
        return self._get_deck(deck_id).cue_point_s

    def get_waveform(self, deck_id: str) -> Optional[WaveformData]:
        return self._get_deck(deck_id).waveform

    def get_waveform_seq(self, deck_id: str) -> int:
        return self._get_deck(deck_id).waveform_seq

    # ── EQ ───────────────────────────────────────────────────────────────────

    def set_eq(self, deck_id: str, band: str, gain_db: float) -> None:
        eq = self._eq_a if deck_id in ("A", DeckID.A) else self._eq_b
        eq.set_gain(band, gain_db)

    def get_eq(self, deck_id: str, band: str) -> float:
        eq = self._eq_a if deck_id in ("A", DeckID.A) else self._eq_b
        return eq.get_gain(band)

    # ── peak levels ──────────────────────────────────────────────────────────

    def get_peak_levels(self, deck_id: str) -> tuple[float, float]:
        """Return (left, right) linear peak, 0.0–1.0+. Audio thread writes."""
        peaks = self._peaks_a if deck_id in ("A", DeckID.A) else self._peaks_b
        return float(peaks[0]), float(peaks[1])

    def get_master_peak(self) -> tuple[float, float]:
        return float(self._peaks_m[0]), float(self._peaks_m[1])

    def get_clip_flags(self, deck_id: str) -> tuple[bool, bool]:
        """Latched clip flags. Call reset_clip() to clear."""
        flags = self._clip_a if deck_id in ("A", DeckID.A) else self._clip_b
        return flags[0], flags[1]

    def get_master_clip(self) -> tuple[bool, bool]:
        return self._clip_m[0], self._clip_m[1]

    def reset_clip(self, deck_id: str) -> None:
        flags = self._clip_a if deck_id in ("A", DeckID.A) else self._clip_b
        flags[0] = flags[1] = False

    def reset_master_clip(self) -> None:
        self._clip_m[0] = self._clip_m[1] = False

    # ── internals ─────────────────────────────────────────────────────────────

    def _get_deck(self, deck_id: str) -> DeckPlayback:
        if deck_id in ("A", DeckID.A):
            return self.deck_a
        if deck_id in ("B", DeckID.B):
            return self.deck_b
        raise ValueError(f"Unknown deck: {deck_id!r}")

    def _dispatch_loop(self) -> None:
        while self._running:
            self._dispatch_event.wait(timeout=0.1)
            self._dispatch_event.clear()
            if not self._state_callbacks:
                continue
            with self._state_lock:
                states = dict(self._states)
            for cb in self._state_callbacks:
                try:
                    cb(states)
                except Exception:
                    pass
