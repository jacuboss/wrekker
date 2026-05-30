"""
Stem fader panel: 4 horizontal faders with mute/solo and LUFS bar.
"""
from __future__ import annotations
from typing import Callable, Optional

from PyQt6.QtCore    import Qt, pyqtSignal
from PyQt6.QtGui     import QColor
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QSlider, QPushButton,
)

from wrekker.core.deck import STEM_NAMES
from wrekker.ui import theme
from wrekker.ui.widgets.meters import LUFSMeterWidget
from wrekker.ui.widgets.stem_horizon import StemHorizonWidget

_GAIN_STEPS = 200   # slider range 0–200 → 0.0–2.0

def _gain_to_slider(gain: float) -> int:
    return max(0, min(_GAIN_STEPS, int(round(gain * 100))))

def _slider_to_gain(pos: int) -> float:
    return pos / 100.0


class _StemRow(QWidget):
    """One stem's control row."""

    gain_changed  = pyqtSignal(str, float)   # stem_name, gain
    mute_changed  = pyqtSignal(str, bool)    # stem_name, muted
    solo_changed  = pyqtSignal(str, bool)    # stem_name, solo

    def __init__(self, stem: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stem   = stem
        self._muted  = False
        self._soloed = False
        color        = theme.STEM_COLORS[stem]
        label_txt    = theme.STEM_LABELS[stem]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Colored label
        lbl = QLabel(label_txt)
        lbl.setStyleSheet(f"color: {color}; font-size: 10px; font-weight: 700;"
                          f"letter-spacing: 0.8px; min-width: 28px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl)

        control_col = QVBoxLayout()
        control_col.setContentsMargins(0, 0, 0, 0)
        control_col.setSpacing(1)

        self._horizon = StemHorizonWidget(stem=stem)
        control_col.addWidget(self._horizon)

        # Fader slider
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, _GAIN_STEPS)
        self._slider.setValue(_gain_to_slider(1.0))   # unity by default
        self._slider.setStyleSheet(
            f"QSlider::sub-page:horizontal {{ background: {color}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {color}; }}"
        )
        self._slider.valueChanged.connect(self._on_slider)
        control_col.addWidget(self._slider)
        layout.addLayout(control_col, stretch=1)

        # Gain readout
        self._gain_lbl = QLabel("1.0")
        self._gain_lbl.setStyleSheet(
            f"color: {theme.TEXT_MED}; font-size: 10px;"
            f"min-width: 28px; font-family: monospace;"
        )
        self._gain_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._gain_lbl)

        # Mute button
        self._mute_btn = QPushButton("M")
        self._mute_btn.setCheckable(True)
        self._mute_btn.setFixedWidth(24)
        self._mute_btn.setStyleSheet(
            "QPushButton { font-size: 9px; font-weight: 700; padding: 2px; }"
            f"QPushButton:checked {{ background: {theme.STATUS_ERR}; color: #fff; border-color: {theme.STATUS_ERR}; }}"
        )
        self._mute_btn.toggled.connect(self._on_mute)
        layout.addWidget(self._mute_btn)

        # Solo button
        self._solo_btn = QPushButton("S")
        self._solo_btn.setCheckable(True)
        self._solo_btn.setFixedWidth(24)
        self._solo_btn.setStyleSheet(
            "QPushButton { font-size: 9px; font-weight: 700; padding: 2px; }"
            f"QPushButton:checked {{ background: {color}; color: #000; border-color: {color}; }}"
        )
        self._solo_btn.toggled.connect(self._on_solo)
        layout.addWidget(self._solo_btn)

        # Mini LUFS bar
        self._lufs_bar = LUFSMeterWidget()
        self._lufs_bar.setMinimumWidth(10)
        self._lufs_bar.setMaximumWidth(10)
        layout.addWidget(self._lufs_bar)

    # ── signal handlers ───────────────────────────────────────────────────────

    def _on_slider(self, pos: int) -> None:
        gain = _slider_to_gain(pos)
        self._gain_lbl.setText(f"{gain:.1f}")
        self.gain_changed.emit(self._stem, gain)

    def _on_mute(self, checked: bool) -> None:
        self._muted = checked
        opacity = "0.3" if checked else "1.0"
        self._slider.setEnabled(not checked)
        self.mute_changed.emit(self._stem, checked)

    def _on_solo(self, checked: bool) -> None:
        self._soloed = checked
        self.solo_changed.emit(self._stem, checked)

    # ── update from state ─────────────────────────────────────────────────────

    def apply_state(self, gain: float, muted: bool, solo: bool,
                    lufs_m: Optional[float] = None) -> None:
        self._slider.blockSignals(True)
        self._slider.setValue(_gain_to_slider(gain))
        self._slider.blockSignals(False)
        self._gain_lbl.setText(f"{gain:.1f}")

        self._mute_btn.blockSignals(True)
        self._mute_btn.setChecked(muted)
        self._mute_btn.blockSignals(False)

        self._solo_btn.blockSignals(True)
        self._solo_btn.setChecked(solo)
        self._solo_btn.blockSignals(False)

        if lufs_m is not None:
            self._lufs_bar.set_levels(lufs_m, lufs_m)

    def set_level(self, lufs_m: float) -> None:
        self._lufs_bar.set_levels(lufs_m, lufs_m)

    def configure_horizon(self, **kwargs) -> None:
        self._horizon.configure(**kwargs)

    def set_horizon(self, horizon: dict | None) -> None:
        self._horizon.set_horizon(horizon)

    def set_horizon_markers(self, markers) -> None:
        self._horizon.set_markers(markers)

    def set_horizon_position(self, position_s: float) -> None:
        self._horizon.set_position(position_s)


class StemsWidget(QWidget):
    """4 stem fader rows stacked vertically."""

    gain_changed = pyqtSignal(str, float)
    mute_changed = pyqtSignal(str, bool)
    solo_changed = pyqtSignal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Header
        hdr = QLabel("STEMS")
        hdr.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 9px; font-weight: 700;"
            f"letter-spacing: 1px; margin-bottom: 2px;"
        )
        layout.addWidget(hdr)

        self._rows: dict[str, _StemRow] = {}
        for stem in STEM_NAMES:
            row = _StemRow(stem)
            row.gain_changed.connect(self.gain_changed)
            row.mute_changed.connect(self.mute_changed)
            row.solo_changed.connect(self.solo_changed)
            self._rows[stem] = row
            layout.addWidget(row)

    def configure_horizon(self, **kwargs) -> None:
        for row in self._rows.values():
            row.configure_horizon(**kwargs)

    def set_horizon(self, horizon: dict | None) -> None:
        for row in self._rows.values():
            row.set_horizon(horizon)

    def set_horizon_markers(self, markers) -> None:
        for row in self._rows.values():
            row.set_horizon_markers(markers)

    def set_horizon_position(self, position_s: float) -> None:
        for row in self._rows.values():
            row.set_horizon_position(position_s)

    def update_stems(self, stems_dict: dict,
                     stem_meters: Optional[dict[str, float]]) -> None:
        """stems_dict: DeckState.stems; stem_meters: stem→lufs_momentary or None to skip meter update."""
        for stem, row in self._rows.items():
            s = stems_dict.get(stem)
            if s:
                lufs = stem_meters.get(stem) if stem_meters else None
                row.apply_state(s.gain, s.muted, s.solo, lufs)

    def update_levels(self, stem_meters: dict[str, float]) -> None:
        """Fast-path meter update without touching fader/button state."""
        for stem, level in stem_meters.items():
            row = self._rows.get(stem)
            if row:
                row.set_level(level)
