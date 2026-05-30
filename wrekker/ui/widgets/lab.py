"""
WREKKER LAB — analysis correction and performance preparation workspace.

This is a standalone preparation/editor window. It edits only analysis/DJ JSON
layers in .wrk containers and delegates persistence to wrekker.lab.session.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import os
from pathlib import Path
from typing import Optional

import numpy as np

from PyQt6.QtCore import Qt, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QBrush, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPushButton,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
    QHeaderView,
)

from wrekker.core.deck import MARKER_MIN_CONFIDENCE
from wrekker.formats.fastload import FastloadCache
from wrekker.formats.wrk import load_wrk_metadata, load_wrk_mix, load_wrk_stems
from wrekker.ui.widgets.stem_horizon import StemHorizonWidget
from wrekker.lab.session import (
    LabEditSession,
    LabStatus,
    begin_lab_session,
    human_change_sentence,
    human_revision_title,
    marker_source_label,
    marker_status_label,
    marker_type_from_ui,
    marker_ui_parts,
    nearest_transient_from_energy,
)
from wrekker.library.prepared_db import PreparedDB
from wrekker.ui.branding import logo_label
from wrekker.ui import theme
from wrekker.ui.widgets.marker_style import marker_color, marker_tier
from wrekker.ui.widgets.lab_texture_waveform import TextureLabWaveform
from wrekker.ui.qml_models import LabTimelineModel

__all__ = ["WrekkerLabWindow"]


log = logging.getLogger(__name__)


_SOURCE_LABELS = ("FULL MIX", "VOCALS", "DRUMS", "BASS", "OTHER", "ANATOMY")
_STEM_INDEX = {"VOCALS": 0, "DRUMS": 1, "BASS": 2, "OTHER": 3}
_SOURCE_TO_STEMS = {
    "FULL MIX": ("vocals", "drums", "bass", "other"),
    "ANATOMY": ("vocals", "drums", "bass", "other"),
    "VOCALS": ("vocals",),
    "DRUMS": ("drums",),
    "BASS": ("bass",),
    "OTHER": ("other",),
}
_SOURCE_COLORS = {
    "FULL MIX": "#d7dce0",
    "VOCALS": "#ff5c7a",
    "DRUMS": "#18d8ff",
    "BASS": "#ffd23f",
    "OTHER": "#9b7cff",
    "ANATOMY": "#ffb000",
}
_PLAYHEAD_COLOR = "#f7fbff"
_PLAYHEAD_ACCENT = "#ff4fd8"
_CUE_COLOR_PRESETS = (
    ("Amber", "#ffb000"),
    ("Cyan", "#18d8ff"),
    ("Magenta", "#ff4fd8"),
    ("Red", "#ff4b4b"),
    ("Green", "#35e6b5"),
    ("Violet", "#9b7cff"),
    ("White", "#f7fbff"),
)
_CATEGORY_LABELS = {
    "PRIMARY": ("DROP", "MIX IN", "MIX OUT", "SWITCH"),
    "WREKK": ("VOCAL", "BASS", "KICK", "TOP", "GHOST", "DECONSTRUCT", "REBUILD", "BASS LOCK", "WASH", "LEGACY"),
    "GUIDE": ("PHRASE",),
}
_DETAIL_LABELS = {
    ("WREKK", "VOCAL"): ("IN", "OUT"),
    ("WREKK", "BASS"): ("IN", "OUT"),
    ("WREKK", "KICK"): ("IN", "OUT"),
    ("WREKK", "TOP"): ("IN", "OUT"),
    ("WREKK", "LEGACY"): ("WREKK TOP", "WREKK RHYTHM", "RHYTHM IN", "DRUM SWAP"),
}


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    m, s = divmod(seconds, 60.0)
    return f"{int(m)}:{s:05.2f}"


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


class _LabPreviewController:
    """Isolated LAB preview player backed by Wrekker's Rust/CPAL engine."""

    def __init__(self, wrk_path: Path, session: LabEditSession) -> None:
        self._wrk_path = Path(wrk_path)
        self._session = session
        self._audio: np.ndarray | None = None
        self._stems_audio: dict[str, np.ndarray] | None = None
        self._play_audio: np.ndarray | None = None
        self._sr = 44100
        self._engine = None
        self._playing = False
        self._metronome = False
        self._click_level = 0.65
        self._beats: list[float] = []
        self._downbeats: list[float] = []
        self._loaded = False
        self._engine_ready = False
        self._needs_audio_refresh = True
        self._muted_sources: set[str] = set()
        self._isolated_source: str | None = None
        self._position_s = 0.0
        self.error: str = ""
        self.refresh_grid()

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def duration_s(self) -> float:
        if self._audio is None:
            return float(self._session.draft.duration_s or 0.0)
        return float(len(self._audio) / max(1, self._sr))

    @property
    def position_s(self) -> float:
        if self._engine is not None and self._engine_ready:
            try:
                self._position_s = float(self._engine.deck_a.position_s)
            except Exception:
                pass
        return self._position_s

    @property
    def is_playing(self) -> bool:
        return self._playing

    def refresh_grid(self) -> None:
        bg = self._session.draft.active_beatgrid
        self._beats = [float(v) for v in (bg.get("beats") or []) if isinstance(v, (int, float))]
        self._downbeats = [float(v) for v in (bg.get("downbeats") or []) if isinstance(v, (int, float))]
        self._needs_audio_refresh = True

    def set_session(self, session: LabEditSession) -> None:
        self._session = session
        self.refresh_grid()

    def set_metronome(self, enabled: bool, level: float) -> None:
        enabled = bool(enabled)
        level = max(0.0, min(1.0, float(level)))
        if enabled != self._metronome or abs(level - self._click_level) > 1e-4:
            self._metronome = enabled
            self._click_level = level
            self._needs_audio_refresh = True
            if self._engine_ready:
                pos = self.position_s
                was_playing = self._playing
                self._load_audio_into_engine(pos)
                if was_playing:
                    self._engine.play("A")

    def set_stem_monitor(self, muted_sources: set[str] | list[str] | tuple[str, ...], isolated_source: str | None) -> None:
        muted = {str(s) for s in (muted_sources or []) if str(s) in _SOURCE_LABELS}
        isolated = str(isolated_source or "") or None
        if isolated not in _SOURCE_LABELS:
            isolated = None
        if muted != self._muted_sources or isolated != self._isolated_source:
            self._muted_sources = muted
            self._isolated_source = isolated
            self._needs_audio_refresh = True
            if self._engine_ready:
                pos = self.position_s
                was_playing = self._playing
                self._load_audio_into_engine(pos)
                if was_playing:
                    self._engine.play("A")

    def load(self) -> bool:
        if self._loaded:
            return True
        try:
            audio = None
            sr = None
            cache = FastloadCache()
            if cache.is_valid(self._wrk_path):
                try:
                    audio, sr = cache.load_mix(self._wrk_path)
                    log.debug("LAB preview loaded mix from fastload: %s", self._wrk_path)
                except Exception as exc:
                    log.warning("LAB preview fastload mix failed for %s: %s", self._wrk_path, exc)
            if audio is None:
                audio, sr = load_wrk_mix(self._wrk_path)
                log.debug("LAB preview loaded mix from .wrk: %s", self._wrk_path)
            audio = np.asarray(audio, dtype=np.float32)
            if audio.ndim == 1:
                audio = audio[:, None]
            if audio.shape[1] == 1:
                audio = np.repeat(audio, 2, axis=1)
            self._audio = np.ascontiguousarray(np.clip(audio[:, :2], -1.0, 1.0))
            self._sr = int(sr or 44100)
            self._loaded = True
            self._needs_audio_refresh = True
            self.error = ""
            return True
        except Exception as exc:
            self.error = f"Preview audio unavailable: {exc}"
            log.exception("LAB preview load failed for %s", self._wrk_path)
            return False

    def play(self, position_s: float | None = None) -> bool:
        if not self.load():
            return False
        if position_s is not None:
            self.seek(position_s)
        try:
            if self._engine is None:
                from wrekker.core.engine_v2 import AudioEngine

                self._engine = AudioEngine(sr=self._sr, blocksize=256)
                self._engine.start()
                self._engine.set_crossfader(0.0)
                self._engine.set_master_gain(0.90)
                self._engine_ready = True
                self._needs_audio_refresh = True
            self._load_audio_into_engine(self._position_s)
            self._engine.play("A")
            self._playing = True
            log.debug("LAB preview play at %.3fs", self.position_s)
            return True
        except Exception as exc:
            self.error = f"Rust preview engine failed to initialize: {exc}"
            log.exception("LAB preview stream failed for %s", self._wrk_path)
            return False

    def pause(self) -> None:
        if self._engine is not None:
            self._engine.pause("A")
        self._playing = False
        self._position_s = self.position_s
        log.debug("LAB preview pause at %.3fs", self.position_s)

    def stop(self) -> None:
        if self._engine is not None:
            self._engine.pause("A")
            self._engine.seek("A", 0.0)
        self._playing = False
        self._position_s = 0.0
        log.debug("LAB preview stop")

    def seek(self, position_s: float) -> None:
        self._position_s = max(0.0, min(self.duration_s, float(position_s)))
        if self._engine is not None and self._engine_ready:
            self._engine.seek("A", self._position_s)
        log.debug("LAB preview seek %.3fs", position_s)

    def close(self) -> None:
        self._playing = False
        if self._engine is not None:
            try:
                self._engine.pause("A")
                self._engine.stop()
            except Exception:
                pass
            self._engine = None
            self._engine_ready = False

    def _load_audio_into_engine(self, position_s: float) -> None:
        if self._engine is None or self._audio is None:
            return
        if self._needs_audio_refresh or self._play_audio is None:
            self._play_audio = self._build_play_audio()
            self._needs_audio_refresh = False
            self._engine.load_track("A", self._play_audio)
        self._engine.seek("A", position_s)

    def _build_play_audio(self) -> np.ndarray:
        assert self._audio is not None
        base = self._build_stem_monitor_audio()
        if base is None:
            base = np.asarray(self._audio, dtype=np.float32)
        # Leave headroom for source material and metronome clicks. LAB is for
        # verification, so avoiding clipping matters more than unity loudness.
        out = np.ascontiguousarray(base * (0.76 if self._metronome else 0.84))
        if self._metronome and self._click_level > 0.0:
            self._add_clicks(out, 0, self._sr, tuple(self._beats), tuple(self._downbeats), self._click_level)
        return np.tanh(out * 0.95).astype(np.float32, copy=False)

    def _build_stem_monitor_audio(self) -> np.ndarray | None:
        if not self._muted_sources and not self._isolated_source:
            return None
        stems = self._ensure_stems_loaded()
        if not stems:
            return None
        if self._isolated_source in {"VOCALS", "DRUMS", "BASS", "OTHER"}:
            selected_sources = [self._isolated_source]
        else:
            selected_sources = ["VOCALS", "DRUMS", "BASS", "OTHER"]
        selected_sources = [s for s in selected_sources if s not in self._muted_sources]
        if not selected_sources:
            return np.zeros_like(self._audio)
        arrays: list[np.ndarray] = []
        for source in selected_sources:
            for stem_name in _SOURCE_TO_STEMS.get(source, ()):
                if stem_name in stems:
                    arrays.append(stems[stem_name])
        if not arrays:
            return None
        n = min(a.shape[0] for a in arrays)
        out = np.zeros((n, 2), dtype=np.float32)
        for arr in arrays:
            out += arr[:n, :2]
        return np.ascontiguousarray(np.clip(out, -1.25, 1.25))

    def _ensure_stems_loaded(self) -> dict[str, np.ndarray] | None:
        if self._stems_audio is not None:
            return self._stems_audio
        stems = None
        cache = FastloadCache()
        if cache.is_valid(self._wrk_path):
            try:
                stems = cache.load_all_stems(self._wrk_path)
                log.debug("LAB preview loaded stems from fastload: %s", self._wrk_path)
            except Exception as exc:
                log.warning("LAB preview fastload stems failed for %s: %s", self._wrk_path, exc)
        if stems is None:
            try:
                stems = load_wrk_stems(self._wrk_path)
                if stems:
                    log.debug("LAB preview loaded stems from .wrk: %s", self._wrk_path)
            except Exception as exc:
                log.warning("LAB preview .wrk stems failed for %s: %s", self._wrk_path, exc)
                stems = None
        if not stems:
            self._stems_audio = {}
            return None
        self._stems_audio = {str(name): self._stem_to_nf2(audio) for name, audio in stems.items()}
        return self._stems_audio

    @staticmethod
    def _stem_to_nf2(audio) -> np.ndarray:
        arr = np.asarray(audio, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr[:, None]
        elif arr.ndim == 2 and arr.shape[0] in (1, 2) and arr.shape[1] > arr.shape[0]:
            arr = arr.T
        if arr.ndim != 2:
            arr = np.zeros((1, 2), dtype=np.float32)
        if arr.shape[1] == 1:
            arr = np.repeat(arr, 2, axis=1)
        return np.ascontiguousarray(np.clip(arr[:, :2], -1.0, 1.0))

    @staticmethod
    def _add_clicks(chunk: np.ndarray, start_frame: int, sr: int, beats: tuple[float, ...], downbeats: tuple[float, ...], level: float) -> None:
        end_frame = start_frame + len(chunk)
        lo = bisect.bisect_left(beats, start_frame / sr)
        hi = bisect.bisect_right(beats, end_frame / sr)
        downbeat_frames = {int(d * sr) for d in downbeats[max(0, bisect.bisect_left(downbeats, start_frame / sr) - 1): bisect.bisect_right(downbeats, end_frame / sr) + 1]}
        for beat in beats[lo:hi]:
            idx = int(beat * sr) - start_frame
            if 0 <= idx < len(chunk):
                is_down = any(abs(int(beat * sr) - df) <= max(1, int(0.002 * sr)) for df in downbeat_frames)
                length = min(len(chunk) - idx, int(sr * (0.028 if is_down else 0.018)))
                if length <= 0:
                    continue
                freq = 1760.0 if is_down else 1120.0
                env = np.linspace(1.0, 0.0, length, dtype=np.float32) ** 2
                t = np.arange(length, dtype=np.float32) / sr
                click = np.sin(2.0 * np.pi * freq * t).astype(np.float32) * env * level * (0.45 if is_down else 0.26)
                chunk[idx:idx + length, 0] += click
                chunk[idx:idx + length, 1] += click


class _LabWaveform(QWidget):
    position_changed = pyqtSignal(float)

    def __init__(self, mode: str = "zoom", parent=None) -> None:
        super().__init__(parent)
        self._mode = mode
        self.setMinimumHeight(280 if mode == "zoom" else 92)
        self._meta = None
        self._session: Optional[LabEditSession] = None
        self._source = "DRUMS"
        self._position_s = 0.0
        self._compare = False
        self._zoom_window_s = 12.0
        self.setMouseTracking(True)

    def set_data(self, meta, session: LabEditSession) -> None:
        self._meta = meta
        self._session = session
        self.update()

    def set_source(self, source: str) -> None:
        self._source = source
        self.update()

    def set_position(self, pos_s: float) -> None:
        self._position_s = max(0.0, float(pos_s))
        self.update()

    def set_zoom_window(self, seconds: float) -> None:
        self._zoom_window_s = max(2.0, float(seconds))
        self.update()

    def set_compare(self, enabled: bool) -> None:
        self._compare = bool(enabled)
        self.update()

    def _duration(self) -> float:
        if self._meta is None:
            return 1.0
        return max(1.0, float(self._meta.duration_s or 1.0))

    def _values(self) -> np.ndarray:
        if self._meta is None:
            return np.zeros(1024, dtype=np.float32)
        if self._source == "FULL MIX":
            return np.asarray(self._meta.waveform_peaks, dtype=np.float32)
        se = np.asarray(self._meta.stem_energy, dtype=np.float32)
        if se.ndim == 2 and se.shape[1] >= 4:
            if self._source in _STEM_INDEX:
                return se[:, _STEM_INDEX[self._source]]
            return np.max(se[:, :4], axis=1)
        return np.asarray(self._meta.waveform_peaks, dtype=np.float32)

    def _window(self) -> tuple[float, float]:
        duration = self._duration()
        if self._mode == "overview":
            return 0.0, duration
        half = self._zoom_window_s * 0.5
        start = max(0.0, min(duration - self._zoom_window_s, self._position_s - half))
        end = min(duration, max(self._zoom_window_s, start + self._zoom_window_s))
        return start, max(start + 0.1, end)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.fillRect(r, QColor("#050607" if self._mode == "zoom" else "#07090b"))
        p.setPen(QPen(QColor("#1e262c"), 1))
        p.drawRect(r)

        if self._session is None:
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "Open a .wrk track in WREKKER LAB")
            return

        vals = self._values()
        if vals.size < 2:
            return
        vals = np.nan_to_num(vals, nan=0.0, posinf=0.0, neginf=0.0)
        vmax = max(float(np.percentile(np.abs(vals), 98)), 1e-6)
        w = max(1, r.width())
        h = max(1, r.height())
        mid = r.top() + h // 2
        source_color = _SOURCE_COLORS.get(self._source, "#ffb000")
        wave_color = QColor(source_color)
        wave_color.setAlpha(128 if self._mode == "zoom" else 82)
        wave_shadow = QColor(source_color)
        wave_shadow.setAlpha(34 if self._mode == "zoom" else 24)
        p.setPen(QPen(Qt.PenStyle.NoPen))
        p.setBrush(QBrush(wave_shadow))
        duration = self._duration()
        start_s, end_s = self._window()
        window_s = max(0.1, end_s - start_s)
        step = 24 if self._mode == "zoom" else 3
        bar_w = 2 if self._mode == "zoom" else 2
        max_bar_h = h * (0.36 if self._mode == "zoom" else 0.28)
        for x in range(0, w, step):
            t0 = start_s + (x / max(1, w)) * window_s
            t1 = start_s + (min(w, x + step) / max(1, w)) * window_s
            i0 = max(0, min(vals.size - 1, int(t0 / duration * (vals.size - 1))))
            i1 = max(i0 + 1, min(vals.size, int(t1 / duration * vals.size) + 1))
            amp = min(1.0, float(np.max(np.abs(vals[i0:i1]))) / vmax)
            y = max(1, int(amp * max_bar_h))
            bx = r.left() + x
            p.drawRoundedRect(QRectF(bx, mid - y - 1, bar_w, y * 2 + 2), 1.5, 1.5)
        p.setBrush(QBrush(wave_color))
        for x in range(0, w, step):
            t0 = start_s + (x / max(1, w)) * window_s
            t1 = start_s + (min(w, x + step) / max(1, w)) * window_s
            i0 = max(0, min(vals.size - 1, int(t0 / duration * (vals.size - 1))))
            i1 = max(i0 + 1, min(vals.size, int(t1 / duration * vals.size) + 1))
            amp = min(1.0, float(np.max(np.abs(vals[i0:i1]))) / vmax)
            y = max(1, int(amp * max_bar_h))
            bx = r.left() + x
            p.drawRoundedRect(QRectF(bx, mid - y, bar_w, y * 2), 1.5, 1.5)

        active = self._session.draft.active_beatgrid
        auto = self._session.draft.auto_beatgrid
        if self._compare:
            self._draw_grid(p, r, auto.get("beats") or [], QColor(120, 130, 136, 70), every=4)
        beat_every = 1 if self._mode == "zoom" and window_s <= 16.0 else 4
        self._draw_grid(p, r, active.get("beats") or [], QColor(255, 255, 255, 46), every=beat_every)
        self._draw_grid(p, r, active.get("downbeats") or [], QColor(255, 176, 0, 155), every=1, height=0.88)
        for ph in active.get("phrase_markers") or []:
            pos = float(ph.get("position_sec", 0.0) or 0.0)
            if start_s <= pos <= end_s:
                x = r.left() + int((pos - start_s) / window_s * w)
                p.fillRect(x, r.top() + 2, 2, 14 if self._mode == "zoom" else 9, QColor(210, 190, 95, 80))

        for marker in self._session.draft.active_markers:
            pos = float(marker.get("position_s", 0.0) or 0.0)
            if not (start_s <= pos <= end_s):
                continue
            mtype = str(marker.get("type") or "")
            if not _marker_relevant_to_source(mtype, self._source):
                continue
            conf = float(marker.get("confidence", 0.0) or 0.0)
            tier = marker_tier(mtype)
            x = r.left() + int((pos - start_s) / window_s * w)
            color = QColor(marker_color(mtype, conf))
            color.setAlpha(210 if tier == "primary" else 150 if tier == "wrekk" else 70)
            tail = 18 if tier == "primary" else 14 if tier == "wrekk" else 8
            width = 2 if tier == "primary" and self._mode == "zoom" else 1
            p.setPen(QPen(color, width))
            p.drawLine(x, r.bottom() - tail, x, r.bottom())

        for cue in self._session.draft.cues:
            pos = float(cue.get("position_s", 0.0) or 0.0)
            if start_s <= pos <= end_s:
                x = r.left() + int((pos - start_s) / window_s * w)
                p.setPen(QPen(QColor(cue.get("color") or "#18d8ff"), 1))
                p.setBrush(QBrush(QColor(cue.get("color") or "#18d8ff")))
                p.drawPolygon(
                    self._poly([(x, r.top() + 2), (x - 5, r.top() + 12), (x + 5, r.top() + 12)])
                )

        for loop in self._session.draft.loops:
            a = float(loop.get("start_s", 0.0) or 0.0)
            b = float(loop.get("end_s", 0.0) or 0.0)
            if b >= start_s and a <= end_s:
                x1 = r.left() + int((max(a, start_s) - start_s) / window_s * w)
                x2 = r.left() + int((min(b, end_s) - start_s) / window_s * w)
                p.fillRect(QRectF(x1, r.top() + 18, max(2, x2 - x1), 8), QColor(24, 216, 255, 50))

        if self._mode == "overview":
            half = self._zoom_window_s * 0.5
            vs = max(0.0, min(duration - self._zoom_window_s, self._position_s - half))
            ve = min(duration, vs + self._zoom_window_s)
            x1 = r.left() + int(vs / duration * w)
            x2 = r.left() + int(ve / duration * w)
            p.fillRect(QRectF(x1, r.top() + 3, max(4, x2 - x1), r.height() - 6), QColor(255, 176, 0, 28))
            p.setPen(QPen(QColor(255, 176, 0, 130), 1))
            p.drawRect(QRectF(x1, r.top() + 3, max(4, x2 - x1), r.height() - 6))

        px = r.left() + int((self._position_s - start_s) / window_s * w)
        if r.left() <= px <= r.right():
            p.setPen(QPen(QColor(_PLAYHEAD_ACCENT), 4))
            p.drawLine(px, r.top(), px, r.bottom())
            p.setPen(QPen(QColor(_PLAYHEAD_COLOR), 2))
            p.drawLine(px, r.top(), px, r.bottom())
            p.setBrush(QBrush(QColor(_PLAYHEAD_COLOR)))
            p.setPen(QPen(Qt.PenStyle.NoPen))
            p.drawRoundedRect(QRectF(px - 4, r.top() + 2, 8, 14), 3, 3)
        p.setPen(QColor(theme.TEXT_DIM))
        title = f"{'ZOOM EDITOR' if self._mode == 'zoom' else 'OVERVIEW / STRUCTURE'} · {self._source}"
        p.drawText(r.adjusted(8, 4, -8, -4), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, title)

    def _draw_grid(self, p: QPainter, r, values, color: QColor, every: int = 1, height: float = 0.55) -> None:
        start_s, end_s = self._window()
        window_s = max(0.1, end_s - start_s)
        p.setPen(QPen(color, 1))
        w = max(1, r.width())
        top = r.top() + int(r.height() * (1.0 - height))
        for idx, pos in enumerate(values):
            if every > 1 and idx % every:
                continue
            try:
                pos_f = float(pos)
                if not (start_s <= pos_f <= end_s):
                    continue
                x = r.left() + int((pos_f - start_s) / window_s * w)
            except Exception:
                continue
            if r.left() <= x <= r.right():
                p.drawLine(x, top, x, r.bottom())

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._seek_from_x(ev.position().x())

    def mouseMoveEvent(self, ev) -> None:
        if ev.buttons() & Qt.MouseButton.LeftButton:
            self._seek_from_x(ev.position().x())

    def _seek_from_x(self, x: float) -> None:
        start_s, end_s = self._window()
        window_s = max(0.1, end_s - start_s)
        r = self.rect().adjusted(1, 1, -1, -1)
        frac = (float(x) - r.left()) / max(1, r.width())
        self._position_s = max(0.0, min(self._duration(), start_s + frac * window_s))
        self.position_changed.emit(self._position_s)
        self.update()

    @staticmethod
    def _poly(points):
        from PyQt6.QtGui import QPolygon
        from PyQt6.QtCore import QPoint
        return QPolygon([QPoint(int(x), int(y)) for x, y in points])


def _make_lab_waveform(mode: str):
    renderer = os.environ.get("WREKKER_LAB_WAVEFORM_RENDERER", "texture").strip().lower()
    if renderer in {"classic", "legacy", "qwidget"}:
        return _LabWaveform(mode)
    return TextureLabWaveform(mode)


class _SourceCard(QWidget):
    selected = pyqtSignal(str)
    mute_requested = pyqtSignal(str)
    isolate_requested = pyqtSignal(str)

    def __init__(self, source: str, parent=None) -> None:
        super().__init__(parent)
        self._source = source
        self._active = False
        self._available = True
        self._muted = False
        self._isolated = False
        self.setMinimumHeight(54)
        self.setMinimumWidth(132)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._select = QToolButton()
        self._select.setText(source)
        self._select.setCheckable(True)
        self._select.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._select.clicked.connect(lambda: self.selected.emit(self._source))
        lay.addWidget(self._select, 1)
        self._mute = QToolButton()
        self._mute.setText("M")
        self._mute.setFixedSize(24, 28)
        self._mute.clicked.connect(lambda: self.mute_requested.emit(self._source))
        lay.addWidget(self._mute)
        self._solo = QToolButton()
        self._solo.setText("S")
        self._solo.setFixedSize(24, 28)
        self._solo.clicked.connect(lambda: self.isolate_requested.emit(self._source))
        lay.addWidget(self._solo)
        self._apply_style()

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self._select.setChecked(active)
        self._apply_style()

    def set_available(self, available: bool) -> None:
        self._available = bool(available)
        self._select.setEnabled(available)
        self._mute.setEnabled(available)
        self._solo.setEnabled(available)
        self._apply_style()

    def set_monitor(self, muted: bool, isolated: bool) -> None:
        self._muted = bool(muted)
        self._isolated = bool(isolated)
        self._apply_style()

    def _apply_style(self) -> None:
        color = _SOURCE_COLORS.get(self._source, "#ffb000")
        border = color if self._active else "#26323a"
        bg = "#101820" if self._active else "#090d10"
        text = color if self._available else "#68737b"
        self._select.setStyleSheet(
            f"""
            QToolButton {{
                background: {bg};
                color: {text};
                border: 1px solid {border};
                border-radius: 5px;
                font-size: 11px;
                font-weight: 800;
                padding: 6px 10px;
            }}
            QToolButton:hover {{
                border-color: {color};
            }}
            """
        )
        self._mute.setStyleSheet(
            f"""
            QToolButton {{
                background: {'#411016' if self._muted else '#11171b'};
                color: {'#ff9aae' if self._muted else '#8a959d'};
                border: 1px solid {'#ff5c7a' if self._muted else '#33404a'};
                border-radius: 4px;
                font-size: 10px;
                font-weight: 800;
            }}
            """
        )
        self._solo.setStyleSheet(
            f"""
            QToolButton {{
                background: {'#342207' if self._isolated else '#11171b'};
                color: {'#ffd166' if self._isolated else '#8a959d'};
                border: 1px solid {'#ffb000' if self._isolated else '#33404a'};
                border-radius: 4px;
                font-size: 10px;
                font-weight: 800;
            }}
            """
        )


class _HueSatWheel(QWidget):
    colorChanged = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(164, 164)
        self.setMouseTracking(True)
        self._color = QColor("#ffb000")
        self._wheel: QImage | None = None

    def set_color(self, color: str) -> None:
        c = QColor(color)
        if c.isValid():
            self._color = c
            self.update()

    def paintEvent(self, _ev) -> None:
        if self._wheel is None or self._wheel.size() != self.size():
            self._wheel = self._build_wheel()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.drawImage(0, 0, self._wheel)
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        h, s, _v, _a = self._color.getHsvF()
        angle = (h if h >= 0.0 else 0.0) * math.tau
        radius = s * (min(self.width(), self.height()) / 2.0 - 6.0)
        x = cx + math.cos(angle) * radius
        y = cy + math.sin(angle) * radius
        p.setPen(QPen(QColor("#050607"), 4))
        p.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        p.drawEllipse(QRectF(x - 6, y - 6, 12, 12))
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawEllipse(QRectF(x - 6, y - 6, 12, 12))

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._pick(ev.position().x(), ev.position().y())

    def mouseMoveEvent(self, ev) -> None:
        if ev.buttons() & Qt.MouseButton.LeftButton:
            self._pick(ev.position().x(), ev.position().y())

    def _pick(self, x: float, y: float) -> None:
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        dx = x - cx
        dy = y - cy
        max_r = min(self.width(), self.height()) / 2.0 - 6.0
        dist = math.hypot(dx, dy)
        if dist > max_r:
            dx *= max_r / max(dist, 1e-6)
            dy *= max_r / max(dist, 1e-6)
            dist = max_r
        hue = (math.atan2(dy, dx) / math.tau) % 1.0
        sat = max(0.0, min(1.0, dist / max_r))
        self._color = QColor.fromHsvF(hue, sat, 1.0)
        hex_color = self._color.name(QColor.NameFormat.HexRgb)
        self.colorChanged.emit(hex_color)
        self.update()

    def _build_wheel(self) -> QImage:
        w = max(1, self.width())
        h = max(1, self.height())
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        cx = w / 2.0
        cy = h / 2.0
        max_r = min(w, h) / 2.0 - 6.0
        for py in range(h):
            for px in range(w):
                dx = px + 0.5 - cx
                dy = py + 0.5 - cy
                dist = math.hypot(dx, dy)
                if dist <= max_r:
                    hue = (math.atan2(dy, dx) / math.tau) % 1.0
                    sat = max(0.0, min(1.0, dist / max_r))
                    img.setPixelColor(px, py, QColor.fromHsvF(hue, sat, 1.0))
                else:
                    img.setPixelColor(px, py, QColor(0, 0, 0, 0))
        return img


class _CueColorButton(QToolButton):
    colorChanged = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._color = _CUE_COLOR_PRESETS[0][1]
        self._name = _CUE_COLOR_PRESETS[0][0]
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setMinimumWidth(98)
        self._menu = QMenu(self)
        self._menu.setStyleSheet(
            """
            QMenu {
                background: #080b0e;
                border: 1px solid #26323a;
                padding: 8px;
            }
            """
        )
        action = QWidgetAction(self._menu)
        picker = QWidget()
        lay = QVBoxLayout(picker)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)
        self._wheel = _HueSatWheel()
        self._wheel.colorChanged.connect(self._choose_hex)
        lay.addWidget(self._wheel)
        self._hex = QLineEdit(self._color)
        self._hex.setMaxLength(7)
        self._hex.setPlaceholderText("#RRGGBB")
        self._hex.setStyleSheet(
            "QLineEdit { background: #050708; color: #f0f3f5; border: 1px solid #26323a; padding: 5px 7px; }"
        )
        self._hex.textChanged.connect(self._on_hex_text)
        lay.addWidget(self._hex)
        presets = QWidget()
        grid = QGridLayout(presets)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(5)
        for i, (name, color) in enumerate(_CUE_COLOR_PRESETS):
            btn = QToolButton()
            btn.setToolTip(name)
            btn.setFixedSize(23, 23)
            btn.setStyleSheet(
                f"QToolButton {{ background: {color}; border: 1px solid #1a2229; border-radius: 4px; }}"
                "QToolButton:hover { border-color: #ffffff; }"
            )
            btn.clicked.connect(lambda _checked=False, c=color: self._choose_hex(c))
            grid.addWidget(btn, 0, i)
        lay.addWidget(presets)
        action.setDefaultWidget(picker)
        self._menu.addAction(action)
        self.setMenu(self._menu)
        self._apply()

    def color(self) -> str:
        return self._color

    def _choose_hex(self, color: str) -> None:
        c = QColor(color)
        if not c.isValid():
            return
        self._color = c.name(QColor.NameFormat.HexRgb)
        self._name = self._preset_name(self._color)
        self._wheel.set_color(self._color)
        self._hex.blockSignals(True)
        self._hex.setText(self._color)
        self._hex.blockSignals(False)
        self._apply()
        self.colorChanged.emit(self._color)

    def _on_hex_text(self, text: str) -> None:
        text = text.strip()
        if len(text) == 6 and not text.startswith("#"):
            text = "#" + text
        c = QColor(text)
        if len(text) == 7 and c.isValid():
            self._choose_hex(c.name(QColor.NameFormat.HexRgb))

    def _apply(self) -> None:
        swatch = QPixmap(14, 14)
        swatch.fill(QColor(self._color))
        self.setIcon(QIcon(swatch))
        self.setText(self._name if self._name != "Custom" else self._color.upper())
        self.setStyleSheet(
            f"""
            QToolButton {{
                background: #090d10;
                color: #f0f3f5;
                border: 1px solid #26323a;
                border-radius: 4px;
                padding: 4px 8px;
                font-weight: 700;
            }}
            QToolButton::menu-indicator {{
                image: none;
                width: 0px;
            }}
            QToolButton:hover {{
                border-color: {self._color};
            }}
            """
        )

    @staticmethod
    def _preset_name(color: str) -> str:
        color_l = color.lower()
        for name, value in _CUE_COLOR_PRESETS:
            if value.lower() == color_l:
                return name
        return "Custom"


class _LabTimelineQuick(QWidget):
    position_changed = pyqtSignal(float)
    source_changed = pyqtSignal(str)
    stem_mute_requested = pyqtSignal(str)
    stem_isolate_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._model = LabTimelineModel(self)
        self._available = False
        self._quick = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        if os.environ.get("WREKKER_LAB_FORCE_WIDGET_TIMELINE", "0").strip().lower() in {"1", "true", "yes", "on"}:
            return
        try:
            from PyQt6.QtCore import QUrl
            from PyQt6.QtQuickWidgets import QQuickWidget

            self._quick = QQuickWidget()
            self._quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
            self._quick.rootContext().setContextProperty("labTimelineModel", self._model)
            qml_path = Path(__file__).resolve().parents[1] / "qml" / "LabTimeline.qml"
            self._quick.setSource(QUrl.fromLocalFile(str(qml_path)))
            root = self._quick.rootObject()
            if root is None:
                raise RuntimeError("LabTimeline.qml did not create a root object")
            root.seekRequested.connect(self.position_changed.emit)
            self._model.sourceSelected.connect(self.source_changed.emit)
            self._model.stemMuteRequested.connect(self.stem_mute_requested.emit)
            self._model.stemIsolateRequested.connect(self.stem_isolate_requested.emit)
            lay.addWidget(self._quick)
            self._available = True
        except Exception as exc:
            log.warning("Qt Quick LAB timeline unavailable; using QWidget fallback: %s", exc)
            label = QLabel("Qt Quick timeline unavailable; using QWidget timeline fallback.")
            label.setStyleSheet("color: #ffb000; background: #090d10; border: 1px solid #26323a; padding: 8px;")
            lay.addWidget(label)

    @property
    def available(self) -> bool:
        return self._available

    def set_data(
        self,
        meta,
        session: LabEditSession,
        source: str,
        compare: bool,
        muted_sources: set[str] | None = None,
        isolated_source: str | None = None,
    ) -> None:
        self._model.set_stem_monitor(muted_sources or set(), isolated_source)
        self._model.sync_from_lab(meta, session, source, compare)

    def set_position(self, position_s: float) -> None:
        self._model.set_position(position_s)

    def set_playing(self, playing: bool) -> None:
        self._model.set_playing(playing)


class WrekkerLabWindow(QMainWindow):
    saved = pyqtSignal(str)

    def __init__(self, wrk_path: str | Path, db: PreparedDB | None = None, settings_store=None, parent=None) -> None:
        super().__init__(parent)
        self._wrk_path = Path(wrk_path)
        self._db = db
        self._settings_store = settings_store
        self._session = begin_lab_session(self._wrk_path)
        self._meta = load_wrk_metadata(self._wrk_path)
        self._position_s = 0.0
        self._preview = _LabPreviewController(self._wrk_path, self._session)
        self._stem_mutes: set[str] = set()
        self._stem_isolate: str | None = None
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(33)
        self._play_timer.timeout.connect(self._on_preview_tick)
        self._source_cards: dict[str, _SourceCard] = {}
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(self._window_title())
        self.resize(1320, 860)
        self.setMinimumSize(1060, 680)
        self._build_ui()
        self._apply_settings_defaults()
        self._refresh_all()

    def _window_title(self) -> str:
        s = self._session.draft
        name = f"{s.artist} — {s.title}" if s.artist else s.title
        return f"WREKKER LAB — {name}"

    def _default_source_from_settings(self) -> str:
        source = "DRUMS" if self._session.draft.has_stems else "FULL MIX"
        if self._settings_store is not None:
            configured = str(self._settings_store.get("lab.default_waveform_source", source) or source)
            if configured != "Last Used":
                source = configured
        if source not in _SOURCE_LABELS:
            source = "FULL MIX"
        if source != "FULL MIX" and not self._session.draft.has_stems:
            source = "FULL MIX"
        return source

    def _apply_settings_defaults(self) -> None:
        if self._settings_store is None:
            return
        self._metronome.setChecked(bool(self._settings_store.get("lab.default_metronome", False)))
        self._preview.set_metronome(self._metronome.isChecked(), self._click.value() / 100.0)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setStyleSheet(f"background: {theme.BG_DEEP}; color: {theme.TEXT_MED};")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)
        self.setCentralWidget(root)

        header = QHBoxLayout()
        header.addWidget(logo_label(24))
        self._title = QLabel()
        self._title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 18px; font-weight: 800;")
        header.addWidget(self._title, 1)
        self._status_badges = QHBoxLayout()
        self._status_badges.setSpacing(5)
        header.addLayout(self._status_badges)
        lay.addLayout(header)

        toolbar = QHBoxLayout()
        self._play = QPushButton("PLAY")
        self._play.clicked.connect(self._on_play_pause)
        toolbar.addWidget(self._play)
        self._stop = QPushButton("STOP")
        self._stop.clicked.connect(self._on_stop)
        toolbar.addWidget(self._stop)
        self._metronome = QCheckBox("METRONOME")
        self._metronome.toggled.connect(self._on_metronome_changed)
        toolbar.addWidget(self._metronome)
        toolbar.addWidget(QLabel("Click"))
        self._click = QSlider(Qt.Orientation.Horizontal)
        self._click.setRange(0, 100)
        click_level = 65
        if self._settings_store is not None:
            try:
                click_level = int(float(self._settings_store.get("lab.metronome_level", 0.65)) * 100)
            except Exception:
                click_level = 65
        self._click.setValue(max(0, min(100, click_level)))
        self._click.valueChanged.connect(self._on_metronome_changed)
        self._click.setFixedWidth(90)
        toolbar.addWidget(self._click)
        self._pos_lbl = QLabel("0:00.00")
        self._pos_lbl.setMinimumWidth(72)
        toolbar.addWidget(self._pos_lbl)
        self._bpm_lbl = QLabel("")
        toolbar.addWidget(self._bpm_lbl)
        toolbar.addStretch()
        self._undo = QPushButton("UNDO")
        self._undo.clicked.connect(self._on_undo)
        toolbar.addWidget(self._undo)
        self._redo = QPushButton("REDO")
        self._redo.clicked.connect(self._on_redo)
        toolbar.addWidget(self._redo)
        lay.addLayout(toolbar)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        lay.addWidget(split, 1)

        timeline = QWidget()
        tlay = QVBoxLayout(timeline)
        tlay.setContentsMargins(0, 0, 0, 0)
        tlay.setSpacing(6)
        self._source = self._default_source_from_settings()
        self._compare = QCheckBox("COMPARE AUTO")
        if self._settings_store is not None:
            self._compare.setChecked(bool(self._settings_store.get("lab.default_compare_mode", False)))
        self._compare.toggled.connect(self._on_compare)
        self._quick_timeline = _LabTimelineQuick()
        self._quick_timeline.position_changed.connect(self._set_position)
        self._quick_timeline.source_changed.connect(self._on_source_changed)
        self._quick_timeline.stem_mute_requested.connect(self._on_stem_mute_requested)
        self._quick_timeline.stem_isolate_requested.connect(self._on_stem_isolate_requested)
        if self._quick_timeline.available:
            compare_row = QHBoxLayout()
            compare_row.addWidget(self._compare)
            compare_row.addStretch()
            tlay.addLayout(compare_row)
            tlay.addWidget(self._quick_timeline, 1)
            self._zoom = None
            self._overview = None
        else:
            row = QHBoxLayout()
            row.addWidget(QLabel("Waveform sources"))
            for source in _SOURCE_LABELS:
                card = _SourceCard(source)
                card.selected.connect(self._on_source_changed)
                card.mute_requested.connect(self._on_stem_mute_requested)
                card.isolate_requested.connect(self._on_stem_isolate_requested)
                self._source_cards[source] = card
                row.addWidget(card)
            row.addWidget(self._compare)
            row.addStretch()
            tlay.addLayout(row)
            self._zoom = _make_lab_waveform("zoom")
            self._zoom.setMinimumHeight(340)
            self._zoom.position_changed.connect(self._set_position)
            tlay.addWidget(self._zoom, 1)
            self._overview = _make_lab_waveform("overview")
            self._overview.setMinimumHeight(108)
            self._overview.position_changed.connect(self._set_position)
            tlay.addWidget(self._overview)
        split.addWidget(timeline)

        self._tabs = QTabWidget()
        self._tabs.setMinimumWidth(390)
        self._tabs.addTab(self._beatgrid_tab(), "Beatgrid")
        self._tabs.addTab(self._markers_tab(), "Markers")
        self._tabs.addTab(self._dj_tab(), "Cues / Loops")
        self._tabs.addTab(self._history_tab(), "History")
        split.addWidget(self._tabs)
        split.setSizes([850, 430])

        bar = QHBoxLayout()
        self._revert_auto = QPushButton("REVERT TO AUTO")
        self._revert_auto.clicked.connect(self._on_revert_auto)
        bar.addWidget(self._revert_auto)
        self._mark_verified = QPushButton("MARK MANUAL VERIFIED")
        self._mark_verified.clicked.connect(self._on_mark_verified)
        bar.addWidget(self._mark_verified)
        bar.addStretch()
        self._save = QPushButton("SAVE CORRECTIONS")
        self._save.setStyleSheet("background: #ffb000; color: #121212; font-weight: 800;")
        self._save.clicked.connect(self._on_save)
        bar.addWidget(self._save)
        close = QPushButton("CLOSE")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        lay.addLayout(bar)

    def _beatgrid_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self._grid_info = QLabel()
        self._grid_info.setWordWrap(True)
        self._grid_info.setStyleSheet(f"color: {theme.TEXT_MED}; background: #07090b; border: 1px solid #1f2a32; padding: 8px;")
        lay.addWidget(self._grid_info)
        form = QFormLayout()
        self._bpm = QDoubleSpinBox()
        self._bpm.setRange(40.0, 260.0)
        self._bpm.setDecimals(4)
        self._bpm.setSingleStep(0.01)
        form.addRow("Active BPM", self._bpm)
        self._shift_ms = QDoubleSpinBox()
        self._shift_ms.setRange(-5000, 5000)
        self._shift_ms.setDecimals(1)
        self._shift_ms.setSingleStep(1.0)
        form.addRow("Shift ms", self._shift_ms)
        self._phrase_bars = QComboBox()
        self._phrase_bars.addItems(["8", "16", "32"])
        phrase_len = "16"
        if self._settings_store is not None:
            try:
                phrase_len = str(int(self._settings_store.get("lab.default_phrase_length", 16)))
            except Exception:
                phrase_len = "16"
        self._phrase_bars.setCurrentText(phrase_len if phrase_len in {"8", "16", "32"} else "16")
        form.addRow("Phrase bars", self._phrase_bars)
        lay.addLayout(form)
        grid = QGridLayout()
        buttons = [
            ("SET FIRST BEAT", self._on_set_first_beat),
            ("SHIFT GRID", self._on_shift_grid),
            ("SET BPM", self._on_set_bpm),
            ("BPM x2", lambda: self._edit(lambda s: s.multiply_bpm(2.0))),
            ("BPM /2", lambda: self._edit(lambda s: s.multiply_bpm(0.5))),
            ("SET DOWNBEAT", self._on_set_downbeat),
            ("SNAP PLAYHEAD TO DRUM", self._on_snap_playhead),
            ("REGEN PHRASES", self._on_regen_phrases),
        ]
        for i, (text, cb) in enumerate(buttons):
            b = QPushButton(text)
            b.clicked.connect(cb)
            grid.addWidget(b, i // 2, i % 2)
        lay.addLayout(grid)
        self._compare_text = QTextEdit()
        self._compare_text.setReadOnly(True)
        self._compare_text.setMaximumHeight(150)
        lay.addWidget(self._compare_text)
        lay.addStretch()
        return w

    def _markers_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self._lab_horizon = StemHorizonWidget()
        self._lab_horizon.configure(mode="Future Bars", range_bars=16, show_countdown=True, show_w_flag=True)
        lay.addWidget(self._lab_horizon)
        self._marker_counts = QLabel()
        self._marker_counts.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        lay.addWidget(self._marker_counts)
        self._marker_table = QTableWidget(0, 8)
        self._marker_table.setHorizontalHeaderLabels(["Time", "Category", "Marker", "Detail", "Confidence", "Source", "Status", "Evidence"])
        self._marker_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._marker_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._marker_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self._marker_table, 1)
        row = QHBoxLayout()
        self._marker_category = QComboBox()
        self._marker_category.addItems(["PRIMARY", "WREKK", "GUIDE"])
        self._marker_category.currentTextChanged.connect(self._refresh_marker_type_controls)
        row.addWidget(self._marker_category)
        self._marker_label = QComboBox()
        self._marker_label.currentTextChanged.connect(self._refresh_marker_detail_controls)
        row.addWidget(self._marker_label)
        self._marker_detail = QComboBox()
        row.addWidget(self._marker_detail)
        add = QPushButton("ADD")
        add.clicked.connect(self._on_add_marker)
        row.addWidget(add)
        apply_type = QPushButton("APPLY TYPE")
        apply_type.clicked.connect(self._on_apply_marker_type)
        row.addWidget(apply_type)
        move = QPushButton("MOVE")
        move.clicked.connect(self._on_move_marker)
        row.addWidget(move)
        lock = QPushButton("LOCK/UNLOCK")
        lock.clicked.connect(self._on_toggle_marker_lock)
        row.addWidget(lock)
        delete = QPushButton("DELETE")
        delete.clicked.connect(self._on_delete_marker)
        row.addWidget(delete)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        clear = QPushButton("CLEAR UNLOCKED AUTO")
        clear.clicked.connect(lambda: self._edit(lambda s: s.clear_unlocked_auto_markers()))
        row2.addWidget(clear)
        cue = QPushButton("CONVERT TO HOT CUE")
        cue.clicked.connect(self._on_marker_to_cue)
        row2.addWidget(cue)
        lay.addLayout(row2)
        self._marker_notes = QLineEdit()
        self._marker_notes.setPlaceholderText("Notes / reason for new manual marker")
        lay.addWidget(self._marker_notes)
        self._refresh_marker_type_controls()
        return w

    def _dj_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self._cue_table = QTableWidget(0, 3)
        self._cue_table.setHorizontalHeaderLabels(["Time", "Label", "Color"])
        lay.addWidget(QLabel("Hot Cues"))
        lay.addWidget(self._cue_table)
        row = QHBoxLayout()
        self._cue_label = QLineEdit("Cue")
        row.addWidget(self._cue_label)
        row.addWidget(QLabel("Color"))
        self._cue_color = _CueColorButton()
        row.addWidget(self._cue_color)
        add = QPushButton("ADD HOT CUE")
        add.clicked.connect(self._on_add_cue)
        row.addWidget(add)
        delete = QPushButton("DELETE")
        delete.clicked.connect(self._on_delete_cue)
        row.addWidget(delete)
        lay.addLayout(row)
        self._loop_table = QTableWidget(0, 3)
        self._loop_table.setHorizontalHeaderLabels(["In", "Out", "Label"])
        lay.addWidget(QLabel("Saved Loops"))
        lay.addWidget(self._loop_table)
        row2 = QHBoxLayout()
        self._loop_len = QComboBox()
        self._loop_len.addItems(["0.5 bar", "1 bar", "2 bars", "4 bars", "8 bars", "16 bars"])
        self._loop_len.setCurrentText("4 bars")
        row2.addWidget(self._loop_len)
        loop = QPushButton("ADD LOOP")
        loop.clicked.connect(self._on_add_loop)
        row2.addWidget(loop)
        del_loop = QPushButton("DELETE")
        del_loop.clicked.connect(self._on_delete_loop)
        row2.addWidget(del_loop)
        lay.addLayout(row2)
        return w

    def _history_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self._show_technical_history = QCheckBox("Show Technical Details")
        self._show_technical_history.toggled.connect(lambda _v: self._refresh_all())
        lay.addWidget(self._show_technical_history)
        self._history = QTextEdit()
        self._history.setReadOnly(True)
        lay.addWidget(self._history)
        return w

    def _edit(self, fn) -> None:
        try:
            fn(self._session)
            self._preview.set_session(self._session)
            self._refresh_all()
        except Exception as exc:
            QMessageBox.warning(self, "WREKKER LAB", str(exc))

    def _refresh_all(self) -> None:
        s = self._session.draft
        self._title.setText(f"{s.artist} — {s.title}" if s.artist else s.title)
        self._refresh_status_badges()
        self._bpm.setValue(float(s.active_bpm))
        self._bpm_lbl.setText(f"BPM {s.active_bpm:.2f} · Auto {s.auto_bpm:.2f}")
        self._pos_lbl.setText(_fmt_time(self._position_s))
        source = self._source
        if self._quick_timeline.available:
            self._quick_timeline.set_data(
                self._meta,
                self._session,
                source,
                self._compare.isChecked(),
                self._stem_mutes,
                self._stem_isolate,
            )
            self._quick_timeline.set_position(self._position_s)
            self._quick_timeline.set_playing(self._preview.is_playing)
        else:
            self._refresh_source_cards()
            self._overview.set_data(self._meta, self._session)
            self._zoom.set_data(self._meta, self._session)
            self._overview.set_source(source)
            self._zoom.set_source(source)
            self._overview.set_position(self._position_s)
            self._zoom.set_position(self._position_s)
            self._overview.set_compare(self._compare.isChecked())
            self._zoom.set_compare(self._compare.isChecked())
        self._refresh_tables()
        self._grid_info.setText(self._format_grid_info())
        self._compare_text.setPlainText(self._format_compare_summary())
        self._history.setPlainText(self._format_history())

    def _refresh_status_badges(self) -> None:
        while self._status_badges.count():
            item = self._status_badges.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        seen: set[str] = set()
        for text, kind, tip in self._status_items():
            if text in seen:
                continue
            seen.add(text)
            lbl = QLabel(text)
            lbl.setToolTip(tip)
            colors = {
                "ok": ("#0d1c19", "#35e6b5", "#1f5f52"),
                "warn": ("#1f1708", "#ffb000", "#6c4c10"),
                "info": ("#10151a", "#a7b0b8", "#293540"),
                "dirty": ("#281606", "#ffb000", "#ffb000"),
            }.get(kind, ("#10151a", "#a7b0b8", "#293540"))
            lbl.setStyleSheet(
                f"background: {colors[0]}; color: {colors[1]}; border: 1px solid {colors[2]}; "
                "border-radius: 5px; padding: 3px 7px; font-size: 10px; font-weight: 800;"
            )
            self._status_badges.addWidget(lbl)

    def _status_items(self) -> list[tuple[str, str, str]]:
        s = self._session.draft
        items = [("WRK READY", "ok", "Valid .wrk container is loaded in LAB.")]
        try:
            cache_ok = FastloadCache().is_valid(self._wrk_path)
        except Exception:
            cache_ok = False
        items.append(("FASTLOAD READY" if cache_ok else "NO CACHE", "ok" if cache_ok else "info", "Fastload cache status. LAB can still use audio inside the .wrk."))
        items.append(("STEMS READY" if s.has_stems else "STEMS MISSING", "ok" if s.has_stems else "info", "Stem waveform availability for correction views."))
        if not s.source_available:
            items.append(("SOURCE OFFLINE", "info", "Original source path is offline; LAB can still edit this valid .wrk."))
        status = s.corrections.get("analysis_status") or LabStatus.AUTO_ANALYZED
        if s.dynamic_tempo:
            items.append(("DYNAMIC TEMPO · LIMITED EDITING", "info", "Dynamic tempo detected. Warp-anchor editing is planned; marker/cue preparation remains available."))
        elif status:
            items.append((str(status), "ok" if status == LabStatus.MANUAL_VERIFIED else "info", "Active analysis status."))
        if s.corrections.get("grid_edited"):
            items.append(("GRID EDITED", "warn", "Active beatgrid differs from preserved AUTO analysis."))
        if s.corrections.get("markers_edited"):
            items.append(("MARKERS EDITED", "warn", "Active markers differ from preserved AUTO markers."))
        if self._session.dirty:
            items.append(("DIRTY", "dirty", "Unsaved LAB edits are in memory."))
        return items

    def _refresh_tables(self) -> None:
        s = self._session.draft
        if hasattr(self, "_lab_horizon"):
            self._lab_horizon.set_horizon(getattr(self._meta, "stem_horizon", None) if self._meta else None)
            self._lab_horizon.set_markers(s.active_markers)
            self._lab_horizon.set_position(self._position_s)
        filtered = int(s.corrections.get("filtered_low_confidence_auto_markers") or 0)
        retained = int(s.corrections.get("manual_or_locked_low_confidence_retained") or 0)
        self._marker_counts.setText(
            f"Active markers: {len(s.active_markers)} · Filtered low-confidence auto markers: {filtered} · "
            f"Manual/locked low-confidence retained: {retained}"
        )
        self._marker_table.setRowCount(len(s.active_markers))
        for row, m in enumerate(s.active_markers):
            category, label, detail = marker_ui_parts(str(m.get("type", "")))
            conf = float(m.get("confidence", 0.0) or 0.0)
            vals = [
                _fmt_time(float(m.get("position_s", 0.0) or 0.0)),
                category.title(),
                label,
                detail or "-",
                f"{conf * 100:.0f}%",
                marker_source_label(m),
                marker_status_label(m),
                str(m.get("reason") or (m.get("evidence") or {}).get("reason") or "-"),
            ]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, m.get("id"))
                if col == 4 and conf < MARKER_MIN_CONFIDENCE:
                    item.setForeground(QBrush(QColor("#ffb000")))
                self._marker_table.setItem(row, col, item)
        self._cue_table.setRowCount(len(s.cues))
        for row, c in enumerate(s.cues):
            color = str(c.get("color") or "#ffb000")
            for col, val in enumerate([_fmt_time(c.get("position_s", 0.0)), c.get("label", ""), ""]):
                item = QTableWidgetItem(str(val))
                if col == 2:
                    item.setIcon(self._cue_led_icon(color))
                    item.setToolTip(color.upper())
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._cue_table.setItem(row, col, item)
        self._loop_table.setRowCount(len(s.loops))
        for row, lp in enumerate(s.loops):
            for col, val in enumerate([_fmt_time(lp.get("start_s", 0.0)), _fmt_time(lp.get("end_s", 0.0)), lp.get("label", "")]):
                self._loop_table.setItem(row, col, QTableWidgetItem(str(val)))

    def _format_history(self) -> str:
        lines = []
        for idx, rev in enumerate(self._session.draft.changelog.get("revisions", []), 1):
            lines.append(f"Revision {idx} · {self._format_revision_time(rev.get('timestamp', ''))}")
            lines.append(human_revision_title(rev))
            for ch in rev.get("changes", []):
                lines.append(f"• {human_change_sentence(ch)}")
            if self._show_technical_history.isChecked():
                lines.append("")
                lines.append("Technical details:")
                lines.append(json.dumps(rev, indent=2, ensure_ascii=False))
            lines.append("")
        return "\n".join(lines) or "No LAB revisions yet."

    def _format_grid_info(self) -> str:
        s = self._session.draft
        meta = s.manifest.get("metadata", {})
        first = s.active_beatgrid.get("beats", [None])[0] if s.active_beatgrid.get("beats") else None
        auto_first = s.auto_beatgrid.get("beats", [None])[0] if s.auto_beatgrid.get("beats") else None
        offset = ""
        if first is not None and auto_first is not None:
            offset = f" · Grid Offset {((float(first) - float(auto_first)) * 1000.0):+.1f} ms"
        return (
            f"Metadata BPM {float(meta.get('bpm') or 0.0):.2f} · Auto BPM {s.auto_bpm:.3f} · "
            f"Active BPM {s.active_bpm:.3f}{offset}\n"
            f"Beatgrid confidence {float(s.active_beatgrid.get('confidence', 0.0) or 0.0) * 100:.0f}% · "
            f"Grid Source {s.corrections.get('analysis_status') or LabStatus.AUTO_ANALYZED}"
        )

    def _format_compare_summary(self) -> str:
        c = self._session.compare_auto_active()
        return (
            "AUTO vs ACTIVE\n"
            f"BPM: {c['auto_bpm']:.3f} → {c['active_bpm']:.3f}\n"
            f"First beat delta: {c['first_beat_delta_ms']} ms\n"
            f"Beats: {c['beat_count_auto']} auto / {c['beat_count_active']} active\n"
            f"Downbeats: {c['downbeat_count_auto']} auto / {c['downbeat_count_active']} active\n"
            f"Phrases: {c['phrase_count_auto']} auto / {c['phrase_count_active']} active\n"
            f"Markers: {c['marker_count_auto']} auto / {c['marker_count_active']} active · changed {c['changed_marker_count']}"
        )

    @staticmethod
    def _format_revision_time(value: str) -> str:
        if not value:
            return ""
        return value.replace("T", " ").replace("+00:00", " UTC")

    def _refresh_source_cards(self) -> None:
        for source, card in self._source_cards.items():
            card.set_available(source == "FULL MIX" or self._session.draft.has_stems)
            card.set_active(source == self._source)
            card.set_monitor(
                self._source_muted(source),
                source == self._stem_isolate,
            )

    def _source_muted(self, source: str) -> bool:
        if source in {"FULL MIX", "ANATOMY"}:
            return {"VOCALS", "DRUMS", "BASS", "OTHER"}.issubset(self._stem_mutes)
        return source in self._stem_mutes

    def _selected_cue_color(self) -> str:
        return str(self._cue_color.color() or "#ffb000")

    @staticmethod
    def _cue_color_name(color: str) -> str:
        color_l = str(color or "").lower()
        for name, value in _CUE_COLOR_PRESETS:
            if value.lower() == color_l:
                return name
        return "Custom"

    @staticmethod
    def _cue_led_icon(color: str) -> QIcon:
        pix = QPixmap(18, 18)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QColor(color)
        if not c.isValid():
            c = QColor("#ffb000")
        glow = QColor(c)
        glow.setAlpha(70)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QRectF(1, 1, 16, 16))
        p.setBrush(QBrush(c))
        p.drawEllipse(QRectF(4, 4, 10, 10))
        p.setPen(QPen(QColor(255, 255, 255, 130), 1))
        p.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        p.drawEllipse(QRectF(4, 4, 10, 10))
        p.end()
        return QIcon(pix)

    def _set_position(self, pos_s: float) -> None:
        self._position_s = max(0.0, min(self._session.draft.duration_s, float(pos_s)))
        self._preview.seek(self._position_s)
        self._refresh_all()

    def _on_source_changed(self, source: str) -> None:
        if source not in _SOURCE_LABELS:
            source = "FULL MIX"
        self._source = source
        self._refresh_all()

    def _on_stem_mute_requested(self, source: str) -> None:
        if source not in _SOURCE_LABELS:
            return
        stem_sources = {"VOCALS", "DRUMS", "BASS", "OTHER"}
        if source in {"FULL MIX", "ANATOMY"}:
            if stem_sources.issubset(self._stem_mutes):
                self._stem_mutes.clear()
            else:
                self._stem_mutes = set(stem_sources)
            self._stem_isolate = None
        else:
            if source in self._stem_mutes:
                self._stem_mutes.remove(source)
            else:
                self._stem_mutes.add(source)
            if self._stem_isolate == source:
                self._stem_isolate = None
        self._apply_stem_monitor()

    def _on_stem_isolate_requested(self, source: str) -> None:
        if source not in _SOURCE_LABELS:
            return
        if source in {"FULL MIX", "ANATOMY"}:
            self._stem_isolate = None
            self._stem_mutes.clear()
        else:
            self._stem_isolate = None if self._stem_isolate == source else source
            self._stem_mutes.discard(source)
        self._apply_stem_monitor()

    def _apply_stem_monitor(self) -> None:
        self._preview.set_stem_monitor(self._stem_mutes, self._stem_isolate)
        if self._stem_isolate:
            msg = f"LAB monitor isolated: {self._stem_isolate}"
        elif self._stem_mutes:
            msg = "LAB monitor muted: " + ", ".join(sorted(self._stem_mutes))
        else:
            msg = "LAB monitor: full mix"
        self.statusBar().showMessage(msg, 3000)
        self._refresh_all()

    def _on_compare(self, _enabled: bool) -> None:
        self._refresh_all()

    def _on_play_pause(self) -> None:
        if self._preview.is_playing:
            self._preview.pause()
            self._play.setText("PLAY")
            self._play_timer.stop()
            if self._quick_timeline.available:
                self._quick_timeline.set_playing(False)
            return
        self._preview.refresh_grid()
        self._preview.set_metronome(self._metronome.isChecked(), self._click.value() / 100.0)
        if not self._preview.play(self._position_s):
            msg = self._preview.error or "Preview playback failed."
            self.statusBar().showMessage(msg, 6000)
            QMessageBox.warning(self, "WREKKER LAB Preview", msg)
            return
        self._play.setText("PAUSE")
        if self._quick_timeline.available:
            self._quick_timeline.set_playing(True)
        self._play_timer.start()

    def _on_stop(self) -> None:
        self._preview.stop()
        self._position_s = 0.0
        self._play.setText("PLAY")
        self._play_timer.stop()
        if self._quick_timeline.available:
            self._quick_timeline.set_playing(False)
            self._quick_timeline.set_position(0.0)
        self._refresh_all()

    def _on_preview_tick(self) -> None:
        self._position_s = max(0.0, min(self._session.draft.duration_s, self._preview.position_s))
        self._pos_lbl.setText(_fmt_time(self._position_s))
        if self._quick_timeline.available:
            self._quick_timeline.set_position(self._position_s)
            self._quick_timeline.set_playing(self._preview.is_playing)
        else:
            self._zoom.set_position(self._position_s)
            self._overview.set_position(self._position_s)
        if hasattr(self, "_lab_horizon"):
            self._lab_horizon.set_position(self._position_s)
        if not self._preview.is_playing:
            self._play.setText("PLAY")
            self._play_timer.stop()
            if self._quick_timeline.available:
                self._quick_timeline.set_playing(False)

    def _on_metronome_changed(self, *_args) -> None:
        self._preview.set_metronome(self._metronome.isChecked(), self._click.value() / 100.0)

    def _on_undo(self) -> None:
        self._session.undo()
        self._preview.set_session(self._session)
        self._refresh_all()

    def _on_redo(self) -> None:
        self._session.redo()
        self._preview.set_session(self._session)
        self._refresh_all()

    def _on_set_first_beat(self) -> None:
        self._edit(lambda s: s.set_first_beat(self._position_s, "set from LAB playhead"))

    def _on_shift_grid(self) -> None:
        self._edit(lambda s: s.shift_grid(self._shift_ms.value() / 1000.0, "manual LAB shift"))

    def _on_set_bpm(self) -> None:
        self._edit(lambda s: s.set_bpm(self._bpm.value(), anchor_s=self._position_s, reason="manual LAB BPM"))

    def _on_set_downbeat(self) -> None:
        bars = int(self._phrase_bars.currentText())
        self._edit(lambda s: s.set_downbeat(self._position_s, phrase_bars=bars))

    def _on_regen_phrases(self) -> None:
        bars = int(self._phrase_bars.currentText())
        self._edit(lambda s: s.regenerate_phrases_from_downbeat(self._position_s, phrase_bars=bars))

    def _on_snap_playhead(self) -> None:
        pos = nearest_transient_from_energy(self._meta.stem_energy, self._position_s, self._session.draft.duration_s)
        if pos is None:
            QMessageBox.information(self, "WREKKER LAB", "No drum transient candidate found near playhead.")
            return
        delta_ms = (pos - self._position_s) * 1000.0
        self._set_position(pos)
        self.statusBar().showMessage(f"Snapped to drum transient: {delta_ms:+.1f} ms", 3000)

    def _refresh_marker_type_controls(self) -> None:
        category = self._marker_category.currentText() if hasattr(self, "_marker_category") else "PRIMARY"
        self._marker_label.blockSignals(True)
        self._marker_label.clear()
        self._marker_label.addItems(_CATEGORY_LABELS.get(category, ()))
        self._marker_label.blockSignals(False)
        self._refresh_marker_detail_controls()

    def _refresh_marker_detail_controls(self) -> None:
        category = self._marker_category.currentText() if hasattr(self, "_marker_category") else "PRIMARY"
        label = self._marker_label.currentText() if hasattr(self, "_marker_label") else ""
        self._marker_detail.clear()
        details = _DETAIL_LABELS.get((category, label), ("",))
        self._marker_detail.addItems(details)
        self._marker_detail.setEnabled(any(details))

    def _selected_ui_marker_type(self) -> tuple[str, str]:
        detail = self._marker_detail.currentText() if self._marker_detail.isEnabled() else ""
        mtype = marker_type_from_ui(self._marker_category.currentText(), self._marker_label.currentText(), detail)
        label = self._marker_label.currentText()
        return mtype, label

    def _selected_marker_id(self) -> Optional[str]:
        row = self._marker_table.currentRow()
        if row < 0:
            return None
        item = self._marker_table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _selected_marker(self) -> Optional[dict]:
        mid = self._selected_marker_id()
        if not mid:
            return None
        for marker in self._session.draft.active_markers:
            if marker.get("id") == mid:
                return marker
        return None

    def _on_add_marker(self) -> None:
        mtype, label = self._selected_ui_marker_type()
        reason = self._marker_notes.text().strip()
        self._edit(lambda s: s.add_marker(self._position_s, mtype, label=label, reason=reason))

    def _on_apply_marker_type(self) -> None:
        mid = self._selected_marker_id()
        if not mid:
            return
        mtype, label = self._selected_ui_marker_type()
        self._edit(lambda s: s.update_marker(mid, type=mtype, label=label, reason=self._marker_notes.text().strip()))

    def _on_move_marker(self) -> None:
        mid = self._selected_marker_id()
        if mid:
            self._edit(lambda s: s.update_marker(mid, position_s=round(self._position_s, 6)))

    def _on_toggle_marker_lock(self) -> None:
        marker = self._selected_marker()
        if marker:
            self._edit(lambda s: s.lock_marker(marker.get("id"), not bool(marker.get("locked"))))

    def _on_delete_marker(self) -> None:
        mid = self._selected_marker_id()
        if mid:
            self._edit(lambda s: s.delete_marker(mid))

    def _on_marker_to_cue(self) -> None:
        marker = self._selected_marker()
        if not marker:
            return
        _cat, label, detail = marker_ui_parts(str(marker.get("type") or ""))
        label = f"{label} {detail}".strip()
        self._edit(lambda s: s.add_hot_cue(float(marker.get("position_s", 0.0)), label=label, color=self._selected_cue_color()))

    def _on_add_cue(self) -> None:
        self._edit(lambda s: s.add_hot_cue(self._position_s, self._cue_label.text().strip() or "Cue", color=self._selected_cue_color()))

    def _on_delete_cue(self) -> None:
        self._edit(lambda s: s.delete_hot_cue(self._cue_table.currentRow()))

    def _on_add_loop(self) -> None:
        bpm = self._session.draft.active_bpm
        bars = float(self._loop_len.currentText().split()[0])
        dur = 60.0 / max(1.0, bpm) * 4.0 * bars
        self._edit(lambda s: s.add_loop(self._position_s, self._position_s + dur, label=self._loop_len.currentText()))

    def _on_delete_loop(self) -> None:
        self._edit(lambda s: s.delete_loop(self._loop_table.currentRow()))

    def _on_revert_auto(self) -> None:
        if QMessageBox.question(self, "WREKKER LAB", "Revert active analysis to original AUTO analysis?") == QMessageBox.StandardButton.Yes:
            self._edit(lambda s: s.revert_active_to_auto())

    def _on_mark_verified(self) -> None:
        self._edit(lambda s: s.mark_verified())

    def _on_save(self) -> None:
        try:
            rev = self._session.save()
            s = self._session.draft
            if self._db is not None:
                self._db.update_lab_status_by_wrk_path(
                    self._wrk_path,
                    analysis_revision=s.manifest.get("analysis_revision"),
                    lab_status=s.corrections.get("analysis_status"),
                    lab_edited_at=rev.timestamp,
                    hot_cue_count=len(s.cues),
                    saved_loop_count=len(s.loops),
                    marker_status="ready" if s.active_markers else "none",
                    marker_count=len(s.active_markers),
                )
            self.saved.emit(str(self._wrk_path))
            self.statusBar().showMessage("Corrections saved. Reload track to apply corrected analysis to an active deck.", 6000)
            self._session = begin_lab_session(self._wrk_path)
            self._meta = load_wrk_metadata(self._wrk_path)
            self._preview.set_session(self._session)
            self._refresh_all()
        except Exception as exc:
            QMessageBox.critical(self, "WREKKER LAB", f"Save failed; original .wrk was left untouched.\n\n{exc}")

    def closeEvent(self, ev) -> None:
        if self._session.dirty:
            res = QMessageBox.question(self, "WREKKER LAB", "Close without saving LAB corrections?")
            if res != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
        self._preview.close()
        super().closeEvent(ev)

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Space and not ev.isAutoRepeat():
            focus = self.focusWidget()
            if isinstance(focus, (QLineEdit, QTextEdit, QDoubleSpinBox, QComboBox)):
                super().keyPressEvent(ev)
                return
            self._on_play_pause()
            ev.accept()
            return
        super().keyPressEvent(ev)
