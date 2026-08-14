"""
Deck data models — pure frozen dataclasses, no audio processing.

DeckState is the single source of truth for what a deck IS.
The audio engine maintains its own mutable mirror for real-time access;
DeckState is rebuilt and published whenever something changes.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

__all__ = [
    "DeckID",
    "DeckStatus",
    "HarmonicKey",
    "TrackInfo",
    "CuePoint",
    "LoopState",
    "PhraseMark",
    "BeatGrid",
    "SpectralBands",
    "LoudnessMeasure",
    "StemState",
    "DeckMetrics",
    "DeckState",
    "WaveformData",
    "MarkerType",
    "AutoMarker",
    "MARKER_MIN_CONFIDENCE",
    "STEM_NAMES",
    "STEM_GAIN_MAX",
]

STEM_NAMES:     tuple[str, ...] = ("vocals", "drums", "bass", "other")
STEM_GAIN_MAX:  float = 2.0   # unity = 1.0, max boost = +6dBFS
MARKER_MIN_CONFIDENCE: float = 0.70


# ─── enumerations ─────────────────────────────────────────────────────────────

class DeckID(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class MarkerType(str, Enum):
    """All auto-detected marker categories."""
    # Structural
    FIRST_BEAT     = "first_beat"
    FIRST_DOWNBEAT = "first_downbeat"
    PHRASE         = "phrase"
    # Mix points
    MIX_IN         = "mix_in"
    MIX_OUT        = "mix_out"
    # Energy transitions
    DROP           = "drop"
    BREAKDOWN      = "breakdown"
    # Stem activity
    VOCAL_IN       = "vocal_in"
    VOCAL_OUT      = "vocal_out"
    RHYTHM_IN      = "rhythm_in"
    BASS_IN        = "bass_in"
    BASS_OUT       = "bass_out"
    KICK_IN        = "kick_in"
    KICK_OUT       = "kick_out"
    TOP_IN         = "top_in"
    TOP_OUT        = "top_out"
    # WREKK performance
    WREKK_TOP      = "wrekk_top"
    WREKK_RHYTHM   = "wrekk_rhythm"
    VOCAL_GHOST    = "vocal_ghost"
    DECONSTRUCT    = "deconstruct"
    REBUILD        = "rebuild"
    BASS_LOCK      = "bass_lock"
    WASH           = "wash"
    DRUM_SWAP      = "drum_swap"
    # Composite
    SWITCH_POINT   = "switch_point"


@dataclass(frozen=True)
class AutoMarker:
    """
    A detected DJ cue point, switch point, or WREKK performance marker.

    Generated offline by AutoMarkerDetector during WREKKED preparation.
    Stored in analysis/markers.json inside the .wrk ZIP.
    Never affects the audio callback.
    """
    id:            str
    type:          MarkerType
    label:         str
    position_s:    float
    confidence:    float           # 0.0–1.0
    source:        str = "auto"    # "auto" | "user"
    user_modified: bool = False
    reason:        str = ""
    phrase_index:  int | None = None
    beat_index:    int | None = None
    # (vocals, drums, bass, other) fractions at the marker position
    stem_profile:  tuple | None = None
    energy_before: float | None = None
    energy_after:  float | None = None
    category:      str | None = None       # "primary" | "wrekk" | "guide" | "legacy"
    family:        str | None = None       # "structural" | "opportunity"
    stem_targets:  tuple | None = None
    evidence:      dict | None = None
    related_events: tuple | None = None
    live_visibility: str | None = None     # "default" | "expanded" | "debug"
    hidden:        bool = False

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "type":          self.type.value,
            "label":         self.label,
            "position_s":    self.position_s,
            "confidence":    self.confidence,
            "source":        self.source,
            "user_modified": self.user_modified,
            "reason":        self.reason,
            "phrase_index":  self.phrase_index,
            "beat_index":    self.beat_index,
            "stem_profile":  list(self.stem_profile) if self.stem_profile else None,
            "energy_before": self.energy_before,
            "energy_after":  self.energy_after,
            "category":      self.category,
            "family":        self.family,
            "stem_targets":  list(self.stem_targets) if self.stem_targets else None,
            "evidence":      self.evidence,
            "related_events": list(self.related_events) if self.related_events else None,
            "live_visibility": self.live_visibility,
            "hidden":        self.hidden,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AutoMarker":
        sp = d.get("stem_profile")
        raw_type = str(d.get("type", "") or "")
        try:
            marker_type = MarkerType(raw_type)
        except ValueError:
            marker_type = MarkerType.PHRASE
        targets = d.get("stem_targets")
        related = d.get("related_events")
        return cls(
            id            = d["id"],
            type          = marker_type,
            label         = d.get("label") or raw_type.upper().replace("_", " "),
            position_s    = float(d["position_s"]),
            confidence    = float(d.get("confidence", 0.5)),
            source        = d.get("source", "auto"),
            user_modified = bool(d.get("user_modified", False)),
            reason        = d.get("reason", ""),
            phrase_index  = d.get("phrase_index"),
            beat_index    = d.get("beat_index"),
            stem_profile  = tuple(sp) if sp else None,
            energy_before = d.get("energy_before"),
            energy_after  = d.get("energy_after"),
            category      = d.get("category"),
            family        = d.get("family"),
            stem_targets  = tuple(targets) if targets else None,
            evidence      = dict(d.get("evidence") or {}) if isinstance(d.get("evidence"), dict) else None,
            related_events = tuple(related) if related else None,
            live_visibility = d.get("live_visibility"),
            hidden        = bool(d.get("hidden", False)),
        )


class DeckStatus(str, Enum):
    EMPTY    = "empty"    # no track loaded
    LOADING  = "loading"  # reading audio from disk
    READY    = "ready"    # loaded, not playing
    PLAYING  = "playing"
    PAUSED   = "paused"


# ─── harmonic key (Camelot wheel) ─────────────────────────────────────────────

# Camelot wheel: number 1-12, mode A=minor / B=major.
# Standard Camelot assignments (8A=Am, 8B=C) so keys match Rekordbox /
# Mixed In Key.  (number, mode) → canonical name first, then enharmonics.
_KEY_FROM_CAMELOT: dict[tuple[int, str], str] = {
    (1, "A"): "Abm",  (1, "B"): "B",
    (2, "A"): "Ebm",  (2, "B"): "F#",
    (3, "A"): "Bbm",  (3, "B"): "Db",
    (4, "A"): "Fm",   (4, "B"): "Ab",
    (5, "A"): "Cm",   (5, "B"): "Eb",
    (6, "A"): "Gm",   (6, "B"): "Bb",
    (7, "A"): "Dm",   (7, "B"): "F",
    (8, "A"): "Am",   (8, "B"): "C",
    (9, "A"): "Em",   (9, "B"): "G",
    (10,"A"): "Bm",   (10,"B"): "D",
    (11,"A"): "F#m",  (11,"B"): "A",
    (12,"A"): "C#m",  (12,"B"): "E",
}

_CAMELOT_FROM_KEY: dict[str, tuple[int, str]] = {
    name: camelot for camelot, name in _KEY_FROM_CAMELOT.items()
}
_CAMELOT_FROM_KEY.update({
    # enharmonic aliases
    "G#m": (1, "A"),  "D#m": (2, "A"),  "A#m": (3, "A"),
    "Gbm": (11,"A"),  "Dbm": (12,"A"),
    "Gb":  (2, "B"),  "C#":  (3, "B"),  "G#":  (4, "B"),
    "D#":  (5, "B"),  "A#":  (6, "B"),
})


@dataclass(frozen=True)
class HarmonicKey:
    """
    Key in Camelot wheel notation (e.g. '8A', '3B').

    Compatibility rules:
      1.0  — same key (perfect)
      0.85 — ±1 number, same mode (energy change, very common in DJ sets)
      0.75 — same number, opposite mode (relative major/minor)
      0.50 — ±2 numbers, same mode (works with care)
      0.25 — ±3 numbers, same mode (used for tension)
      0.0  — tritone or unrelated (avoid)
    """

    number: int   # 1–12
    mode:   str   # "A" | "B"

    def __post_init__(self) -> None:
        if not (1 <= self.number <= 12):
            raise ValueError(f"Camelot number must be 1-12, got {self.number}")
        if self.mode not in ("A", "B"):
            raise ValueError(f"Camelot mode must be A or B, got {self.mode}")

    @classmethod
    def from_key_name(cls, key: str) -> "HarmonicKey | None":
        """Parse from music key name like 'Am', 'F#', 'Bbm', 'Db'."""
        entry = _CAMELOT_FROM_KEY.get(key)
        if entry is None:
            return None
        return cls(*entry)

    @classmethod
    def from_camelot(cls, s: str) -> "HarmonicKey | None":
        """Parse '8A', '3B', etc."""
        s = s.strip()
        if len(s) < 2:
            return None
        try:
            mode = s[-1].upper()
            num  = int(s[:-1])
            return cls(num, mode)
        except (ValueError, IndexError):
            return None

    def compatibility(self, other: "HarmonicKey") -> float:
        if self.number == other.number:
            return 1.0 if self.mode == other.mode else 0.75
        diff = min(
            abs(self.number - other.number),
            12 - abs(self.number - other.number),   # wrap around
        )
        if self.mode != other.mode:
            diff += 0.5   # mode mismatch adds penalty
        if diff <= 1:   return 0.85
        if diff <= 2:   return 0.50
        if diff <= 3:   return 0.25
        return 0.0

    @property
    def key_name(self) -> str:
        return _KEY_FROM_CAMELOT.get((self.number, self.mode), str(self))

    def __str__(self) -> str:
        return f"{self.number}{self.mode}"


# ─── track info ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TrackInfo:
    path:        Path
    title:       str
    artist:      str
    duration_s:  float
    sample_rate: int
    channels:    int
    bpm:         float | None   # None = not analyzed yet
    key:         HarmonicKey | None
    file_hash:   str            # SHA256(path+mtime) — matches stems cache key
    artwork_data: bytes | None = None  # cover art bytes (JPEG/PNG)


# ─── transport sub-states ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class PhraseMark:
    position_sec: float
    phrase_length: int = 8
    energy_level: float = 0.5


@dataclass(frozen=True)
class BeatGrid:
    """
    Beat grid for a loaded track — anchors phase-accurate sync between decks.

    bpm            : global/primary BPM (DJ range 80-185, used as fallback)
    first_beat_s   : timestamp of the first detected beat in the track
    confidence     : 0.0 (no beats detected) → 1.0 (many consistent beats)
    source         : "analyzed" | "metadata" | "manual" | "imported"
    user_adjusted  : True if the user has manually corrected the grid
    beats          : explicit beat timestamps in seconds; () = constant-BPM grid
    dynamic_tempo  : True when local BPM varies by more than 5% across the track
    bpm_min        : lowest local BPM seen (5th percentile); None if constant
    bpm_max        : highest local BPM seen (95th percentile); None if constant
    """
    bpm:           float
    first_beat_s:  float
    confidence:    float = 0.5
    source:        str   = "analyzed"
    user_adjusted: bool  = False
    beats:         tuple[float, ...] = ()
    downbeats:     tuple[float, ...] = ()
    phrase_markers: tuple[PhraseMark, ...] = ()
    swing_factor:  float = 0.0
    beat_period_ms: float = 0.0
    schema_version: int = 1
    analysis_model: str = ""
    low_confidence: bool = False
    dynamic_tempo: bool  = False
    bpm_min:       float | None = None
    bpm_max:       float | None = None

    @property
    def beat_period_s(self) -> float:
        return 60.0 / max(self.bpm, 1.0)

    @property
    def bpm_display(self) -> str:
        """Human-readable BPM: 'min–max' for dynamic grids, else global BPM."""
        if self.dynamic_tempo and self.bpm_min is not None and self.bpm_max is not None:
            return f"{self.bpm_min:.0f}–{self.bpm_max:.0f}"
        return f"{self.bpm:.1f}"

    def local_bpm_at(self, pos_s: float) -> float:
        """Local BPM at pos_s from inter-beat spacing; falls back to global BPM."""
        b = self.beats
        if len(b) < 2:
            return self.bpm
        i = bisect.bisect_right(b, pos_s) - 1
        if 0 <= i < len(b) - 1:
            return 60.0 / max(b[i + 1] - b[i], 1e-4)
        if i < 0:
            return 60.0 / max(b[1] - b[0], 1e-4)
        return 60.0 / max(b[-1] - b[-2], 1e-4)

    def phase_at(self, pos_s: float) -> float:
        """Beat phase in [0.0, 1.0) at the given track position."""
        b = self.beats
        if len(b) >= 2:
            i = bisect.bisect_right(b, pos_s) - 1
            if 0 <= i < len(b) - 1:
                dt = b[i + 1] - b[i]
                return ((pos_s - b[i]) / max(dt, 1e-9)) % 1.0
            if i < 0:
                dt = b[1] - b[0]
                return ((pos_s - b[0]) / max(dt, 1e-9)) % 1.0
            dt = b[-1] - b[-2]
            return ((pos_s - b[-1]) / max(dt, 1e-9)) % 1.0
        # Fallback: constant-BPM grid
        return ((pos_s - self.first_beat_s) / self.beat_period_s) % 1.0

    def snap_to_phase(self, target_phase: float, near_pos_s: float) -> float:
        """
        Return the position nearest to near_pos_s whose beat phase equals
        target_phase.  Result is always ≥ 0.0.
        """
        b = self.beats
        if len(b) >= 2:
            i = bisect.bisect_right(b, near_pos_s) - 1
            best_pos  = None
            best_dist = float("inf")
            for ci in range(max(0, i - 1), min(len(b) - 1, i + 3)):
                dt        = b[ci + 1] - b[ci]
                candidate = b[ci] + target_phase * dt
                dist      = abs(candidate - near_pos_s)
                if dist < best_dist:
                    best_dist = dist
                    best_pos  = candidate
            if best_pos is not None:
                return max(0.0, best_pos)
        # Fallback: constant-BPM
        period = self.beat_period_s
        n      = round((near_pos_s - self.first_beat_s) / period - target_phase)
        return max(0.0, self.first_beat_s + (n + target_phase) * period)


@dataclass(frozen=True)
class CuePoint:
    position_s: float
    label:      str
    color:      str
    type:       str   # "hot_cue" | "loop_in" | "loop_out" | "grid"


@dataclass(frozen=True)
class LoopState:
    active:  bool
    start_s: float
    end_s:   float
    bars:    int     # 1, 2, 4, 8, 16


# ─── metering / analysis snapshots ────────────────────────────────────────────

@dataclass(frozen=True)
class SpectralBands:
    """RMS energy per frequency band, in dBFS. Updated ~10Hz from audio thread."""
    sub:   float   # 20–80 Hz
    bass:  float   # 80–300 Hz
    mids:  float   # 300–3000 Hz
    highs: float   # 3000–20000 Hz

    @classmethod
    def silent(cls) -> "SpectralBands":
        return cls(-96.0, -96.0, -96.0, -96.0)


@dataclass(frozen=True)
class LoudnessMeasure:
    """ITU-R BS.1770-4 loudness. Updated ~10Hz from audio thread."""
    momentary_lufs:  float   # 400 ms integration
    short_term_lufs: float   # 3 s integration
    true_peak_dbfs:  float   # sample-peak (no oversampling in MVP)

    @classmethod
    def silence(cls) -> "LoudnessMeasure":
        return cls(-math.inf, -math.inf, -math.inf)


# ─── stem state ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StemState:
    """
    State of one stem on a deck.
    gain range: 0.0 (muted) – 1.0 (unity) – 2.0 (+6 dBFS boost).
    muted is explicit mute (gain fader preserved); solo mutes all other stems.
    """
    gain:     float          # 0.0–2.0
    muted:    bool
    solo:     bool
    lufs:     LoudnessMeasure | None
    spectral: SpectralBands  | None

    @classmethod
    def default(cls) -> "StemState":
        return cls(gain=1.0, muted=False, solo=False, lufs=None, spectral=None)

    @property
    def effective_gain(self) -> float:
        """Actual gain applied in the audio chain (mute overrides fader)."""
        return 0.0 if self.muted else self.gain


# ─── deck metrics (aggregate) ─────────────────────────────────────────────────

@dataclass(frozen=True)
class DeckMetrics:
    """
    Aggregate real-time analysis for one deck.
    Replaced atomically by the audio engine ~10Hz.
    """
    lufs:       LoudnessMeasure
    spectral:   SpectralBands
    phase_corr: float | None = None
    spectrum:   tuple[float, ...] = field(default_factory=lambda: tuple(-96.0 for _ in range(16)))


# ─── waveform visualization data ─────────────────────────────────────────────

@dataclass
class WaveformData:
    """
    Precomputed visualization arrays for one loaded track.

    peaks:       (N,) float32  — amplitude envelope 0-1, one value per display column
    colors:      (N, 3) uint8  — spectral RGB tint (bass=warm, mid=green, high=blue)
    beats:       sorted beat/transient positions in seconds
    stem_energy: (N, 4) float32 — per-column mean |amplitude| per stem
                 columns: vocals(0), drums(1), bass(2), other(3)
                 None until stem analysis completes.
    zoom_peaks:  (M,) float32 — high-res peaks at zoom_chunk samples/col
    zoom_colors: (M, 3) uint8 — spectral colors for zoom view
    zoom_chunk:  samples per zoom column (256 → ~172 cols/sec at 44100 Hz)

    Set incrementally: peaks+colors at load, beats by analysis worker,
    stem_energy when Demucs separation finishes.
    """
    peaks:       "np.ndarray"                    # (N,) float32
    colors:      "np.ndarray"                    # (N, 3) uint8
    beats:       tuple[float, ...] = ()
    stem_energy: "np.ndarray | None" = None      # (N, 4) float32
    stem_horizon: dict | None = None             # analysis/stem_horizon.json
    zoom_peaks:  "np.ndarray | None" = None      # (M,) float32
    zoom_colors: "np.ndarray | None" = None      # (M, 3) uint8
    zoom_chunk:  int = 256


# ─── deck state ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeckState:
    """
    Complete snapshot of one deck. Immutable — replaced on every state change.
    The audio engine owns the mutable playback state; DeckState is what the
    rest of the application (UI, hardware, library) reads.
    """
    id:     DeckID
    status: DeckStatus

    # Track
    track: TrackInfo | None

    # Playback position and pitch
    position_s:   float   # current read head in seconds
    pitch_pct:    float   # -16.0 to +16.0 semitones
    bpm_live:     float   # track BPM × pitch factor

    # Stems: one StemState per name in STEM_NAMES
    stems:        dict[str, StemState]
    stems_status: str     # StemStatus value from wrekker.stems

    # Transport
    loop:       LoopState        | None
    cue_points: tuple[CuePoint, ...]

    # Sync
    sync_enabled:     bool
    sync_master:      bool
    beatgrid:         "BeatGrid | None" = None
    sync_phase_error: float | None      = None  # beats, (-0.5, 0.5] when synced

    # Analysis (may be None before first audio frame)
    metrics: DeckMetrics | None = None

    # Auto-detected markers (loaded from .wrk at track load time)
    auto_markers: tuple["AutoMarker", ...] = ()

    @property
    def dynamic_tempo(self) -> bool:
        """True when the loaded track has a variable-BPM beatgrid."""
        return self.beatgrid.dynamic_tempo if self.beatgrid else False

    @classmethod
    def empty(cls, deck_id: DeckID) -> "DeckState":
        return cls(
            id               = deck_id,
            status           = DeckStatus.EMPTY,
            track            = None,
            position_s       = 0.0,
            pitch_pct        = 0.0,
            bpm_live         = 0.0,
            stems            = {n: StemState.default() for n in STEM_NAMES},
            stems_status     = "none",
            loop             = None,
            cue_points       = (),
            sync_enabled     = False,
            sync_master      = False,
            beatgrid         = None,
            sync_phase_error = None,
            metrics          = None,
        )

    def with_stem(self, name: str, **kwargs) -> "DeckState":
        """Return a new DeckState with one stem field updated."""
        old = self.stems[name]
        new_stem = StemState(
            gain    = kwargs.get("gain",  old.gain),
            muted   = kwargs.get("muted", old.muted),
            solo    = kwargs.get("solo",  old.solo),
            lufs    = kwargs.get("lufs",  old.lufs),
            spectral= kwargs.get("spectral", old.spectral),
        )
        new_stems = {**self.stems, name: new_stem}
        return DeckState(
            id=self.id, status=self.status, track=self.track,
            position_s=self.position_s, pitch_pct=self.pitch_pct,
            bpm_live=self.bpm_live, stems=new_stems,
            stems_status=self.stems_status, loop=self.loop,
            cue_points=self.cue_points, sync_enabled=self.sync_enabled,
            sync_master=self.sync_master,
            beatgrid=self.beatgrid, sync_phase_error=self.sync_phase_error,
            metrics=self.metrics, auto_markers=self.auto_markers,
        )

    def harmonic_compatibility(self, other: "DeckState") -> float | None:
        """
        Compatibility score [0.0–1.0] between this deck's key and another's.
        Returns None if either deck has no key information.
        """
        if self.track is None or other.track is None:
            return None
        ka = self.track.key
        kb = other.track.key
        if ka is None or kb is None:
            return None
        return ka.compatibility(kb)


import math  # noqa: E402  (needed for LoudnessMeasure.silence())
