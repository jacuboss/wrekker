"""
Custom meter widgets: LUFS bar, spectrum bands, phase correlator.
All drawn with QPainter — no external assets required.
"""
from __future__ import annotations
import math
from PyQt6.QtCore  import Qt, QRectF, QPointF, QTimer
from PyQt6.QtGui   import QPainter, QColor, QPen, QFont
from PyQt6.QtGui   import QLinearGradient
from PyQt6.QtWidgets import QWidget

from wrekker.ui import theme


class LUFSMeterWidget(QWidget):
    """
    Vertical segmented LUFS bar.
    Range –40 dBFS → 0 dBFS.
    Green below –14, yellow –14→–6, red above –6.
    Two needles: momentary (bright) + short-term (dim).
    """

    _DB_MIN  = -40.0
    _DB_MAX  =   0.0
    _N_SEGS  =  20

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._m  = float("-inf")   # momentary
        self._st = float("-inf")   # short-term
        self.setMinimumWidth(14)
        self.setMinimumHeight(60)
        self.setMaximumWidth(20)

    def set_levels(self, momentary: float, short_term: float) -> None:
        self._m  = momentary
        self._st = short_term
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        gap  = 1
        sh   = (h - gap * (self._N_SEGS - 1)) / self._N_SEGS

        def _norm(db: float) -> float:
            if math.isinf(db):
                return 0.0
            return max(0.0, min(1.0, (db - self._DB_MIN) / (self._DB_MAX - self._DB_MIN)))

        norm_m  = _norm(self._m)
        norm_st = _norm(self._st)

        for i in range(self._N_SEGS):
            frac = i / self._N_SEGS          # 0 = bottom, 1 = top
            y    = h - (i + 1) * sh - i * gap
            rect = QRectF(0, y, w, sh)

            # Segment color based on level
            if frac < 0.6:     raw = QColor("#1a5c2a")   # green dim
            elif frac < 0.85:  raw = QColor("#7a6200")   # yellow dim
            else:              raw = QColor("#7a1a1a")   # red dim

            lit_m  = frac < norm_m
            lit_st = frac < norm_st

            if lit_m:
                if frac < 0.6:     c = QColor(theme.STATUS_OK)
                elif frac < 0.85:  c = QColor(theme.STATUS_WARN)
                else:              c = QColor(theme.STATUS_ERR)
                p.fillRect(rect, c)
            elif lit_st:
                dim = QColor(raw)
                dim.setAlphaF(0.6)
                p.fillRect(rect, dim)
            else:
                p.fillRect(rect, raw)

        p.end()


class SpectrumBarsWidget(QWidget):
    """
    16 vertical bars (30 Hz – 18 kHz, log-spaced) with attack/decay smoothing.

    Receives raw dBFS values via set_spectrum(). An internal 60 Hz timer
    applies fast attack + controlled decay so bars animate smoothly.
    """

    _N      = 16
    _DB_MIN = -72.0
    _DB_MAX =  -3.0   # leave headroom so the bars aren't always clipping

    # Per-frame smoothing factors at 60 Hz.
    _ATTACK = 0.85
    _DECAY  = 0.08

    # Peak hold: number of 60 Hz frames to hold before decaying
    _PEAK_HOLD_FRAMES = 18

    def __init__(self, color: str = theme.DECK_A, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color    = QColor(color)
        self._targets  = [-96.0] * self._N
        self._smoothed = [-96.0] * self._N
        self._peaks    = [-96.0] * self._N
        self._hold_cnt = [0]     * self._N

        self.setMinimumHeight(56)
        self.setMaximumHeight(80)

        self._anim = QTimer(self)
        self._anim.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim.setInterval(16)   # 60 Hz
        self._anim.timeout.connect(self._step)
        self._anim.start()

    def set_spectrum(self, bands: tuple[float, ...]) -> None:
        """Called at 10 Hz from the UI tick with new dBFS values."""
        for i, v in enumerate(bands[:self._N]):
            self._targets[i] = v

    def _step(self) -> None:
        changed = False
        for i in range(self._N):
            cur = self._smoothed[i]
            tgt = self._targets[i]
            if tgt > cur:
                nv = cur + (tgt - cur) * self._ATTACK
            else:
                nv = cur + (tgt - cur) * self._DECAY
            self._smoothed[i] = nv

            if nv > self._peaks[i]:
                self._peaks[i] = nv
                self._hold_cnt[i] = self._PEAK_HOLD_FRAMES
            elif self._hold_cnt[i] > 0:
                self._hold_cnt[i] -= 1
            else:
                self._peaks[i] += (nv - self._peaks[i]) * self._DECAY

            if abs(nv - cur) > 0.05:
                changed = True
        if changed:
            self.update()

    def paintEvent(self, _):
        p  = QPainter(self)
        w, h = self.width(), self.height()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        n     = self._N
        gap   = 2
        bar_w = max(2, (w - (n - 1) * gap) // n)

        for i in range(n):
            val  = self._smoothed[i]
            norm = max(0.0, min(1.0, (val - self._DB_MIN) / (self._DB_MAX - self._DB_MIN)))
            x    = i * (bar_w + gap)
            bh   = int(norm * h)

            # Background slot
            p.fillRect(x, 0, bar_w, h, QColor(theme.BORDER))

            # Filled column
            if bh > 0:
                y = h - bh
                if norm < 0.65:
                    col = QColor(self._color)
                    col.setAlphaF(0.75 + norm * 0.25)
                elif norm < 0.85:
                    col = QColor(theme.STATUS_WARN)
                else:
                    col = QColor(theme.STATUS_ERR)
                p.fillRect(x, y, bar_w, bh, col)

            # Peak hold marker (1 px line)
            pk   = self._peaks[i]
            pnrm = max(0.0, min(1.0, (pk - self._DB_MIN) / (self._DB_MAX - self._DB_MIN)))
            if pnrm > 0.01:
                py = max(0, h - int(pnrm * h) - 1)
                pk_col = QColor(theme.WHITE)
                pk_col.setAlphaF(0.8)
                p.fillRect(x, py, bar_w, 1, pk_col)

        p.end()

    def hideEvent(self, ev):
        self._anim.stop()
        super().hideEvent(ev)

    def showEvent(self, ev):
        self._anim.start()
        super().showEvent(ev)


class PhaseBarWidget(QWidget):
    """
    Horizontal phase-correlation bar.
    Range –1 (anti-phase) → +1 (in-phase).
    Center = 0, green when >0.7, red when <0.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self.setMinimumHeight(12)
        self.setMaximumHeight(16)

    def set_value(self, v: float) -> None:
        self._value = max(-1.0, min(1.0, v))
        self.update()

    def paintEvent(self, _):
        p    = QPainter(self)
        w, h = self.width(), self.height()
        cx   = w // 2

        # Background
        p.fillRect(0, 0, w, h, QColor(theme.BORDER))

        norm  = (self._value + 1.0) / 2.0   # 0=left, 0.5=center, 1=right
        bar_x = int(cx * (1.0 - abs(self._value))) if self._value < 0 else cx
        bar_w = abs(int(norm * w - cx)) if self._value != 0 else 0

        if self._value > 0.7:  c = QColor(theme.STATUS_OK)
        elif self._value > 0:  c = QColor(theme.STATUS_WARN)
        else:                  c = QColor(theme.STATUS_ERR)

        if bar_w > 0:
            p.fillRect(QRectF(bar_x, 1, bar_w, h - 2), c)

        # Center tick
        p.setPen(QPen(QColor(theme.TEXT_MED), 1))
        p.drawLine(cx, 0, cx, h)
        p.end()
