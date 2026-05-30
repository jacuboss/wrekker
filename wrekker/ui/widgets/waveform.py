"""
Waveform / position bar widget.

Layers (bottom → top):
  1. Background fill
  2. Spectral-tinted waveform bars (bass=amber, mid=green, high=violet)
     played = full color, unplayed = 30 % alpha
  3. Beat / transient tick marks (white, bottom 5 px)
  4. Loop region overlay — orange (#ff9f43), distinct from every deck accent
  5. Vocal zone strip — pink highlight (top 3 px) when vocals > 35 % weighted mix
  6. Stem dominance strip — 5 px, colored by dominant stem (appears above vocal strip)
  7. Cue point markers with colored triangles
  8. Playhead line + downward triangle
  9. Elapsed / remaining timestamps

Stem colors:  vocals=#ff6b81  drums=#ffd32a  bass=#ff4757  other=#2ed573
"""
from __future__ import annotations
from typing import Optional, TYPE_CHECKING

import numpy as np

from PyQt6.QtCore    import Qt, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui     import (
    QPainter, QColor, QLinearGradient, QPen, QFont, QPolygonF,
)
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

_LOOP_COLOR = "#ff9f43"   # amber/orange — distinct from cyan (A) and orange-red (B)
_BEAT_ALPHA = 55          # beat tick opacity
# Stem strip colours (index = STEM_NAMES order: vocals, drums, bass, other)
_STEM_QCOL = [
    QColor("#ff6b81"),   # 0 vocals — rose/pink
    QColor("#ffd32a"),   # 1 drums  — yellow
    QColor("#ff4757"),   # 2 bass   — red-orange
    QColor("#2ed573"),   # 3 other  — green
]
_VOCAL_IDX       = 0      # index of vocals in STEM_NAMES
_VOCAL_THRESHOLD = 0.35   # vocals fraction of weighted total → show vocal strip
_STEM_STRIP_H    = 5      # px height of dominant-stem strip
_VOCAL_STRIP_H   = 3      # px height of vocal-zone strip


def _fmt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


class PositionBarWidget(QWidget):
    """
    Waveform scrubber bar with spectral coloring, stem overlay, and beat marks.
    Clickable / draggable for seeking.

    Signals
    -------
    seek_requested(float): position_s when user clicks/drags.
    """

    seek_requested       = pyqtSignal(float)
    marker_right_clicked = pyqtSignal(object)   # AutoMarker | None

    def __init__(self, deck_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._deck_id   = deck_id
        self._position  = 0.0
        self._duration  = 0.0
        self._cues:     list[float] = []
        self._loop:     Optional["LoopState"] = None

        # Waveform arrays (set from WaveformData, updated as analysis completes)
        self._peaks:       Optional[np.ndarray] = None   # (N,) float32 0-1
        self._colors:      Optional[np.ndarray] = None   # (N, 3) uint8
        self._beats:       tuple[float, ...]    = ()
        self._stem_energy: Optional[np.ndarray] = None   # (N, 4) float32

        # Stem gains (4 floats, pushed every UI tick by the deck widget)
        self._stem_gains: np.ndarray = np.ones(4, dtype=np.float32)

        # Precomputed stem visual arrays (recomputed when energy or gains change)
        self._stem_strip: Optional[np.ndarray] = None   # (N,) int8  dominant stem
        self._vocal_mask: Optional[np.ndarray] = None   # (N,) bool  vocal zone

        # Auto markers (AutoMarker objects, pushed from deck widget)
        self._markers: list = []
        self._marker_mode = MarkerDisplayMode.ESSENTIAL

        self._dragging = False

        self.setMinimumHeight(56)
        self.setMaximumHeight(72)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

    # ── public setters ────────────────────────────────────────────────────────

    def set_waveform(self, data: Optional["WaveformData"]) -> None:
        """Call whenever WaveformData changes (load → beats → stem energy)."""
        if data is None:
            self._peaks = self._colors = self._stem_energy = None
            self._beats = ()
            self._stem_strip = self._vocal_mask = None
        else:
            self._peaks       = data.peaks
            self._colors      = data.colors
            self._beats       = data.beats
            self._stem_energy = data.stem_energy
            self._recompute_stem_visuals()
        self.update()

    def set_markers(self, markers) -> None:
        """Push auto-detected markers (list of AutoMarker). Triggers repaint."""
        self._markers = sorted(markers, key=marker_paint_sort_key) if markers else []
        self.update()

    def set_marker_display_mode(self, mode: MarkerDisplayMode | str) -> None:
        self._marker_mode = coerce_marker_display_mode(mode)
        self.update()

    def set_stem_gains(self, gains: list[float]) -> None:
        """Push current stem fader gains from the UI tick (4 values: vocal/drums/bass/other)."""
        new_g = np.array(gains[:4], dtype=np.float32)
        if not np.allclose(new_g, self._stem_gains, atol=1e-3):
            self._stem_gains = new_g
            self._recompute_stem_visuals()
            self.update()

    def update_position(
        self,
        position_s:    float,
        duration_s:    float,
        cue_positions: list[float],
        loop:          Optional["LoopState"] = None,
    ) -> None:
        changed = (
            abs(position_s - self._position) >= 0.005   # 5 ms — responsive to jog
            or duration_s != self._duration
            or cue_positions != self._cues
            or loop != self._loop
        )
        if not changed:
            return
        self._position = position_s
        self._duration = duration_s
        self._cues     = cue_positions
        self._loop     = loop
        self.update()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _recompute_stem_visuals(self) -> None:
        """Vectorised: dominant-stem strip + vocal mask from energy × gains."""
        if self._stem_energy is None:
            self._stem_strip = None
            self._vocal_mask = None
            return
        weighted = self._stem_energy * self._stem_gains          # (N, 4)
        total    = weighted.sum(axis=1)                          # (N,)
        valid    = total > 1e-5
        dominant = np.where(valid, weighted.argmax(axis=1), -1).astype(np.int8)
        vocal_f  = np.where(valid, weighted[:, _VOCAL_IDX] / np.maximum(total, 1e-8), 0.0)
        self._stem_strip = dominant
        self._vocal_mask = vocal_f > _VOCAL_THRESHOLD

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p    = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()

        lbl_h  = 16
        bar_h  = h - lbl_h - 2
        dur    = self._duration or 1.0
        frac   = max(0.0, min(1.0, self._position / dur))
        ph_x   = int(frac * w)
        accent = QColor(theme.deck_color(self._deck_id))

        # Stem strip occupies the top rows; waveform fills the rest
        strip_h  = (_STEM_STRIP_H + _VOCAL_STRIP_H) if self._stem_strip is not None else 0
        wave_top = strip_h

        # ── background ────────────────────────────────────────────────────────
        p.fillRect(0, 0, w, bar_h, QColor(theme.BG_CTRL))

        # ── waveform ──────────────────────────────────────────────────────────
        if self._peaks is not None and len(self._peaks) > 0:
            wf     = self._peaks
            colors = self._colors
            n_wf   = len(wf)
            wave_h = bar_h - wave_top
            mid_y  = wave_top + wave_h / 2.0

            for x in range(w):
                idx  = min(int(x / w * n_wf), n_wf - 1)
                peak = float(wf[idx])
                half = max(1, int(peak * (wave_h / 2.0) * 0.9))

                if colors is not None and idx < len(colors):
                    r, g, b = int(colors[idx, 0]), int(colors[idx, 1]), int(colors[idx, 2])
                    col = QColor(r, g, b)
                else:
                    col = QColor(accent)

                if x > ph_x:
                    col.setAlphaF(0.30)

                p.fillRect(x, int(mid_y - half), 1, half * 2, col)
        else:
            # Placeholder gradient
            grad = QLinearGradient(QPointF(0, wave_top), QPointF(ph_x, wave_top))
            dim  = QColor(accent); dim.setAlphaF(0.15)
            brt  = QColor(accent); brt.setAlphaF(0.35)
            grad.setColorAt(0.0, dim)
            grad.setColorAt(1.0, brt)
            if ph_x > 0:
                p.fillRect(QRectF(0, wave_top, ph_x, bar_h - wave_top), grad)

        # ── beat markers (bottom 5 px of waveform) ────────────────────────────
        if self._beats:
            beat_col = QColor(255, 255, 255, _BEAT_ALPHA)
            for beat_s in self._beats:
                bx = int((beat_s / dur) * w)
                if 0 <= bx < w:
                    p.fillRect(bx, bar_h - 5, 1, 5, beat_col)

        # ── stem dominance strip + vocal zone (top rows) ──────────────────────
        if self._stem_strip is not None:
            n_se = len(self._stem_strip)
            for x in range(w):
                idx = min(int(x / w * n_se), n_se - 1)
                d   = int(self._stem_strip[idx])

                # Dominant stem row
                if d >= 0:
                    c = QColor(_STEM_QCOL[d])
                    c.setAlphaF(0.80)
                    p.fillRect(x, _VOCAL_STRIP_H, 1, _STEM_STRIP_H, c)

                # Vocal zone row (above stem row)
                if self._vocal_mask is not None and self._vocal_mask[idx]:
                    vc = QColor(_STEM_QCOL[_VOCAL_IDX])
                    vc.setAlphaF(0.55)
                    p.fillRect(x, 0, 1, _VOCAL_STRIP_H, vc)

        # ── loop region overlay ───────────────────────────────────────────────
        if self._loop and self._loop.start_s < self._loop.end_s:
            lx1 = int(self._loop.start_s / dur * w)
            lx2 = int(self._loop.end_s   / dur * w)
            loop_base   = QColor(_LOOP_COLOR)
            loop_fill   = QColor(loop_base); loop_fill.setAlphaF(0.18)
            loop_border = QColor(loop_base)  # full opacity for markers

            # Fill: stays within the waveform area
            p.fillRect(lx1, 0, max(1, lx2 - lx1), bar_h, loop_fill)

            # ── Marker lines spanning FULL widget height (including timestamp) ──
            # This makes them "stick out" below the waveform border.
            OVERHANG = lbl_h - 2     # extend into the timestamp strip
            TICK_W   = 8             # width of the bracket tick marks
            y_top    = 0
            y_bot    = bar_h + OVERHANG

            pen2 = QPen(loop_border, 2)
            p.setPen(pen2)
            p.drawLine(lx1, y_top, lx1, y_bot)
            p.drawLine(lx2, y_top, lx2, y_bot)

            # Horizontal bracket ticks at top and bottom of each marker
            # IN marker (lx1): ticks pointing right (into the loop)
            p.drawLine(lx1, y_top, lx1 + TICK_W, y_top)
            p.drawLine(lx1, y_bot, lx1 + TICK_W, y_bot)
            # OUT marker (lx2): ticks pointing left (into the loop)
            p.drawLine(lx2, y_top, lx2 - TICK_W, y_top)
            p.drawLine(lx2, y_bot, lx2 - TICK_W, y_bot)

            # Downward triangle ear at top of IN marker (pointing into the loop)
            p.setBrush(loop_border)
            p.setPen(Qt.PenStyle.NoPen)
            tri_in = QPolygonF([
                QPointF(lx1,          y_top),
                QPointF(lx1 + TICK_W, y_top),
                QPointF(lx1,          y_top + 6),
            ])
            p.drawPolygon(tri_in)

            # Downward triangle ear at top of OUT marker (pointing into the loop)
            tri_out = QPolygonF([
                QPointF(lx2,          y_top),
                QPointF(lx2 - TICK_W, y_top),
                QPointF(lx2,          y_top + 6),
            ])
            p.drawPolygon(tri_out)

            # "LOOP" label when active
            if self._loop.active:
                font = QFont(); font.setPointSize(7); font.setBold(True)
                p.setFont(font)
                p.setPen(loop_border)
                cx = (lx1 + lx2) // 2
                label_w = lx2 - lx1 - 4
                if label_w > 20:
                    p.drawText(
                        QRectF(lx1 + 2, strip_h + 2, label_w, 12),
                        Qt.AlignmentFlag.AlignCenter, "LOOP",
                    )

        # ── auto markers ──────────────────────────────────────────────────────
        if self._markers and dur > 0 and self._marker_mode != MarkerDisplayMode.OFF:
            mfont = QFont()
            mfont.setPointSize(7)
            mfont.setBold(True)
            p.setFont(mfont)
            for m in self._markers:
                conf = float(getattr(m, "confidence", 0.0) or 0.0)
                mval = marker_value(m)
                if not should_draw_marker(mval, conf, self._marker_mode, view="overview"):
                    continue
                mpos = getattr(m, "position_s", 0.0)
                mx     = int((mpos / dur) * w)
                if not (0 <= mx < w):
                    continue
                style = marker_draw_style(mval, conf, self._marker_mode, view="overview")
                mc = QColor(style["color"])
                mc.setAlphaF(style["alpha"])
                pen = QPen(mc, style["line_width"])
                pen.setStyle(style["pen_style"])
                p.setPen(pen)
                tail_h = int(style.get("tail_height", 8))
                y1 = max(strip_h, bar_h - tail_h)
                p.drawLine(mx, y1, mx, bar_h)

                if style["tier"] == "primary":
                    mc.setAlphaF(0.92)
                    tri = QPolygonF([
                        QPointF(mx - 3, bar_h),
                        QPointF(mx + 3, bar_h),
                        QPointF(mx,     bar_h - 5),
                    ])
                    p.setBrush(mc)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.drawPolygon(tri)

                if style["label"]:
                    label = style["label_text"]
                    label_w = min(70, max(26, len(label) * 6 + 10))
                    lx = max(1, min(w - label_w - 1, mx + 3))
                    bg = QColor(theme.BG_DEEP)
                    bg.setAlphaF(0.78)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(bg)
                    p.drawRoundedRect(QRectF(lx, 2, label_w, 13), 3, 3)
                    txt = QColor(style["color"])
                    txt.setAlphaF(0.95)
                    p.setPen(txt)
                    p.drawText(
                        QRectF(lx + 4, 2, label_w - 6, 12),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        label,
                    )

        # ── cue point markers ─────────────────────────────────────────────────
        cue_colors = ["#00d4ff", "#ff6b6b", "#4ecdc4", "#ffe66d",
                      "#a29bfe", "#fd79a8", "#55efc4", "#fdcb6e"]
        for idx, cue_s in enumerate(self._cues):
            cx = int((cue_s / dur) * w)
            c  = QColor(cue_colors[idx % len(cue_colors)])
            p.setPen(QPen(c, 1))
            p.drawLine(cx, 0, cx, bar_h)
            tri = QPolygonF([
                QPointF(cx - 4, 0),
                QPointF(cx + 4, 0),
                QPointF(cx,     6),
            ])
            p.setBrush(c)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(tri)

        # ── playhead ──────────────────────────────────────────────────────────
        p.setPen(QPen(QColor(theme.WHITE), 2))
        p.drawLine(ph_x, 0, ph_x, bar_h)
        tri_size = 5
        tri = QPolygonF([
            QPointF(ph_x - tri_size, bar_h),
            QPointF(ph_x + tri_size, bar_h),
            QPointF(ph_x,            bar_h - tri_size * 1.5),
        ])
        p.setBrush(QColor(theme.WHITE))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(tri)

        # ── bottom border ─────────────────────────────────────────────────────
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(0, bar_h, w, bar_h)

        # ── timestamps ────────────────────────────────────────────────────────
        font = QFont("monospace"); font.setPointSize(8)
        p.setFont(font)
        p.setPen(QColor(theme.TEXT_MED))
        elapsed   = _fmt_time(self._position)
        remaining = "-" + _fmt_time(max(0.0, self._duration - self._position))
        ts_y = bar_h + 2
        p.drawText(QRectF(4,      ts_y, w // 2,     lbl_h - 2),
                   Qt.AlignmentFlag.AlignLeft  | Qt.AlignmentFlag.AlignTop, elapsed)
        p.drawText(QRectF(w // 2, ts_y, w // 2 - 4, lbl_h - 2),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, remaining)
        p.end()

    # ── mouse interaction ─────────────────────────────────────────────────────

    def _seek_from_x(self, x: int) -> None:
        if self._duration <= 0:
            return
        frac = max(0.0, min(1.0, x / self.width()))
        self.seek_requested.emit(frac * self._duration)

    def _marker_at_x(self, x: int):
        """Return the AutoMarker closest to pixel x (within 9px), or None."""
        if not self._markers or self._duration <= 0:
            return None
        w   = self.width()
        dur = self._duration
        best = None
        best_dist = 9
        for m in self._markers:
            conf = float(getattr(m, "confidence", 0.0) or 0.0)
            mval = marker_value(m)
            if not should_draw_marker(mval, conf, self._marker_mode, view="overview"):
                continue
            mx   = int((getattr(m, "position_s", 0.0) / dur) * w)
            dist = abs(x - mx)
            if dist < best_dist:
                best_dist = dist
                best = m
        return best

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            self.marker_right_clicked.emit(self._marker_at_x(ev.pos().x()))
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._seek_from_x(ev.pos().x())

    def mouseMoveEvent(self, ev):
        if self._dragging:
            self._seek_from_x(ev.pos().x())
            return
        m = self._marker_at_x(ev.pos().x())
        if m:
            QToolTip.showText(ev.globalPosition().toPoint(), marker_tooltip(m), self)
        else:
            QToolTip.hideText()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
