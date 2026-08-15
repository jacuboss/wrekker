"""
Offline beat tracking for WREKKED preparation.

This module wraps Beat This! (Foscarin, Schlüter, Widmer, ISMIR 2024) when the
package and checkpoint are available. It is intentionally offline-only: model
inference, phrase analysis, and heuristics run during `.wrk` preparation, never
inside the Rust audio callback.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
MODEL_NAME = "beat_this_v1"


@dataclass(frozen=True)
class PhraseMark:
    position_sec: float
    phrase_length: int  # bars: 8 or 16
    energy_level: float


@dataclass(frozen=True)
class BeatGrid:
    bpm: float
    bpm_variable: bool
    beats: list[float]
    downbeats: list[float]
    confidence: float
    phrase_markers: list[PhraseMark]
    beat_period_ms: float
    swing_factor: float
    analysis_model: str = MODEL_NAME
    analyzed_at: str = ""
    low_confidence: bool = False
    schema_version: int = SCHEMA_VERSION
    source: str = "beat_this"
    first_beat_s: float = 0.0
    dynamic_tempo: bool = False
    bpm_min: float | None = None
    bpm_max: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["model"] = self.analysis_model
        data["beatgrid_version"] = self.schema_version
        return data


class BeatTracker:
    """
    State of the art beat tracker usando Beat This! (Foscarin, Schlüter, Widmer
    - ISMIR 2024).

    Detecta beats, downbeats, BPM, y frases de 8/16 compases. Diseñado para
    correr OFFLINE durante el análisis WREKKED.
    """

    _model_lock = threading.Lock()
    _model = None
    _model_key: tuple[str, str, bool] | None = None
    _model_error: Exception | None = None

    def __init__(
        self,
        checkpoint: str = "final0",
        device: str = "cpu",
        use_dbn: bool = False,
    ) -> None:
        self.checkpoint = checkpoint
        self.device = device
        self.use_dbn = use_dbn
        if checkpoint == "final0":
            try:
                from wrekker.setup.model_registry import model_path
                local_checkpoint = model_path("beat_this") / "final0.ckpt"
                if local_checkpoint.exists():
                    checkpoint = str(local_checkpoint)
                    self.checkpoint = checkpoint
            except Exception:
                pass
        self._ensure_model(checkpoint, device, use_dbn)

    def analyze(self, audio_path: str) -> BeatGrid:
        """
        Analyze an audio file and return a schema-v2 BeatGrid.

        Beat This! is preferred. If its checkpoint is unavailable, this falls
        back to Wrekker's existing librosa tracker so preparation still succeeds.
        """
        path = Path(audio_path)
        from wrekker.audio import load_audio

        audio, sr = load_audio(path)
        return self.analyze_audio(audio, sr, source=str(path))

    def analyze_audio(
        self,
        audio: np.ndarray,
        sr: int,
        source: str = "",
    ) -> BeatGrid:
        """
        Analyze already-decoded audio and return a schema-v2 BeatGrid.

        Preparation uses this entry point so the source file is decoded once and
        shared with waveform, key, loudness, Beat This!, stems, and packing.
        """
        path = Path(source) if source else Path("<decoded-audio>")
        beats: list[float]
        downbeats: list[float]

        if self._model is not None:
            beats_arr, downbeats_arr = self._model(audio, sr)
            beats = self._clean_times(beats_arr)
            downbeats = self._clean_times(downbeats_arr)
        else:
            log.warning("Beat This! unavailable, falling back to librosa: %s", self._model_error)
            from wrekker.core.transport import _detect_bpm_beats

            bpm_hint, raw_beats = _detect_bpm_beats(audio, sr, None)
            beats = self._clean_times(raw_beats)
            downbeats = beats[::4]
            if not beats and bpm_hint:
                duration = audio.shape[0] / sr
                period = 60.0 / bpm_hint
                beats = [i * period for i in range(int(duration / period))]
                downbeats = beats[::4]

        stats = self._tempo_stats(beats)
        bpm = stats["bpm"]
        beat_period_ms = 60000.0 / bpm if bpm > 0 else 0.0
        swing_factor = self._swing_factor(beats, bpm)
        confidence = self._confidence(beats, downbeats, stats, swing_factor)
        phrase_markers = self._phrase_markers(audio, sr, downbeats, beats)
        low_confidence = confidence < 0.6
        if low_confidence:
            log.warning("Low beatgrid confidence %.2f for %s", confidence, path)

        analyzed_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return BeatGrid(
            bpm=bpm,
            bpm_variable=stats["bpm_variable"],
            beats=beats,
            downbeats=downbeats,
            confidence=confidence,
            phrase_markers=phrase_markers,
            beat_period_ms=beat_period_ms,
            swing_factor=swing_factor,
            analyzed_at=analyzed_at,
            low_confidence=low_confidence,
            first_beat_s=beats[0] if beats else 0.0,
            dynamic_tempo=stats["bpm_variable"],
            bpm_min=stats["bpm_min"],
            bpm_max=stats["bpm_max"],
        )

    @classmethod
    def _ensure_model(cls, checkpoint: str, device: str, use_dbn: bool) -> None:
        key = (checkpoint, device, use_dbn)
        with cls._model_lock:
            if cls._model is not None and (cls._model_key is None or cls._model_key == key):
                return
            if cls._model_error is not None and cls._model_key == key:
                return
            try:
                import torch
                from beat_this.inference import Audio2Beats

                torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
                cls._model = Audio2Beats(checkpoint_path=checkpoint, device=device, dbn=use_dbn)
                cls._model_key = key
                cls._model_error = None
            except Exception as exc:
                cls._model_error = exc
                cls._model_key = key
                cls._model = None

    @staticmethod
    def _clean_times(values) -> list[float]:
        arr = np.asarray(values, dtype=np.float64).reshape(-1)
        if arr.size == 0:
            return []
        arr = arr[np.isfinite(arr)]
        arr = np.unique(np.maximum(arr, 0.0))
        return [float(round(v, 6)) for v in arr]

    @staticmethod
    def _tempo_stats(beats: list[float]) -> dict[str, float | bool | None]:
        if len(beats) < 2:
            return {"bpm": 0.0, "bpm_variable": False, "bpm_min": None, "bpm_max": None}

        intervals = np.diff(np.asarray(beats, dtype=np.float64))
        median_int = float(np.median(intervals))
        good = intervals[(intervals > 0.3 * median_int) & (intervals < 2.5 * median_int)]
        if len(good) == 0:
            good = intervals

        local_bpms = 60.0 / np.maximum(good, 1e-4)
        bpm = float(np.median(local_bpms))
        bpm_min = float(np.percentile(local_bpms, 5))
        bpm_max = float(np.percentile(local_bpms, 95))
        bpm_variable = (bpm_max - bpm_min) / max(bpm, 1.0) > 0.02
        return {
            "bpm": bpm,
            "bpm_variable": bool(bpm_variable),
            "bpm_min": bpm_min,
            "bpm_max": bpm_max,
        }

    @staticmethod
    def _swing_factor(beats: list[float], bpm: float) -> float:
        if len(beats) < 4 or bpm <= 0:
            return 0.0
        b = np.asarray(beats, dtype=np.float64)
        # For each consecutive triplet (i, i+1, i+2), measure how far beat[i+1]
        # deviates from the exact midpoint of beat[i] and beat[i+2].
        # This is local — no accumulated tempo drift across the track.
        # Straight = 0.0; 2:1 triplet swing ≈ 0.167 → scaled to ≈ 1.0.
        pair_spans = b[2:] - b[:-2]
        midpoints  = b[:-2] + pair_spans / 2
        deviations = np.abs(b[1:-1] - midpoints) / np.maximum(pair_spans, 1e-4)
        return float(max(0.0, min(1.0, float(np.median(deviations)) * 6.0)))

    @staticmethod
    def _confidence(
        beats: list[float],
        downbeats: list[float],
        stats: dict[str, float | bool | None],
        swing_factor: float,
    ) -> float:
        if len(beats) < 4 or not stats["bpm"]:
            return 0.2
        intervals = np.diff(np.asarray(beats, dtype=np.float64))
        median_int = float(np.median(intervals))
        jitter = float(np.median(np.abs(intervals - median_int)) / max(median_int, 1e-6))
        beat_density = min(1.0, len(beats) / 64.0)
        downbeat_score = min(1.0, len(downbeats) / max(1.0, len(beats) / 4.0))
        stability = max(0.0, 1.0 - min(1.0, jitter * 8.0))
        swing_penalty = max(0.75, 1.0 - swing_factor * 0.25)
        return float(max(0.0, min(1.0, (0.45 * stability + 0.35 * beat_density + 0.20 * downbeat_score) * swing_penalty)))

    def _phrase_markers(
        self,
        audio: "np.ndarray",
        sr: int,
        downbeats: list[float],
        beats: list[float],
    ) -> list[PhraseMark]:
        # Phrases are counted in bars, so they have to be measured on a
        # continuous grid: a section the detector found no beats in would
        # otherwise shift every later phrase by the number of missing bars.
        # Only these derived positions are bridged — the stored beats and
        # downbeats stay exactly as detected.
        from wrekker.core.deck import fill_beat_gaps

        if downbeats:
            bar_starts = list(fill_beat_gaps(downbeats))
        else:
            bar_starts = list(fill_beat_gaps(beats))[::4]
        if not bar_starts:
            return []

        energies = self._bar_energies(audio, sr, bar_starts)
        marks: list[PhraseMark] = []
        for i in range(0, len(bar_starts), 8):
            pos = bar_starts[i]
            energy = energies[i] if i < len(energies) else 0.5
            phrase_len = 8
            if i + 16 < len(bar_starts):
                e0 = float(np.mean(energies[i : i + 8])) if len(energies) >= i + 8 else energy
                e1 = float(np.mean(energies[i + 8 : i + 16])) if len(energies) >= i + 16 else energy
                if abs(e0 - e1) < 0.12:
                    phrase_len = 16
            marks.append(PhraseMark(
                position_sec=float(round(pos, 6)),
                phrase_length=phrase_len,
                energy_level=float(max(0.0, min(1.0, energy))),
            ))
        return marks

    @staticmethod
    def _bar_energies(audio: "np.ndarray", sr: int, bar_starts: list[float]) -> list[float]:
        try:
            mono = np.mean(audio, axis=1) if audio.ndim > 1 else audio
            rms = []
            for i, start in enumerate(bar_starts):
                end = bar_starts[i + 1] if i + 1 < len(bar_starts) else start + 2.0
                a = max(0, int(start * sr))
                if a >= len(mono):
                    rms.append(0.0)
                    continue
                b = min(len(mono), max(a + 1, int(end * sr)))
                rms.append(float(np.sqrt(np.mean(np.square(mono[a:b], dtype=np.float64)))))
            peak = max(rms) if rms else 1.0
            return [v / peak if peak > 0 else 0.0 for v in rms]
        except Exception:
            return [0.5 for _ in bar_starts]


def needs_reanalysis(beatgrid: dict[str, Any] | None) -> bool:
    if not beatgrid:
        return True
    return int(beatgrid.get("schema_version", beatgrid.get("beatgrid_version", 0)) or 0) < SCHEMA_VERSION
