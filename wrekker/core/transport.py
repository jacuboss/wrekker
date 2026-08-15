"""
Transport — public control API for Wrekker decks.

All DJ and sound-engineer operations go through Transport:
  - load_track()
  - play() / pause() / cue() / seek()
  - set_stem_gain() / mute_stem() / solo_stem()
  - set_pitch()
  - loop_in() / loop_out() / loop_toggle()
  - add_cue() / jump_to_cue()
  - set_crossfader() / set_master_gain()

Transport keeps the authoritative DeckState snapshots and pushes them
to AudioEngine for dispatch to registered listeners (UI, hardware, etc.).

Thread safety: all public methods acquire a deck-level lock.
"""

from __future__ import annotations

import hashlib
import bisect
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

import math

import numpy as np

from wrekker.audio import load_audio
from wrekker.core.deck import (
    MARKER_MIN_CONFIDENCE,
    STEM_NAMES,
    STEM_GAIN_MAX,
    AutoMarker,
    BeatGrid,
    CuePoint,
    DeckID,
    DeckMetrics,
    DeckState,
    DeckStatus,
    HarmonicKey,
    LoopState,
    LoudnessMeasure,
    MarkerType,
    PhraseMark,
    SpectralBands,
    StemState,
    TrackInfo,
    fill_beat_gaps,
)
from wrekker.core.engine_v2 import AudioEngine
from wrekker.stems.analyzer import StemAnalyzer
from wrekker.stems.models import Priority, StemStatus
from wrekker.sync import PhraseLockSync

try:
    from wrekker_engine import NativePhaseSync as _NativePhaseSync
except ImportError:  # pragma: no cover - native extension is optional in tests
    _NativePhaseSync = None

__all__ = [
    "Transport", "FXState", "MonitorCueState",
    "FX_BANK_NORMAL", "FX_BANK_WREKK", "FX_NAMES", "WREKK_FX_NAMES", "WREKK_STEM_TARGETS",
    "FX_TARGET_A", "FX_TARGET_B", "FX_TARGET_BOTH",
]

# ── FX constants ──────────────────────────────────────────────────────────────

FX_TARGET_A    = 0
FX_TARGET_B    = 1
FX_TARGET_BOTH = 2
FX_BANK_NORMAL = "normal"
FX_BANK_WREKK  = "wrekk"

FX_NAMES = [
    "Filter", "Echo", "Delay", "Reverb", "Flanger",
    "Phaser", "Bitcrusher", "Roll", "Trans", "Noise",
]
WREKK_FX_NAMES = [
    "VOCAL GHOST", "TOP WASH", "DRUM CRUSH", "RHYTHM GATE",
    "STEM ROLL", "BASS LOCK", "DECONSTRUCT", "REBUILD",
]
WREKK_STEM_TARGETS = [
    ("VOC", 0), ("DRM", 1), ("BSS", 2), ("OTH", 3), ("TOP", 4), ("RHYTHM", 5),
]

# Beat-division presets for echo/delay/roll/trans
FX_TIME_DIVISIONS = [
    ("1/16", 0.0625), ("1/8", 0.125), ("1/4", 0.25),
    ("1/2", 0.5),     ("1",   1.0),   ("2",   2.0), ("4", 4.0),
]


from dataclasses import dataclass as _dc, replace as _dc_replace

@_dc(frozen=True)
class FXState:
    enabled:       bool
    fx_type:       int    # index into FX_NAMES
    target:        int    # FX_TARGET_A / B / BOTH
    wet:           float
    depth:         float
    feedback:      float
    time_division: float
    color:         float
    fx_bank:       str = FX_BANK_NORMAL
    wrekk_enabled: bool = False
    wrekk_fx_type: int = 0
    wrekk_target:  int = FX_TARGET_A
    wrekk_stem_target: int = 1
    wrekk_wet:     float = 0.8
    wrekk_depth:   float = 0.5
    wrekk_feedback: float = 0.5
    wrekk_time_division: float = 0.5
    wrekk_color:   float = 0.0
    wrekk_stems_ready: bool = True
    wrekk_stems_status: str = ""

    @property
    def fx_name(self) -> str:
        return FX_NAMES[self.fx_type] if 0 <= self.fx_type < len(FX_NAMES) else "?"

    @property
    def target_label(self) -> str:
        return ("A", "B", "Both")[self.target] if 0 <= self.target <= 2 else "?"

    @property
    def active_enabled(self) -> bool:
        return self.wrekk_enabled if self.fx_bank == FX_BANK_WREKK else self.enabled


@_dc(frozen=True)
class MonitorCueState:
    """Headphone / PFL (Pre-Fader Listen) state.

    headphone_mix: 0.0 = full CUE signal, 1.0 = full master mix.
    headphone_level: output gain 0.0–2.0 (1.0 = unity).
    cue_master: when True, forces headphones to output the master mix
                (overrides deck CUE and the hp_mix knob position).
    """
    cue_deck_a:      bool  = False
    cue_deck_b:      bool  = False
    cue_master:      bool  = False
    headphone_mix:   float = 0.0
    headphone_level: float = 1.0


# Sentinel: used in _set_state to distinguish "don't change" from explicit None.
_UNSET = object()


# ── Sync helpers ──────────────────────────────────────────────────────────────

class _SyncPLL:
    """
    Thin controller facade for beat-phase drift between a synced follower and
    its master. Correction math runs in Rust when the native engine is loaded.

    Output is a fractional rate correction: +0.02 means run 2% faster.
    Clamped to ±MAX_CORRECTION to avoid audible pitch jumps.
    """
    Kp             = 0.180  # proportional gain; update() uses measured dt
    Ki             = 0.0120 # integral gain; corrects steady drift between grids
    MAX_CORRECTION = 0.060  # ±6 % temporary correction while chasing phase
    WINDUP_LIMIT   = 1.0   # anti-windup clamp on integral (beat·seconds)

    def __init__(self) -> None:
        self.integral = 0.0
        self._native = _NativePhaseSync(0.35, 0.02, 2.0) if _NativePhaseSync else None

    def update(
        self,
        phase_error_beats: float,
        dt: float,
        master_bpm: float = 120.0,
        slave_bpm: float = 120.0,
    ) -> float:
        if self._native is not None:
            # Python transport computes master_phase - follower_phase. The
            # Rust PLL uses slave_phase - master_phase so positive means
            # "slave is ahead, slow it down".
            ratio = self._native.update_phase_error(
                -phase_error_beats,
                master_bpm,
                slave_bpm,
                max(0.0, dt),
            )
            return max(-self.MAX_CORRECTION, min(self.MAX_CORRECTION, ratio - 1.0))

        self.integral += phase_error_beats * dt
        self.integral  = max(-self.WINDUP_LIMIT,
                             min(self.WINDUP_LIMIT, self.integral))
        correction = self.Kp * phase_error_beats + self.Ki * self.integral
        return max(-self.MAX_CORRECTION, min(self.MAX_CORRECTION, correction))

    def reset(self) -> None:
        self.integral = 0.0
        if self._native is not None:
            self._native.reset()


class _FollowerSync:
    """Mutable sync state for one follower deck (stored in Transport)."""

    def __init__(self, master_id: str, base_rate: float) -> None:
        self.master_id = master_id
        self.nominal_rate = base_rate
        self.rate_bias = 0.0
        self.base_rate = base_rate   # nominal rate from BPM match; updated live
        self.pll       = _SyncPLL()
        self.applied_rate = base_rate

    def set_nominal_rate(self, rate: float) -> None:
        prev_base = self.base_rate
        self.nominal_rate = rate
        self._refresh_base_rate()
        if prev_base > 1e-6:
            scale = self.base_rate / prev_base
            self.applied_rate = max(0.5, min(2.0, self.applied_rate * scale))

    def absorb_correction(self, correction: float, dt: float) -> None:
        # If the PLL needs a persistent offset, fold it slowly into the
        # nominal rate so analyzed-BPM error does not become long-term drift.
        self.rate_bias += correction * dt * 0.06
        self.rate_bias = max(-0.04, min(0.04, self.rate_bias))
        self._refresh_base_rate()

    def _refresh_base_rate(self) -> None:
        self.base_rate = max(0.5, min(2.0, self.nominal_rate * (1.0 + self.rate_bias)))


def _parse_auto_markers(markers_data: list) -> tuple:
    """Convert list[dict] from WrkMetadata.markers → tuple[AutoMarker, ...]."""
    result = []
    for d in markers_data or []:
        try:
            marker = AutoMarker.from_dict(d)
            if marker.confidence >= MARKER_MIN_CONFIDENCE:
                result.append(marker)
        except Exception:
            pass
    return tuple(result)


def _native_bpm(state: DeckState) -> float:
    """Track BPM before playback-rate changes; prefer analyzed beatgrid."""
    if state.beatgrid and state.beatgrid.bpm > 0:
        return state.beatgrid.bpm
    if state.track and state.track.bpm:
        return state.track.bpm
    return 0.0


def _live_bpm(state: DeckState) -> float:
    """Current audible BPM from state, falling back to native BPM."""
    return state.bpm_live or _native_bpm(state)


def _native_bpm_at(state: DeckState, pos_s: float) -> float:
    """Native track tempo at a position, using beatgrid local tempo when present."""
    if state.beatgrid:
        bpm = state.beatgrid.local_bpm_at(pos_s)
        if bpm > 0:
            return bpm
    return _native_bpm(state)


def _sync_bpm_base(state: DeckState) -> float:
    """Stable BPM for sync rate matching. Avoid per-beat jitter here."""
    return _native_bpm(state)


def _sync_phase_at(state: DeckState, pos_s: float) -> float:
    """Stable sync phase independent of audio output level/stem isolation."""
    bg = state.beatgrid
    bpm = _sync_bpm_base(state)
    if not bg or bpm <= 0:
        return 0.0
    return bg.phase_at(pos_s)


def _sync_snap_to_phase(state: DeckState, target_phase: float, near_pos_s: float) -> float:
    """Snap using the stable sync grid, not noisy per-beat intervals."""
    bg = state.beatgrid
    bpm = _sync_bpm_base(state)
    if not bg or bpm <= 0:
        return max(0.0, near_pos_s)
    return bg.snap_to_phase(target_phase, near_pos_s)


def _resample_audio(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Resample (samples, channels) audio to the engine sample rate."""
    if src_sr == dst_sr:
        return np.ascontiguousarray(audio.astype(np.float32))
    try:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(src_sr, dst_sr)
        out = resample_poly(audio, dst_sr // g, src_sr // g, axis=0)
        return np.ascontiguousarray(out.astype(np.float32))
    except Exception:
        old_n = audio.shape[0]
        new_n = max(1, int(round(old_n * dst_sr / src_sr)))
        x_old = np.linspace(0.0, 1.0, old_n, endpoint=False)
        x_new = np.linspace(0.0, 1.0, new_n, endpoint=False)
        cols = [
            np.interp(x_new, x_old, audio[:, ch]).astype(np.float32)
            for ch in range(audio.shape[1])
        ]
        return np.ascontiguousarray(np.stack(cols, axis=1))


def _resample_stem_result(result: "StemResult", dst_sr: int) -> "StemResult":
    """Resample stem arrays to the engine sample rate using duration metadata."""
    from wrekker.stems.models import StemResult

    first = result[STEM_NAMES[0]]
    if result.duration_s <= 0 or first.shape[-1] <= 0:
        return result
    src_sr = max(1, int(round(first.shape[-1] / result.duration_s)))
    if src_sr == dst_sr:
        return result

    def _stem(name: str) -> np.ndarray:
        arr = result[name]
        if arr.ndim == 2 and arr.shape[0] <= 8:
            samples_ch = arr.T
            out = _resample_audio(samples_ch, src_sr, dst_sr).T
        else:
            out = _resample_audio(arr, src_sr, dst_sr)
        return np.ascontiguousarray(out.astype(np.float32))

    return StemResult(
        vocals=_stem("vocals"),
        drums=_stem("drums"),
        bass=_stem("bass"),
        other=_stem("other"),
        model=result.model,
        duration_s=result.duration_s,
    )


def _parse_harmonic_key(key_str: "str | None") -> "HarmonicKey | None":
    """Parse a Camelot-notation key string ('8A', '3B') into a HarmonicKey."""
    if not key_str:
        return None
    try:
        num  = int(key_str[:-1]) if key_str[:-1].isdigit() else None
        mode = key_str[-1] if key_str[-1] in ("A", "B") else None
        if num and mode:
            return HarmonicKey(number=num, mode=mode)
    except Exception:
        pass
    return None


def _make_beatgrid(bg: "dict | None") -> "BeatGrid | None":
    """Build a BeatGrid from a raw beatgrid dict (as stored in .wrk)."""
    if not bg:
        return None
    phrase_marks = tuple(
        PhraseMark(
            position_sec=float(p.get("position_sec", 0.0)),
            phrase_length=int(p.get("phrase_length", 8)),
            energy_level=float(p.get("energy_level", 0.5)),
        )
        for p in bg.get("phrase_markers", ())
        if isinstance(p, dict)
    )
    schema_version = int(bg.get("schema_version", bg.get("beatgrid_version", 1)) or 1)
    bpm = bg.get("bpm")
    if not bpm or bpm <= 0:
        return None
    return BeatGrid(
        bpm           = bpm,
        first_beat_s  = bg.get("first_beat_s", 0.0),
        confidence    = bg.get("confidence", 0.5),
        source        = bg.get("source", "analyzed"),
        beats         = tuple(bg.get("beats", ())),
        downbeats     = tuple(bg.get("downbeats", ())),
        phrase_markers = phrase_marks,
        swing_factor  = float(bg.get("swing_factor", 0.0) or 0.0),
        beat_period_ms = float(bg.get("beat_period_ms", 0.0) or 0.0),
        schema_version = schema_version,
        analysis_model = bg.get("analysis_model", bg.get("model", "")),
        low_confidence = bool(bg.get("low_confidence", False)),
        dynamic_tempo = bool(bg.get("dynamic_tempo", False)),
        bpm_min       = bg.get("bpm_min"),
        bpm_max       = bg.get("bpm_max"),
    )


def _display_beats(beatgrid: "BeatGrid | None", raw: "dict | None") -> tuple[float, ...]:
    """Beat positions for the waveform overlay.

    Uses the same gap-bridged grid the sync engine runs on, so what the DJ
    sees through a solo or breakdown is what the deck is actually locked to.
    """
    if beatgrid is not None:
        return beatgrid.grid_beats
    return tuple(raw.get("beats") or ()) if raw else ()


def _track_hash(path: Path) -> str:
    stat    = path.stat()
    payload = f"{path.absolute()}:{stat.st_mtime_ns}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _compute_stem_energy(stems: "StemResult", n_points: int = 2000) -> np.ndarray:
    """
    Per-column mean absolute amplitude for each stem.
    stems[name]: (channels, total_samples) float32
    Returns (n_points, 4) float32 — columns: vocals, drums, bass, other.
    """
    first = stems[STEM_NAMES[0]]
    n_total = first.shape[-1]
    chunk = max(1, n_total // n_points)
    pts   = min(n_points, n_total // chunk)

    energy = np.zeros((n_points, len(STEM_NAMES)), dtype=np.float32)
    for col, name in enumerate(STEM_NAMES):
        mono = np.mean(np.abs(stems[name]), axis=0).astype(np.float32)  # (n_total,)
        trimmed = mono[:pts * chunk].reshape(pts, chunk)
        energy[:pts, col] = trimmed.mean(axis=1)
    return energy


_ZOOM_CHUNK = 256   # samples per zoom waveform column (~172 cols/sec at 44100 Hz)


def _spectral_colors(
    mono_raw: np.ndarray,
    n_pts: int,
    chunk: int,
    sr: int,
) -> np.ndarray:
    """Batch-FFT spectral RGB tint for n_pts waveform columns. Returns (n_pts, 3) uint8."""
    fft_n  = 512
    freqs  = np.fft.rfftfreq(fft_n, 1.0 / sr)
    b_mask = freqs <  300.0
    m_mask = (freqs >= 300.0) & (freqs < 3000.0)
    h_mask = freqs >= 3000.0

    chunks_raw = mono_raw[:n_pts * chunk].reshape(n_pts, chunk)
    if chunk >= fft_n:
        frames = chunks_raw[:, :fft_n].copy()
    else:
        frames = np.zeros((n_pts, fft_n), dtype=np.float32)
        frames[:, :chunk] = chunks_raw

    window = np.hanning(fft_n).astype(np.float32)
    spec   = np.abs(np.fft.rfft(frames * window, axis=1))

    b_e = spec[:, b_mask].mean(axis=1)
    m_e = spec[:, m_mask].mean(axis=1)
    h_e = spec[:, h_mask].mean(axis=1)
    tot = b_e + m_e + h_e + 1e-8
    rb, rm, rh = b_e / tot, m_e / tot, h_e / tot

    # bass → amber (#ffb347), mid → green (#2ecc71), high → violet (#7b68ee)
    cf = np.stack([
        rb * 255 + rm *  46 + rh * 123,
        rb * 179 + rm * 204 + rh * 104,
        rb *  71 + rm * 113 + rh * 238,
    ], axis=1)
    np.clip(cf, 0, 255, out=cf)
    return cf.astype(np.uint8)


def _compute_zoom_peaks(
    audio: np.ndarray,
    sr: int = 44100,
) -> "tuple[np.ndarray | None, np.ndarray | None]":
    """High-resolution peaks + colors at _ZOOM_CHUNK samples/col for the zoom waveform."""
    mono_abs = np.max(np.abs(audio), axis=1).astype(np.float32) if audio.ndim > 1 \
               else np.abs(audio).astype(np.float32)
    mono_raw = np.mean(audio, axis=1).astype(np.float32) if audio.ndim > 1 \
               else audio.astype(np.float32)
    n = len(mono_abs)
    if n < _ZOOM_CHUNK:
        return None, None

    zoom_n   = n // _ZOOM_CHUNK
    trimmed  = mono_abs[:zoom_n * _ZOOM_CHUNK].reshape(zoom_n, _ZOOM_CHUNK)
    z_peaks  = trimmed.max(axis=1).astype(np.float32)
    z_colors = _spectral_colors(mono_raw, zoom_n, _ZOOM_CHUNK, sr)
    return z_peaks, z_colors


def _compute_waveform(audio: np.ndarray, sr: int = 44100, n_points: int = 2000) -> "WaveformData":
    """
    Build WaveformData for UI display. Called in the load thread.

    peaks:       amplitude envelope 0-1
    colors:      per-column spectral tint (bass=warm/amber, mid=green, high=blue)
    zoom_peaks:  high-res peaks at _ZOOM_CHUNK samples/col for the zoom waveform
    zoom_colors: high-res spectral colors for the zoom waveform
    beats:       empty tuple; filled later by the analysis worker.
    """
    from wrekker.core.deck import WaveformData  # local import avoids cycle at top level

    mono_abs = np.max(np.abs(audio), axis=1).astype(np.float32) if audio.ndim > 1 \
               else np.abs(audio).astype(np.float32)
    mono_raw = np.mean(audio, axis=1).astype(np.float32) if audio.ndim > 1 \
               else audio.astype(np.float32)
    n = len(mono_abs)
    if n == 0:
        empty = np.zeros(n_points, dtype=np.float32)
        return WaveformData(peaks=empty, colors=np.zeros((n_points, 3), dtype=np.uint8))

    chunk = max(1, n // n_points)
    pts   = min(n_points, n // chunk)

    # ── amplitude peaks ───────────────────────────────────────────────────────
    trimmed = mono_abs[:pts * chunk].reshape(pts, chunk)
    peaks   = trimmed.max(axis=1).astype(np.float32)
    if len(peaks) < n_points:
        peaks = np.pad(peaks, (0, n_points - len(peaks)))

    # ── spectral colors ───────────────────────────────────────────────────────
    cf = _spectral_colors(mono_raw, pts, chunk, sr)
    colors = np.zeros((n_points, 3), dtype=np.uint8)
    colors[:pts] = cf

    # ── high-res zoom data ────────────────────────────────────────────────────
    z_peaks, z_colors = _compute_zoom_peaks(audio, sr)

    return WaveformData(
        peaks=peaks, colors=colors,
        zoom_peaks=z_peaks, zoom_colors=z_colors, zoom_chunk=_ZOOM_CHUNK,
    )


# Krumhansl-Schmuckler key profiles
_KS_MAJOR = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
_KS_MINOR = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
# Chroma index 0=C, 1=C#, ..., 11=B (matches librosa convention)
_MAJOR_NAMES = ["C","C#","D","Eb","E","F","F#","G","Ab","A","Bb","B"]
_MINOR_NAMES = ["Cm","C#m","Dm","Ebm","Em","Fm","F#m","Gm","G#m","Am","Bbm","Bm"]


def _read_track_meta(path: Path) -> dict:
    """
    Read artist, title, bpm, and cover artwork from an audio file using mutagen.
    Returns a plain dict; never raises.
    """
    meta: dict = {"artist": "", "title": "", "bpm": None, "artwork": None}
    try:
        from mutagen import File as MutagenFile
        fe = MutagenFile(str(path), easy=True)
        if fe is not None and fe.tags:
            def _etag(key: str) -> str:
                val = fe.tags.get(key)
                if val is None:
                    return ""
                return str(val[0]) if isinstance(val, list) else str(val)

            t = _etag("title")
            meta["title"] = t if t else path.stem

            for field in ("artist", "albumartist", "album_artist", "performer", "composer"):
                v = _etag(field)
                if v:
                    meta["artist"] = v
                    break

            bpm_raw = _etag("bpm")
            if bpm_raw:
                try:
                    meta["bpm"] = float(bpm_raw)
                except ValueError:
                    pass

        # Artwork — needs non-easy API for full tag access
        f_full = MutagenFile(str(path), easy=False)
        if f_full is not None:
            meta["artwork"] = _extract_artwork(f_full)

    except Exception:
        pass
    return meta


def _extract_artwork(f) -> "bytes | None":
    """Extract cover art bytes from a mutagen File object."""
    try:
        tags = f.tags
        if tags is None:
            return None

        # ID3 (MP3, AIFF, etc.): look for APIC frames
        for key in list(tags.keys()):
            if key.startswith("APIC"):
                frame = tags[key]
                if hasattr(frame, "data"):
                    return frame.data

        # FLAC / OGG with embedded pictures
        if hasattr(f, "pictures") and f.pictures:
            return f.pictures[0].data

        # MP4 / M4A / AAC
        covr = tags.get("covr")
        if covr:
            return bytes(covr[0])

    except Exception:
        pass
    return None


def _detect_bpm_beats(
    audio: np.ndarray,
    sr: int,
    metadata_bpm: "float | None" = None,
) -> tuple["float | None", tuple[float, ...]]:
    """
    Robust BPM detection via librosa with half/double-time correction.

    Analyzes up to 3 sections of the track and takes the median estimate.
    Uses ``metadata_bpm`` as a hint to resolve half/double-time ambiguity.
    Returns (corrected_bpm_or_None, beat_positions_tuple).
    """
    try:
        import librosa

        n_total = audio.shape[0] if audio.ndim > 1 else len(audio)
        duration_s = n_total / sr
        window = min(sr * 60, n_total)

        def _mono(arr: np.ndarray) -> np.ndarray:
            return arr.mean(axis=1).astype(np.float32) if arr.ndim > 1 else arr.astype(np.float32)

        # Choose analysis windows: start, mid (if long enough), end (if very long)
        starts = [0]
        if duration_s > 120:
            starts.append(max(0, int(duration_s / 2 * sr - window // 2)))
        if duration_s > 240:
            starts.append(max(0, n_total - window))

        raw_candidates: list[float] = []
        best_beats: tuple[float, ...] = ()

        for start in starts:
            end = min(start + window, n_total)
            seg = _mono(audio[start:end])
            try:
                tempo, beat_frames = librosa.beat.beat_track(y=seg, sr=sr)
                t = float(np.atleast_1d(tempo)[0])
                if 40.0 < t < 260.0:
                    raw_candidates.append(t)
                if not best_beats and len(beat_frames) > 4:
                    best_beats = tuple(
                        float(b) + start / sr
                        for b in librosa.frames_to_time(beat_frames, sr=sr)
                    )
            except Exception:
                continue

        if not raw_candidates:
            return metadata_bpm, ()

        raw_bpm = float(np.median(raw_candidates))

        # ── Half/double-time correction ───────────────────────────────────
        # Target range for DJ music (electronic, hip-hop, etc.): 80–185 BPM
        DJ_MIN, DJ_MAX = 80.0, 185.0

        def _to_dj_range(bpm: float) -> float:
            for _ in range(4):
                if bpm < DJ_MIN:
                    bpm *= 2.0
                elif bpm > DJ_MAX:
                    bpm /= 2.0
                else:
                    break
            return bpm

        corrected = _to_dj_range(raw_bpm)

        # ── Metadata hint: resolve ambiguity between raw and corrected ────
        if metadata_bpm and 40.0 < metadata_bpm < 260.0:
            meta_c = _to_dj_range(metadata_bpm)
            ratio  = corrected / meta_c if meta_c > 0 else 1.0
            # Accept if within 5% or off by an exact power of 2
            if 0.95 <= ratio <= 1.05:
                corrected = meta_c
            elif 0.48 <= ratio <= 0.52 or 1.90 <= ratio <= 2.10:
                corrected = meta_c

        # Build a full-track beatgrid anchored to the chosen tempo. The windowed
        # passes above choose a stable BPM; this pass gives sync enough anchors
        # to avoid phase drift later in the track.
        try:
            mono_full = _mono(audio)
            _tempo, beat_frames = librosa.beat.beat_track(
                y=mono_full,
                sr=sr,
                start_bpm=corrected,
                trim=False,
            )
            if len(beat_frames) > 4:
                best_beats = tuple(
                    float(b) for b in librosa.frames_to_time(beat_frames, sr=sr)
                )
        except Exception:
            pass

        return corrected, best_beats

    except Exception:
        return metadata_bpm, ()


def _detect_key(audio: np.ndarray, sr: int) -> "HarmonicKey | None":
    """Krumhansl-Schmuckler key estimate from chroma, first 60s."""
    try:
        import librosa
        mono = audio[:sr * 60].mean(axis=1).astype(np.float32) if audio.ndim > 1 \
               else audio[:sr * 60].astype(np.float32)
        chroma = librosa.feature.chroma_cqt(y=mono, sr=sr)
        mean_c = chroma.mean(axis=1)
        mean_c /= (mean_c.max() + 1e-8)
        best_corr, best_name = -np.inf, None
        for root in range(12):
            shifted = np.roll(mean_c, -root)
            r_maj = float(np.corrcoef(shifted, _KS_MAJOR)[0, 1])
            r_min = float(np.corrcoef(shifted, _KS_MINOR)[0, 1])
            if r_maj > best_corr:
                best_corr = r_maj
                best_name = _MAJOR_NAMES[root]
            if r_min > best_corr:
                best_corr = r_min
                best_name = _MINOR_NAMES[root]
        return HarmonicKey.from_key_name(best_name) if best_name else None
    except Exception:
        return None


class _DeckLock:
    """Per-deck mutable state protected by a lock."""

    def __init__(self, deck_id: DeckID) -> None:
        self.id     = deck_id
        self.lock   = threading.Lock()
        self.state  = DeckState.empty(deck_id)
        self.active_job_id: str | None = None   # stem analysis job


class Transport:
    """
    DJ + sound engineer control surface for the Wrekker engine.

    Instantiate once; pass the same AudioEngine and StemAnalyzer to both.
    """

    def __init__(
        self,
        engine:      AudioEngine,
        analyzer:    StemAnalyzer,
        prepared_db=None,   # PreparedDB | None
    ) -> None:
        self._engine      = engine
        self._analyzer    = analyzer
        self._prepared_db = prepared_db
        self._decks    = {
            "A": _DeckLock(DeckID.A),
            "B": _DeckLock(DeckID.B),
        }
        # Deck ID of the current sync master ("A", "B", or None)
        self._sync_master: Optional[str] = None
        # Per-follower sync state (None = not synced)
        self._follower_sync: dict[str, Optional[_FollowerSync]] = {"A": None, "B": None}
        # FX state (Python-side mirror of FxShared atomics)
        self._fx_state = FXState(
            enabled=False, fx_type=0, target=FX_TARGET_A,
            wet=0.8, depth=0.5, feedback=0.5, time_division=0.5, color=0.0,
        )
        self._smart_cfx_enabled: bool = False
        self._monitor_cue = MonitorCueState()
        self._manual_stem_gains: dict[str, dict[str, float]] = {
            did: {stem: 1.0 for stem in STEM_NAMES} for did in ("A", "B")
        }
        self._wrekk_macro: dict[str, float] = {"A": 0.0, "B": 0.0}
        self._normal_channel_filters: dict[str, float] = {"A": 0.0, "B": 0.0}
        self._phrase_sync = PhraseLockSync()

    # ── track loading ─────────────────────────────────────────────────────────

    def load_track(
        self,
        deck_id:  str,
        path:     Path,
        priority: Priority = Priority.HIGH,
    ) -> None:
        """
        Load a track into a deck asynchronously.

        1. Read audio from disk (fast path via ffmpeg fallback).
        2. Push to engine for immediate playback of the original.
        3. Start stem analysis in background; switch to stems when ready.
        """
        dl = self._decks[deck_id]

        with dl.lock:
            # Cancel any previous stem job for this deck
            if dl.active_job_id:
                self._analyzer.cancel(dl.active_job_id)
                dl.active_job_id = None

            # Clear sync when loading a new track (BPM/grid will be stale)
            self._follower_sync[deck_id] = None
            self._manual_stem_gains[deck_id] = {stem: 1.0 for stem in STEM_NAMES}
            self._wrekk_macro[deck_id] = 0.0
            old = dl.state
            dl.state = DeckState(
                id=old.id, status=DeckStatus.LOADING, track=old.track,
                position_s=old.position_s, pitch_pct=old.pitch_pct,
                bpm_live=old.bpm_live, stems=old.stems,
                stems_status=old.stems_status, loop=old.loop,
                cue_points=old.cue_points,
                sync_enabled=False, sync_master=old.sync_master,
                beatgrid=None, sync_phase_error=None,
                metrics=old.metrics, auto_markers=(),
            )
        self._push_states()
        # Silence the engine immediately — old audio must not play during loading
        try:
            self._engine.pause(deck_id)
        except Exception:
            pass

        # Load audio in a background thread to avoid blocking the caller
        threading.Thread(
            target=self._load_worker,
            args=(deck_id, path, priority),
            daemon=True,
            name=f"load-{deck_id}",
        ).start()

    def load_wrk_track(self, deck_id: str, wrk_path: str | Path) -> None:
        """
        Load a prepared .wrk track directly — no source file required.

        Used by the WREKKED browser.  Works even when the original SMB/source
        path is unavailable.  Follows the same staged 3-phase load as
        _load_from_wrk() but derives track identity from the .wrk file itself.
        """
        wrk_path = Path(wrk_path)
        dl = self._decks[deck_id]

        with dl.lock:
            if dl.active_job_id:
                self._analyzer.cancel(dl.active_job_id)
                dl.active_job_id = None
            self._follower_sync[deck_id] = None
            self._manual_stem_gains[deck_id] = {stem: 1.0 for stem in STEM_NAMES}
            self._wrekk_macro[deck_id] = 0.0
            old = dl.state
            dl.state = DeckState(
                id=old.id, status=DeckStatus.LOADING, track=old.track,
                position_s=old.position_s, pitch_pct=old.pitch_pct,
                bpm_live=old.bpm_live, stems=old.stems,
                stems_status=old.stems_status, loop=old.loop,
                cue_points=old.cue_points,
                sync_enabled=False, sync_master=old.sync_master,
                beatgrid=None, sync_phase_error=None, metrics=old.metrics,
                auto_markers=(),
            )
        self._push_states()
        try:
            self._engine.pause(deck_id)
        except Exception:
            pass

        threading.Thread(
            target=self._load_wrk_direct,
            args=(deck_id, wrk_path),
            daemon=True,
            name=f"wrk-direct-{deck_id}",
        ).start()

    def _load_wrk_direct(self, deck_id: str, wrk_path: Path) -> None:
        """
        Background worker for load_wrk_track().

        Uses _load_from_wrk() with a synthetic "path" equal to wrk_path so that
        all the staging, fastload, and stem-loading logic is reused without change.
        The track hash is derived from the .wrk file itself rather than a source file.
        """
        dl = self._decks[deck_id]
        try:
            self._load_from_wrk(deck_id, wrk_path, wrk_path, dl)
        except Exception:
            traceback.print_exc()
            with dl.lock:
                self._set_state(dl, dl.state.id, DeckStatus.EMPTY, track=None)
            self._push_states()

    def _load_from_wrk(
        self, deck_id: str, path: Path, wrk_path: Path, dl
    ) -> None:
        """
        Staged fast load from a .wrk prepared file.

        Phase 1  (~3–50 ms):   Read metadata / waveform / artwork.
                               UI updates immediately — track info visible.
        Phase 2  (~50–500 ms): Load full mix.
                               Fastload cache hit → read PCM16 + convert (fast).
                               Fastload cache miss → decode FLAC from ZIP (slow),
                               then build mix-only cache in background for next time.
                               Deck becomes READY — playback can begin.
        Phase 3  (background): Load stems from fastload or .wrk async.
                               Update fastload cache with stems when done.

        No HTDemucs, no BPM/key analysis, no waveform re-computation.
        """
        from wrekker.formats.wrk import load_wrk_metadata, load_wrk_mix, load_wrk_stems
        from wrekker.formats.fastload import FastloadCache, FORMAT_PCM16
        from wrekker.core.deck import WaveformData
        from wrekker.stems.models import StemResult, StemStatus

        t0 = time.monotonic()

        def _ms(t_ref: float) -> int:
            return int((time.monotonic() - t_ref) * 1000)

        # ── Fastload cache lookup ─────────────────────────────────────────────
        t_lookup   = time.monotonic()
        cache      = FastloadCache()
        cache_hit  = cache.is_valid(wrk_path)
        stems_hit  = cache_hit and cache.has_stems(wrk_path)
        print(f"[wrk-load] fastload lookup: {_ms(t_lookup)}ms  "
              f"({'HIT' if cache_hit else 'MISS'}"
              f"{'+STEMS' if stems_hit else ''})")

        # ── Phase 1: metadata ─────────────────────────────────────────────────
        t_meta = time.monotonic()
        try:
            meta = cache.load_metadata(wrk_path) if cache_hit else load_wrk_metadata(wrk_path)
        except Exception:
            traceback.print_exc()
            with dl.lock:
                self._set_state(dl, dl.state.id, DeckStatus.EMPTY, track=None)
            self._push_states()
            raise

        print(f"[wrk-load] manifest: {_ms(t_meta)}ms")

        engine_sr    = getattr(self._engine, "_sr", meta.sr)
        key_obj      = _parse_harmonic_key(meta.key)
        beatgrid     = _make_beatgrid(meta.beatgrid)
        track_hash   = _track_hash(path)
        auto_markers = _parse_auto_markers(getattr(meta, "markers", []))

        track = TrackInfo(
            path         = path,
            title        = meta.title or path.stem,
            artist       = meta.artist,
            duration_s   = meta.duration_s,
            sample_rate  = engine_sr,
            channels     = meta.channels,
            bpm          = meta.bpm,
            key          = key_obj,
            file_hash    = track_hash,
            artwork_data = meta.artwork,
        )

        pb = self._engine._get_deck(deck_id)
        pb.waveform = WaveformData(
            peaks       = meta.waveform_peaks,
            colors      = meta.waveform_colors,
            beats       = _display_beats(beatgrid, meta.beatgrid),
            stem_energy = meta.stem_energy if meta.has_stems else None,
            stem_horizon = getattr(meta, "stem_horizon", None) if meta.has_stems else None,
            zoom_peaks  = None,
            zoom_colors = None,
            zoom_chunk  = _ZOOM_CHUNK,
        )
        pb.waveform_seq += 1

        with dl.lock:
            old = dl.state
            dl.state = DeckState(
                id=old.id, status=DeckStatus.LOADING, track=track,
                position_s=0.0, pitch_pct=old.pitch_pct,
                bpm_live=meta.bpm or 0.0,
                stems=old.stems, stems_status=StemStatus.WRK_LOADING.value,
                loop=old.loop, cue_points=old.cue_points,
                sync_enabled=False, sync_master=old.sync_master,
                beatgrid=beatgrid, sync_phase_error=None, metrics=None,
                auto_markers=auto_markers,
            )
        self._push_states()

        print(f"[wrk-load] metadata/artwork: {_ms(t_meta)}ms")
        print(f"[wrk-load] total time to UI metadata: {_ms(t0)}ms")

        # ── Phase 2: full mix ─────────────────────────────────────────────────
        t_mix = time.monotonic()
        try:
            if cache_hit:
                mix_audio, mix_sr = cache.load_mix(wrk_path)
                print(f"[wrk-load] fastload mix read: {_ms(t_mix)}ms")
            else:
                mix_audio, mix_sr = load_wrk_mix(wrk_path)
                print(f"[wrk-load] flac mix decode: {_ms(t_mix)}ms  "
                      "(building fastload cache in background)")
        except Exception:
            traceback.print_exc()
            with dl.lock:
                self._set_state(dl, dl.state.id, DeckStatus.EMPTY, track=None)
            self._push_states()
            raise

        engine_audio = _resample_audio(mix_audio, mix_sr, engine_sr)

        z_peaks, z_colors = _compute_zoom_peaks(engine_audio, engine_sr)
        pb.waveform = WaveformData(
            peaks       = meta.waveform_peaks,
            colors      = meta.waveform_colors,
            beats       = _display_beats(beatgrid, meta.beatgrid),
            stem_energy = meta.stem_energy if meta.has_stems else None,
            stem_horizon = getattr(meta, "stem_horizon", None) if meta.has_stems else None,
            zoom_peaks  = z_peaks,
            zoom_colors = z_colors,
            zoom_chunk  = _ZOOM_CHUNK,
        )
        pb.waveform_seq += 1

        from dataclasses import replace as _dc_replace
        track = _dc_replace(track, duration_s=engine_audio.shape[0] / engine_sr)

        t_eng = time.monotonic()
        self._engine.load_track(deck_id, engine_audio)
        print(f"[wrk-load] engine.load_track: {_ms(t_eng)}ms")

        stems_initial = (
            StemStatus.MIX_READY.value if meta.has_stems else StemStatus.NONE.value
        )
        with dl.lock:
            old = dl.state
            dl.state = DeckState(
                id=old.id, status=DeckStatus.READY, track=track,
                position_s=0.0, pitch_pct=old.pitch_pct,
                bpm_live=meta.bpm or 0.0,
                stems=old.stems, stems_status=stems_initial,
                loop=old.loop, cue_points=old.cue_points,
                sync_enabled=False, sync_master=old.sync_master,
                beatgrid=beatgrid, sync_phase_error=None, metrics=None,
                auto_markers=auto_markers,
            )
        self._push_states()

        path_label = "fastload" if cache_hit else "flac"
        print(f"[wrk-load] playable ({path_label}): {_ms(t0)}ms")

        # If the cache was a miss, build mix-only fastload now (background).
        # This ensures the next load of this track hits the cache path.
        if not cache_hit:
            def _build_mix_cache(
                _wrk_path = wrk_path,
                _cache    = cache,
                _meta     = meta,
                _mix      = mix_audio,
                _sr       = mix_sr,
            ) -> None:
                try:
                    t_b = time.monotonic()
                    _cache.build(
                        wrk_path     = _wrk_path,
                        meta         = _meta,
                        mix_audio    = _mix,
                        mix_sr       = _sr,
                        stems_raw    = None,
                        audio_format = FORMAT_PCM16,
                    )
                    print(f"[wrk-load] fastload mix cache built: "
                          f"{_wrk_path.name}  ({int((time.monotonic()-t_b)*1000)}ms)")
                except Exception:
                    traceback.print_exc()

            threading.Thread(
                target=_build_mix_cache, daemon=True, name="fastload-build-mix"
            ).start()

        if not meta.has_stems:
            return

        # ── Phase 3: stems in background ──────────────────────────────────────
        def _stems_bg(
            _deck_id    = deck_id,
            _wrk_path   = wrk_path,
            _dl         = dl,
            _mix_audio  = mix_audio,
            _mix_sr     = mix_sr,
            _cache      = cache,
            _cache_hit  = cache_hit,
            _stems_hit  = stems_hit,
            _meta       = meta,
            _t0         = t0,
            _track_hash = track_hash,
        ) -> None:
            with _dl.lock:
                self._set_state(_dl, _dl.state.id, _dl.state.status,
                                stems_status=StemStatus.STEMS_LOADING.value)
            self._push_states()

            t_stems = time.monotonic()
            try:
                if _stems_hit:
                    stems_raw = _cache.load_all_stems(_wrk_path)
                    if stems_raw is not None:
                        print(f"[wrk-load] fastload stems read: {_ms(t_stems)}ms")
                    else:
                        # stems.meta.json existed but files missing — fall through
                        stems_raw = load_wrk_stems(_wrk_path)
                        print(f"[wrk-load] flac stems decode (cache broken): {_ms(t_stems)}ms")
                else:
                    stems_raw = load_wrk_stems(_wrk_path)
                    print(f"[wrk-load] flac stems decode: {_ms(t_stems)}ms")
            except Exception:
                traceback.print_exc()
                with _dl.lock:
                    self._set_state(_dl, _dl.state.id, _dl.state.status,
                                    stems_status=StemStatus.FAILED.value)
                self._push_states()
                return

            if stems_raw is None:
                with _dl.lock:
                    self._set_state(_dl, _dl.state.id, _dl.state.status,
                                    stems_status=StemStatus.NONE.value)
                self._push_states()
                return

            # Guard: track may have changed while stems were loading
            with _dl.lock:
                current_hash = (_dl.state.track.file_hash
                                if _dl.state.track else None)
            if current_hash != _track_hash:
                return

            e_sr = getattr(self._engine, "_sr", _mix_sr)
            stem_result = StemResult(
                vocals     = stems_raw["vocals"],
                drums      = stems_raw["drums"],
                bass       = stems_raw["bass"],
                other      = stems_raw["other"],
                model      = "wrk",
                duration_s = _meta.duration_s,
            )
            stem_result = _resample_stem_result(stem_result, e_sr)

            t_eng_stems = time.monotonic()
            self._engine.update_stems(_deck_id, stem_result)
            print(f"[wrk-load] engine.load_stems: {_ms(t_eng_stems)}ms")

            # Final guard before state update
            with _dl.lock:
                current_hash = (_dl.state.track.file_hash
                                if _dl.state.track else None)
            if current_hash != _track_hash:
                return

            with _dl.lock:
                self._set_state(_dl, _dl.state.id, _dl.state.status,
                                stems_status=StemStatus.READY.value)
            self._push_states()
            print(f"[wrk-load] stems ready: {_ms(_t0)}ms total")

            # Update cache with stems if not already there
            if not _stems_hit:
                def _build_stems_cache(
                    _wrk  = _wrk_path,
                    _c    = _cache,
                    _m    = _meta,
                    _mix  = _mix_audio,
                    _sr   = _mix_sr,
                    _sr_  = stems_raw,
                ) -> None:
                    try:
                        t_b = time.monotonic()
                        _c.build(
                            wrk_path     = _wrk,
                            meta         = _m,
                            mix_audio    = _mix,
                            mix_sr       = _sr,
                            stems_raw    = _sr_,
                            audio_format = FORMAT_PCM16,
                        )
                        print(f"[wrk-load] fastload cache + stems built: "
                              f"{_wrk.name}  ({int((time.monotonic()-t_b)*1000)}ms)")
                    except Exception:
                        traceback.print_exc()

                threading.Thread(
                    target=_build_stems_cache, daemon=True, name="fastload-build-stems"
                ).start()

        threading.Thread(
            target=_stems_bg, daemon=True, name=f"wrk-stems-{deck_id}"
        ).start()


    def _load_worker(self, deck_id: str, path: Path, priority: Priority) -> None:
        dl = self._decks[deck_id]

        # ── .wrk priority check ───────────────────────────────────────────────
        if self._prepared_db is not None:
            try:
                rec = self._prepared_db.find_wrk(path)
                if rec and rec.wrk_ready:
                    wrk_path = Path(rec.wrk_path)
                    if wrk_path.exists() and rec.is_current(path):
                        self._load_from_wrk(deck_id, path, wrk_path, dl)
                        return
                    elif wrk_path.exists() and not rec.is_current(path):
                        # Source changed — mark outdated, fall through to source
                        self._prepared_db.mark_outdated(path)
            except Exception:
                pass  # any DB/IO error → fall through to normal load

        try:
            audio, sr = load_audio(path)
            engine_sr = getattr(self._engine, "_sr", sr)
            engine_audio = _resample_audio(audio, sr, engine_sr)

            # Read file metadata (artist, title, bpm hint, artwork)
            file_meta = _read_track_meta(path)

            track = TrackInfo(
                path         = path,
                title        = file_meta["title"] or path.stem,
                artist       = file_meta["artist"],
                duration_s   = engine_audio.shape[0] / engine_sr,
                sample_rate  = engine_sr,
                channels     = engine_audio.shape[1] if engine_audio.ndim > 1 else 1,
                bpm          = file_meta["bpm"],  # tag BPM as initial value
                key          = None,
                file_hash    = _track_hash(path),
                artwork_data = file_meta["artwork"],
            )

            self._engine.load_track(deck_id, engine_audio)

            # Waveform envelope + spectral colors — done in this thread before beats
            pb = self._engine._get_deck(deck_id)
            pb.waveform = _compute_waveform(engine_audio, engine_sr)
            pb.waveform_seq += 1

            with dl.lock:
                self._set_state(
                    dl, dl.state.id, DeckStatus.READY,
                    track=track,
                    position_s=0.0,
                    stems_status=StemStatus.NONE.value,
                )

            # BPM + key + beat detection (slow librosa — separate thread)
            threading.Thread(
                target=self._analysis_worker,
                args=(deck_id, engine_audio, engine_sr, track, file_meta["bpm"]),
                daemon=True,
                name=f"analysis-{deck_id}",
            ).start()

        except Exception:
            traceback.print_exc()
            with dl.lock:
                self._set_state(dl, dl.state.id, DeckStatus.EMPTY, track=None)
            return

        # Start stem analysis after the deck is playable. Stem failures should
        # not roll back the loaded audio.
        def on_complete(result, _did=deck_id, _dl=dl):
            engine_sr = getattr(self._engine, "_sr", 44100)
            result = _resample_stem_result(result, engine_sr)
            self._engine.update_stems(_did, result)
            with _dl.lock:
                self._set_state(
                    _dl, _dl.state.id, _dl.state.status,
                    stems_status=StemStatus.READY.value,
                )
            threading.Thread(
                target=self._stem_waveform_worker,
                args=(_did, result),
                daemon=True,
                name=f"stem-wf-{_did}",
            ).start()

        def on_progress(frac: float):
            with dl.lock:
                status = (StemStatus.ANALYZING if frac > 0
                          else StemStatus.QUEUED).value
                self._set_state(dl, dl.state.id, dl.state.status,
                                stems_status=status)

        try:
            job_id = self._analyzer.analyze(path, priority, on_complete, on_progress)
        except Exception:
            traceback.print_exc()
            with dl.lock:
                self._set_state(
                    dl, dl.state.id, dl.state.status,
                    stems_status=StemStatus.FAILED.value,
                )
        else:
            with dl.lock:
                dl.active_job_id = job_id

    def _analysis_worker(
        self,
        deck_id:      str,
        audio:        np.ndarray,
        sr:           int,
        track:        "TrackInfo",
        metadata_bpm: "float | None" = None,
    ) -> None:
        """Background thread: BPM, beat positions, and key detection via librosa."""
        bpm, beats = _detect_bpm_beats(audio, sr, metadata_bpm)
        key        = _detect_key(audio, sr)

        # Patch beat positions into the waveform that was already displayed
        pb = self._engine._get_deck(deck_id)
        if pb.waveform is not None and beats:
            from wrekker.core.deck import WaveformData
            old_wf = pb.waveform
            pb.waveform = WaveformData(
                peaks       = old_wf.peaks,
                colors      = old_wf.colors,
                beats       = fill_beat_gaps(beats, bpm or 120.0),
                stem_energy = old_wf.stem_energy,
                stem_horizon = old_wf.stem_horizon,
                zoom_peaks  = old_wf.zoom_peaks,
                zoom_colors = old_wf.zoom_colors,
                zoom_chunk  = old_wf.zoom_chunk,
            )
            pb.waveform_seq += 1

        if bpm is None and key is None:
            return

        from dataclasses import replace as dc_replace
        new_track = dc_replace(
            track,
            bpm = bpm if bpm is not None else track.bpm,
            key = key if key is not None else track.key,
        )

        # Build BeatGrid from analysis results
        new_beatgrid: object = _UNSET   # _UNSET = keep existing; None/BeatGrid = update
        if bpm is not None:
            first_beat_s = beats[0] if beats else 0.0
            confidence   = min(1.0, len(beats) / 64.0) if beats else 0.3
            source       = ("metadata"
                            if track.bpm is not None and abs(bpm - track.bpm) < 1.0
                            else "analyzed")

            new_beatgrid = BeatGrid(
                bpm=bpm,
                first_beat_s=first_beat_s,
                confidence=confidence,
                source=source,
                beats=beats,
            )

        dl = self._decks[deck_id]
        with dl.lock:
            if dl.state.track and dl.state.track.file_hash == track.file_hash:
                bpm_live: Optional[float] = None
                if bpm is not None:
                    bpm_live = bpm * self._engine._get_deck(deck_id).pitch_factor
                old = dl.state
                dl.state = DeckState(
                    id=old.id, status=old.status, track=new_track,
                    position_s=old.position_s, pitch_pct=old.pitch_pct,
                    bpm_live=bpm_live if bpm_live is not None else old.bpm_live,
                    stems=old.stems, stems_status=old.stems_status,
                    loop=old.loop, cue_points=old.cue_points,
                    sync_enabled=old.sync_enabled, sync_master=old.sync_master,
                    beatgrid=(new_beatgrid if new_beatgrid is not _UNSET
                              else old.beatgrid),
                    sync_phase_error=old.sync_phase_error,
                    metrics=old.metrics, auto_markers=old.auto_markers,
                )
                self._push_states()

    def _stem_waveform_worker(self, deck_id: str, stems: "StemResult") -> None:
        """Compute per-stem energy array and attach it to the waveform."""
        try:
            stem_energy = _compute_stem_energy(stems)
        except Exception:
            return
        from wrekker.core.deck import WaveformData
        pb = self._engine._get_deck(deck_id)
        if pb.waveform is not None:
            old_wf = pb.waveform
            pb.waveform = WaveformData(
                peaks       = old_wf.peaks,
                colors      = old_wf.colors,
                beats       = old_wf.beats,
                stem_energy = stem_energy,
                stem_horizon = old_wf.stem_horizon,
                zoom_peaks  = old_wf.zoom_peaks,
                zoom_colors = old_wf.zoom_colors,
                zoom_chunk  = old_wf.zoom_chunk,
            )
            pb.waveform_seq += 1

    # ── transport controls ────────────────────────────────────────────────────

    def play(self, deck_id: str) -> None:
        dl = self._decks[deck_id]
        with dl.lock:
            current_status = dl.state.status
        # Do not start engine playback while a new track is loading — the
        # old audio would play until the engine buffer is replaced.
        if current_status == DeckStatus.LOADING:
            return
        if current_status == DeckStatus.EMPTY:
            return
        fs = self._follower_sync.get(deck_id)
        if fs is not None and self.get_state(deck_id).sync_enabled:
            self._phase_align_resume(deck_id, fs)
        self._engine.play(deck_id)
        with dl.lock:
            self._set_state(dl, dl.state.id, DeckStatus.PLAYING)

    def _phase_align_resume(self, deck_id: str, fs: _FollowerSync) -> None:
        """Seek follower to beat-aligned position immediately before resuming."""
        master_state   = self.get_state(fs.master_id)
        follower_state = self.get_state(deck_id)

        master_grid   = master_state.beatgrid
        follower_grid = follower_state.beatgrid

        # Re-apply sync BPM rate regardless of beatgrid availability
        self._engine.set_playback_rate(deck_id, fs.base_rate)
        self._engine._get_deck(deck_id).pitch_factor = fs.base_rate

        if not master_grid or not follower_grid:
            return

        master_pos   = self._engine._get_deck(fs.master_id).position_s
        follower_pos = self._engine._get_deck(deck_id).position_s

        master_phase = _sync_phase_at(master_state, master_pos)
        target_pos   = _sync_snap_to_phase(follower_state, master_phase, follower_pos)

        if target_pos >= 0:
            self._engine.seek(deck_id, target_pos)
        fs.pll.reset()

    def pause(self, deck_id: str) -> None:
        dl = self._decks[deck_id]
        self._engine.pause(deck_id)
        with dl.lock:
            self._set_state(dl, dl.state.id, DeckStatus.PAUSED)

    def cue(self, deck_id: str) -> None:
        """
        Pioneer-style CUE:
        - While PLAYING  → pause, set cue point at current position.
        - While PAUSED   → play from cue point (previewing); use cue_release()
                           to stop and return. If no cue set, play from start.
        """
        dl = self._decks[deck_id]
        pb = self._engine._get_deck(deck_id)
        with dl.lock:
            if dl.state.status == DeckStatus.PLAYING:
                pos = pb.position_s
                self._engine.pause(deck_id)
                self._engine.seek(deck_id, pos)
                self._engine.set_cue_point(deck_id, pos)
                self._set_state(dl, dl.state.id, DeckStatus.PAUSED,
                                position_s=pos)
            else:
                # Jump to cue point and start playing
                cue_s = pb.cue_point_s
                self._engine.seek(deck_id, cue_s)
                self._engine.play(deck_id)
                self._set_state(dl, dl.state.id, DeckStatus.PLAYING,
                                position_s=cue_s)

    def cue_release(self, deck_id: str) -> None:
        """Release CUE while previewing: return to cue point and pause."""
        dl = self._decks[deck_id]
        pb = self._engine._get_deck(deck_id)
        with dl.lock:
            if dl.state.status == DeckStatus.PLAYING:
                cue_s = pb.cue_point_s
                self._engine.pause(deck_id)
                self._engine.seek(deck_id, cue_s)
                self._set_state(dl, dl.state.id, DeckStatus.PAUSED,
                                position_s=cue_s)

    def seek(self, deck_id: str, position_s: float) -> None:
        self._engine.seek(deck_id, position_s)
        dl = self._decks[deck_id]
        with dl.lock:
            self._set_state(dl, dl.state.id, dl.state.status,
                            position_s=position_s)

    def set_pitch(self, deck_id: str, pitch_pct: float) -> None:
        """pitch_pct: -16.0 to +16.0 semitones. Manual pitch disengages sync on the follower."""
        pitch_pct = max(-16.0, min(16.0, pitch_pct))
        factor    = 2.0 ** (pitch_pct / 12.0)
        dl        = self._decks[deck_id]
        self._engine._get_deck(deck_id).pitch_factor = factor
        self._engine.set_playback_rate(deck_id, factor)

        # Manual pitch on a follower disengages sync
        was_synced = self._follower_sync.get(deck_id) is not None
        if was_synced:
            self._follower_sync[deck_id] = None

        with dl.lock:
            old = dl.state
            native_bpm = _native_bpm(old)
            bpm_live = native_bpm * factor if native_bpm > 0 else 0.0
            dl.state = DeckState(
                id=old.id, status=old.status, track=old.track,
                position_s=old.position_s, pitch_pct=pitch_pct,
                bpm_live=bpm_live, stems=old.stems,
                stems_status=old.stems_status, loop=old.loop,
                cue_points=old.cue_points,
                sync_enabled=False if was_synced else old.sync_enabled,
                sync_master=old.sync_master,
                beatgrid=old.beatgrid,
                sync_phase_error=None if was_synced else old.sync_phase_error,
                metrics=old.metrics, auto_markers=old.auto_markers,
            )
        self._push_states()

    # ── stem controls ─────────────────────────────────────────────────────────

    def _wrekk_multiplier(self, stem: str, macro: float) -> float:
        v = max(-1.0, min(1.0, macro))
        if abs(v) < 0.025:
            return 1.0
        amount = (abs(v) - 0.025) / 0.975
        harmonic = stem in ("vocals", "other")
        rhythm = stem in ("drums", "bass")
        if v < 0.0:
            return 1.0 + 0.20 * amount if harmonic else 1.0 - amount
        return 1.0 + 0.20 * amount if rhythm else 1.0 - amount

    def _apply_stem_layer(self, deck_id: str) -> None:
        dl = self._decks[deck_id]
        with dl.lock:
            old = dl.state
            macro = self._wrekk_macro[deck_id] if self._smart_cfx_enabled else 0.0
            any_solo = any(s.solo for s in old.stems.values())
            new_stems = {}
            for stem in STEM_NAMES:
                prev = old.stems[stem]
                base = self._manual_stem_gains[deck_id].get(stem, prev.gain)
                final = max(0.0, min(2.0, base * self._wrekk_multiplier(stem, macro)))
                effective = final
                if prev.muted or (any_solo and not prev.solo):
                    effective = 0.0
                self._engine.set_stem_gain(deck_id, stem, effective)
                new_stems[stem] = StemState(
                    gain=final,
                    muted=prev.muted,
                    solo=prev.solo,
                    lufs=prev.lufs,
                    spectral=prev.spectral,
                )
            dl.state = DeckState(
                id=old.id, status=old.status, track=old.track,
                position_s=old.position_s, pitch_pct=old.pitch_pct,
                bpm_live=old.bpm_live, stems=new_stems,
                stems_status=old.stems_status, loop=old.loop,
                cue_points=old.cue_points,
                sync_enabled=old.sync_enabled, sync_master=old.sync_master,
                beatgrid=old.beatgrid, sync_phase_error=old.sync_phase_error,
                metrics=old.metrics, auto_markers=old.auto_markers,
            )
        self._push_states()

    def set_stem_gain(self, deck_id: str, stem_name: str, gain: float) -> None:
        """Set manual stem base gain. WREKK macro is layered on top."""
        self._manual_stem_gains[deck_id][stem_name] = max(0.0, min(2.0, gain))
        self._apply_stem_layer(deck_id)

    def set_stem_gain_from_hardware(self, deck_id: str, stem_name: str, gain: float) -> None:
        self.set_stem_gain(deck_id, stem_name, gain)

    def set_wrekk_macro(self, deck_id: str, value: float) -> None:
        self._wrekk_macro[deck_id] = max(-1.0, min(1.0, value))
        if self._smart_cfx_enabled:
            self._apply_stem_layer(deck_id)

    def mute_stem(self, deck_id: str, stem_name: str, muted: bool) -> None:
        dl = self._decks[deck_id]
        with dl.lock:
            new_state = dl.state.with_stem(stem_name, muted=muted)
            dl.state  = new_state
        self._apply_stem_layer(deck_id)

    def solo_stem(self, deck_id: str, stem_name: str, solo: bool) -> None:
        """Solo a stem: mute all others, unmute this one."""
        dl = self._decks[deck_id]
        with dl.lock:
            # Update StemState.solo flags
            new_stems = {}
            for name in STEM_NAMES:
                old = dl.state.stems[name]
                new_stems[name] = StemState(
                    gain     = old.gain,
                    muted    = old.muted,
                    solo     = (name == stem_name) and solo,
                    lufs     = old.lufs,
                    spectral = old.spectral,
                )
            # Rebuild state with updated stems dict
            old = dl.state
            dl.state = DeckState(
                id=old.id, status=old.status,
                track=old.track, position_s=old.position_s,
                pitch_pct=old.pitch_pct, bpm_live=old.bpm_live,
                stems=new_stems, stems_status=old.stems_status,
                loop=old.loop, cue_points=old.cue_points,
                sync_enabled=old.sync_enabled, sync_master=old.sync_master,
                beatgrid=old.beatgrid, sync_phase_error=old.sync_phase_error,
                metrics=old.metrics, auto_markers=old.auto_markers,
            )
        self._apply_stem_layer(deck_id)

    # ── loops ─────────────────────────────────────────────────────────────────

    def loop_in(self, deck_id: str) -> None:
        """Set loop start at current position."""
        pos = self._engine._get_deck(deck_id).position_s
        dl  = self._decks[deck_id]
        with dl.lock:
            old_loop = dl.state.loop
            end_s    = old_loop.end_s if old_loop and old_loop.end_s > pos else pos + 4.0
            new_loop = LoopState(active=False, start_s=pos, end_s=end_s, bars=0)
            self._engine.set_loop(deck_id, False, pos, end_s)
            self._set_state(dl, dl.state.id, dl.state.status, loop=new_loop)

    def loop_out(self, deck_id: str) -> None:
        """Set loop end at current position and activate the loop."""
        pos = self._engine._get_deck(deck_id).position_s
        dl  = self._decks[deck_id]
        with dl.lock:
            old_loop = dl.state.loop
            start_s  = old_loop.start_s if old_loop and old_loop.start_s < pos else max(0.0, pos - 4.0)
            new_loop = LoopState(active=True, start_s=start_s, end_s=pos, bars=0)
            self._engine.set_loop(deck_id, True, start_s, pos)
            self._set_state(dl, dl.state.id, dl.state.status, loop=new_loop)

    def loop_toggle(self, deck_id: str) -> None:
        dl = self._decks[deck_id]
        with dl.lock:
            if dl.state.loop is None:
                return
            old     = dl.state.loop
            active  = not old.active
            new     = LoopState(active=active, start_s=old.start_s,
                                end_s=old.end_s, bars=old.bars)
            self._engine.set_loop(deck_id, active, old.start_s, old.end_s)
            self._set_state(dl, dl.state.id, dl.state.status, loop=new)

    def loop_set_bars(self, deck_id: str, bars: float) -> None:
        """Set loop length in bars from current position and activate."""
        state = self.get_state(deck_id)
        bpm = state.bpm_live or (state.track.bpm if state.track else None)
        if not bpm:
            return
        beat_s   = 60.0 / bpm
        length_s = beat_s * 4 * bars
        pos      = self._engine._get_deck(deck_id).position_s
        end_s    = pos + length_s
        dl = self._decks[deck_id]
        with dl.lock:
            new_loop = LoopState(active=True, start_s=pos, end_s=end_s, bars=int(bars))
            self._engine.set_loop(deck_id, True, pos, end_s)
            self._set_state(dl, dl.state.id, dl.state.status, loop=new_loop)

    # ── cue points ────────────────────────────────────────────────────────────

    def add_cue(
        self,
        deck_id:    str,
        label:      str = "",
        color:      str = "#00d4ff",
        cue_type:   str = "hot_cue",
    ) -> None:
        pos = self._engine._get_deck(deck_id).position_s
        dl  = self._decks[deck_id]
        cue = CuePoint(position_s=pos, label=label, color=color, type=cue_type)
        with dl.lock:
            new_cues = (*dl.state.cue_points, cue)
            self._set_state(dl, dl.state.id, dl.state.status,
                            cue_points=new_cues)

    def jump_to_cue(self, deck_id: str, index: int) -> None:
        dl = self._decks[deck_id]
        with dl.lock:
            cues = dl.state.cue_points
        if 0 <= index < len(cues):
            self.seek(deck_id, cues[index].position_s)

    _CUE_SLOT_COLORS = [
        "#00d4ff", "#ff6b6b", "#4ecdc4", "#ffe66d",
        "#a29bfe", "#fd79a8", "#55efc4", "#fdcb6e",
    ]

    def add_cue_at(
        self,
        deck_id:    str,
        position_s: float,
        label:      str = "",
        color:      str = "",
        cue_type:   str = "hot_cue",
    ) -> bool:
        """Add a cue at an explicit position. Returns False if all 8 slots are full."""
        dl = self._decks[deck_id]
        with dl.lock:
            cues = dl.state.cue_points
            if len(cues) >= 8:
                return False
            slot_color = color or self._CUE_SLOT_COLORS[len(cues) % 8]
            cue = CuePoint(position_s=position_s, label=label,
                           color=slot_color, type=cue_type)
            self._set_state(dl, dl.state.id, dl.state.status,
                            cue_points=(*cues, cue))
        return True

    def remove_auto_marker(self, deck_id: str, marker_id: str) -> None:
        """Remove a single auto marker by its id."""
        dl = self._decks[deck_id]
        with dl.lock:
            old = dl.state
            new_markers = tuple(m for m in old.auto_markers if m.id != marker_id)
            self._set_state(dl, old.id, old.status, auto_markers=new_markers)

    def clear_auto_markers(self, deck_id: str) -> None:
        """Remove all auto markers from a deck."""
        dl = self._decks[deck_id]
        with dl.lock:
            old = dl.state
            self._set_state(dl, old.id, old.status, auto_markers=())

    def regenerate_markers_bg(self, deck_id: str) -> None:
        """Re-run auto marker detection in a background thread using live waveform data."""
        import threading
        threading.Thread(
            target=self._regenerate_markers_worker,
            args=(deck_id,),
            daemon=True,
            name=f"regen-markers-{deck_id}",
        ).start()

    def _regenerate_markers_worker(self, deck_id: str) -> None:
        dl   = self._decks[deck_id]
        with dl.lock:
            state = dl.state

        if not state.track or not state.beatgrid:
            print(f"[transport] regen markers: no track/beatgrid for {deck_id}")
            return

        # Build beatgrid dict from live BeatGrid object
        bg = state.beatgrid
        beatgrid_dict: dict = {
            "bpm":            bg.bpm,
            "confidence":     bg.confidence,
            "beats":          list(bg.beats),
            "downbeats":      list(bg.downbeats),
            "phrase_markers": [
                {"position_sec": pm.position_sec,
                 "phrase_length": pm.phrase_length,
                 "energy_level":  pm.energy_level}
                for pm in (bg.phrase_markers or ())
            ],
            "dynamic_tempo":  bg.dynamic_tempo,
            "schema_version": 2,
        }

        # Read live waveform data from the engine's deck playback buffer
        try:
            pb = self._engine._get_deck(deck_id)
            stem_energy    = pb.waveform.stem_energy if pb.waveform else None
            waveform_peaks = pb.waveform.peaks       if pb.waveform else None
        except Exception:
            stem_energy = waveform_peaks = None

        duration_s = state.track.duration_s

        try:
            from wrekker.analysis.auto_markers import AutoMarkerDetector
            detector  = AutoMarkerDetector()
            raw_marks = detector.analyze(
                beatgrid       = beatgrid_dict,
                stem_energy    = stem_energy,
                waveform_peaks = waveform_peaks,
                duration_s     = duration_s,
            )
        except Exception as e:
            print(f"[transport] marker regeneration failed for {deck_id}: {e}")
            return

        # Preserve user-modified markers from the current set
        with dl.lock:
            user_modified = {m.id: m for m in dl.state.auto_markers
                             if getattr(m, "user_modified", False)}

        final: list = []
        seen_ids: set = set()
        for m in raw_marks:
            resolved = user_modified.get(m.id, m)
            if resolved.confidence >= MARKER_MIN_CONFIDENCE and resolved.id not in seen_ids:
                final.append(resolved)
                seen_ids.add(resolved.id)
        # Re-add any user-modified markers not matched by new detection
        for mid, um in user_modified.items():
            if um.confidence >= MARKER_MIN_CONFIDENCE and mid not in seen_ids:
                final.append(um)
        final.sort(key=lambda m: m.position_s)

        with dl.lock:
            old = dl.state
            self._set_state(dl, old.id, old.status, auto_markers=tuple(final))
        print(f"[transport] markers regenerated for {deck_id}: {len(final)} markers")

    # ── sync / beatmatching ───────────────────────────────────────────────────

    def set_sync_master(self, deck_id: str) -> None:
        """Designate deck_id as the tempo master.  The other deck gets sync_master=False."""
        other_id = "B" if deck_id == "A" else "A"
        self._sync_master = deck_id
        for did, is_master in ((deck_id, True), (other_id, False)):
            dl = self._decks[did]
            with dl.lock:
                old = dl.state
                dl.state = DeckState(
                    id=old.id, status=old.status, track=old.track,
                    position_s=old.position_s, pitch_pct=old.pitch_pct,
                    bpm_live=old.bpm_live, stems=old.stems,
                    stems_status=old.stems_status, loop=old.loop,
                    cue_points=old.cue_points,
                    sync_enabled=old.sync_enabled,
                    sync_master=is_master,
                    beatgrid=old.beatgrid,
                    sync_phase_error=old.sync_phase_error,
                    metrics=old.metrics, auto_markers=old.auto_markers,
                )
        self._push_states()

    def get_sync_master(self) -> "Optional[str]":
        return self._sync_master

    def sync(self, deck_id: str) -> None:
        """
        Toggle sync for deck_id.

        First press:  enable — match BPM to master and phase-snap within 5% of a beat.
        Second press: disable — leave playback rate unchanged, deactivate PLL.

        Pressing SYNC on the current master automatically designates the other
        deck as master and makes the clicked deck follow.
        """
        if self.get_state(deck_id).sync_enabled:
            self.unsync(deck_id)
            return

        other_id  = "B" if deck_id == "A" else "A"
        master_id = self._sync_master
        if master_id is None or master_id == deck_id:
            master_id = other_id
            self.set_sync_master(master_id)

        master_state = self.get_state(master_id)
        my_state     = self.get_state(deck_id)

        if not my_state.track or not master_state.track:
            return

        # ── Current positions (needed for local BPM and phase snap) ─────────
        master_grid   = master_state.beatgrid
        follower_grid = my_state.beatgrid
        master_pos    = self._engine._get_deck(master_id).position_s
        follower_pos  = self._engine._get_deck(deck_id).position_s

        # ── BPM rate — use real playback rate and local beatgrid tempo ──────
        master_rate = self._engine.get_playback_rate(master_id)
        master_bpm = _sync_bpm_base(master_state) * master_rate
        follower_native = _sync_bpm_base(my_state)
        if master_bpm <= 0 or follower_native <= 0:
            return

        sync_rate = max(0.5, min(2.0, master_bpm / follower_native))
        self._engine.set_playback_rate(deck_id, sync_rate)
        self._engine._get_deck(deck_id).pitch_factor = sync_rate

        pitch_pct = 12.0 * math.log2(sync_rate)
        bpm_live  = follower_native * sync_rate

        # ── Phrase snap first, then phase snap inside that phrase ─────────────
        initial_phase_err = 0.0

        if master_grid and follower_grid:
            master_live = _dc_replace(master_state, position_s=master_pos)
            follower_live = _dc_replace(my_state, position_s=follower_pos)
            phrase_target = self._phrase_sync.snap_slave_to_phrase(master_live, follower_live)
            if phrase_target >= 0:
                self._engine.seek(deck_id, phrase_target)
                follower_pos = phrase_target
                follower_live = _dc_replace(my_state, position_s=follower_pos)

            master_phase   = _sync_phase_at(master_state, master_pos)
            follower_phase = _sync_phase_at(follower_live, follower_pos)
            phase_err      = master_phase - follower_phase
            phase_err     -= round(phase_err)  # normalize to (-0.5, 0.5]
            initial_phase_err = phase_err

            if abs(phase_err) > 0.01:    # > 1 % of a beat → snap
                target = _sync_snap_to_phase(follower_live, master_phase, follower_pos)
                if target >= 0:
                    self._engine.seek(deck_id, target)
                    initial_phase_err = 0.0

        # ── Activate follower PLL ────────────────────────────────────────────
        self._follower_sync[deck_id] = _FollowerSync(
            master_id = master_id,
            base_rate = sync_rate,
        )

        dl = self._decks[deck_id]
        with dl.lock:
            old = dl.state
            dl.state = DeckState(
                id=old.id, status=old.status, track=old.track,
                position_s=old.position_s, pitch_pct=pitch_pct,
                bpm_live=bpm_live, stems=old.stems,
                stems_status=old.stems_status, loop=old.loop,
                cue_points=old.cue_points,
                sync_enabled=True,
                sync_master=old.sync_master,
                beatgrid=old.beatgrid,
                sync_phase_error=initial_phase_err,
                metrics=old.metrics, auto_markers=old.auto_markers,
            )
        self._push_states()

    def unsync(self, deck_id: str) -> None:
        """Disable sync for deck_id; playback rate stays as-is."""
        self._follower_sync[deck_id] = None
        dl = self._decks[deck_id]
        with dl.lock:
            old = dl.state
            dl.state = DeckState(
                id=old.id, status=old.status, track=old.track,
                position_s=old.position_s, pitch_pct=old.pitch_pct,
                bpm_live=old.bpm_live, stems=old.stems,
                stems_status=old.stems_status, loop=old.loop,
                cue_points=old.cue_points,
                sync_enabled=False,
                sync_master=old.sync_master,
                beatgrid=old.beatgrid,
                sync_phase_error=None,
                metrics=old.metrics, auto_markers=old.auto_markers,
            )
        self._push_states()

    def tick_sync(self, dt: float) -> None:
        """
        Phase-locked-loop tick, called at UI rate.

        For each active synced follower:
          1. Track master BPM changes (master pitch fader moved).
          2. Compute beat-phase error between master and follower.
          3. Apply a PI-controller correction to the follower's playback rate.
          4. Update DeckState.sync_phase_error for the UI phase meter.
        """
        def _apply_follower_rate(follower_id: str, fs: _FollowerSync,
                                 native_bpm: float,
                                 phase_err: Optional[float]) -> None:
            self._engine.set_playback_rate(follower_id, fs.base_rate)
            self._engine._get_deck(follower_id).pitch_factor = fs.base_rate
            fs.applied_rate = fs.base_rate
            bpm_live = native_bpm * fs.base_rate if native_bpm > 0 else 0.0
            pitch_pct = 12.0 * math.log2(fs.base_rate) if fs.base_rate > 0 else 0.0

            dl = self._decks[follower_id]
            with dl.lock:
                old = dl.state
                if (
                    abs(old.pitch_pct - pitch_pct) < 1e-3
                    and abs((old.bpm_live or 0.0) - (bpm_live or old.bpm_live or 0.0)) < 1e-3
                    and old.sync_phase_error == phase_err
                ):
                    return
                dl.state = DeckState(
                    id=old.id, status=old.status, track=old.track,
                    position_s=old.position_s, pitch_pct=pitch_pct,
                    bpm_live=bpm_live or old.bpm_live,
                    stems=old.stems,
                    stems_status=old.stems_status, loop=old.loop,
                    cue_points=old.cue_points,
                    sync_enabled=old.sync_enabled,
                    sync_master=old.sync_master,
                    beatgrid=old.beatgrid,
                    sync_phase_error=phase_err,
                    metrics=old.metrics, auto_markers=old.auto_markers,
                )
            self._push_states()

        for follower_id, fs in list(self._follower_sync.items()):
            if fs is None:
                continue

            follower_state = self.get_state(follower_id)
            master_state   = self.get_state(fs.master_id)

            follower_grid = follower_state.beatgrid
            master_grid   = master_state.beatgrid
            master_rate = self._engine.get_playback_rate(fs.master_id)
            master_bpm = _sync_bpm_base(master_state) * master_rate
            follower_native = _sync_bpm_base(follower_state)

            # Track master BPM changes (pitch fader moved)
            if master_bpm > 0 and follower_native > 0:
                ideal = max(0.5, min(2.0, master_bpm / follower_native))
                if abs(ideal - fs.nominal_rate) > 1e-4:
                    fs.set_nominal_rate(ideal)

            # PLL only while both decks are playing
            if (follower_state.status != DeckStatus.PLAYING
                    or master_state.status != DeckStatus.PLAYING):
                # While paused: keep rate applied so resume is instant
                _apply_follower_rate(follower_id, fs, follower_native, None)
                continue

            if not master_grid or not follower_grid:
                # No beatgrid: BPM-only sync, no phase correction
                _apply_follower_rate(follower_id, fs, follower_native, None)
                continue

            master_pos   = self._engine._get_deck(fs.master_id).position_s
            follower_pos = self._engine._get_deck(follower_id).position_s

            master_phase   = _sync_phase_at(master_state, master_pos)
            follower_phase = _sync_phase_at(follower_state, follower_pos)
            phase_err      = master_phase - follower_phase
            phase_err     -= round(phase_err)   # normalize to (-0.5, 0.5]

            correction = fs.pll.update(phase_err, dt, master_bpm, follower_native)
            if abs(correction) > 0.002:
                fs.absorb_correction(correction, dt)
            target_rate = fs.base_rate * (1.0 + correction)
            max_step = 0.0010
            delta = max(-max_step, min(max_step, target_rate - fs.applied_rate))
            new_rate = fs.applied_rate + delta
            fs.applied_rate = new_rate

            self._engine.set_playback_rate(follower_id, new_rate)
            self._engine._get_deck(follower_id).pitch_factor = new_rate

            # Display the nominal sync BPM (base_rate), not the instantaneous
            # PLL-corrected rate, so the UI readout doesn't oscillate.
            bpm_live = follower_native * fs.base_rate
            pitch_pct = 12.0 * math.log2(fs.base_rate) if fs.base_rate > 0 else 0.0

            dl = self._decks[follower_id]
            with dl.lock:
                old = dl.state
                dl.state = DeckState(
                    id=old.id, status=old.status, track=old.track,
                    position_s=old.position_s, pitch_pct=pitch_pct,
                    bpm_live=bpm_live, stems=old.stems,
                    stems_status=old.stems_status, loop=old.loop,
                    cue_points=old.cue_points,
                    sync_enabled=old.sync_enabled,
                    sync_master=old.sync_master,
                    beatgrid=old.beatgrid,
                    sync_phase_error=phase_err,
                    metrics=old.metrics, auto_markers=old.auto_markers,
                )
            self._push_states()

        # Push live BPM to FX engine so beat-synced effects track tempo changes
        fx_target = self._fx_state.target
        bpm: Optional[float] = None
        for did in (("A",) if fx_target == FX_TARGET_A
                    else ("B",) if fx_target == FX_TARGET_B
                    else ("A", "B")):
            st = self.get_state(did)
            b  = st.bpm_live or (st.track.bpm if st.track else None)
            if b and b > 0:
                bpm = b
                break
        if bpm:
            self._engine.fx_set_bpm(bpm)
        wrekk_target = self._fx_state.wrekk_target
        wrekk_bpm: Optional[float] = None
        for did in (("A",) if wrekk_target == FX_TARGET_A
                    else ("B",) if wrekk_target == FX_TARGET_B
                    else ("A", "B")):
            st = self.get_state(did)
            b = st.bpm_live or (st.track.bpm if st.track else None)
            if b and b > 0:
                wrekk_bpm = b
                break
        if wrekk_bpm:
            self._engine.wrekk_fx_set_bpm(wrekk_bpm)

    # ── FX control ────────────────────────────────────────────────────────────

    def get_fx_state(self) -> FXState:
        ready, status = self._wrekk_target_stems_ready(self._fx_state.wrekk_target)
        return _dc_replace(self._fx_state, wrekk_stems_ready=ready, wrekk_stems_status=status)

    def _wrekk_target_stems_ready(self, target: int) -> tuple[bool, str]:
        decks = ("A",) if target == FX_TARGET_A else ("B",) if target == FX_TARGET_B else ("A", "B")
        missing = []
        for did in decks:
            st = self.get_state(did)
            if not st or not st.stems or st.stems_status != "ready":
                missing.append(did)
        if not missing:
            return True, ""
        if len(decks) == 2 and len(missing) == 1:
            return True, f"STEMS REQUIRED: deck {missing[0]} skipped"
        return False, "STEMS REQUIRED"

    def set_fx_enabled(self, enabled: bool) -> None:
        if self._fx_state.fx_bank == FX_BANK_WREKK:
            ready, _ = self._wrekk_target_stems_ready(self._fx_state.wrekk_target)
            active = bool(enabled) and ready
            self._fx_state = _dc_replace(self._fx_state, wrekk_enabled=active)
            self._engine.wrekk_fx_set_enabled(active)
        else:
            self._fx_state = _dc_replace(self._fx_state, enabled=bool(enabled))
            self._engine.fx_set_enabled(enabled)

    def set_fx_bank(self, bank: str) -> None:
        bank = FX_BANK_WREKK if bank == FX_BANK_WREKK else FX_BANK_NORMAL
        self._fx_state = _dc_replace(self._fx_state, fx_bank=bank)
        if bank == FX_BANK_WREKK:
            ready, _ = self._wrekk_target_stems_ready(self._fx_state.wrekk_target)
            if not ready and self._fx_state.wrekk_enabled:
                self._fx_state = _dc_replace(self._fx_state, wrekk_enabled=False)
            self._engine.fx_set_enabled(False)
            self._engine.wrekk_fx_set_enabled(self._fx_state.wrekk_enabled and ready)
        else:
            self._engine.wrekk_fx_set_enabled(False)
            self._engine.fx_set_enabled(self._fx_state.enabled)

    def set_fx_type(self, fx_type: int) -> None:
        self._fx_state = _dc_replace(self._fx_state, fx_type=fx_type)
        self._engine.fx_set_type(fx_type)

    def set_fx_target(self, target: int) -> None:
        self._fx_state = _dc_replace(self._fx_state, target=target)
        self._engine.fx_set_target(target)

    def set_fx_wet(self, wet: float) -> None:
        self._fx_state = _dc_replace(self._fx_state, wet=wet)
        self._engine.fx_set_wet(wet)

    def set_fx_depth(self, depth: float) -> None:
        self._fx_state = _dc_replace(self._fx_state, depth=depth)
        self._engine.fx_set_depth(depth)

    def set_fx_feedback(self, feedback: float) -> None:
        self._fx_state = _dc_replace(self._fx_state, feedback=feedback)
        self._engine.fx_set_feedback(feedback)

    def set_fx_time_division(self, td: float) -> None:
        self._fx_state = _dc_replace(self._fx_state, time_division=td)
        self._engine.fx_set_time_division(td)

    def set_fx_color(self, color: float) -> None:
        self._fx_state = _dc_replace(self._fx_state, color=color)
        self._engine.fx_set_color(color)

    def set_wrekk_fx_type(self, fx_type: int) -> None:
        self._fx_state = _dc_replace(self._fx_state, wrekk_fx_type=fx_type)
        self._engine.wrekk_fx_set_type(fx_type)

    def set_wrekk_fx_target(self, target: int) -> None:
        self._fx_state = _dc_replace(self._fx_state, wrekk_target=target)
        self._engine.wrekk_fx_set_target(target)
        ready, _ = self._wrekk_target_stems_ready(target)
        if self._fx_state.wrekk_enabled and not ready:
            self._fx_state = _dc_replace(self._fx_state, wrekk_enabled=False)
            self._engine.wrekk_fx_set_enabled(False)

    def set_wrekk_fx_stem_target(self, target: int) -> None:
        self._fx_state = _dc_replace(self._fx_state, wrekk_stem_target=target)
        self._engine.wrekk_fx_set_stem_target(target)

    def set_wrekk_fx_wet(self, wet: float) -> None:
        self._fx_state = _dc_replace(self._fx_state, wrekk_wet=wet)
        self._engine.wrekk_fx_set_wet(wet)

    def set_wrekk_fx_depth(self, depth: float) -> None:
        self._fx_state = _dc_replace(self._fx_state, wrekk_depth=depth)
        self._engine.wrekk_fx_set_depth(depth)

    def set_wrekk_fx_feedback(self, feedback: float) -> None:
        self._fx_state = _dc_replace(self._fx_state, wrekk_feedback=feedback)
        self._engine.wrekk_fx_set_feedback(feedback)

    def set_wrekk_fx_time_division(self, td: float) -> None:
        self._fx_state = _dc_replace(self._fx_state, wrekk_time_division=td)
        self._engine.wrekk_fx_set_time_division(td)

    def set_wrekk_fx_color(self, color: float) -> None:
        self._fx_state = _dc_replace(self._fx_state, wrekk_color=color)
        self._engine.wrekk_fx_set_color(color)

    # ── Smart CFX / WREKK mode ───────────────────────────────────────────────

    def set_smart_cfx_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._smart_cfx_enabled == enabled:
            return
        self._smart_cfx_enabled = enabled
        if enabled:
            for deck_id in ("A", "B"):
                self._normal_channel_filters[deck_id] = self._engine.get_channel_filter(deck_id)
                self._engine.set_channel_filter(deck_id, 0.0)
                self._apply_stem_layer(deck_id)
        else:
            for deck_id in ("A", "B"):
                self._apply_stem_layer(deck_id)
                self._engine.set_channel_filter(deck_id, self._normal_channel_filters[deck_id])

    def toggle_smart_cfx(self) -> bool:
        self.set_smart_cfx_enabled(not self._smart_cfx_enabled)
        return self._smart_cfx_enabled

    def get_smart_cfx_enabled(self) -> bool:
        return self._smart_cfx_enabled

    # ── headphone / monitor CUE ───────────────────────────────────────────────

    def toggle_monitor_cue(self, target: str) -> None:
        s = self._monitor_cue
        if target == "A":
            self._monitor_cue = _dc_replace(s, cue_deck_a=not s.cue_deck_a)
            self._engine.set_headphone_cue("A", self._monitor_cue.cue_deck_a)
        elif target == "B":
            self._monitor_cue = _dc_replace(s, cue_deck_b=not s.cue_deck_b)
            self._engine.set_headphone_cue("B", self._monitor_cue.cue_deck_b)
        elif target == "master":
            self._monitor_cue = _dc_replace(s, cue_master=not s.cue_master)
            self._engine.set_headphone_cue_master(self._monitor_cue.cue_master)

    def set_monitor_cue(self, target: str, enabled: bool) -> None:
        s = self._monitor_cue
        if target == "A":
            self._monitor_cue = _dc_replace(s, cue_deck_a=enabled)
            self._engine.set_headphone_cue("A", enabled)
        elif target == "B":
            self._monitor_cue = _dc_replace(s, cue_deck_b=enabled)
            self._engine.set_headphone_cue("B", enabled)
        elif target == "master":
            self._monitor_cue = _dc_replace(s, cue_master=enabled)
            self._engine.set_headphone_cue_master(enabled)

    def get_monitor_state(self) -> MonitorCueState:
        return self._monitor_cue

    def set_headphone_mix(self, value: float) -> None:
        """0.0 = full CUE signal, 1.0 = full master blend."""
        v = max(0.0, min(1.0, value))
        self._monitor_cue = _dc_replace(self._monitor_cue, headphone_mix=v)
        self._engine.set_headphone_mix(v)

    def set_headphone_level(self, value: float) -> None:
        v = max(0.0, min(2.0, value))
        self._monitor_cue = _dc_replace(self._monitor_cue, headphone_level=v)
        self._engine.set_headphone_level(v)

    def set_channel_filter(self, deck_id: str, value: float) -> None:
        if self._smart_cfx_enabled:
            return
        v = max(-1.0, min(1.0, value))
        self._normal_channel_filters[deck_id] = v
        self._engine.set_channel_filter(deck_id, v)

    # ── crossfader / master ───────────────────────────────────────────────────

    def set_crossfader(self, value: float) -> None:
        self._engine.set_crossfader(value)

    def set_master_gain(self, gain: float) -> None:
        self._engine.set_master_gain(gain)

    def set_eq(self, deck_id: str, band: str, gain_db: float) -> None:
        """band: 'low' | 'mid' | 'high', gain_db: ±12 dB."""
        if self._smart_cfx_enabled:
            return
        self._engine.set_eq(deck_id, band, gain_db)

    def set_channel_volume(self, deck_id: str, vol: float) -> None:
        """vol: 0.0 (silent) → 1.0 (unity). Linear channel fader."""
        self._engine.set_channel_gain(deck_id, float(vol))

    def set_pregain(self, deck_id: str, gain: float) -> None:
        """gain: 0.0 → 4.0 linear. 1.0 = unity, 2.0 = +6 dB. Trim knob."""
        if self._smart_cfx_enabled:
            return
        self._engine.set_pregain(deck_id, float(gain))

    # ── state access ──────────────────────────────────────────────────────────

    def get_state(self, deck_id: str) -> DeckState:
        dl = self._decks[deck_id]
        with dl.lock:
            return dl.state

    def get_all_states(self) -> dict[str, DeckState]:
        return {did: self.get_state(did) for did in self._decks}

    def get_harmonic_compatibility(self) -> float | None:
        sa = self.get_state("A")
        sb = self.get_state("B")
        return sa.harmonic_compatibility(sb)

    # ── internals ─────────────────────────────────────────────────────────────

    def _set_state(
        self,
        dl:          _DeckLock,
        deck_id:     DeckID,
        status:      DeckStatus,
        *,
        track:        object               = _UNSET,
        position_s:   Optional[float]      = None,
        pitch_pct:    Optional[float]      = None,
        bpm_live:     Optional[float]      = None,
        stems_status: Optional[str]        = None,
        loop:         Optional[LoopState]  = None,
        cue_points:   Optional[tuple]      = None,
        metrics:      Optional[DeckMetrics] = None,
        auto_markers: Optional[tuple]      = None,
    ) -> None:
        """Rebuild DeckState in-place. Must be called with dl.lock held."""
        old = dl.state
        dl.state = DeckState(
            id           = deck_id,
            status       = status,
            track        = track        if track     is not _UNSET else old.track,
            position_s   = position_s   if position_s   is not None else old.position_s,
            pitch_pct    = pitch_pct    if pitch_pct    is not None else old.pitch_pct,
            bpm_live     = bpm_live     if bpm_live     is not None else old.bpm_live,
            stems        = old.stems,
            stems_status = stems_status if stems_status is not None else old.stems_status,
            loop         = loop         if loop         is not None else old.loop,
            cue_points   = cue_points   if cue_points   is not None else old.cue_points,
            sync_enabled     = old.sync_enabled,
            sync_master      = old.sync_master,
            beatgrid         = old.beatgrid,
            sync_phase_error = old.sync_phase_error,
            metrics      = metrics      if metrics      is not None else old.metrics,
            auto_markers = auto_markers if auto_markers is not None else old.auto_markers,
        )
        self._push_states()

    def _push_states(self) -> None:
        states = {did: dl.state for did, dl in self._decks.items()}
        self._engine.publish_state(states)
