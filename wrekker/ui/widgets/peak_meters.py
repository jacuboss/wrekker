"""
Peak level meters — vertical LED-style bars with clip indicator.

PeakMeterWidget  : one source (L + R bars), click to reset clip.
VolumePanelWidget: 3 sources side by side (Deck A | Master | Deck B).
"""
from __future__ import annotations
import math

from PyQt6.QtCore    import Qt, QRectF, pyqtSignal
from PyQt6.QtGui     import QPainter, QColor, QPen, QFont
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel

from wrekker.ui import theme

_DB_MIN = -50.0
_DB_MAX  =  3.0   # headroom above 0 dBFS shown
_N_SEGS  = 24

# Color thresholds (dBFS)
_THRESH_GREEN  = -9.0
_THRESH_YELLOW = -3.0
_THRESH_RED    =  0.0   # ≥0 = clip territory


def _lin_to_db(lin: float) -> float:
    if lin <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(lin)


def _seg_color(seg_db: float, lit: bool) -> QColor:
    if not lit:
        if seg_db < _THRESH_GREEN:    return QColor("#122212")
        if seg_db < _THRESH_YELLOW:   return QColor("#1f1f00")
        return QColor("#220000")
    if seg_db < _THRESH_GREEN:        return QColor(theme.STATUS_OK)
    if seg_db < _THRESH_YELLOW:       return QColor(theme.STATUS_WARN)
    return QColor(theme.STATUS_ERR)


class PeakMeterWidget(QWidget):
    """
    Single-source peak meter (L + R bars + clip LED).
    Click anywhere to reset the clip latch.
    """

    clip_reset = pyqtSignal()   # emitted when user clicks to reset

    def __init__(
        self,
        label:    str  = "",
        color:    str  = theme.DECK_A,
        parent:   QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._label   = label
        self._accent  = QColor(color)
        self._peak_l  = 0.0   # linear, 0.0–1.0+
        self._peak_r  = 0.0
        self._clip_l  = False
        self._clip_r  = False

        self.setMinimumWidth(30)
        self.setMinimumHeight(130)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_levels(self, peak_l: float, peak_r: float,
                   clip_l: bool, clip_r: bool) -> None:
        if (abs(peak_l - self._peak_l) < 0.002 and
                abs(peak_r - self._peak_r) < 0.002 and
                clip_l == self._clip_l and clip_r == self._clip_r):
            return
        self._peak_l = peak_l
        self._peak_r = peak_r
        self._clip_l = clip_l
        self._clip_r = clip_r
        self.update()

    def mouseReleaseEvent(self, _) -> None:
        self._clip_l = self._clip_r = False
        self.update()
        self.clip_reset.emit()

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p    = QPainter(self)
        w, h = self.width(), self.height()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # Layout
        clip_h  = 8
        lbl_h   = 14
        bar_h   = h - clip_h - lbl_h - 4
        gap     = 2
        bar_w   = (w - gap) // 2

        seg_h   = bar_h / _N_SEGS
        db_range = _DB_MAX - _DB_MIN

        def _draw_bar(x: int, peak_lin: float, clipped: bool) -> None:
            db_peak = _lin_to_db(peak_lin)
            norm    = max(0.0, min(1.0, (db_peak - _DB_MIN) / db_range))

            for i in range(_N_SEGS):
                seg_db   = _DB_MIN + db_range * (i / _N_SEGS)
                seg_norm = i / _N_SEGS
                lit      = seg_norm < norm
                y        = clip_h + 2 + bar_h - (i + 1) * seg_h
                p.fillRect(QRectF(x, y, bar_w - 1, seg_h - 1),
                           _seg_color(seg_db, lit))

            # Clip LED
            clip_color = QColor(theme.STATUS_ERR) if clipped else QColor("#330000")
            p.fillRect(QRectF(x, 0, bar_w - 1, clip_h - 1), clip_color)

        _draw_bar(0,       self._peak_l, self._clip_l)
        _draw_bar(bar_w + gap, self._peak_r, self._clip_r)

        # Label
        font = QFont()
        font.setPointSize(7)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(self._accent))
        p.drawText(
            QRectF(0, h - lbl_h, w, lbl_h),
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignBottom,
            self._label,
        )
        p.end()


class VolumePanelWidget(QWidget):
    """
    3 PeakMeterWidgets side by side: Deck A | Master | Deck B.
    Also shows the CLIP label + reset button per source.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 2)
        layout.setSpacing(0)

        bars_row = QHBoxLayout()
        bars_row.setSpacing(8)
        bars_row.setContentsMargins(0, 0, 0, 0)

        self._meter_a = PeakMeterWidget("A", theme.DECK_A)
        self._meter_m = PeakMeterWidget("M", theme.TEXT_BRIGHT)
        self._meter_b = PeakMeterWidget("B", theme.DECK_B)

        # Widen the master meter slightly
        self._meter_m.setMinimumWidth(36)

        bars_row.addWidget(self._meter_a)
        bars_row.addWidget(self._meter_m)
        bars_row.addWidget(self._meter_b)

        layout.addLayout(bars_row)

    def update_levels(
        self,
        peak_a:  tuple[float, float],
        peak_b:  tuple[float, float],
        peak_m:  tuple[float, float],
        clip_a:  tuple[bool, bool],
        clip_b:  tuple[bool, bool],
        clip_m:  tuple[bool, bool],
    ) -> None:
        self._meter_a.set_levels(*peak_a, *clip_a)
        self._meter_b.set_levels(*peak_b, *clip_b)
        self._meter_m.set_levels(*peak_m, *clip_m)

    def connect_clip_reset(
        self,
        reset_a,
        reset_b,
        reset_m,
    ) -> None:
        self._meter_a.clip_reset.connect(reset_a)
        self._meter_b.clip_reset.connect(reset_b)
        self._meter_m.clip_reset.connect(reset_m)
