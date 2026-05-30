"""Compact WREKK Stem Horizon QWidget renderer."""
from __future__ import annotations

import os
from typing import Any

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from wrekker.analysis.stem_horizon import STEM_ORDER, normalize_stem_horizon
from wrekker.ui import theme


_COLORS = {
    "vocals": "#ff5c7a",
    "drums": "#18d8ff",
    "bass": "#ffd23f",
    "other": "#9b7cff",
}
_LABELS = {"vocals": "VOC", "drums": "DRM", "bass": "BSS", "other": "OTH"}


class StemHorizonWidget(QWidget):
    """Compact stem activity forecast.

    When ``stem`` is set, the widget renders only that stem lane so it can sit
    directly above the matching live fader. With ``stem=None`` it renders the
    four-lane overview used by LAB.
    """

    def __init__(self, parent=None, *, stem: str | None = None) -> None:
        super().__init__(parent)
        self._stem = stem if stem in STEM_ORDER else None
        self._mode = "LED Blocks"
        self._range_bars = 8
        self._show_countdown = True
        self._show_w_flag = True
        self._horizon: dict[str, Any] | None = None
        self._markers: tuple = ()
        self._position_s = 0.0
        if self._stem:
            self.setMinimumHeight(10)
            self.setMaximumHeight(12)
        else:
            self.setMinimumHeight(50)
            self.setMaximumHeight(58)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setToolTip("WREKK Stem Horizon" if not self._stem else f"{_LABELS[self._stem]} Stem Horizon")

    def configure(
        self,
        *,
        mode: str | None = None,
        range_bars: int | None = None,
        show_countdown: bool | None = None,
        show_w_flag: bool | None = None,
    ) -> None:
        if mode is not None:
            self._mode = str(mode)
        if range_bars is not None:
            self._range_bars = max(4, min(32, int(range_bars)))
        if show_countdown is not None:
            self._show_countdown = bool(show_countdown)
        if show_w_flag is not None:
            self._show_w_flag = bool(show_w_flag)
        self.setVisible(self._mode.lower() != "off")
        self.update()

    def set_horizon(self, horizon: dict | None) -> None:
        self._horizon = normalize_stem_horizon(horizon)
        self.update()

    def set_markers(self, markers) -> None:
        self._markers = tuple(markers or ())
        self.update()

    def set_position(self, position_s: float) -> None:
        pos = max(0.0, float(position_s or 0.0))
        if abs(pos - self._position_s) < 0.10:
            return
        self._position_s = pos
        self.update()

    def paintEvent(self, _ev) -> None:
        if self._mode.lower() == "off":
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.fillRect(r, QColor(theme.BG_DEEP))
        if not self._horizon:
            self._draw_empty(p, QRectF(r), "" if self._stem else "HORIZON NOT GENERATED")
            p.end()
            return

        mode = self._mode.lower()
        if "waveform" in mode:
            self._draw_waveforms(p, QRectF(r))
        elif "future" in mode:
            self._draw_future_bars(p, QRectF(r))
        else:
            self._draw_led_blocks(p, QRectF(r))
        p.end()

    def _lane_rects(self, r: QRectF) -> list[tuple[str, QRectF]]:
        if self._stem:
            return [(self._stem, QRectF(r.left(), r.top() + 1, max(10.0, r.width()), max(4.0, r.height() - 2)))]
        top = r.top() + 2
        lane_h = max(8.0, (r.height() - 6) / 4.0)
        label_w = 27.0
        return [
            (stem, QRectF(r.left() + label_w, top + i * lane_h, max(10.0, r.width() - label_w - 2), lane_h - 2))
            for i, stem in enumerate(STEM_ORDER)
        ]

    def _draw_label(self, p: QPainter, stem: str, lane: QRectF) -> None:
        if self._stem:
            return
        c = QColor(_COLORS[stem])
        p.setPen(QPen(c, 1))
        p.drawText(QRectF(0, lane.top() - 1, 25, lane.height() + 2), Qt.AlignmentFlag.AlignCenter, _LABELS[stem])

    def _bar_index(self) -> int:
        bars = self._horizon.get("bars") if self._horizon else []
        idx = 0
        for i, pos in enumerate(bars):
            if float(pos) <= self._position_s:
                idx = i
            else:
                break
        return idx

    def _values(self, stem: str, start: int, count: int) -> list[int]:
        vals = ((self._horizon or {}).get("values") or {}).get(stem) or []
        out = []
        for i in range(start, start + count):
            out.append(int(vals[i]) if 0 <= i < len(vals) else 0)
        return out

    def _draw_led_blocks(self, p: QPainter, r: QRectF) -> None:
        start = self._bar_index()
        count = self._range_bars
        for stem, lane in self._lane_rects(r):
            self._draw_label(p, stem, lane)
            vals = self._values(stem, start, count)
            gap = 2.0
            bw = max(3.0, (lane.width() - gap * (count - 1)) / count)
            for i, state in enumerate(vals):
                x = lane.left() + i * (bw + gap)
                col = QColor(_COLORS[stem])
                col.setAlpha(38 if state <= 0 else 120 if state == 1 else 235)
                y_pad = 0.0 if self._stem else 1.0
                p.fillRect(QRectF(x, lane.top() + y_pad, bw, max(1.0, lane.height() - 2 * y_pad)), col)
            p.setPen(QPen(QColor(theme.WHITE), 1))
            p.drawLine(int(lane.left()), int(lane.top()), int(lane.left()), int(lane.bottom()))
        self._draw_next_change(p, r, start)

    def _draw_future_bars(self, p: QPainter, r: QRectF) -> None:
        start = self._bar_index()
        count = self._range_bars
        for stem, lane in self._lane_rects(r):
            self._draw_label(p, stem, lane)
            vals = self._values(stem, start, count)
            if not vals:
                continue
            step = lane.width() / max(1, count)
            mid = lane.center().y()
            for i, state in enumerate(vals):
                col = QColor(_COLORS[stem])
                col.setAlpha(45 if state <= 0 else 115 if state == 1 else 220)
                h = max(1.5, lane.height() * (0.18 if state <= 0 else 0.48 if state == 1 else 0.82))
                p.fillRect(QRectF(lane.left() + i * step, mid - h / 2, step + 0.5, h), col)
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.drawLine(int(lane.left()), int(mid), int(lane.right()), int(mid))
        self._draw_next_change(p, r, start)

    def _draw_waveforms(self, p: QPainter, r: QRectF) -> None:
        # Compact full-track anatomy approximation from bar states. This avoids
        # decoding stems in the live UI while still showing whole-track context.
        bars = (self._horizon or {}).get("bars") or []
        total = max(1, len(bars))
        cur = self._bar_index()
        for stem, lane in self._lane_rects(r):
            self._draw_label(p, stem, lane)
            vals = ((self._horizon or {}).get("values") or {}).get(stem) or []
            step = lane.width() / total
            mid = lane.center().y()
            for i, state in enumerate(vals):
                col = QColor(_COLORS[stem])
                col.setAlpha(50 if state <= 0 else 120 if state == 1 else 220)
                h = lane.height() * (0.18 if state <= 0 else 0.46 if state == 1 else 0.82)
                p.fillRect(QRectF(lane.left() + i * step, mid - h / 2, max(1.0, step), h), col)
            x = lane.left() + cur / max(1, total) * lane.width()
            p.setPen(QPen(QColor(theme.WHITE), 1))
            p.drawLine(int(x), int(lane.top()), int(x), int(lane.bottom()))
        self._draw_next_change(p, r, cur)

    def _draw_next_change(self, p: QPainter, r: QRectF, start_idx: int) -> None:
        if not (self._show_countdown or self._show_w_flag):
            return
        transitions = (self._horizon or {}).get("transitions") or []
        nxt = None
        for tr in transitions:
            if self._stem and tr.get("stem") != self._stem:
                continue
            idx = int(tr.get("bar_index") or 0)
            if start_idx < idx <= start_idx + self._range_bars:
                nxt = tr
                break
        if not nxt:
            return
        stem = str(nxt.get("stem") or "")
        bars = int(nxt.get("bar_index") or 0) - start_idx
        p.setPen(QPen(QColor(theme.STATUS_WARN), 1))
        if self._stem:
            p.drawLine(int(r.right() - 2), int(r.top() + 1), int(r.right() - 2), int(r.bottom() - 1))
            if self._show_countdown and r.width() >= 70:
                p.drawText(QRectF(r.left(), r.top(), r.width() - 5, r.height()), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{bars}b")
        else:
            label = f"{_LABELS.get(stem, stem[:3].upper())} {str(nxt.get('change') or '').upper()} · {bars}b"
            p.drawText(QRectF(r.left(), r.bottom() - 13, r.width() - 3, 12), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)

    def _draw_empty(self, p: QPainter, r: QRectF, text: str) -> None:
        p.setPen(QPen(QColor(theme.TEXT_DIM), 1))
        p.drawRect(r.adjusted(0, 0, -1, -1))
        if text:
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        if os.environ.get("WREKKER_STEM_HORIZON_DEBUG") == "1":
            print("[stem-horizon] no active horizon data", flush=True)
