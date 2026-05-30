"""
Texture-backed waveform renderer for WREKKER LAB's QWidget timeline fallback.

The LAB window can use a Qt Quick timeline, but the QWidget fallback has its
own waveform implementation. This class keeps the same small public API as the
classic LAB widget while making the waveform body an explicit pre-rendered
texture. It is the LAB-side handoff point for future .wrk waveform tiles.
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np

from PyQt6.QtCore import Qt, QRectF, QPoint, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QBrush, QPixmap, QPolygon
from PyQt6.QtWidgets import QWidget

from wrekker.lab.session import LabEditSession
from wrekker.ui import theme
from wrekker.ui.widgets.marker_style import marker_color, marker_tier


_STEM_INDEX = {"VOCALS": 0, "DRUMS": 1, "BASS": 2, "OTHER": 3}
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


class TextureLabWaveform(QWidget):
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
        self._texture: QPixmap | None = None
        self._texture_key: tuple | None = None
        deck_cache_default = os.environ.get("WREKKER_TEXTURE_ZOOM_CACHE_SCALE", "4")
        self._cache_scale = self._env_int("WREKKER_LAB_TEXTURE_CACHE_SCALE", deck_cache_default, 1, 8)
        self._peak_smooth = self._env_int("WREKKER_LAB_TEXTURE_PEAK_SMOOTH", "0", 0, 15)
        self._px_per_second = self._env_int("WREKKER_LAB_TEXTURE_PX_PER_SECOND", "256", 64, 1024)
        self.setMouseTracking(True)

    @staticmethod
    def _env_int(name: str, default: str, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(os.environ.get(name, default))))
        except ValueError:
            return int(default)

    def set_data(self, meta, session: LabEditSession) -> None:
        self._meta = meta
        self._session = session
        self._invalidate_texture()
        self.update()

    def set_source(self, source: str) -> None:
        if source != self._source:
            self._source = source
            self._invalidate_texture()
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

    def resizeEvent(self, ev) -> None:
        self._invalidate_texture()
        super().resizeEvent(ev)

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

    def _colors(self, target_size: int) -> np.ndarray | None:
        if self._meta is None or self._source != "FULL MIX":
            return None
        raw = getattr(self._meta, "waveform_colors", None)
        if raw is None:
            return None
        colors = np.asarray(raw, dtype=np.uint8)
        if colors.ndim != 2 or colors.shape[1] < 3 or colors.shape[0] < 2:
            return None
        if colors.shape[0] == target_size:
            return colors[:, :3]
        x_old = np.arange(colors.shape[0], dtype=np.float32)
        x_new = np.linspace(0.0, float(colors.shape[0] - 1), target_size, dtype=np.float32)
        return np.stack(
            [
                np.interp(x_new, x_old, colors[:, ch]).astype(np.uint8)
                for ch in range(3)
            ],
            axis=1,
        )

    def _window(self) -> tuple[float, float]:
        duration = self._duration()
        if self._mode == "overview":
            return 0.0, duration
        half = self._zoom_window_s * 0.5
        start = max(0.0, min(duration - self._zoom_window_s, self._position_s - half))
        end = min(duration, max(self._zoom_window_s, start + self._zoom_window_s))
        return start, max(start + 0.1, end)

    def _invalidate_texture(self) -> None:
        self._texture = None
        self._texture_key = None

    def _ensure_texture(self, r) -> QPixmap | None:
        vals = self._values()
        if vals.size < 2:
            return None
        height = max(8, int(r.height()))
        key = (
            id(self._meta),
            self._source,
            vals.size,
            height,
            self._cache_scale,
            self._peak_smooth,
            self._px_per_second,
        )
        if self._texture is not None and self._texture_key == key:
            return self._texture
        vals = np.nan_to_num(vals.astype(np.float32, copy=False), nan=0.0, posinf=0.0, neginf=0.0)
        vals = np.abs(vals)
        vmax = max(float(np.percentile(vals, 98)), 1e-6)
        vals = np.clip(vals / vmax, 0.0, 1.0)
        colors = self._colors(vals.size)
        if self._peak_smooth > 1 and vals.size > self._peak_smooth:
            raw_vals = vals
            radius = self._peak_smooth // 2
            kernel = np.arange(1, radius + 2, dtype=np.float32)
            kernel = np.concatenate([kernel, kernel[-2::-1]])
            kernel = kernel / kernel.sum()
            padded = np.pad(vals, (radius, radius), mode="edge")
            smoothed = np.convolve(padded, kernel, mode="valid").astype(np.float32)
            vals = np.maximum(raw_vals, smoothed * 0.72)

        duration_w = int(self._duration() * self._px_per_second)
        target_w = max(int(r.width()) * self._cache_scale, vals.size * self._cache_scale, duration_w)
        target_w = max(1024, min(262144, target_w))
        # Do not linearly interpolate low-resolution LAB analysis into a smooth
        # envelope: it turns real transients into polygon ramps. Deck texture
        # rendering is column/pixmap based, so LAB uses sample-held columns too.
        src_x = np.linspace(0.0, vals.size - 1, target_w, dtype=np.float32)
        idx = np.rint(src_x).astype(np.int32)
        idx = np.clip(idx, 0, vals.size - 1)
        peaks = vals[idx]
        interp_colors = None
        if colors is not None:
            interp_colors = colors[idx]

        color = QColor(_SOURCE_COLORS.get(self._source, "#ffb000"))
        alpha = 245 if self._mode == "zoom" else 150
        shadow_alpha = 0
        max_h = height * (0.42 if self._mode == "zoom" else 0.32)
        mid = height // 2
        arr = np.zeros((height, target_w, 4), dtype=np.uint8)
        rgb = np.array([color.red(), color.green(), color.blue()], dtype=np.uint8)
        stride = 28 if self._mode == "zoom" else 8
        bar_w = 2 if self._mode == "zoom" else 1
        for x in range(0, target_w, stride):
            chunk = peaks[x:min(target_w, x + stride)]
            if chunk.size == 0:
                continue
            local = int(np.argmax(chunk))
            src_i = min(target_w - 1, x + local)
            amp = peaks[src_i]
            y = max(1, int(float(amp) * max_h))
            y0 = max(0, mid - y)
            y1 = min(height, mid + y + 1)
            sy0 = max(0, y0 - 1)
            sy1 = min(height, y1 + 1)
            x1 = min(target_w, x + bar_w)
            col = interp_colors[src_i] if interp_colors is not None else rgb
            if shadow_alpha:
                arr[sy0:sy1, x:x1, :3] = col
                arr[sy0:sy1, x:x1, 3] = shadow_alpha
            arr[y0:y1, x:x1, :3] = col
            arr[y0:y1, x:x1, 3] = alpha
        image = QImage(arr.data, target_w, height, target_w * 4, QImage.Format.Format_RGBA8888).copy()
        self._texture = QPixmap.fromImage(image)
        self._texture_key = key
        return self._texture

    def _draw_screen_bars(
        self,
        p: QPainter,
        r: QRectF,
        *,
        start_s: float,
        window_s: float,
        duration: float,
    ) -> None:
        vals = self._values()
        if vals.size < 2:
            return
        vals = np.nan_to_num(vals.astype(np.float32, copy=False), nan=0.0, posinf=0.0, neginf=0.0)
        vals = np.abs(vals)
        vmax = max(float(np.percentile(vals, 98)), 1e-6)
        vals = np.clip(vals / vmax, 0.0, 1.0)
        colors = self._colors(vals.size)

        stride = 7 if self._mode == "zoom" else 5
        bar_w = 2 if self._mode == "zoom" else 1
        max_h = r.height() * (0.42 if self._mode == "zoom" else 0.32)
        mid = r.center().y()
        fallback = QColor(_SOURCE_COLORS.get(self._source, "#ffb000"))
        alpha = 245 if self._mode == "zoom" else 150

        p.setPen(QPen(Qt.PenStyle.NoPen))
        for x in range(int(r.left()) + 2, int(r.right()) - bar_w, stride):
            t0 = start_s + ((x - r.left()) / max(1.0, r.width())) * window_s
            t1 = start_s + ((x + stride - r.left()) / max(1.0, r.width())) * window_s
            i0 = max(0, min(vals.size - 1, int(t0 / duration * vals.size)))
            i1 = max(i0 + 1, min(vals.size, int(t1 / duration * vals.size) + 1))
            chunk = vals[i0:i1]
            if chunk.size == 0:
                continue
            local = int(np.argmax(chunk))
            amp = float(chunk[local])
            y = max(1.0, amp * max_h)
            if colors is not None:
                rgb = colors[min(colors.shape[0] - 1, i0 + local)]
                col = QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]), alpha)
            else:
                col = QColor(fallback)
                col.setAlpha(alpha)
            p.fillRect(QRectF(float(x), mid - y, float(bar_w), y * 2.0), col)

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.fillRect(r, QColor("#050607" if self._mode == "zoom" else "#07090b"))
        p.setPen(QPen(QColor("#1e262c"), 1))
        p.drawRect(r)

        if self._session is None:
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "Open a .wrk track in WREKKER LAB")
            return

        duration = self._duration()
        start_s, end_s = self._window()
        window_s = max(0.1, end_s - start_s)
        w = max(1, r.width())
        self._draw_screen_bars(p, QRectF(r), start_s=start_s, window_s=window_s, duration=duration)

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
                p.drawPolygon(QPolygon([QPoint(x, r.top() + 2), QPoint(x - 5, r.top() + 12), QPoint(x + 5, r.top() + 12)]))

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
