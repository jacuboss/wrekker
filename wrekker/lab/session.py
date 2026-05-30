"""
WREKKER LAB edit-session model.

The session is intentionally pure-Python and offline: edits mutate an in-memory
draft and are persisted explicitly through a transactional .wrk JSON update.
Audio/stem assets are never decoded or rewritten by this layer.
"""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from wrekker.core.deck import MARKER_MIN_CONFIDENCE


PRIMARY_MARKER_LABELS = ("DROP", "MIX IN", "MIX OUT", "SWITCH")
WREKK_MARKER_LABELS = ("VOCAL", "BASS", "KICK", "TOP", "GHOST", "DECONSTRUCT", "REBUILD", "BASS LOCK", "WASH", "LEGACY")
SECONDARY_MARKER_LABELS = ()
GUIDE_MARKER_LABELS = ("PHRASE",)

MARKER_TYPE_TO_UI: dict[str, tuple[str, str, str]] = {
    "drop": ("PRIMARY", "DROP", ""),
    "mix_in": ("PRIMARY", "MIX IN", ""),
    "mix_out": ("PRIMARY", "MIX OUT", ""),
    "switch_point": ("PRIMARY", "SWITCH", ""),
    "vocal_in": ("WREKK", "VOCAL", "IN"),
    "vocal_out": ("WREKK", "VOCAL", "OUT"),
    "bass_in": ("WREKK", "BASS", "IN"),
    "bass_out": ("WREKK", "BASS", "OUT"),
    "kick_in": ("WREKK", "KICK", "IN"),
    "kick_out": ("WREKK", "KICK", "OUT"),
    "top_in": ("WREKK", "TOP", "IN"),
    "top_out": ("WREKK", "TOP", "OUT"),
    "vocal_ghost": ("WREKK", "GHOST", ""),
    "deconstruct": ("WREKK", "DECONSTRUCT", ""),
    "rebuild": ("WREKK", "REBUILD", ""),
    "bass_lock": ("WREKK", "BASS LOCK", ""),
    "wash": ("WREKK", "WASH", ""),
    "wrekk_top": ("WREKK", "LEGACY", "WREKK TOP"),
    "wrekk_rhythm": ("WREKK", "LEGACY", "WREKK RHYTHM"),
    "rhythm_in": ("WREKK", "LEGACY", "RHYTHM IN"),
    "drum_swap": ("WREKK", "LEGACY", "DRUM SWAP"),
    "phrase": ("GUIDE", "PHRASE", ""),
}

UI_MARKER_TO_TYPE: dict[tuple[str, str, str], str] = {
    ("PRIMARY", "DROP", ""): "drop",
    ("PRIMARY", "MIX IN", ""): "mix_in",
    ("PRIMARY", "MIX OUT", ""): "mix_out",
    ("PRIMARY", "SWITCH", ""): "switch_point",
    ("WREKK", "VOCAL", "IN"): "vocal_in",
    ("WREKK", "VOCAL", "OUT"): "vocal_out",
    ("WREKK", "VOCAL", ""): "vocal_in",
    ("WREKK", "BASS", "IN"): "bass_in",
    ("WREKK", "BASS", "OUT"): "bass_out",
    ("WREKK", "BASS", ""): "bass_in",
    ("WREKK", "KICK", "IN"): "kick_in",
    ("WREKK", "KICK", "OUT"): "kick_out",
    ("WREKK", "KICK", ""): "kick_in",
    ("WREKK", "TOP", "IN"): "top_in",
    ("WREKK", "TOP", "OUT"): "top_out",
    ("WREKK", "TOP", ""): "top_in",
    ("WREKK", "GHOST", ""): "vocal_ghost",
    ("WREKK", "DECONSTRUCT", ""): "deconstruct",
    ("WREKK", "REBUILD", ""): "rebuild",
    ("WREKK", "BASS LOCK", ""): "bass_lock",
    ("WREKK", "WASH", ""): "wash",
    ("WREKK", "LEGACY", "WREKK TOP"): "wrekk_top",
    ("WREKK", "LEGACY", "WREKK RHYTHM"): "wrekk_rhythm",
    ("WREKK", "LEGACY", "RHYTHM IN"): "rhythm_in",
    ("WREKK", "LEGACY", "DRUM SWAP"): "drum_swap",
    # Legacy UI compatibility: map old controls into the new WREKK category.
    ("PRIMARY", "WREKK", "TOP"): "wrekk_top",
    ("PRIMARY", "WREKK", "RHYTHM"): "wrekk_rhythm",
    ("PRIMARY", "WREKK", "GENERIC"): "wrekk_top",
    ("PRIMARY", "GHOST", ""): "vocal_ghost",
    ("SECONDARY", "VOCAL", "IN"): "vocal_in",
    ("SECONDARY", "VOCAL", "OUT"): "vocal_out",
    ("SECONDARY", "BASS", "IN"): "bass_in",
    ("SECONDARY", "BASS", "LOCK"): "bass_lock",
    ("GUIDE", "PHRASE", ""): "phrase",
}


def marker_ui_parts(marker_type: str) -> tuple[str, str, str]:
    return MARKER_TYPE_TO_UI.get(
        str(marker_type or "").lower(),
        ("GUIDE", str(marker_type or "MARKER").upper().replace("_", " "), ""),
    )


def marker_type_from_ui(category: str, label: str, detail: str = "") -> str:
    category = str(category or "").upper()
    label = str(label or "").upper()
    detail = str(detail or "").upper()
    return UI_MARKER_TO_TYPE.get((category, label, detail), UI_MARKER_TO_TYPE.get((category, label, ""), "phrase"))


def marker_source_label(marker: dict) -> str:
    src = str(marker.get("source") or "auto").lower()
    if src == "manual":
        return "Manual"
    if src == "user_modified":
        return "Edited"
    return "Auto"


def marker_status_label(marker: dict) -> str:
    if marker.get("locked"):
        return "Locked"
    if str(marker.get("source") or "").lower() == "manual":
        return "Manual"
    return "Unlocked"


def is_active_performance_marker(marker: dict) -> bool:
    source = str(marker.get("source") or "auto").lower()
    if source == "manual" or bool(marker.get("locked")):
        return True
    try:
        return float(marker.get("confidence", 0.0) or 0.0) >= MARKER_MIN_CONFIDENCE
    except Exception:
        return False


def filter_active_markers(markers: list[dict]) -> tuple[list[dict], list[dict]]:
    active: list[dict] = []
    filtered: list[dict] = []
    for marker in markers or []:
        m = dict(marker)
        if is_active_performance_marker(m):
            active.append(m)
        else:
            filtered.append(m)
    active.sort(key=lambda m: float(m.get("position_s", 0.0) or 0.0))
    filtered.sort(key=lambda m: float(m.get("position_s", 0.0) or 0.0))
    return active, filtered


class LabStatus:
    AUTO_ANALYZED = "AUTO ANALYZED"
    EDITED = "EDITED"
    MANUAL_VERIFIED = "MANUAL VERIFIED"
    NEEDS_REVIEW = "NEEDS REVIEW"
    LOW_CONFIDENCE = "LOW CONFIDENCE"
    GRID_EDITED = "GRID EDITED"
    MARKERS_EDITED = "MARKERS EDITED"
    CUES_READY = "CUES READY"
    LAB_EDITED = "LAB EDITED"
    DYNAMIC_TEMPO_TODO = "DYNAMIC TEMPO TODO"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _deepcopy_json(value):
    return copy.deepcopy(value)


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    return json.dumps(
        value,
        indent=2 if pretty else None,
        ensure_ascii=False,
        sort_keys=pretty,
    ).encode("utf-8")


def _read_json(zf: zipfile.ZipFile, name: str, default):
    try:
        return json.loads(zf.read(name))
    except KeyError:
        return _deepcopy_json(default)


def _beats_from_grid(beatgrid: dict) -> list[float]:
    beats = beatgrid.get("beats") or []
    return [float(b) for b in beats if isinstance(b, (int, float))]


def _set_beats(beatgrid: dict, beats: list[float]) -> None:
    beatgrid["beats"] = [round(max(0.0, float(b)), 6) for b in beats]


def _grid_bpm(beatgrid: dict) -> float:
    bpm = beatgrid.get("bpm")
    try:
        bpm_f = float(bpm)
        if bpm_f > 0:
            return bpm_f
    except Exception:
        pass
    beats = _beats_from_grid(beatgrid)
    if len(beats) >= 2:
        diffs = [b - a for a, b in zip(beats, beats[1:]) if b > a]
        if diffs:
            diffs.sort()
            return 60.0 / diffs[len(diffs) // 2]
    return 120.0


def _duration_from_manifest(manifest: dict) -> float:
    try:
        return float(manifest.get("metadata", {}).get("duration_s") or 0.0)
    except Exception:
        return 0.0


def _downbeats(beatgrid: dict) -> list[float]:
    return [float(v) for v in (beatgrid.get("downbeats") or []) if isinstance(v, (int, float))]


def _phrase_markers(beatgrid: dict) -> list[dict]:
    return [dict(p) for p in (beatgrid.get("phrase_markers") or []) if isinstance(p, dict)]


def _next_revision_number(manifest: dict) -> int:
    lab = manifest.setdefault("lab", {})
    return int(lab.get("analysis_revision") or manifest.get("analysis_revision") or 0) + 1


def _marker_id(marker: dict) -> str:
    mid = marker.get("id")
    if mid:
        return str(mid)
    mid = f"lab-{uuid.uuid4().hex[:12]}"
    marker["id"] = mid
    return mid


@dataclass
class AnalysisChange:
    entity: str
    operation: str
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    reason: str = ""
    automatic: bool = False

    def to_dict(self) -> dict:
        return {
            "entity": self.entity,
            "operation": self.operation,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
            "automatic": self.automatic,
        }


@dataclass
class AnalysisRevision:
    revision_id: str
    timestamp: str
    summary: str
    changes: list[AnalysisChange]
    manual_verified: bool = False
    marker_regeneration: Optional[dict] = None

    def to_dict(self, track_id: str = "") -> dict:
        out = {
            "revision_id": self.revision_id,
            "timestamp": self.timestamp,
            "editor": "Wrekker LAB",
            "action": "analysis_correction_commit",
            "track_id": track_id,
            "summary": self.summary,
            "changes": [c.to_dict() for c in self.changes],
            "manual_verified": self.manual_verified,
        }
        if self.marker_regeneration is not None:
            out["marker_regeneration"] = self.marker_regeneration
        return out


@dataclass
class LabAnalysisState:
    wrk_path: Path
    manifest: dict
    auto_beatgrid: dict
    active_beatgrid: dict
    auto_markers: list[dict]
    active_markers: list[dict]
    cues: list[dict]
    loops: list[dict]
    corrections: dict
    changelog: dict
    has_stems: bool = False
    source_available: bool = False

    @property
    def title(self) -> str:
        return self.manifest.get("metadata", {}).get("title", "") or self.wrk_path.stem

    @property
    def artist(self) -> str:
        return self.manifest.get("metadata", {}).get("artist", "")

    @property
    def duration_s(self) -> float:
        return _duration_from_manifest(self.manifest)

    @property
    def active_bpm(self) -> float:
        return _grid_bpm(self.active_beatgrid)

    @property
    def auto_bpm(self) -> float:
        return _grid_bpm(self.auto_beatgrid)

    @property
    def dynamic_tempo(self) -> bool:
        return bool(
            self.active_beatgrid.get("bpm_variable")
            or self.active_beatgrid.get("dynamic_tempo")
        )


class LabEditSession:
    """In-memory draft edit session with coarse undo/redo snapshots."""

    def __init__(self, state: LabAnalysisState) -> None:
        self.saved = state
        self.draft = _deepcopy_json(state)
        self._undo: list[LabAnalysisState] = []
        self._redo: list[LabAnalysisState] = []
        self._changes: list[AnalysisChange] = []
        self.dirty = False

    @property
    def changes(self) -> tuple[AnalysisChange, ...]:
        return tuple(self._changes)

    def _snapshot(self) -> None:
        self._undo.append(_deepcopy_json(self.draft))
        self._redo.clear()

    def _record(self, change: AnalysisChange) -> None:
        self._changes.append(change)
        self.dirty = True

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(_deepcopy_json(self.draft))
        self.draft = self._undo.pop()
        self.dirty = True
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(_deepcopy_json(self.draft))
        self.draft = self._redo.pop()
        self.dirty = True
        return True

    def revert_draft_to_saved(self) -> None:
        self._snapshot()
        self.draft = _deepcopy_json(self.saved)
        self._record(AnalysisChange("analysis", "revert_draft_to_saved"))

    def shift_grid(self, delta_s: float, reason: str = "") -> None:
        self._snapshot()
        bg = self.draft.active_beatgrid
        before = {"delta_s": 0.0, "first_beat_s": first_beat(bg)}
        beats = [max(0.0, b + delta_s) for b in _beats_from_grid(bg)]
        _set_beats(bg, beats)
        bg["downbeats"] = [round(max(0.0, d + delta_s), 6) for d in _downbeats(bg)]
        bg["phrase_markers"] = [
            {**p, "position_sec": round(max(0.0, float(p.get("position_sec", 0.0)) + delta_s), 6)}
            for p in _phrase_markers(bg)
        ]
        self._record(AnalysisChange(
            "beatgrid", "shift_grid", before,
            {"delta_s": delta_s, "first_beat_s": first_beat(bg)}, reason,
        ))

    def set_first_beat(self, position_s: float, reason: str = "") -> None:
        self._snapshot()
        bg = self.draft.active_beatgrid
        before = {"first_beat_s": first_beat(bg)}
        old_first = first_beat(bg)
        if old_first is None:
            old_first = 0.0
        self.shift_grid(float(position_s) - float(old_first), reason=reason)
        # shift_grid already snapshotted and recorded; collapse extra snapshot is acceptable.
        self._changes[-1].operation = "set_first_beat"
        self._changes[-1].before = before
        self._changes[-1].after = {"first_beat_s": first_beat(bg)}

    def set_bpm(self, bpm: float, anchor_s: Optional[float] = None, reason: str = "") -> None:
        bpm = max(40.0, min(260.0, float(bpm)))
        self._snapshot()
        bg = self.draft.active_beatgrid
        before = {"bpm": _grid_bpm(bg), "first_beat_s": first_beat(bg)}
        duration = max(self.draft.duration_s, 1.0)
        anchor = float(anchor_s if anchor_s is not None else (first_beat(bg) or 0.0))
        start = first_beat(bg)
        if start is None:
            start = anchor
        period = 60.0 / bpm
        first = anchor - round((anchor - start) / period) * period
        while first > 0.0:
            first -= period
        while first + period < 0.0:
            first += period
        beats = []
        t = max(0.0, first)
        # If first is before zero, find first non-negative beat.
        while t < 0.0:
            t += period
        while t <= duration + period:
            beats.append(round(t, 6))
            t += period
        bg["bpm"] = round(bpm, 6)
        bg["bpm_variable"] = False
        _set_beats(bg, beats)
        self._record(AnalysisChange(
            "beatgrid", "set_bpm", before,
            {"bpm": bpm, "first_beat_s": first_beat(bg)}, reason,
        ))

    def multiply_bpm(self, factor: float) -> None:
        old = _grid_bpm(self.draft.active_beatgrid)
        self.set_bpm(old * factor, reason=f"BPM x{factor:g}")
        self._changes[-1].operation = "bpm_multiply" if factor > 1 else "bpm_divide"

    def set_downbeat(self, position_s: float, phrase_bars: int = 16) -> None:
        self._snapshot()
        bg = self.draft.active_beatgrid
        beats = _beats_from_grid(bg)
        if beats:
            nearest = min(beats, key=lambda b: abs(b - position_s))
        else:
            nearest = float(position_s)
        before = {"downbeats": _downbeats(bg)[:8]}
        bpm = _grid_bpm(bg)
        beat_period = 60.0 / bpm
        bar_period = beat_period * 4.0
        duration = max(self.draft.duration_s, nearest)
        downbeats = []
        t = nearest
        while t >= 0.0:
            t -= bar_period
        t += bar_period
        while t <= duration + bar_period:
            downbeats.append(round(t, 6))
            t += bar_period
        bg["downbeats"] = downbeats
        self.regenerate_phrases_from_downbeat(nearest, phrase_bars=phrase_bars, snapshot=False)
        self._record(AnalysisChange(
            "downbeat", "set_downbeat", before,
            {"downbeats": downbeats[:8], "phrase_bars": phrase_bars},
            "regenerated bar/phrase structure",
        ))

    def regenerate_phrases_from_downbeat(self, position_s: float, phrase_bars: int = 16, snapshot: bool = True) -> None:
        if snapshot:
            self._snapshot()
        bg = self.draft.active_beatgrid
        bpm = _grid_bpm(bg)
        phrase_bars = int(phrase_bars if phrase_bars in (8, 16, 32) else 16)
        phrase_period = 60.0 / bpm * 4.0 * phrase_bars
        duration = max(self.draft.duration_s, position_s)
        t = float(position_s)
        while t - phrase_period >= 0.0:
            t -= phrase_period
        phrases = []
        while t <= duration + phrase_period:
            phrases.append({
                "position_sec": round(max(0.0, t), 6),
                "phrase_length": phrase_bars,
                "energy_level": 0.5,
            })
            t += phrase_period
        before = {"phrase_count": len(bg.get("phrase_markers") or [])}
        bg["phrase_markers"] = phrases
        if snapshot:
            self._record(AnalysisChange(
                "phrase_marker", "regenerate_from_downbeat", before,
                {"phrase_count": len(phrases), "phrase_bars": phrase_bars},
            ))

    def add_phrase_marker(self, position_s: float, bars: int = 16) -> None:
        self._snapshot()
        bg = self.draft.active_beatgrid
        markers = _phrase_markers(bg)
        marker = {"position_sec": round(float(position_s), 6), "phrase_length": int(bars), "energy_level": 0.5}
        markers.append(marker)
        markers.sort(key=lambda p: float(p.get("position_sec", 0.0)))
        bg["phrase_markers"] = markers
        self._record(AnalysisChange("phrase_marker", "add", after=marker))

    def delete_nearest_phrase_marker(self, position_s: float, tolerance_s: float = 2.0) -> bool:
        markers = _phrase_markers(self.draft.active_beatgrid)
        if not markers:
            return False
        idx, marker = min(enumerate(markers), key=lambda it: abs(float(it[1].get("position_sec", 0.0)) - position_s))
        if abs(float(marker.get("position_sec", 0.0)) - position_s) > tolerance_s:
            return False
        self._snapshot()
        removed = markers.pop(idx)
        self.draft.active_beatgrid["phrase_markers"] = markers
        self._record(AnalysisChange("phrase_marker", "delete", before=removed))
        return True

    def add_marker(self, position_s: float, marker_type: str, label: str = "", reason: str = "") -> dict:
        self._snapshot()
        marker = {
            "id": f"manual-{uuid.uuid4().hex[:12]}",
            "type": marker_type,
            "position_s": round(float(position_s), 6),
            "confidence": 1.0,
            "source": "manual",
            "locked": True,
            "label": label or marker_type.upper().replace("_", " "),
            "reason": reason or "manual marker",
        }
        self.draft.active_markers.append(marker)
        self.draft.active_markers.sort(key=lambda m: float(m.get("position_s", 0.0)))
        self._record(AnalysisChange("auto_marker", "manual_create", after=marker))
        return marker

    def update_marker(self, marker_id: str, **fields) -> bool:
        for marker in self.draft.active_markers:
            if _marker_id(marker) == marker_id:
                self._snapshot()
                before = dict(marker)
                marker.update(fields)
                marker["source"] = "manual" if marker.get("source") == "manual" else "user_modified"
                if marker.get("source") == "user_modified":
                    marker["locked"] = True
                self._record(AnalysisChange("auto_marker", "edit", before, dict(marker)))
                return True
        return False

    def delete_marker(self, marker_id: str) -> bool:
        for i, marker in enumerate(self.draft.active_markers):
            if _marker_id(marker) == marker_id:
                self._snapshot()
                removed = self.draft.active_markers.pop(i)
                self._record(AnalysisChange("auto_marker", "delete", before=removed))
                return True
        return False

    def lock_marker(self, marker_id: str, locked: bool = True) -> bool:
        return self.update_marker(marker_id, locked=bool(locked))

    def clear_unlocked_auto_markers(self) -> int:
        self._snapshot()
        before_count = len(self.draft.active_markers)
        kept = [
            m for m in self.draft.active_markers
            if bool(m.get("locked")) or str(m.get("source", "")).lower() == "manual"
        ]
        self.draft.active_markers = kept
        removed = before_count - len(kept)
        self._record(AnalysisChange(
            "auto_marker", "clear_unlocked_auto",
            {"count": before_count}, {"count": len(kept), "removed": removed},
        ))
        return removed

    def add_hot_cue(self, position_s: float, label: str = "Cue", color: str = "#00d4ff") -> dict:
        self._snapshot()
        cue = {
            "position_s": round(float(position_s), 6),
            "label": label,
            "color": color,
            "type": "hot_cue",
        }
        self.draft.cues.append(cue)
        self.draft.cues.sort(key=lambda c: float(c.get("position_s", 0.0)))
        self._record(AnalysisChange("hot_cue", "add", after=cue))
        return cue

    def delete_hot_cue(self, index: int) -> bool:
        if not (0 <= index < len(self.draft.cues)):
            return False
        self._snapshot()
        cue = self.draft.cues.pop(index)
        self._record(AnalysisChange("hot_cue", "delete", before=cue))
        return True

    def add_loop(self, start_s: float, end_s: float, label: str = "Loop") -> dict:
        self._snapshot()
        loop = {
            "start_s": round(float(min(start_s, end_s)), 6),
            "end_s": round(float(max(start_s, end_s)), 6),
            "label": label,
        }
        self.draft.loops.append(loop)
        self.draft.loops.sort(key=lambda l: float(l.get("start_s", 0.0)))
        self._record(AnalysisChange("loop", "add", after=loop))
        return loop

    def delete_loop(self, index: int) -> bool:
        if not (0 <= index < len(self.draft.loops)):
            return False
        self._snapshot()
        loop = self.draft.loops.pop(index)
        self._record(AnalysisChange("loop", "delete", before=loop))
        return True

    def set_key_override(self, key: str) -> None:
        self._snapshot()
        meta = self.draft.manifest.setdefault("metadata", {})
        before = {"key": meta.get("key")}
        meta["key"] = key.strip()
        self._record(AnalysisChange("key", "override", before, {"key": meta.get("key")}))

    def mark_verified(self) -> None:
        self._snapshot()
        corr = self.draft.corrections
        before = {"status": corr.get("analysis_status")}
        corr["analysis_status"] = LabStatus.MANUAL_VERIFIED
        corr["manual_verified"] = True
        corr["manual_verified_at"] = _utc_now()
        self._record(AnalysisChange("analysis_status", "mark_manual_verified", before, dict(corr)))

    def revert_active_to_auto(self) -> None:
        self._snapshot()
        self.draft.active_beatgrid = _deepcopy_json(self.draft.auto_beatgrid)
        self.draft.active_markers, filtered = filter_active_markers(_deepcopy_json(self.draft.auto_markers))
        self.draft.corrections["analysis_status"] = LabStatus.AUTO_ANALYZED
        self.draft.corrections["filtered_low_confidence_auto_markers"] = len(filtered)
        self._record(AnalysisChange("analysis", "revert_to_auto"))

    def compare_auto_active(self) -> dict:
        return compare_auto_active(self.draft)

    def save(self, summary: str = "") -> AnalysisRevision:
        if not self.dirty:
            return AnalysisRevision(uuid.uuid4().hex, _utc_now(), "No changes", [])
        revision = AnalysisRevision(
            revision_id=uuid.uuid4().hex,
            timestamp=_utc_now(),
            summary=summary or summarize_changes(self._changes),
            changes=list(self._changes),
            manual_verified=bool(self.draft.corrections.get("manual_verified")),
        )
        save_lab_state(self.draft, revision)
        self.saved = _deepcopy_json(self.draft)
        self._undo.clear()
        self._redo.clear()
        self._changes.clear()
        self.dirty = False
        return revision


def first_beat(beatgrid: dict) -> Optional[float]:
    beats = _beats_from_grid(beatgrid)
    if beats:
        return float(beats[0])
    value = beatgrid.get("first_beat_s")
    try:
        return float(value)
    except Exception:
        return None


def compare_auto_active(state: LabAnalysisState) -> dict:
    auto_beats = _beats_from_grid(state.auto_beatgrid)
    active_beats = _beats_from_grid(state.active_beatgrid)
    marker_by_id = {str(m.get("id") or ""): m for m in state.auto_markers}
    changed_markers = []
    for marker in state.active_markers:
        mid = str(marker.get("id") or "")
        old = marker_by_id.get(mid)
        if old is None or old != marker:
            changed_markers.append(marker)
    return {
        "auto_bpm": _grid_bpm(state.auto_beatgrid),
        "active_bpm": _grid_bpm(state.active_beatgrid),
        "first_beat_delta_ms": (
            None if first_beat(state.auto_beatgrid) is None or first_beat(state.active_beatgrid) is None
            else round((first_beat(state.active_beatgrid) - first_beat(state.auto_beatgrid)) * 1000.0, 3)
        ),
        "beat_count_auto": len(auto_beats),
        "beat_count_active": len(active_beats),
        "downbeat_count_auto": len(_downbeats(state.auto_beatgrid)),
        "downbeat_count_active": len(_downbeats(state.active_beatgrid)),
        "phrase_count_auto": len(_phrase_markers(state.auto_beatgrid)),
        "phrase_count_active": len(_phrase_markers(state.active_beatgrid)),
        "marker_count_auto": len(state.auto_markers),
        "marker_count_active": len(state.active_markers),
        "changed_marker_count": len(changed_markers),
    }


def summarize_changes(changes: list[AnalysisChange]) -> str:
    if not changes:
        return "No changes"
    if any(c.operation in {"shift_grid", "set_first_beat", "set_bpm", "bpm_multiply", "bpm_divide", "set_downbeat"} for c in changes):
        return "Corrected beatgrid alignment"
    if any(c.entity == "auto_marker" for c in changes):
        return "Updated performance markers"
    if any(c.entity in {"hot_cue", "loop"} for c in changes):
        return "Prepared performance cues"
    if any(c.operation == "mark_manual_verified" for c in changes):
        return "Marked track manually verified"
    if any(c.operation == "revert_to_auto" for c in changes):
        return "Reverted active analysis to auto"
    ops = []
    for c in changes:
        label = f"{c.entity}:{c.operation}"
        if label not in ops:
            ops.append(label)
    return "Updated " + ", ".join(ops[:5]) + ("..." if len(ops) > 5 else "")


def human_change_sentence(change: dict) -> str:
    entity = str(change.get("entity") or "")
    operation = str(change.get("operation") or "")
    before = change.get("before") or {}
    after = change.get("after") or {}
    if operation == "initialize_lab_layers":
        return "Created editable LAB analysis layers while preserving the original automatic analysis."
    if operation == "shift_grid":
        delta = float(after.get("delta_s") or 0.0) * 1000.0
        direction = "later" if delta > 0 else "earlier"
        return f"Shifted the beatgrid {abs(delta):.1f} ms {direction}."
    if operation == "set_first_beat":
        return f"Set the first beat at {_format_plain_time(after.get('first_beat_s'))}."
    if operation == "set_bpm":
        return f"Changed active BPM from {float(before.get('bpm') or 0.0):.3f} to {float(after.get('bpm') or 0.0):.3f}."
    if operation == "bpm_multiply":
        return f"Doubled the active BPM to {float(after.get('bpm') or 0.0):.3f}."
    if operation == "bpm_divide":
        return f"Halved the active BPM to {float(after.get('bpm') or 0.0):.3f}."
    if operation == "set_downbeat":
        return f"Set the downbeat and regenerated {int(after.get('phrase_bars') or 16)}-bar phrase structure."
    if operation == "regenerate_from_downbeat":
        return f"Regenerated {int(after.get('phrase_count') or 0)} phrase markers."
    if entity == "auto_marker" and operation == "manual_create":
        _, label, detail = marker_ui_parts(str(after.get("type") or ""))
        extra = f" {detail}" if detail else ""
        return f"Added {label}{extra} marker at {_format_plain_time(after.get('position_s'))}."
    if entity == "auto_marker" and operation == "edit":
        _, label, detail = marker_ui_parts(str(after.get("type") or ""))
        extra = f" {detail}" if detail else ""
        return f"Updated {label}{extra} marker at {_format_plain_time(after.get('position_s'))}."
    if entity == "auto_marker" and operation == "delete":
        _, label, detail = marker_ui_parts(str(before.get("type") or ""))
        extra = f" {detail}" if detail else ""
        return f"Deleted {label}{extra} marker at {_format_plain_time(before.get('position_s'))}."
    if entity == "auto_marker" and operation == "clear_unlocked_auto":
        return f"Cleared {int(after.get('removed') or 0)} unlocked automatic markers."
    if entity == "hot_cue" and operation == "add":
        return f"Added hot cue {after.get('label') or 'Cue'} at {_format_plain_time(after.get('position_s'))}."
    if entity == "hot_cue" and operation == "delete":
        return f"Deleted hot cue {before.get('label') or 'Cue'}."
    if entity == "loop" and operation == "add":
        return f"Added loop {after.get('label') or 'Loop'} from {_format_plain_time(after.get('start_s'))} to {_format_plain_time(after.get('end_s'))}."
    if entity == "loop" and operation == "delete":
        return f"Deleted loop {before.get('label') or 'Loop'}."
    if operation == "mark_manual_verified":
        return "Marked the track as MANUAL VERIFIED."
    if operation == "revert_to_auto":
        return "Reverted active beatgrid and markers to the preserved automatic analysis."
    return str(change.get("reason") or f"Updated {entity.replace('_', ' ')}.")


def human_revision_title(revision: dict) -> str:
    changes = revision.get("changes") or []
    if any((c.get("operation") == "initialize_lab_layers") for c in changes):
        return "Initialized Wrekker LAB for this track"
    if any((c.get("operation") == "mark_manual_verified") for c in changes):
        return "Verified analysis for performance"
    if any((c.get("entity") in {"beatgrid", "downbeat", "phrase_marker"}) for c in changes):
        return "Corrected beatgrid alignment"
    if any((c.get("entity") in {"hot_cue", "loop"}) for c in changes):
        return "Prepared performance cues"
    if any((c.get("entity") == "auto_marker") for c in changes):
        return "Updated performance markers"
    return str(revision.get("summary") or "LAB revision")


def _format_plain_time(value) -> str:
    try:
        seconds = max(0.0, float(value or 0.0))
    except Exception:
        seconds = 0.0
    mins, secs = divmod(seconds, 60.0)
    return f"{int(mins)}:{secs:05.2f}"


def _default_corrections(manifest: dict, beatgrid: dict, markers: list[dict], cues: list[dict], loops: list[dict]) -> dict:
    status = LabStatus.LOW_CONFIDENCE
    try:
        conf = float(beatgrid.get("confidence", 1.0) or 1.0)
        if conf >= 0.6:
            status = LabStatus.AUTO_ANALYZED
    except Exception:
        pass
    if beatgrid.get("bpm_variable") or beatgrid.get("dynamic_tempo"):
        status = LabStatus.DYNAMIC_TEMPO_TODO
    return {
        "schema_version": 1,
        "analysis_status": status,
        "manual_verified": False,
        "analysis_revision": int(manifest.get("analysis_revision") or 0),
        "grid_edited": False,
        "markers_edited": False,
        "cues_ready": bool(cues),
        "loop_count": len(loops),
        "hot_cue_count": len(cues),
    }


def _default_changelog() -> dict:
    return {"schema_version": 1, "revisions": []}


def load_lab_state(wrk_path: Path | str, *, migrate: bool = True) -> LabAnalysisState:
    wrk_path = Path(wrk_path)
    with zipfile.ZipFile(wrk_path, "r") as zf:
        names = set(zf.namelist())
        manifest = _read_json(zf, "manifest.json", {})
        active_bg = _read_json(zf, "analysis/beatgrid.json", {})
        auto_bg = _read_json(zf, "analysis/beatgrid_auto.json", active_bg)
        active_markers_raw = _read_json(zf, "analysis/markers.json", [])
        auto_markers = _read_json(zf, "analysis/markers_auto.json", active_markers_raw)
        cues = _read_json(zf, "dj/cues.json", [])
        loops = _read_json(zf, "dj/loops.json", [])
        corrections = _read_json(zf, "analysis/corrections.json", None)
        changelog = _read_json(zf, "analysis/changelog.json", None)
        if corrections is None:
            corrections = _default_corrections(manifest, active_bg, active_markers_raw, cues, loops)
        if changelog is None:
            changelog = _default_changelog()
        has_stems = bool(manifest.get("contents", {}).get("has_stems"))
        source_path = manifest.get("source", {}).get("path") or ""
        source_available = bool(source_path and Path(source_path).exists())
        needs_migration = (
            "analysis/beatgrid_auto.json" not in names
            or "analysis/markers_auto.json" not in names
            or "analysis/corrections.json" not in names
            or "analysis/changelog.json" not in names
        )

    active_markers, low_conf_filtered = filter_active_markers(active_markers_raw)
    corrections["filtered_low_confidence_auto_markers"] = max(
        int(corrections.get("filtered_low_confidence_auto_markers") or 0),
        len(low_conf_filtered),
    )
    corrections["active_marker_count"] = len(active_markers)
    corrections["manual_or_locked_low_confidence_retained"] = sum(
        1 for m in active_markers
        if (float(m.get("confidence", 0.0) or 0.0) < MARKER_MIN_CONFIDENCE)
        and (bool(m.get("locked")) or str(m.get("source") or "").lower() == "manual")
    )

    state = LabAnalysisState(
        wrk_path=wrk_path,
        manifest=manifest,
        auto_beatgrid=auto_bg,
        active_beatgrid=active_bg,
        auto_markers=auto_markers,
        active_markers=active_markers,
        cues=cues,
        loops=loops,
        corrections=corrections,
        changelog=changelog,
        has_stems=has_stems,
        source_available=source_available,
    )
    if migrate and needs_migration:
        rev = AnalysisRevision(
            revision_id=uuid.uuid4().hex,
            timestamp=_utc_now(),
            summary="Initialized WREKKER LAB analysis layers",
            changes=[AnalysisChange("analysis", "initialize_lab_layers", automatic=True)],
        )
        save_lab_state(state, rev, migration=True)
        state = load_lab_state(wrk_path, migrate=False)
    return state


def begin_lab_session(wrk_path: Path | str) -> LabEditSession:
    return LabEditSession(load_lab_state(Path(wrk_path), migrate=True))


def save_lab_state(state: LabAnalysisState, revision: AnalysisRevision, *, migration: bool = False) -> None:
    wrk_path = Path(state.wrk_path)
    state.active_markers, filtered = filter_active_markers(state.active_markers)
    state.corrections["filtered_low_confidence_auto_markers"] = max(
        int(state.corrections.get("filtered_low_confidence_auto_markers") or 0),
        len(filtered),
    )
    state.corrections["active_marker_count"] = len(state.active_markers)
    manifest = _deepcopy_json(state.manifest)
    corrections = _deepcopy_json(state.corrections)
    changelog = _deepcopy_json(state.changelog)
    revisions = changelog.setdefault("revisions", [])
    track_id = manifest.get("source", {}).get("hash") or manifest.get("source", {}).get("path", "")
    revisions.append(revision.to_dict(track_id=track_id))

    revision_no = _next_revision_number(manifest)
    manifest["analysis_revision"] = revision_no
    lab = manifest.setdefault("lab", {})
    lab["schema_version"] = 1
    lab["analysis_revision"] = revision_no
    lab["last_lab_edit_at"] = revision.timestamp
    lab["analysis_status"] = corrections.get("analysis_status") or LabStatus.LAB_EDITED
    lab["manual_verified"] = bool(corrections.get("manual_verified"))
    lab["hot_cue_count"] = len(state.cues)
    lab["loop_count"] = len(state.loops)
    lab["marker_count"] = len(state.active_markers)
    lab["grid_edited"] = state.auto_beatgrid != state.active_beatgrid
    lab["markers_edited"] = state.auto_markers != state.active_markers
    lab["dynamic_tempo_todo"] = bool(
        state.active_beatgrid.get("bpm_variable")
        or state.active_beatgrid.get("dynamic_tempo")
    )

    corrections["analysis_revision"] = revision_no
    corrections["grid_edited"] = lab["grid_edited"]
    corrections["markers_edited"] = lab["markers_edited"]
    corrections["hot_cue_count"] = len(state.cues)
    corrections["loop_count"] = len(state.loops)
    corrections["cues_ready"] = bool(state.cues)
    if not corrections.get("analysis_status") or corrections.get("analysis_status") == LabStatus.AUTO_ANALYZED:
        if lab["dynamic_tempo_todo"]:
            corrections["analysis_status"] = LabStatus.DYNAMIC_TEMPO_TODO
        elif lab["grid_edited"] or lab["markers_edited"]:
            corrections["analysis_status"] = LabStatus.LAB_EDITED

    replacements = {
        "manifest.json": _json_bytes(manifest, pretty=True),
        "analysis/beatgrid_auto.json": _json_bytes(state.auto_beatgrid, pretty=True),
        "analysis/beatgrid.json": _json_bytes(state.active_beatgrid, pretty=True),
        "analysis/markers_auto.json": _json_bytes(state.auto_markers, pretty=True),
        "analysis/markers.json": _json_bytes(state.active_markers, pretty=True),
        "analysis/corrections.json": _json_bytes(corrections, pretty=True),
        "analysis/changelog.json": _json_bytes(changelog, pretty=True),
        "dj/cues.json": _json_bytes(state.cues, pretty=True),
        "dj/loops.json": _json_bytes(state.loops, pretty=True),
    }
    _transactional_zip_replace(wrk_path, replacements)
    _refresh_fastload_metadata(wrk_path, manifest, state.active_beatgrid, state.active_markers, state.cues, state.loops)
    state.manifest = manifest
    state.corrections = corrections
    state.changelog = changelog


def _transactional_zip_replace(wrk_path: Path, replacements: dict[str, bytes]) -> None:
    wrk_path = Path(wrk_path)
    fd, tmp_name = tempfile.mkstemp(prefix=wrk_path.stem + ".", suffix=".tmp", dir=str(wrk_path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(wrk_path, "r") as zin:
            existing = zin.namelist()
            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zout:
                written: set[str] = set()
                for info in zin.infolist():
                    if info.filename in replacements:
                        data = replacements[info.filename]
                        zout.writestr(
                            info.filename,
                            data,
                            compress_type=zipfile.ZIP_DEFLATED,
                            compresslevel=1,
                        )
                        written.add(info.filename)
                    else:
                        zout.writestr(info, zin.read(info.filename))
                for name, data in replacements.items():
                    if name not in written:
                        zout.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=1)
        with zipfile.ZipFile(tmp_path, "r") as ztest:
            names = set(ztest.namelist())
            if "manifest.json" not in names or "audio/full.flac" not in names:
                raise RuntimeError("transactional .wrk save validation failed")
            json.loads(ztest.read("manifest.json"))
            json.loads(ztest.read("analysis/beatgrid.json"))
        os.replace(tmp_path, wrk_path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _refresh_fastload_metadata(
    wrk_path: Path,
    manifest: dict,
    beatgrid: dict,
    markers: list[dict],
    cues: list[dict],
    loops: list[dict],
) -> None:
    try:
        from wrekker.formats.fastload import FastloadCache
        cache = FastloadCache()
        d = cache.cache_dir(wrk_path)
        if not d.exists():
            return
        (d / "metadata.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        (d / "beatgrid.json").write_text(json.dumps(beatgrid, ensure_ascii=False), encoding="utf-8")
        (d / "markers.json").write_text(json.dumps(markers, ensure_ascii=False), encoding="utf-8")
        (d / "cues.json").write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
        (d / "loops.json").write_text(json.dumps(loops, ensure_ascii=False), encoding="utf-8")
        flag_path = d / "ready.flag"
        flag = {}
        if flag_path.exists():
            try:
                flag = json.loads(flag_path.read_text(encoding="utf-8"))
            except Exception:
                flag = {}
        stat = wrk_path.stat()
        flag.update({
            "wrk_mtime_ns": stat.st_mtime_ns,
            "wrk_size": stat.st_size,
            "analysis_revision": manifest.get("analysis_revision"),
            "fastload_analysis_revision": manifest.get("analysis_revision"),
            "metadata_refreshed_at": _utc_now(),
        })
        flag_path.write_text(json.dumps(flag), encoding="utf-8")
    except Exception:
        # Fastload metadata refresh is best-effort; the .wrk transaction is the
        # source of truth and old caches can always be rebuilt.
        return


def nearest_transient_from_energy(stem_energy, position_s: float, duration_s: float, stem_index: int = 1, window_s: float = 0.25) -> Optional[float]:
    """Lightweight transient candidate from precomputed stem_energy columns."""
    try:
        import numpy as np
        arr = np.asarray(stem_energy)
        if arr.ndim != 2 or arr.shape[0] < 3 or duration_s <= 0:
            return None
        if arr.shape[1] <= stem_index:
            return None
        col_s = duration_s / arr.shape[0]
        center = int(position_s / col_s)
        radius = max(2, int(window_s / col_s))
        lo = max(1, center - radius)
        hi = min(arr.shape[0] - 1, center + radius)
        if hi <= lo:
            return None
        stem = arr[:, stem_index].astype(float)
        onset = np.maximum(0.0, stem[1:] - stem[:-1])
        local = onset[lo:hi]
        if local.size == 0:
            return None
        idx = int(np.argmax(local)) + lo
        return round(idx * col_s, 6)
    except Exception:
        return None
