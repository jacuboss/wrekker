"""QObject view-models exposed to Qt Quick scenes.

The Python side remains the owner of Transport/LAB state. QML receives compact
rendering state and emits interaction intents back to Python.
"""
from __future__ import annotations

import os
from typing import Any

import numpy as np
from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from wrekker.ui import theme
from wrekker.ui.widgets.marker_style import (
    MarkerDisplayMode,
    coerce_marker_display_mode,
    marker_color,
    marker_draw_style,
    marker_label,
    marker_paint_sort_key,
    marker_tier,
    marker_value,
    should_draw_marker,
)

_STEM_INDEX = {"VOCALS": 0, "DRUMS": 1, "BASS": 2, "OTHER": 3}
_SOURCE_COLORS = {
    "FULL MIX": "#d7dce0",
    "VOCALS": "#ff5c7a",
    "DRUMS": "#18d8ff",
    "BASS": "#ffd23f",
    "OTHER": "#9b7cff",
    "ANATOMY": "#ffb000",
}
_CUE_COLORS = [
    "#00d4ff", "#ff6b6b", "#4ecdc4", "#ffe66d",
    "#a29bfe", "#fd79a8", "#55efc4", "#fdcb6e",
]


class LabTimelineModel(QObject):
    positionSecondsChanged = pyqtSignal()
    playingChanged = pyqtSignal()
    selectedSourceChanged = pyqtSignal()
    compareAutoChanged = pyqtSignal()
    timelineRevisionChanged = pyqtSignal()
    stemMonitorChanged = pyqtSignal()
    sourceSelected = pyqtSignal(str)
    stemMuteRequested = pyqtSignal(str)
    stemIsolateRequested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._duration = 1.0
        self._position = 0.0
        self._playing = False
        self._selected_source = "FULL MIX"
        self._has_stems = False
        self._compare_auto = False
        self._zoom_window = 12.0
        self._sources = ["FULL MIX", "VOCALS", "DRUMS", "BASS", "OTHER", "ANATOMY"]
        self._muted_sources: set[str] = set()
        self._isolated_source = ""
        self._stem_monitor_revision = 0
        self._waveform_peaks: list[float] = []
        self._active_beats: list[float] = []
        self._auto_beats: list[float] = []
        self._downbeats: list[float] = []
        self._phrases: list[float] = []
        self._beatgrid_edits: list[dict[str, Any]] = []
        self._markers: list[dict[str, Any]] = []
        self._cues: list[dict[str, Any]] = []
        self._loops: list[dict[str, Any]] = []
        self._fps_log = os.environ.get("WREKKER_QML_FPS_LOG") == "1" or os.environ.get("WREKKER_WAVEFORM_FPS_LOG") == "1"
        self._revision = 0

    def sync_from_lab(self, meta, session, source: str, compare_auto: bool) -> None:
        state = session.draft
        duration = max(0.01, float(getattr(meta, "duration_s", 0.0) or state.duration_s or 1.0))
        source = source if source in self._sources else "FULL MIX"
        self._duration = duration
        se = np.asarray(getattr(meta, "stem_energy", []), dtype=np.float32)
        self._has_stems = bool(state.has_stems) or (se.ndim == 2 and se.shape[1] >= 4 and se.shape[0] > 4)
        self._selected_source = source
        self._compare_auto = bool(compare_auto)
        self._waveform_peaks = self._source_values(meta, source)
        self._active_beats = self._float_list(state.active_beatgrid.get("beats") or [])
        self._auto_beats = self._float_list(state.auto_beatgrid.get("beats") or [])
        self._downbeats = self._float_list(state.active_beatgrid.get("downbeats") or [])
        self._phrases = [
            float(p.get("position_sec", 0.0) or 0.0)
            for p in (state.active_beatgrid.get("phrase_markers") or [])
            if isinstance(p, dict)
        ]
        self._beatgrid_edits = self._make_beatgrid_edit_markers(state.active_beatgrid)
        self._markers = [
            {
                "id": str(m.get("id") or ""),
                "position": float(m.get("position_s", 0.0) or 0.0),
                "tier": marker_tier(str(m.get("type") or "")),
                "color": marker_color(str(m.get("type") or ""), float(m.get("confidence", 0.0) or 0.0)),
                "label": self._marker_label(str(m.get("type") or "")),
            }
            for m in state.active_markers
            if self._marker_relevant_to_source(str(m.get("type") or ""), source)
        ]
        self._cues = [
            {
                "position": float(c.get("position_s", 0.0) or 0.0),
                "label": str(c.get("label") or ""),
                "color": str(c.get("color") or "#18d8ff"),
            }
            for c in state.cues
        ]
        self._loops = [
            {
                "start": float(l.get("start_s", 0.0) or 0.0),
                "end": float(l.get("end_s", 0.0) or 0.0),
                "label": str(l.get("label") or ""),
            }
            for l in state.loops
        ]
        self._revision += 1
        self.timelineRevisionChanged.emit()
        self.selectedSourceChanged.emit()
        self.compareAutoChanged.emit()

    def set_stem_monitor(self, muted_sources, isolated_source: str | None) -> None:
        muted = {str(s) for s in (muted_sources or []) if str(s) in self._sources}
        isolated = str(isolated_source or "")
        if isolated not in self._sources:
            isolated = ""
        if muted != self._muted_sources or isolated != self._isolated_source:
            self._muted_sources = muted
            self._isolated_source = isolated
            self._stem_monitor_revision += 1
            self.stemMonitorChanged.emit()

    def set_position(self, value: float) -> None:
        value = max(0.0, min(self._duration, float(value)))
        if abs(value - self._position) > 1e-4:
            self._position = value
            self.positionSecondsChanged.emit()

    def set_playing(self, playing: bool) -> None:
        playing = bool(playing)
        if playing != self._playing:
            self._playing = playing
            self.playingChanged.emit()

    @staticmethod
    def _float_list(values) -> list[float]:
        return [float(v) for v in values if isinstance(v, (int, float))]

    @staticmethod
    def _normalize(values: np.ndarray) -> list[float]:
        arr = np.asarray(values, dtype=np.float32)
        if arr.size < 2:
            return [0.0, 0.0]
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        vmax = max(float(np.percentile(np.abs(arr), 98)), 1e-6)
        return [float(max(-1.0, min(1.0, v / vmax))) for v in arr]

    def _source_values(self, meta, source: str) -> list[float]:
        if source == "FULL MIX":
            return self._normalize(np.asarray(meta.waveform_peaks, dtype=np.float32))
        se = np.asarray(meta.stem_energy, dtype=np.float32)
        if se.ndim == 2 and se.shape[1] >= 4:
            if source in _STEM_INDEX:
                return self._normalize(se[:, _STEM_INDEX[source]])
            return self._normalize(np.max(se[:, :4], axis=1))
        return self._normalize(np.asarray(meta.waveform_peaks, dtype=np.float32))

    @staticmethod
    def _make_beatgrid_edit_markers(beatgrid: dict) -> list[dict[str, Any]]:
        markers: list[dict[str, Any]] = []
        beats = [float(v) for v in (beatgrid.get("beats") or []) if isinstance(v, (int, float))]
        downbeats = [float(v) for v in (beatgrid.get("downbeats") or []) if isinstance(v, (int, float))]
        if beats:
            markers.append({"position": beats[0], "label": "FIRST BEAT", "kind": "firstBeat", "color": "#35e6b5"})
        if downbeats:
            markers.append({"position": downbeats[0], "label": "DOWNBEAT", "kind": "downbeat", "color": "#ffb000"})
        for phrase in (beatgrid.get("phrase_markers") or [])[:32]:
            if isinstance(phrase, dict):
                markers.append({
                    "position": float(phrase.get("position_sec", 0.0) or 0.0),
                    "label": "PHRASE",
                    "kind": "phrase",
                    "color": "#cdbf64",
                })
        return markers

    @staticmethod
    def _marker_relevant_to_source(marker_type: str, source: str) -> bool:
        mtype = marker_type.lower().strip()
        source = str(source).upper()
        if source in {"FULL MIX", "ANATOMY"}:
            return True
        if mtype == "phrase":
            return True
        relevant = {
            "VOCALS": {"vocal_in", "vocal_out", "vocal_ghost"},
            "DRUMS": {"kick_in", "kick_out", "rhythm_in", "drum_swap", "wrekk_rhythm", "drop", "switch_point", "deconstruct", "rebuild"},
            "BASS": {"bass_in", "bass_out", "bass_lock", "wrekk_rhythm", "drop", "switch_point", "deconstruct", "rebuild"},
            "OTHER": {"top_in", "top_out", "wash", "wrekk_top", "mix_in", "mix_out", "drop", "switch_point", "deconstruct", "rebuild"},
        }
        return mtype in relevant.get(source, set())

    @staticmethod
    def _marker_label(marker_type: str) -> str:
        mtype = marker_type.lower().strip()
        if mtype == "drop":
            return "DROP"
        if mtype == "mix_in":
            return "MIX IN"
        if mtype == "mix_out":
            return "MIX OUT"
        if mtype == "vocal_ghost":
            return "W:GHOST"
        if mtype == "deconstruct":
            return "W:DECON"
        if mtype == "rebuild":
            return "W:REBUILD"
        if mtype == "switch_point":
            return "SWITCH"
        if mtype in {"vocal_in", "vocal_out"}:
            return "W:VOC"
        if mtype in {"kick_in", "kick_out"}:
            return "W:KICK"
        if mtype in {"bass_in", "bass_out", "bass_lock"}:
            return "W:BSS"
        if mtype in {"top_in", "top_out", "wash"}:
            return "W:TOP"
        if mtype in {"wrekk_top", "wrekk_rhythm", "rhythm_in", "drum_swap"}:
            return "W:LEGACY"
        if mtype == "phrase":
            return "PHRASE"
        return marker_type.replace("_", " ").upper()

    @pyqtProperty(float, notify=timelineRevisionChanged)
    def durationSeconds(self) -> float:
        return self._duration

    @pyqtProperty(float, notify=positionSecondsChanged)
    def positionSeconds(self) -> float:
        return self._position

    @pyqtProperty(bool, notify=playingChanged)
    def playing(self) -> bool:
        return self._playing

    @pyqtProperty(str, notify=selectedSourceChanged)
    def selectedSource(self) -> str:
        return self._selected_source

    @pyqtProperty(bool, notify=compareAutoChanged)
    def compareAuto(self) -> bool:
        return self._compare_auto

    @pyqtProperty(int, notify=stemMonitorChanged)
    def stemMonitorRevision(self) -> int:
        return self._stem_monitor_revision

    @pyqtProperty(float, constant=True)
    def zoomWindowSeconds(self) -> float:
        return self._zoom_window

    @pyqtProperty("QVariantList", constant=True)
    def sources(self):
        return list(self._sources)

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def waveformPeaks(self):
        return self._waveform_peaks

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def activeBeats(self):
        return self._active_beats

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def autoBeats(self):
        return self._auto_beats

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def downbeats(self):
        return self._downbeats

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def phrases(self):
        return self._phrases

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def beatgridEdits(self):
        return self._beatgrid_edits

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def markers(self):
        return self._markers

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def cues(self):
        return self._cues

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def loops(self):
        return self._loops

    @pyqtProperty(bool, constant=True)
    def fpsLogEnabled(self) -> bool:
        return self._fps_log

    @pyqtSlot(str, result=str)
    def sourceColor(self, source: str) -> str:
        return _SOURCE_COLORS.get(str(source), "#ffb000")

    @pyqtSlot(str, result=bool)
    def sourceAvailable(self, source: str) -> bool:
        # Selection should never be blocked at the visual layer. If a legacy
        # .wrk lacks stem-energy data, _source_values() falls back gracefully.
        return str(source) in self._sources

    @pyqtSlot(str)
    def requestSource(self, source: str) -> None:
        self.sourceSelected.emit(str(source))

    @pyqtSlot(str)
    def requestStemMute(self, source: str) -> None:
        self.stemMuteRequested.emit(str(source))

    @pyqtSlot(str)
    def requestStemIsolate(self, source: str) -> None:
        self.stemIsolateRequested.emit(str(source))

    @pyqtSlot(str, result=bool)
    def sourceMuted(self, source: str) -> bool:
        source = str(source)
        if source in {"FULL MIX", "ANATOMY"}:
            return {"VOCALS", "DRUMS", "BASS", "OTHER"}.issubset(self._muted_sources)
        return source in self._muted_sources

    @pyqtSlot(str, result=bool)
    def sourceIsolated(self, source: str) -> bool:
        return str(source) == self._isolated_source

    @pyqtSlot(str, float, result=str)
    def withAlpha(self, color: str, alpha: float) -> str:
        c = str(color)
        a = max(0.0, min(1.0, float(alpha)))
        if c.startswith("#") and len(c) == 7:
            r = int(c[1:3], 16)
            g = int(c[3:5], 16)
            b = int(c[5:7], 16)
            return f"rgba({r},{g},{b},{a:.3f})"
        return c


class DeckTimelineModel(QObject):
    """Compact QML bridge for one performance deck timeline.

    Waveform and overlay lists are updated on data/revision changes. Position
    is the high-frequency scalar update consumed by QML interpolation.
    """

    positionSecondsChanged = pyqtSignal()
    playingChanged = pyqtSignal()
    timelineRevisionChanged = pyqtSignal()
    otherDeckChanged = pyqtSignal()

    def __init__(self, deck_id: str, parent=None) -> None:
        super().__init__(parent)
        self._deck_id = str(deck_id)
        self._accent = theme.deck_color(self._deck_id)
        self._duration = 0.0
        self._position = 0.0
        self._playing = False
        self._bpm = 120.0
        self._first_beat = 0.0
        self._beats: list[float] = []
        self._zoom_peaks: list[float] = []
        self._overview_peaks: list[float] = []
        self._cues: list[dict[str, Any]] = []
        self._loop_start = 0.0
        self._loop_end = 0.0
        self._loop_active = False
        self._markers: list[dict[str, Any]] = []
        self._marker_lookup: dict[str, Any] = {}
        self._marker_source = []
        self._marker_mode = MarkerDisplayMode.ESSENTIAL
        self._overlay_key: tuple[Any, ...] | None = None
        self._other_position = 0.0
        self._other_beats: list[float] = []
        self._other_bpm = 0.0
        self._other_first = 0.0
        self._sync_enabled = False
        self._phase_error = 0.0
        self._revision = 0
        self._fps_log = os.environ.get("WREKKER_WAVEFORM_FPS_LOG") == "1"

    def set_waveform(self, data) -> None:
        if data is None:
            self._zoom_peaks = []
            self._overview_peaks = []
            self._beats = []
        else:
            zoom = getattr(data, "zoom_peaks", None)
            if zoom is None or len(zoom) == 0:
                zoom = getattr(data, "peaks", [])
            self._zoom_peaks = self._normalize(zoom)
            self._overview_peaks = self._normalize(getattr(data, "peaks", []))
            self._beats = [float(v) for v in (getattr(data, "beats", ()) or ()) if isinstance(v, (int, float))]
        self._revision += 1
        self.timelineRevisionChanged.emit()

    def set_markers(self, markers) -> None:
        self._marker_source = sorted(markers or [], key=marker_paint_sort_key)
        self._rebuild_markers()

    def set_marker_display_mode(self, mode: MarkerDisplayMode | str) -> None:
        self._marker_mode = coerce_marker_display_mode(mode)
        self._rebuild_markers()

    def marker_object(self, marker_id: str):
        return self._marker_lookup.get(str(marker_id))

    def update_position(
        self,
        pos_s: float,
        duration_s: float,
        beats,
        bpm: float,
        first_beat_s: float,
        cue_positions,
        loop,
        playing: bool,
        sync_enabled: bool = False,
        phase_err: float | None = None,
    ) -> None:
        duration = max(0.0, float(duration_s or 0.0))
        position = max(0.0, min(duration or 0.0, float(pos_s or 0.0)))
        bpm_value = max(1.0, float(bpm or 120.0))
        first_beat = float(first_beat_s or 0.0)
        beats_tuple = tuple(float(v) for v in (beats or ()) if isinstance(v, (int, float)))
        cue_list = [
            {"position": float(v), "color": _CUE_COLORS[i % len(_CUE_COLORS)]}
            for i, v in enumerate(cue_positions or [])
        ]
        cue_key = tuple(float(v) for v in (cue_positions or []))
        self._duration = duration
        self._position = position
        self._bpm = bpm_value
        self._first_beat = first_beat
        if beats_tuple != tuple(self._beats):
            self._beats = list(beats_tuple)
        self._cues = cue_list
        if loop and getattr(loop, "start_s", 0.0) < getattr(loop, "end_s", 0.0):
            self._loop_start = float(getattr(loop, "start_s", 0.0) or 0.0)
            self._loop_end = float(getattr(loop, "end_s", 0.0) or 0.0)
            self._loop_active = bool(getattr(loop, "active", False))
        else:
            self._loop_start = self._loop_end = 0.0
            self._loop_active = False
        self._sync_enabled = bool(sync_enabled)
        self._phase_error = float(phase_err or 0.0)
        self.positionSecondsChanged.emit()
        overlay_key = (
            round(duration, 3),
            round(bpm_value, 4),
            round(first_beat, 4),
            beats_tuple,
            cue_key,
            round(self._loop_start, 4),
            round(self._loop_end, 4),
            self._loop_active,
            self._sync_enabled,
        )
        if overlay_key != self._overlay_key:
            self._overlay_key = overlay_key
            self.timelineRevisionChanged.emit()
        playing = bool(playing)
        if playing != self._playing:
            self._playing = playing
            self.playingChanged.emit()

    def set_other_deck(self, pos_s: float, beats, bpm: float, first_beat_s: float) -> None:
        self._other_position = max(0.0, float(pos_s or 0.0))
        if beats:
            self._other_beats = [float(v) for v in beats if isinstance(v, (int, float))]
        if bpm > 0:
            self._other_bpm = float(bpm)
        self._other_first = float(first_beat_s or 0.0)
        self.otherDeckChanged.emit()

    def _rebuild_markers(self) -> None:
        markers: list[dict[str, Any]] = []
        lookup: dict[str, Any] = {}
        for i, m in enumerate(self._marker_source):
            conf = float(getattr(m, "confidence", 0.0) or 0.0)
            mval = marker_value(m)
            show_zoom = should_draw_marker(mval, conf, self._marker_mode, view="zoom", window_s=8.0)
            show_overview = should_draw_marker(mval, conf, self._marker_mode, view="overview")
            if not show_zoom and not show_overview:
                continue
            style = marker_draw_style(mval, conf, self._marker_mode, view="overview")
            marker_id = str(getattr(m, "id", "") or f"marker-{i}")
            lookup[marker_id] = m
            markers.append({
                "id": marker_id,
                "position": float(getattr(m, "position_s", 0.0) or 0.0),
                "color": style["color"],
                "tier": style["tier"],
                "label": marker_label(mval),
                "showZoom": show_zoom,
                "showOverview": show_overview,
            })
        self._markers = markers
        self._marker_lookup = lookup
        self._revision += 1
        self.timelineRevisionChanged.emit()

    @staticmethod
    def _normalize(values) -> list[float]:
        arr = np.asarray(values if values is not None else [], dtype=np.float32)
        if arr.size < 2:
            return []
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        vmax = max(float(np.percentile(np.abs(arr), 98)), 1e-6)
        return [float(max(0.0, min(1.0, abs(v) / vmax))) for v in arr]

    @pyqtProperty(str, constant=True)
    def deckId(self) -> str:
        return self._deck_id

    @pyqtProperty(str, constant=True)
    def accentColor(self) -> str:
        return self._accent

    @pyqtProperty(float, notify=timelineRevisionChanged)
    def durationSeconds(self) -> float:
        return self._duration

    @pyqtProperty(float, notify=positionSecondsChanged)
    def positionSeconds(self) -> float:
        return self._position

    @pyqtProperty(bool, notify=playingChanged)
    def playing(self) -> bool:
        return self._playing

    @pyqtProperty(float, notify=timelineRevisionChanged)
    def bpm(self) -> float:
        return self._bpm

    @pyqtProperty(float, notify=timelineRevisionChanged)
    def firstBeatSeconds(self) -> float:
        return self._first_beat

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def zoomPeaks(self):
        return self._zoom_peaks

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def overviewPeaks(self):
        return self._overview_peaks

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def beats(self):
        return self._beats

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def cues(self):
        return self._cues

    @pyqtProperty("QVariantList", notify=timelineRevisionChanged)
    def markers(self):
        return self._markers

    @pyqtProperty(float, notify=timelineRevisionChanged)
    def loopStart(self) -> float:
        return self._loop_start

    @pyqtProperty(float, notify=timelineRevisionChanged)
    def loopEnd(self) -> float:
        return self._loop_end

    @pyqtProperty(bool, notify=timelineRevisionChanged)
    def loopActive(self) -> bool:
        return self._loop_active

    @pyqtProperty(float, notify=otherDeckChanged)
    def otherPositionSeconds(self) -> float:
        return self._other_position

    @pyqtProperty("QVariantList", notify=otherDeckChanged)
    def otherBeats(self):
        return self._other_beats

    @pyqtProperty(float, notify=otherDeckChanged)
    def otherBpm(self) -> float:
        return self._other_bpm

    @pyqtProperty(bool, constant=True)
    def fpsLogEnabled(self) -> bool:
        return self._fps_log

    @pyqtSlot(str, float, result=str)
    def withAlpha(self, color: str, alpha: float) -> str:
        c = str(color)
        a = max(0.0, min(1.0, float(alpha)))
        if c.startswith("#") and len(c) == 7:
            r = int(c[1:3], 16)
            g = int(c[3:5], 16)
            b = int(c[5:7], 16)
            return f"rgba({r},{g},{b},{a:.3f})"
        return c
