"""
EQ widget — 3 vertical faders: LOW / MID / HIGH, ±12 dB per deck.
"""
from __future__ import annotations

from PyQt6.QtCore    import Qt, pyqtSignal
from PyQt6.QtGui     import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
)

from wrekker.ui import theme

_GAIN_STEPS = 240   # 0 → -12 dB, 120 → 0 dB, 240 → +12 dB
_CENTER     = 120

_BAND_META = [
    ("HIGH", "#4fc3f7", "high"),   # light blue
    ("MID",  "#80cbc4", "mid"),    # teal
    ("LOW",  "#ffb74d", "low"),    # amber
]


def _slider_to_db(pos: int) -> float:
    return (pos - _CENTER) / _CENTER * 12.0

def _db_to_slider(db: float) -> int:
    return int(round((db / 12.0) * _CENTER + _CENTER))

def _gain_to_slider(gain: float) -> int:
    return max(0, min(_GAIN_STEPS, int(round(gain * _CENTER))))


class _BandFader(QWidget):
    """Single vertical fader for one EQ band."""

    changed = pyqtSignal(str, float)   # band_name, gain_db

    def __init__(self, label: str, color: str, band: str, parent=None) -> None:
        super().__init__(parent)
        self._band  = band
        self._color = color

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        # Label (top)
        lbl = QLabel(label)
        self._label_widget = lbl
        self._normal_label = label
        lbl.setStyleSheet(
            f"color: {color}; font-size: 9px; font-weight: 700; letter-spacing: 0.5px;"
        )
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)

        # +12 marker
        top_mark = QLabel("+12")
        top_mark.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 8px;")
        top_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(top_mark)

        # Slider
        self._slider = QSlider(Qt.Orientation.Vertical)
        self._slider.setRange(0, _GAIN_STEPS)
        self._slider.setValue(_CENTER)
        self._slider.setInvertedAppearance(False)
        self._slider.setMinimumHeight(120)
        self._slider.setFixedWidth(28)
        self._slider.setStyleSheet(
            f"QSlider::groove:vertical {{"
            f"  width: 3px; background: {theme.BORDER}; border-radius: 2px;"
            f"  margin: 0 12px;"
            f"}}"
            f"QSlider::sub-page:vertical {{"     # below handle = cut
            f"  background: {theme.BG_CTRL}; border-radius: 2px; margin: 0 12px;"
            f"}}"
            f"QSlider::add-page:vertical {{"     # above handle = boost
            f"  background: {color}55; border-radius: 2px; margin: 0 12px;"
            f"}}"
            f"QSlider::handle:vertical {{"
            f"  width: 18px; height: 6px; margin: 0 -8px;"
            f"  border-radius: 3px; background: {color};"
            f"}}"
        )
        self._slider.valueChanged.connect(self._on_change)
        layout.addWidget(self._slider, alignment=Qt.AlignmentFlag.AlignHCenter)

        # -12 marker
        bot_mark = QLabel("-12")
        bot_mark.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 8px;")
        bot_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(bot_mark)

        # dB readout
        self._val_lbl = QLabel("0.0")
        self._val_lbl.setStyleSheet(
            f"color: {theme.TEXT_MED}; font-size: 9px; font-family: monospace;"
        )
        self._val_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._val_lbl)

    def _on_change(self, pos: int) -> None:
        db = _slider_to_db(pos)
        sign = "+" if db >= 0 else ""
        self._val_lbl.setText(f"{sign}{db:.1f}")
        self.changed.emit(self._band, db)

    def set_gain_db(self, db: float) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(_db_to_slider(db))
        self._slider.blockSignals(False)
        sign = "+" if db >= 0 else ""
        self._val_lbl.setText(f"{sign}{db:.1f}")

    def set_display_label(self, label: str) -> None:
        self._label_widget.setText(label)

    def set_stem_gain_visual(self, gain: float) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(_gain_to_slider(gain))
        self._slider.blockSignals(False)
        self._val_lbl.setText(f"{gain:.2f}")


class EQWidget(QWidget):
    """
    3-band EQ panel for one deck.
    Emits eq_changed(deck_id, band, gain_db) when any band moves.
    """

    eq_changed = pyqtSignal(str, str, float)   # deck_id, band, gain_db

    def __init__(self, deck_id: str, parent=None) -> None:
        super().__init__(parent)
        self._deck_id = deck_id
        self.setMinimumWidth(80)
        self.setMaximumWidth(110)

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 8, 4, 8)
        root.setSpacing(4)

        # Deck label
        accent = theme.deck_color(deck_id)
        hdr = QLabel(f"EQ {deck_id}")
        hdr.setStyleSheet(
            f"color: {accent}; font-size: 9px; font-weight: 800; letter-spacing: 1px;"
        )
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(hdr)

        # 3 faders side by side
        faders_row = QHBoxLayout()
        faders_row.setSpacing(4)
        faders_row.setContentsMargins(0, 0, 0, 0)

        self._faders: dict[str, _BandFader] = {}
        for label, color, band in _BAND_META:
            f = _BandFader(label, color, band)
            f.changed.connect(lambda b, db, did=deck_id: self.eq_changed.emit(did, b, db))
            self._faders[band] = f
            faders_row.addWidget(f)

        root.addLayout(faders_row)
        root.addStretch()

    def set_gains(self, low: float, mid: float, high: float) -> None:
        self._faders["low"].set_gain_db(low)
        self._faders["mid"].set_gain_db(mid)
        self._faders["high"].set_gain_db(high)

    def set_wrekk_mode(self, enabled: bool) -> None:
        """WREKK mode affects control routing, not EQ band labels."""
        for band, label in (("high", "HIGH"), ("mid", "MID"), ("low", "LOW")):
            self._faders[band].set_display_label(label)

    def set_stem_gains(self, vocals: float, drums: float, bass: float) -> None:
        self._faders["high"].set_stem_gain_visual(vocals)
        self._faders["mid"].set_stem_gain_visual(drums)
        self._faders["low"].set_stem_gain_visual(bass)
