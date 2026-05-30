"""
ZoomWaveformWidget — high-resolution beat-window view centered on the playhead.

Shows 4 / 8 / 16 / 32 beats around the current position for precise beatmatching.
Mouse-wheel zooms in/out; click seeks; right-bottom label "OVL" toggles overlay.

Cross-deck overlay:
  When the other deck's beat/BPM data is set via set_other_deck(), its beats are
  drawn as thin translucent lines in the other deck's accent color.  The mapping is:
      x = (beat_s_other - other_pos_s + window_s/2) / window_s * w
  i.e., the other deck's beat offset relative to its current position is rendered
  at the matching offset on this deck's centered playhead.
"""
from __future__ import annotations

import bisect
import math
import os
import time
from typing import Optional, TYPE_CHECKING

import numpy as np

from PyQt6.QtCore    import Qt, QRectF, QPointF, QTimer, pyqtSignal
from PyQt6.QtGui     import QPainter, QColor, QPen, QFont, QPolygonF, QImage, QPixmap
from PyQt6.QtWidgets import QWidget, QToolTip

if TYPE_CHECKING:
    from wrekker.core.deck import WaveformData, LoopState

from wrekker.ui import theme
from wrekker.ui.widgets.marker_style import (
    MarkerDisplayMode,
    coerce_marker_display_mode,
    marker_draw_style,
    marker_paint_sort_key,
    marker_tooltip,
    marker_value,
    should_draw_marker,
)

ZOOM_BEATS = [4, 8, 16, 32]
_CUE_COLORS = [
    "#00d4ff", "#ff6b6b", "#4ecdc4", "#ffe66d",
    "#a29bfe", "#fd79a8", "#55efc4", "#fdcb6e",
]

# Overlay toggle hitbox (bottom-right, left of zoom label)
_OVL_LABEL_W = 28
_OVL_LABEL_H = 14


class ZoomWaveformWidget(QWidget):
    """
    Centered-playhead zoom waveform strip for beatmatching.

    Signals
    -------
    seek_requested(float): position_s when user clicks.
    """

    seek_requested       = pyqtSignal(float)
    marker_right_clicked = pyqtSignal(object)   # AutoMarker | None

    def __init__(self, deck_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._deck_id   = deck_id
        self._zoom_idx  = 1   # index into ZOOM_BEATS (default 8 beats)

        self._zoom_peaks:   Optional[np.ndarray] = None   # (M,) float32
        self._zoom_colors:  Optional[np.ndarray] = None   # (M, 3) uint8
        self._zoom_chunk:   int   = 256
        self._sr:           int   = 44100
        try:
            self._cache_scale = max(1, min(4, int(os.environ.get("WREKKER_ZOOM_CACHE_SCALE", "2"))))
        except ValueError:
            self._cache_scale = 2
        try:
            self._peak_smooth = max(0, min(9, int(os.environ.get("WREKKER_ZOOM_PEAK_SMOOTH", "3"))))
        except ValueError:
            self._peak_smooth = 3

        self._pos_s:        float = 0.0
        self._visual_pos_s: float = 0.0
        self._last_auth_pos_s: float = 0.0
        self._last_snapshot_t = time.perf_counter()
        self._visual_rate: float = 1.0
        self._playing:     bool = False
        self._dur_s:        float = 0.0
        self._beats:        tuple[float, ...] = ()
        self._bpm:          float = 120.0
        self._first_beat_s: float = 0.0
        self._cues:         list[float] = []
        self._loop:         Optional["LoopState"] = None
        self._sync_on:      bool  = False
        self._phase_err:    Optional[float] = None   # beats, (-0.5, 0.5]

        # Auto markers
        self._markers: list = []
        self._marker_mode = MarkerDisplayMode.ESSENTIAL

        # Pre-rendered waveform cache — rebuilt once on load, blitted each frame
        self._waveform_cache:  Optional[QPixmap] = None
        self._cache_height:    int = 0

        # Other deck overlay state
        other_id                  = "B" if deck_id == "A" else "A"
        self._other_color         = QColor(theme.deck_color(other_id))
        self._other_pos_s:  float = 0.0
        self._other_beats:  tuple[float, ...] = ()
        self._other_bpm:    float = 0.0
        self._other_first_s: float = 0.0
        self._overlay_on:   bool  = False   # enabled when other deck has grid data

        self._fps_log_enabled = os.environ.get("WREKKER_ZOOM_FPS_LOG", "0") == "1"
        self._visual_debug_enabled = os.environ.get("WREKKER_WAVEFORM_POSITION_DEBUG", "0") == "1"
        self._disable_repaint = os.environ.get("WREKKER_ZOOM_DISABLE_REPAINT", "0") == "1"
        try:
            self._target_frame_s = 1.0 / max(30.0, min(240.0, float(os.environ.get("WREKKER_ZOOM_TARGET_FPS", "60"))))
        except ValueError:
            self._target_frame_s = 1.0 / 60.0
        self._next_anim_t = time.perf_counter()
        self._paint_count = 0
        self._paint_dropped = 0
        self._paint_times_ms: list[float] = []
        self._paint_intervals_ms: list[float] = []
        self._paint_last_t = 0.0
        self._paint_log_t = time.perf_counter()
        self._last_frame_t = self._paint_log_t

        self.setMinimumHeight(80)
        self.setMaximumHeight(100)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.WheelFocus)
        self.setMouseTracking(True)
        self._anim_timer: QTimer | None = None
        if os.environ.get("WREKKER_ZOOM_OWN_TIMER", "0") == "1":
            self._anim_timer = QTimer(self)
            self._anim_timer.setTimerType(Qt.TimerType.PreciseTimer)
            self._anim_timer.setInterval(int(os.environ.get("WREKKER_ZOOM_ANIM_MS", "8")))
            self._anim_timer.timeout.connect(self.animate_frame)
            self._anim_timer.start()

    # ── public API ────────────────────────────────────────────────────────────

    def set_markers(self, markers) -> None:
        """Push auto-detected markers (list of AutoMarker). Triggers repaint."""
        self._markers = sorted(markers, key=marker_paint_sort_key) if markers else []
        self.update()

    def set_marker_display_mode(self, mode: MarkerDisplayMode | str) -> None:
        self._marker_mode = coerce_marker_display_mode(mode)
        self.update()

    def set_waveform(self, data: Optional["WaveformData"]) -> None:
        if data is None:
            self._zoom_peaks = self._zoom_colors = None
            self._waveform_cache = None
        else:
            self._zoom_peaks  = data.zoom_peaks
            self._zoom_colors = data.zoom_colors
            if hasattr(data, "zoom_chunk") and data.zoom_chunk:
                self._zoom_chunk = data.zoom_chunk
            self._rebuild_waveform_cache()
        self.update()

    def update_position(
        self,
        pos_s:         float,
        duration_s:    float,
        beats:         tuple[float, ...],
        bpm:           float,
        first_beat_s:  float,
        cue_positions: list[float],
        loop:          Optional["LoopState"],
        sync_enabled:  bool,
        phase_err:     Optional[float],
        playing:       bool = False,
    ) -> None:
        self._pos_s        = pos_s
        self._playing      = bool(playing)
        now = time.perf_counter()
        signed_delta = float(pos_s) - self._last_auth_pos_s
        pos_delta = abs(signed_delta)
        snapshot_dt = max(1e-4, now - self._last_snapshot_t)
        discontinuity = pos_delta > 0.18 or signed_delta < -0.02 or duration_s != self._dur_s
        if discontinuity or not (self._dur_s > 0.0):
            self._visual_pos_s = float(pos_s)
            self._visual_rate = 1.0
        elif pos_delta < 0.0015:
            self._visual_pos_s = float(pos_s)
            self._visual_rate = 0.0
        else:
            observed_rate = max(0.25, min(4.0, signed_delta / snapshot_dt))
            self._visual_rate = self._visual_rate * 0.70 + observed_rate * 0.30
            # The audio position is already monotonic and cheap to read from the
            # Rust engine. Keep the visual timeline phase-locked to that
            # authoritative sample; the animation timer predicts forward between
            # samples. Easing here adds a persistent 1-2 frame visual delay.
            self._visual_pos_s = float(pos_s)
        self._last_auth_pos_s = float(pos_s)
        self._last_snapshot_t = now
        self._dur_s        = duration_s
        self._beats        = beats
        self._bpm          = max(bpm, 1.0)
        self._first_beat_s = first_beat_s
        self._cues         = cue_positions
        self._loop         = loop
        self._sync_on      = sync_enabled
        self._phase_err    = phase_err
        if not self._playing:
            self.update()

    def animate_frame(self, now: float | None = None) -> None:
        """Advance visual prediction and schedule one repaint."""
        if not self._playing or self._dur_s <= 0.0:
            return
        now = time.perf_counter() if now is None else now
        self._last_frame_t = now
        dt = max(0.0, min(0.050, now - self._last_snapshot_t))
        target = min(self._dur_s, self._pos_s + dt * max(0.0, self._visual_rate))
        self._visual_pos_s = target
        if not self._disable_repaint:
            self.update()

    def set_other_deck(
        self,
        pos_s:        float,
        beats:        tuple[float, ...],
        bpm:          float,
        first_beat_s: float,
        source_playing: bool = True,
    ) -> None:
        """Push other deck beat data for the cross-deck overlay.
        source_playing: True when the other/source deck is playing.  The overlay
        follows that deck even if this viewer deck is paused.
        """
        self._other_pos_s = pos_s
        # Never clear good beat data with an empty update (e.g. before analysis done).
        if beats:
            self._other_beats   = beats
            self._other_first_s = first_beat_s
        # Never reset BPM to 0 from a transient empty call.
        if bpm > 0:
            self._other_bpm = bpm
        # Auto-enable once we have something to show.
        if not self._overlay_on and (self._other_beats or self._other_bpm > 0):
            self._overlay_on = True

    # ── waveform cache ────────────────────────────────────────────────────────

    def _rebuild_waveform_cache(self) -> None:
        """Pre-render the entire waveform into a QPixmap (1px per chunk column).

        Blitting a sub-rect of this pixmap each frame is O(1) vs the O(width)
        Python loop we'd otherwise pay on every paintEvent.
        """
        if self._zoom_peaks is None or len(self._zoom_peaks) == 0:
            self._waveform_cache = None
            return
        h = self.height()
        if h <= 0:
            return

        peaks  = self._zoom_peaks   # (n,) float32
        colors = self._zoom_colors  # (n, 3) uint8 | None
        if self._cache_scale > 1 and len(peaks) > 1:
            x_old = np.arange(len(peaks), dtype=np.float32)
            x_new = np.linspace(0.0, float(len(peaks) - 1), len(peaks) * self._cache_scale, dtype=np.float32)
            peaks = np.interp(x_new, x_old, peaks).astype(np.float32)
            if colors is not None:
                colors = np.stack(
                    [
                        np.interp(x_new, x_old, colors[:, ch]).astype(np.uint8)
                        for ch in range(3)
                    ],
                    axis=1,
                )
        if self._peak_smooth > 1 and len(peaks) > self._peak_smooth:
            radius = self._peak_smooth // 2
            kernel = np.arange(1, radius + 2, dtype=np.float32)
            kernel = np.concatenate([kernel, kernel[-2::-1]])
            kernel = kernel / kernel.sum()
            padded = np.pad(peaks, (radius, radius), mode="edge")
            peaks = np.convolve(padded, kernel, mode="valid").astype(np.float32)
        n      = len(peaks)

        # Bar geometry per column (vectorised)
        halfs   = np.maximum(1, (peaks * h * 0.42).astype(np.int32))  # (n,)
        bar_h   = halfs * 2                                            # (n,)
        y_top   = (h - bar_h) // 2                                    # (n,)
        y_bot   = y_top + bar_h                                        # (n,)

        # RGBA image: (h, n, 4) uint8 — background stays transparent (alpha=0)
        img = np.zeros((h, n, 4), dtype=np.uint8)

        ys           = np.arange(h)[:, np.newaxis]                    # (h, 1)
        mask         = (ys >= y_top[np.newaxis, :]) & (ys < y_bot[np.newaxis, :])  # (h, n)
        row_i, col_i = np.where(mask)

        if colors is not None:
            img[row_i, col_i, 0] = colors[col_i, 0]
            img[row_i, col_i, 1] = colors[col_i, 1]
            img[row_i, col_i, 2] = colors[col_i, 2]
        else:
            c = QColor(theme.deck_color(self._deck_id))
            img[row_i, col_i, 0] = c.red()
            img[row_i, col_i, 1] = c.green()
            img[row_i, col_i, 2] = c.blue()
        img[row_i, col_i, 3] = 255

        buf   = np.ascontiguousarray(img)
        qimg  = QImage(buf.data, n, h, n * 4, QImage.Format.Format_RGBA8888)
        # QPixmap.fromImage makes a deep copy → buf can be freed after this line
        self._waveform_cache = QPixmap.fromImage(qimg)
        self._cache_height   = h

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        if self._zoom_peaks is not None:
            self._rebuild_waveform_cache()

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _) -> None:
        paint_started = time.perf_counter()
        if self._fps_log_enabled and self._paint_last_t:
            interval_ms = (paint_started - self._paint_last_t) * 1000.0
            self._paint_intervals_ms.append(interval_ms)
            if paint_started - self._paint_last_t > 1.0 / 50.0:
                self._paint_dropped += 1
        self._paint_last_t = paint_started

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        w, h = self.width(), self.height()

        accent        = QColor(theme.deck_color(self._deck_id))
        zoom_beats    = ZOOM_BEATS[self._zoom_idx]
        beat_period_s = 60.0 / self._bpm
        window_s      = zoom_beats * beat_period_s
        visual_pos_s  = self._visual_pos_s if self._dur_s > 0 else self._pos_s
        t_start       = visual_pos_s - window_s * 0.5
        ph_x          = w * 0.5   # playhead always at center

        # ── background ────────────────────────────────────────────────────────
        p.fillRect(0, 0, w, h, QColor(theme.BG_CTRL))

        # ── waveform bars (pixmap blit — O(1) per frame) ─────────────────────────
        if self._waveform_cache is not None and window_s > 0:
            col_per_s = (self._sr / self._zoom_chunk) * self._cache_scale
            src_x     = t_start * col_per_s
            src_w     = max(1.0, window_s * col_per_s)
            mid_src   = src_x + src_w * 0.5   # cache column at the playhead
            cache_w   = self._waveform_cache.width()
            left_src  = max(0.0, min(float(cache_w), src_x))
            mid_src_c = max(0.0, min(float(cache_w), mid_src))
            right_src = max(0.0, min(float(cache_w), src_x + src_w))

            # Played portion (left of playhead): full brightness
            if mid_src_c > left_src:
                left_span = max(1.0, mid_src - src_x)
                dst_x = (left_src - src_x) / left_span * ph_x
                dst_w = (mid_src_c - left_src) / left_span * ph_x
                p.setOpacity(1.0)
                p.drawPixmap(
                    QRectF(dst_x, 0, dst_w, h),
                    self._waveform_cache,
                    QRectF(left_src, 0, mid_src_c - left_src, h),
                )
            # Unplayed portion (right of playhead): 40% opacity
            if right_src > mid_src_c:
                right_span = max(1.0, src_x + src_w - mid_src)
                dst_x = ph_x + (mid_src_c - mid_src) / right_span * (w - ph_x)
                dst_w = (right_src - mid_src_c) / right_span * (w - ph_x)
                p.setOpacity(0.40)
                p.drawPixmap(
                    QRectF(dst_x, 0, dst_w, h),
                    self._waveform_cache,
                    QRectF(mid_src_c, 0, right_src - mid_src_c, h),
                )
            p.setOpacity(1.0)

        # ── loop overlay ──────────────────────────────────────────────────────
        if self._loop and self._loop.start_s < self._loop.end_s:
            lx1 = (self._loop.start_s - t_start) / window_s * w
            lx2 = (self._loop.end_s   - t_start) / window_s * w
            if lx1 < w and lx2 > 0:
                fill = QColor("#ff9f43")
                fill.setAlphaF(0.20)
                p.fillRect(QRectF(max(0.0, lx1), 0.0, min(float(w), lx2) - max(0.0, lx1), float(h)), fill)
                p.setPen(QPen(QColor("#ff9f43"), 1))
                if 0 <= lx1 < w:
                    p.drawLine(QPointF(lx1, 0), QPointF(lx1, h))
                if 0 <= lx2 < w:
                    p.drawLine(QPointF(lx2, 0), QPointF(lx2, h))
                p.setPen(Qt.PenStyle.NoPen)

        # ── cross-deck beat overlay ───────────────────────────────────────────
        if self._overlay_on and self._other_bpm > 0.0:
            self._paint_other_beats(p, w, h, window_s)

        # ── this deck's beat markers ──────────────────────────────────────────
        strong = QColor(255, 255, 255, 100)
        weak   = QColor(255, 255, 255, 35)

        if self._beats:
            t_end = t_start + window_s
            i0 = max(0, bisect.bisect_left(self._beats, t_start) - 1)
            i1 = min(len(self._beats), bisect.bisect_right(self._beats, t_end) + 1)
            for i in range(i0, i1):
                beat_s = self._beats[i]
                bx = (beat_s - t_start) / window_s * w
                if 0 <= bx < w:
                    is_down = (i % 4 == 0)
                    bar_h   = h if is_down else (h * 2 // 3)
                    p.fillRect(QRectF(bx, h - bar_h, 1.0, bar_h), strong if is_down else weak)
        elif beat_period_s > 0:
            # Constant-BPM grid fallback
            first_n = math.ceil((t_start - self._first_beat_s) / beat_period_s)
            last_n  = math.floor((t_start + window_s - self._first_beat_s) / beat_period_s)
            for beat_n in range(first_n - 1, last_n + 2):
                beat_s = self._first_beat_s + beat_n * beat_period_s
                bx = (beat_s - t_start) / window_s * w
                if 0 <= bx < w:
                    is_down = (beat_n % 4 == 0)
                    bar_h   = h if is_down else (h * 2 // 3)
                    p.fillRect(QRectF(bx, h - bar_h, 1.0, bar_h), strong if is_down else weak)

        # ── cue markers ───────────────────────────────────────────────────────
        for idx, cue_s in enumerate(self._cues):
            cx = (cue_s - t_start) / window_s * w
            if 0 <= cx < w:
                c = QColor(_CUE_COLORS[idx % len(_CUE_COLORS)])
                p.setPen(QPen(c, 1))
                p.drawLine(QPointF(cx, 0), QPointF(cx, h))
                tri = QPolygonF([
                    QPointF(cx - 4, 0),
                    QPointF(cx + 4, 0),
                    QPointF(cx,     6),
                ])
                p.setBrush(c)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawPolygon(tri)

        # ── auto markers ──────────────────────────────────────────────────────
        if self._markers and window_s > 0 and self._marker_mode != MarkerDisplayMode.OFF:
            mfont = QFont()
            mfont.setPointSize(6)
            mfont.setBold(True)
            p.setFont(mfont)
            for m in self._markers:
                conf = float(getattr(m, "confidence", 0.0) or 0.0)
                mval = marker_value(m)
                if not should_draw_marker(
                    mval, conf, self._marker_mode, view="zoom", window_s=window_s
                ):
                    continue
                mpos = getattr(m, "position_s", 0.0)
                if not (t_start <= mpos <= t_start + window_s):
                    continue
                mx    = (mpos - t_start) / window_s * w
                style = marker_draw_style(
                    mval, conf, self._marker_mode, view="zoom", window_s=window_s
                )
                mc = QColor(style["color"])
                mc.setAlphaF(style["alpha"])
                pen = QPen(mc, style["line_width"])
                pen.setStyle(style["pen_style"])
                p.setPen(pen)
                marker_bottom = h - 16
                tail_h = int(style.get("tail_height", 8))
                p.drawLine(QPointF(mx, max(0, marker_bottom - tail_h)), QPointF(mx, marker_bottom))
                if style["label"]:
                    label = style["label_text"]
                    label_w = min(72, max(28, len(label) * 6 + 8))
                    lx = max(1, min(w - label_w - 1, mx + 3))
                    if style["tier"] == "primary":
                        bg = QColor(theme.BG_DEEP)
                        bg.setAlphaF(0.72)
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(bg)
                        p.drawRoundedRect(QRectF(lx, 1, label_w, 12), 3, 3)
                    txt = QColor(style["color"])
                    txt.setAlphaF(0.96)
                    p.setPen(txt)
                    p.drawText(
                        QRectF(lx + 3, 1, label_w - 4, 12),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                        label,
                    )
            p.setPen(Qt.PenStyle.NoPen)

        # ── centered playhead ─────────────────────────────────────────────────
        p.setPen(QPen(QColor(theme.WHITE), 2))
        p.drawLine(QPointF(ph_x, 0), QPointF(ph_x, h))

        # ── bottom labels (font shared) ───────────────────────────────────────
        font = QFont()
        font.setPointSize(7)
        font.setBold(True)
        p.setFont(font)

        # Phase indicator (bottom-left)
        if self._sync_on and self._phase_err is not None:
            err    = self._phase_err
            err_ms = abs(err) * beat_period_s * 1000
            if abs(err) < 0.05:
                txt = "LOCKED"
                col = QColor("#2ecc71")
            elif err > 0:
                txt = f"LATE {err_ms:.0f}ms"
                col = QColor("#f39c12") if abs(err) < 0.20 else QColor("#e74c3c")
            else:
                txt = f"EARLY {err_ms:.0f}ms"
                col = QColor("#f39c12") if abs(err) < 0.20 else QColor("#e74c3c")
            p.setPen(col)
            p.drawText(
                QRectF(4, h - 14, (w - 8) // 2, 12),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                txt,
            )

        # OVL toggle (bottom-right, left of zoom label) — shows only when data available
        if self._other_bpm > 0.0 or self._other_beats:
            ovl_col = QColor(self._other_color)
            if not self._overlay_on:
                ovl_col.setAlphaF(0.30)
            p.setPen(ovl_col)
            p.drawText(
                QRectF(w - 56, h - 14, _OVL_LABEL_W, _OVL_LABEL_H),
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                "OVL",
            )

        # Zoom label (bottom-right)
        p.setPen(QColor(theme.TEXT_MED))
        p.drawText(
            QRectF(w - 28, h - 14, 24, 12),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{zoom_beats}B",
        )

        # ── border ────────────────────────────────────────────────────────────
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()
        self._record_paint_time(paint_started)

    def _record_paint_time(self, started: float) -> None:
        if not self._fps_log_enabled:
            return
        now = time.perf_counter()
        self._paint_count += 1
        self._paint_times_ms.append((now - started) * 1000.0)
        elapsed = now - self._paint_log_t
        if elapsed < 3.0:
            return
        fps = self._paint_count / max(elapsed, 1e-6)
        avg = sum(self._paint_times_ms) / max(1, len(self._paint_times_ms))
        peak = max(self._paint_times_ms) if self._paint_times_ms else 0.0
        interval_avg = sum(self._paint_intervals_ms) / max(1, len(self._paint_intervals_ms))
        interval_max = max(self._paint_intervals_ms) if self._paint_intervals_ms else 0.0
        print(
            f"[zoom-fps {self._deck_id}] fps={fps:.1f} "
            f"paint_avg={avg:.2f}ms paint_max={peak:.2f}ms "
            f"dt_avg={interval_avg:.1f}ms dt_max={interval_max:.1f}ms "
            f"dropped={self._paint_dropped}",
            flush=True,
        )
        self._paint_count = 0
        self._paint_dropped = 0
        self._paint_times_ms.clear()
        self._paint_intervals_ms.clear()
        self._paint_log_t = now

    def _paint_other_beats(self, p: QPainter, w: int, h: int, window_s: float) -> None:
        """Draw the other deck's beat markers as a subtle translucent overlay."""
        other_pos = self._other_pos_s
        other_bpm = self._other_bpm
        other_bp  = 60.0 / other_bpm

        # Opacity: downbeats more visible
        strong = QColor(self._other_color); strong.setAlphaF(0.55)
        weak   = QColor(self._other_color); weak.setAlphaF(0.22)

        half_win = window_s * 0.5

        if self._other_beats:
            # Fast path: binary-search for beats near other_pos within this window
            lo = other_pos - half_win - other_bp
            hi = other_pos + half_win + other_bp
            i0 = max(0, bisect.bisect_left(self._other_beats, lo))
            i1 = min(len(self._other_beats), bisect.bisect_right(self._other_beats, hi))
            for i in range(i0, i1):
                beat_s = self._other_beats[i]
                # x = (offset_from_other_pos + window_s/2) / window_s * w
                bx = (beat_s - other_pos + half_win) / window_s * w
                if 0 <= bx < w:
                    is_down = (i % 4 == 0)
                    bar_h   = h if is_down else (h * 2 // 3)
                    pen_w   = 2 if is_down else 1
                    p.fillRect(QRectF(bx, h - bar_h, float(pen_w), bar_h), strong if is_down else weak)
        elif other_bp > 0:
            # Static-BPM grid fallback
            t_lo    = other_pos - half_win - other_bp
            t_hi    = other_pos + half_win + other_bp
            first_n = math.ceil((t_lo - self._other_first_s) / other_bp)
            last_n  = math.floor((t_hi - self._other_first_s) / other_bp)
            for beat_n in range(first_n, last_n + 1):
                beat_s = self._other_first_s + beat_n * other_bp
                bx = (beat_s - other_pos + half_win) / window_s * w
                if 0 <= bx < w:
                    is_down = (beat_n % 4 == 0)
                    bar_h   = h if is_down else (h * 2 // 3)
                    pen_w   = 2 if is_down else 1
                    p.fillRect(QRectF(bx, h - bar_h, float(pen_w), bar_h), strong if is_down else weak)

    # ── interaction ───────────────────────────────────────────────────────────

    def wheelEvent(self, ev) -> None:
        if ev.angleDelta().y() > 0:
            self._zoom_idx = max(0, self._zoom_idx - 1)
        else:
            self._zoom_idx = min(len(ZOOM_BEATS) - 1, self._zoom_idx + 1)
        self.update()
        ev.accept()

    def _marker_at_x(self, x: int):
        """Return the AutoMarker closest to pixel x (within 8px), or None."""
        if not self._markers or self._bpm <= 0:
            return None
        zoom_beats    = ZOOM_BEATS[self._zoom_idx]
        beat_period_s = 60.0 / self._bpm
        window_s      = zoom_beats * beat_period_s
        t_start       = self._pos_s - window_s * 0.5
        w = self.width()
        if w <= 0 or window_s <= 0:
            return None
        tol_s    = 8.0 * window_s / w
        best     = None
        best_dist = tol_s
        for m in self._markers:
            conf = float(getattr(m, "confidence", 0.0) or 0.0)
            mval = marker_value(m)
            if not should_draw_marker(
                mval, conf, self._marker_mode, view="zoom", window_s=window_s
            ):
                continue
            mpos = getattr(m, "position_s", 0.0)
            if not (t_start <= mpos <= t_start + window_s):
                continue
            dist = abs(mpos - (t_start + (x / w) * window_s))
            if dist < best_dist:
                best_dist = dist
                best = m
        return best

    def mouseMoveEvent(self, ev) -> None:
        m = self._marker_at_x(ev.pos().x())
        if m:
            QToolTip.showText(ev.globalPosition().toPoint(), marker_tooltip(m), self)
        else:
            QToolTip.hideText()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.RightButton:
            self.marker_right_clicked.emit(self._marker_at_x(ev.pos().x()))
            ev.accept()
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        x, y = ev.pos().x(), ev.pos().y()
        h = self.height()
        # Hit-test OVL toggle (bottom-right corner)
        if x >= self.width() - 56 and x < self.width() - 56 + _OVL_LABEL_W and y >= h - _OVL_LABEL_H:
            self._overlay_on = not self._overlay_on
            self.update()
            return
        self._seek_from_x(x)

    def _seek_from_x(self, x: int) -> None:
        if self._dur_s <= 0:
            return
        zoom_beats    = ZOOM_BEATS[self._zoom_idx]
        beat_period_s = 60.0 / self._bpm
        window_s      = zoom_beats * beat_period_s
        t_s = (self._pos_s - window_s * 0.5) + (x / self.width()) * window_s
        self.seek_requested.emit(max(0.0, min(self._dur_s, t_s)))
