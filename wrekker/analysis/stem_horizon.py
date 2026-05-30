"""Stem Horizon generation from prepared beatgrid and stem energy.

The horizon is a compact, bar-synchronous activity timeline. It is generated
offline from data already present in a prepared .wrk: beatgrid + stem_energy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

STEM_HORIZON_SCHEMA_VERSION = 1
STEM_ORDER = ("vocals", "drums", "bass", "other")


@dataclass(frozen=True)
class StemHorizonSettings:
    present_threshold: float = 0.10
    dominant_threshold: float = 0.24


def _duration_from_beatgrid(beatgrid: dict, fallback: float) -> float:
    beats = [float(b) for b in (beatgrid or {}).get("beats", []) if isinstance(b, (int, float))]
    if len(beats) >= 2:
        return max(float(fallback or 0.0), beats[-1] + (beats[-1] - beats[-2]) * 4)
    return max(float(fallback or 0.0), 1.0)


def _bar_starts(beatgrid: dict, duration_s: float) -> list[float]:
    downbeats = [
        float(b)
        for b in (beatgrid or {}).get("downbeats", [])
        if isinstance(b, (int, float)) and 0.0 <= float(b) < duration_s
    ]
    if downbeats:
        return sorted(set(round(v, 6) for v in downbeats))

    beats = [
        float(b)
        for b in (beatgrid or {}).get("beats", [])
        if isinstance(b, (int, float)) and 0.0 <= float(b) < duration_s
    ]
    if beats:
        return [round(v, 6) for v in beats[::4]]

    bpm = float((beatgrid or {}).get("bpm", 120.0) or 120.0)
    bar_s = 240.0 / max(40.0, bpm)
    n = max(1, int(duration_s / bar_s))
    return [round(i * bar_s, 6) for i in range(n)]


def _state_for(activity: float, settings: StemHorizonSettings) -> int:
    if activity >= settings.dominant_threshold:
        return 2
    if activity >= settings.present_threshold:
        return 1
    return 0


def generate_stem_horizon(
    beatgrid: dict | None,
    stem_energy: np.ndarray | None,
    duration_s: float,
    *,
    analysis_revision: int | None = None,
    settings: StemHorizonSettings | None = None,
) -> dict[str, Any] | None:
    """Return schema-v1 stem horizon JSON or None when inputs are insufficient."""
    if beatgrid is None or stem_energy is None:
        return None
    arr = np.asarray(stem_energy, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] < 8 or arr.shape[1] < 4:
        return None

    settings = settings or StemHorizonSettings()
    duration_s = _duration_from_beatgrid(beatgrid, duration_s)
    bars = _bar_starts(beatgrid, duration_s)
    if not bars:
        return None

    n = arr.shape[0]
    total = arr[:, :4].sum(axis=1)
    norm = np.zeros((n, 4), dtype=np.float32)
    valid = total > 1e-7
    norm[valid] = arr[valid, :4] / total[valid, None]

    starts: list[float] = []
    ends: list[float] = []
    values = {stem: [] for stem in STEM_ORDER}
    confidence = {stem: [] for stem in STEM_ORDER}

    for i, start in enumerate(bars):
        end = bars[i + 1] if i + 1 < len(bars) else min(duration_s, start + max(0.5, (bars[1] - bars[0]) if len(bars) > 1 else 2.0))
        if end <= start:
            continue
        c1 = max(0, min(n - 1, int(start / duration_s * n)))
        c2 = max(c1 + 1, min(n, int(end / duration_s * n)))
        means = norm[c1:c2, :4].mean(axis=0)
        starts.append(round(start, 6))
        ends.append(round(end, 6))
        for col, stem in enumerate(STEM_ORDER):
            activity = float(means[col])
            state = _state_for(activity, settings)
            values[stem].append(state)
            confidence[stem].append(round(min(1.0, max(0.0, abs(activity - settings.present_threshold) * 3.0 + 0.45)), 3))

    transitions: list[dict[str, Any]] = []
    for stem in STEM_ORDER:
        vals = values[stem]
        for idx in range(1, len(vals)):
            prev, cur = int(vals[idx - 1]), int(vals[idx])
            if prev == cur:
                continue
            if prev < 2 <= cur:
                change = "in"
            elif prev >= 2 > cur:
                change = "out"
            else:
                change = "shift"
            transitions.append({
                "stem": stem,
                "bar_index": idx,
                "position_s": starts[idx],
                "from": prev,
                "to": cur,
                "change": change,
                "confidence": confidence[stem][idx],
            })

    return {
        "schema_version": STEM_HORIZON_SCHEMA_VERSION,
        "generated_from_analysis_revision": analysis_revision,
        "resolution": "bar",
        "stems": list(STEM_ORDER),
        "bars": starts,
        "bar_ends": ends,
        "values": values,
        "confidence": confidence,
        "transitions": transitions,
    }


def normalize_stem_horizon(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    if int(data.get("schema_version") or 0) != STEM_HORIZON_SCHEMA_VERSION:
        return None
    values = data.get("values")
    bars = data.get("bars")
    if not isinstance(values, dict) or not isinstance(bars, list):
        return None
    if not all(stem in values for stem in STEM_ORDER):
        return None
    return data
