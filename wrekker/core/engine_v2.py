"""
AudioEngine v2 — Rust-backed audio engine.

Identical public API to engine.py (v1).  Transport and UI require zero changes.
The audio callback runs entirely in a CPAL/Rust thread — the Python GIL is never
held during audio processing, which eliminates the primary source of xruns.

Differences from v1
-------------------
- No Python GIL acquisition in the hot path (biggest win).
- Stem-gain fading uses a per-sample exponential smoother instead of the
  sin/cos equal-power ramp; audibly equivalent for DJ use.
- Per-stem loudness is real K-weighted BS.1770 (momentary + short-term),
  measured post-gain/FX in the Rust callback. Per-stem spectral bands are
  not measured (SpectralBands stays at −inf).
- Waveform data stays Python-side (unaffected by this change).
"""
from __future__ import annotations

import math
import threading
from typing import Callable, Optional

import numpy as np

from wrekker.core.deck import (
    STEM_NAMES,
    DeckID,
    DeckMetrics,
    DeckState,
    DeckStatus,
    LoudnessMeasure,
    SpectralBands,
    WaveformData,
)
from wrekker.stems.models import StemResult

try:
    from wrekker_engine import NativeEngine as _NativeEngine
    _NATIVE_AVAILABLE = True
except ImportError:
    _NATIVE_AVAILABLE = False

__all__ = ["AudioEngine"]

_NEG_INF    = float("-inf")
_STEM_INDEX = {name: i for i, name in enumerate(STEM_NAMES)}


# ── Deck proxy (replaces DeckPlayback for code that accesses deck_a/deck_b) ──

class _DeckProxy:
    """
    Stand-in for v1 DeckPlayback.  Exposes the attributes that transport.py
    and main_window.py read directly from engine.deck_a / engine.deck_b.
    """

    def __init__(self, deck_id: str, native: "_NativeEngine") -> None:
        self._id     = deck_id
        self._native = native
        # Python-side only (not used by the audio thread)
        self.waveform:     Optional[WaveformData] = None
        self.waveform_seq: int = 0
        self.stems:        Optional[StemResult]   = None
        self._eq: dict[str, float] = {"low": 0.0, "mid": 0.0, "high": 0.0}
        self.channel_gain: float = 1.0
        self.pregain:      float = 1.0
        # pitch_factor: stored for BPM-live display; v2 doesn't apply time-stretch yet
        self.pitch_factor: float = 1.0

    @property
    def position_s(self) -> float:
        return self._native.get_position_s(self._id)

    @property
    def cue_point_s(self) -> float:
        return self._native.get_cue_point_s(self._id)

    def set_stems(self, stems: StemResult) -> None:
        """Called by transport when Demucs finishes."""
        self.stems = stems
        self._push_stems_to_native(stems)

    def _push_stems_to_native(self, stems: StemResult) -> None:
        def to_f32_nf2(arr: np.ndarray) -> np.ndarray:
            """Convert (2, n_samples) or (n_samples, 2) → contiguous (n_samples, 2) f32."""
            if arr.ndim == 2 and arr.shape[0] == 2:
                arr = arr.T        # (n_samples, 2)
            return np.ascontiguousarray(arr.astype(np.float32))

        self._native.load_stems(
            self._id,
            to_f32_nf2(stems[STEM_NAMES[0]]),   # vocals
            to_f32_nf2(stems[STEM_NAMES[1]]),   # drums
            to_f32_nf2(stems[STEM_NAMES[2]]),   # bass
            to_f32_nf2(stems[STEM_NAMES[3]]),   # other
        )


# ── Public AudioEngine facade ─────────────────────────────────────────────────

class AudioEngine:
    """
    Rust-backed audio engine with the same API as v1 AudioEngine.
    Pass ``use_v2=True`` in wrekker/__main__.py to select this engine.
    """

    def __init__(self, sr: int = 44100, blocksize: int = 256) -> None:
        if not _NATIVE_AVAILABLE:
            raise ImportError(
                "wrekker_engine native module not found.\n"
                "Build with:  cd wrekker/engine_rs && maturin develop --release"
            )
        self._sr        = sr
        self._blocksize = blocksize
        self._native    = _NativeEngine(sr, blocksize)

        self.deck_a = _DeckProxy("A", self._native)
        self.deck_b = _DeckProxy("B", self._native)
        self._crossfader: float = 0.5

        self._states: dict[str, DeckState] = {
            "A": DeckState.empty(DeckID.A),
            "B": DeckState.empty(DeckID.B),
        }
        self._state_lock      = threading.Lock()
        self._state_callbacks: list[Callable] = []
        self._dispatch_event  = threading.Event()
        self._running         = False
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="engine-dispatch",
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._native.start()
        self._running = True
        self._dispatch_thread.start()

    def stop(self) -> None:
        self._running = False
        self._native.stop()

    def enable_debug(self) -> None:
        pass  # Rust engine logs to stderr automatically on stream error

    # ── Playback transport ────────────────────────────────────────────────────

    def play(self, deck_id: str) -> None:
        self._native.set_playing(deck_id, True)

    def pause(self, deck_id: str) -> None:
        self._native.set_playing(deck_id, False)

    def seek(self, deck_id: str, position_s: float) -> None:
        self._native.seek(deck_id, float(position_s))

    def set_stem_gain(self, deck_id: str, stem_name: str, gain: float) -> None:
        self._native.set_stem_gain(deck_id, _STEM_INDEX[stem_name], float(gain))

    def set_crossfader(self, value: float) -> None:
        self._crossfader = float(value)
        self._native.set_crossfader(float(value))

    def set_master_gain(self, gain: float) -> None:
        self._native.set_master_gain(float(gain))

    def get_master_gain(self) -> float:
        return float(self._native.get_master_gain())

    # ── Headphone / CUE bus ───────────────────────────────────────────────────

    def set_headphone_cue(self, deck_id: str, enabled: bool) -> None:
        if deck_id in ("A", DeckID.A):
            self._native.set_cue_a(bool(enabled))
        else:
            self._native.set_cue_b(bool(enabled))

    def set_headphone_cue_master(self, enabled: bool) -> None:
        self._native.set_cue_master(bool(enabled))

    def set_headphone_mix(self, value: float) -> None:
        self._native.set_hp_mix(float(value))

    def set_headphone_level(self, value: float) -> None:
        self._native.set_hp_level(float(value))

    # ── Buffer loading ────────────────────────────────────────────────────────

    def load_track(
        self,
        deck_id:  str,
        original: np.ndarray,
        stems:    Optional[StemResult] = None,
    ) -> None:
        proxy = self._proxy(deck_id)

        # Normalise to (n_frames, 2) float32
        if original.ndim == 1:
            original = np.stack([original, original], axis=1)
        elif original.shape[1] == 1:
            original = np.repeat(original, 2, axis=1)
        original = np.ascontiguousarray(original.astype(np.float32))

        if stems is not None:
            proxy.stems = stems
            proxy._push_stems_to_native(stems)
        else:
            self._native.load_original(deck_id, original)

    def update_stems(self, deck_id: str, stems: StemResult) -> None:
        self._proxy(deck_id).set_stems(stems)

    # ── Nudge / pitch-bend (side jog) ────────────────────────────────────────

    def set_nudge(self, deck_id: str, offset: float) -> None:
        """Temporary rate offset for side-jog pitch bend. Rust decays it to zero."""
        self._native.set_nudge(deck_id, float(offset))

    # ── Scratch ──────────────────────────────────────────────────────────────

    def scratch_enable(self, deck_id: str, target_rate: float) -> None:
        """Enable vinyl scratch. target_rate is the rate to ramp back to on release."""
        self._native.scratch_enable(deck_id, float(target_rate))

    def scratch_disable(self, deck_id: str) -> None:
        """Disable scratch — Rust starts exponential release ramp to target_rate."""
        self._native.scratch_disable(deck_id)

    def scratch_tick(self, deck_id: str, delta: int) -> None:
        """Accumulate jog ticks (positive=forward, negative=backward)."""
        self._native.scratch_tick(deck_id, int(delta))

    # ── Playback rate ────────────────────────────────────────────────────────

    def set_playback_rate(self, deck_id: str, rate: float) -> None:
        """Set playback rate: 1.0 = normal, 1.05 = +5% tempo (vinyl-style)."""
        self._native.set_playback_rate(deck_id, float(rate))

    def get_playback_rate(self, deck_id: str) -> float:
        return float(self._native.get_playback_rate(deck_id))

    # ── Live audio (oscilloscope) ─────────────────────────────────────────────

    def get_live_audio(self, deck_id: str) -> list[float]:
        """Returns flat interleaved stereo f32 list of recent audio."""
        return self._native.get_live_audio(deck_id)

    def get_master_live_audio(self) -> list[float]:
        """Returns flat interleaved stereo f32 list of recent master output."""
        return self._native.get_master_live_audio()

    # ── EQ ───────────────────────────────────────────────────────────────────

    def set_eq(self, deck_id: str, band: str, gain_db: float) -> None:
        self._proxy(deck_id)._eq[band] = gain_db
        if band == "low":
            self._native.set_eq_low(deck_id, float(gain_db))
        elif band == "mid":
            self._native.set_eq_mid(deck_id, float(gain_db))
        else:
            self._native.set_eq_high(deck_id, float(gain_db))

    def get_eq(self, deck_id: str, band: str) -> float:
        return self._proxy(deck_id)._eq[band]

    def set_channel_gain(self, deck_id: str, gain: float) -> None:
        self._proxy(deck_id).channel_gain = float(gain)
        self._native.set_channel_gain(deck_id, float(gain))

    def set_pregain(self, deck_id: str, gain: float) -> None:
        self._proxy(deck_id).pregain = float(gain)
        self._native.set_pregain(deck_id, float(gain))

    def set_channel_filter(self, deck_id: str, value: float) -> None:
        self._native.set_channel_filter(deck_id, float(value))

    def get_channel_filter(self, deck_id: str) -> float:
        return float(self._native.get_channel_filter(deck_id))

    def get_channel_gain(self, deck_id: str) -> float:
        return self._proxy(deck_id).channel_gain

    def get_pregain(self, deck_id: str) -> float:
        return self._proxy(deck_id).pregain

    def get_crossfader(self) -> float:
        return self._crossfader

    # ── Loop / cue ────────────────────────────────────────────────────────────

    def set_loop(self, deck_id: str, active: bool, start_s: float, end_s: float) -> None:
        self._native.set_loop(deck_id, active, float(start_s), float(end_s))

    def set_cue_point(self, deck_id: str, position_s: float) -> None:
        self._native.set_cue_point(deck_id, float(position_s))

    def get_cue_point(self, deck_id: str) -> float:
        return self._native.get_cue_point_s(deck_id)

    # ── Metering ─────────────────────────────────────────────────────────────

    def get_phase_correlation(self) -> float:
        return float(self._native.get_phase_correlation())

    def get_master_lufs(self) -> tuple[float, float]:
        """Master-bus K-weighted loudness → (momentary_LUFS, short_term_LUFS)."""
        try:
            m, st = self._native.get_master_lufs()
            return float(m), float(st)
        except AttributeError:
            return _NEG_INF, _NEG_INF

    def get_deck_metrics(self, deck_id: str) -> Optional[DeckMetrics]:
        m  = float(self._native.get_lufs_momentary(deck_id))
        st = float(self._native.get_lufs_shortterm(deck_id))
        if math.isinf(m) and math.isinf(st):
            return None

        pl, pr = self._native.get_peak_levels(deck_id)
        peak = max(float(pl), float(pr))
        lufs = LoudnessMeasure(
            momentary_lufs  = m,
            short_term_lufs = st,
            true_peak_dbfs  = 20.0 * math.log10(peak) if peak > 1e-6 else _NEG_INF,
        )
        spec_raw = tuple(float(x) for x in self._native.get_spectrum(deck_id))
        # Map 16-band raw to SpectralBands (sub/bass/mids/highs)
        spectral = SpectralBands(
            sub   = spec_raw[0],
            bass  = spec_raw[2],
            mids  = spec_raw[7],
            highs = spec_raw[14],
        )
        return DeckMetrics(
            lufs       = lufs,
            spectral   = spectral,
            spectrum   = spec_raw,
            phase_corr = self.get_phase_correlation(),
        )

    def get_spectrum(self, deck_id: str) -> tuple[float, ...]:
        """Raw 16-band deck spectrum in dBFS."""
        return tuple(float(x) for x in self._native.get_spectrum(deck_id))

    def get_stem_meters(self, deck_id: str, stem_name: str) -> LoudnessMeasure:
        """Per-stem loudness. K-weighted BS.1770 momentary/short-term from the
        Rust callback (post-gain/FX), plus the decayed sample peak in dBFS.

        Per-stem spectral bands were never measured or displayed; that half of
        the old (LoudnessMeasure, SpectralBands) return has been removed."""
        idx  = _STEM_INDEX.get(stem_name, 0)
        peak = float(self._native.get_stem_peak(deck_id, idx))
        tp_dbfs = 20.0 * math.log10(peak) if peak > 1e-5 else _NEG_INF
        try:
            m_lufs, st_lufs = self._native.get_stem_lufs(deck_id, idx)
            m_lufs, st_lufs = float(m_lufs), float(st_lufs)
        except AttributeError:
            # Older native engine build — fall back to the peak approximation.
            m_lufs = tp_dbfs - 3.0 if peak > 1e-5 else _NEG_INF
            st_lufs = m_lufs
        return LoudnessMeasure(
            momentary_lufs  = m_lufs,
            short_term_lufs = st_lufs,
            true_peak_dbfs  = tp_dbfs,
        )

    def get_stem_peak(self, deck_id: str, stem_name: str) -> float:
        """Per-stem peak level (0.0–2.0 linear)."""
        idx = _STEM_INDEX.get(stem_name, 0)
        return float(self._native.get_stem_peak(deck_id, idx))

    def get_peak_levels(self, deck_id: str) -> tuple[float, float]:
        l, r = self._native.get_peak_levels(deck_id)
        return float(l), float(r)

    def get_master_peak(self) -> tuple[float, float]:
        l, r = self._native.get_master_peak()
        return float(l), float(r)

    def get_clip_flags(self, deck_id: str) -> tuple[bool, bool]:
        return self._native.get_clip_flags(deck_id)

    def get_master_clip(self) -> tuple[bool, bool]:
        return self._native.get_master_clip()

    def reset_clip(self, deck_id: str) -> None:
        self._native.reset_clip(deck_id)

    def reset_master_clip(self) -> None:
        self._native.reset_master_clip()

    # ── FX ───────────────────────────────────────────────────────────────────

    def fx_set_enabled(self, v: bool)   -> None: self._native.fx_set_enabled(v)
    def fx_get_enabled(self)            -> bool:  return bool(self._native.fx_get_enabled())
    def fx_set_type(self, v: int)       -> None: self._native.fx_set_type(int(v))
    def fx_get_type(self)               -> int:   return int(self._native.fx_get_type())
    def fx_set_target(self, v: int)     -> None: self._native.fx_set_target(int(v))
    def fx_get_target(self)             -> int:   return int(self._native.fx_get_target())
    def fx_set_wet(self, v: float)      -> None: self._native.fx_set_wet(float(v))
    def fx_get_wet(self)                -> float: return float(self._native.fx_get_wet())
    def fx_set_depth(self, v: float)    -> None: self._native.fx_set_depth(float(v))
    def fx_get_depth(self)              -> float: return float(self._native.fx_get_depth())
    def fx_set_feedback(self, v: float) -> None: self._native.fx_set_feedback(float(v))
    def fx_get_feedback(self)           -> float: return float(self._native.fx_get_feedback())
    def fx_set_time_division(self, v: float) -> None: self._native.fx_set_time_division(float(v))
    def fx_get_time_division(self)           -> float: return float(self._native.fx_get_time_division())
    def fx_set_color(self, v: float)    -> None: self._native.fx_set_color(float(v))
    def fx_get_color(self)              -> float: return float(self._native.fx_get_color())
    def fx_set_bpm(self, v: float)      -> None: self._native.fx_set_bpm(float(v))

    # ── WREKK FX ─────────────────────────────────────────────────────────────

    def wrekk_fx_set_enabled(self, v: bool)   -> None: self._native.wrekk_fx_set_enabled(bool(v))
    def wrekk_fx_get_enabled(self)            -> bool:  return bool(self._native.wrekk_fx_get_enabled())
    def wrekk_fx_set_type(self, v: int)       -> None: self._native.wrekk_fx_set_type(int(v))
    def wrekk_fx_get_type(self)               -> int:   return int(self._native.wrekk_fx_get_type())
    def wrekk_fx_set_target(self, v: int)     -> None: self._native.wrekk_fx_set_target(int(v))
    def wrekk_fx_get_target(self)             -> int:   return int(self._native.wrekk_fx_get_target())
    def wrekk_fx_set_stem_target(self, v: int) -> None: self._native.wrekk_fx_set_stem_target(int(v))
    def wrekk_fx_get_stem_target(self)         -> int:   return int(self._native.wrekk_fx_get_stem_target())
    def wrekk_fx_set_wet(self, v: float)      -> None: self._native.wrekk_fx_set_wet(float(v))
    def wrekk_fx_get_wet(self)                -> float: return float(self._native.wrekk_fx_get_wet())
    def wrekk_fx_set_depth(self, v: float)    -> None: self._native.wrekk_fx_set_depth(float(v))
    def wrekk_fx_get_depth(self)              -> float: return float(self._native.wrekk_fx_get_depth())
    def wrekk_fx_set_feedback(self, v: float) -> None: self._native.wrekk_fx_set_feedback(float(v))
    def wrekk_fx_get_feedback(self)           -> float: return float(self._native.wrekk_fx_get_feedback())
    def wrekk_fx_set_time_division(self, v: float) -> None: self._native.wrekk_fx_set_time_division(float(v))
    def wrekk_fx_get_time_division(self)           -> float: return float(self._native.wrekk_fx_get_time_division())
    def wrekk_fx_set_color(self, v: float)    -> None: self._native.wrekk_fx_set_color(float(v))
    def wrekk_fx_get_color(self)              -> float: return float(self._native.wrekk_fx_get_color())
    def wrekk_fx_set_bpm(self, v: float)      -> None: self._native.wrekk_fx_set_bpm(float(v))

    # ── Waveform (Python-side, not touched by audio thread) ───────────────────

    def get_waveform(self, deck_id: str) -> Optional[WaveformData]:
        return self._proxy(deck_id).waveform

    def get_waveform_seq(self, deck_id: str) -> int:
        return self._proxy(deck_id).waveform_seq

    # ── State publishing (called by Transport) ────────────────────────────────

    def register_state_callback(self, cb: Callable) -> None:
        self._state_callbacks.append(cb)

    def publish_state(self, states: dict[str, DeckState]) -> None:
        with self._state_lock:
            self._states = dict(states)
        self._dispatch_event.set()

    def get_state(self, deck_id: str) -> DeckState:
        with self._state_lock:
            return self._states[deck_id]

    # ── Internals ─────────────────────────────────────────────────────────────

    def _proxy(self, deck_id: str) -> _DeckProxy:
        return self.deck_a if deck_id in ("A", DeckID.A) else self.deck_b

    # transport.py accesses this directly in many places
    def _get_deck(self, deck_id: str) -> "_DeckProxy":
        return self._proxy(deck_id)

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
