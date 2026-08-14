"""
MasterWidget — center section.

Layout (top → bottom):
  Phase correlator + key compat
  ── separator ──
  [EQ A] | [A|M|B meters] | [EQ B]
  ── separator ──
  Master volume slider
  [A] ──── Crossfader ──── [B]
  FLX4 status
"""
from __future__ import annotations
import math
from typing import Optional

import numpy as np

from PyQt6.QtCore    import Qt, pyqtSignal
from PyQt6.QtGui     import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QFrame, QPushButton,
    QSizePolicy,
)

from wrekker.core.deck import DeckState
from wrekker.ui import theme
from wrekker.ui.widgets.meters      import PhaseBarWidget
from wrekker.ui.widgets.peak_meters import VolumePanelWidget
from wrekker.ui.widgets.eq          import EQWidget
from wrekker.ui.widgets.fx_panel    import FXPanel

__all__ = ["MasterWidget"]


# ── Live oscilloscope ─────────────────────────────────────────────────────────

class _LiveScopeWidget(QWidget):
    """Tiny oscilloscope: paints recent audio as a time-domain waveform."""

    def __init__(self, color: str, label: str, parent=None) -> None:
        super().__init__(parent)
        self._color   = QColor(color)
        self._label   = label
        self._samples: np.ndarray = np.asarray([], dtype=np.float32)
        self.setMinimumSize(30, 56)
        self.setMaximumWidth(80)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_audio(self, flat_stereo: "list[float]") -> None:
        """Accept flat interleaved stereo f32; mix to mono for display."""
        n = len(flat_stereo) // 2
        if n == 0:
            self._samples = np.asarray([], dtype=np.float32)
        else:
            arr = np.asarray(flat_stereo, dtype=np.float32).reshape(n, 2)
            self._samples = (arr[:, 0] + arr[:, 1]) * 0.5
        self.update()

    def paintEvent(self, _ev) -> None:
        from wrekker.ui import theme
        w, h = self.width(), self.height()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bg = QColor("#050607")
        painter.fillRect(0, 0, w, h, bg)

        border = QColor(self._color)
        border.setAlphaF(0.52)
        painter.setPen(QPen(border, 1))
        painter.drawRect(0, 0, max(0, w - 1), max(0, h - 1))

        inner = QColor(theme.WHITE)
        inner.setAlphaF(0.06)
        painter.setPen(QPen(inner, 1))
        painter.drawRect(1, 1, max(0, w - 3), max(0, h - 3))

        center = QColor(theme.TEXT_DIM)
        center.setAlphaF(0.38)
        painter.setPen(QPen(center, 1))
        painter.drawLine(2, h // 2, max(2, w - 3), h // 2)

        # Label
        dim = QColor(self._color)
        dim.setAlphaF(0.84)
        painter.setPen(QPen(dim, 1))
        painter.drawText(3, 13, self._label)

        samples = self._samples
        n = len(samples)
        if n < 2:
            painter.end()
            return

        peak = float(np.percentile(np.abs(samples), 98)) if n > 8 else float(np.max(np.abs(samples)))
        peak = max(0.025, min(1.0, peak))
        gain = 1.0 / peak

        glow = QColor(self._color)
        glow.setAlphaF(0.32)
        painter.setPen(QPen(glow, 3.4))

        top_pad = 15.0
        bottom_pad = 5.0
        drawable_h = max(16.0, h - top_pad - bottom_pad)
        mid_y = top_pad + drawable_h * 0.5
        amp = drawable_h * 0.48
        path = QPainterPath()
        path.moveTo(0, mid_y)
        for xi in range(w):
            src_i = xi * (n - 1) // max(1, w - 1)
            y = mid_y - float(np.clip(samples[src_i] * gain, -1.0, 1.0)) * amp
            y = max(top_pad, min(h - bottom_pad, y))
            path.lineTo(float(xi), y)
        painter.drawPath(path)

        trace = QColor(self._color)
        trace.setAlphaF(1.0)
        pen = QPen(trace)
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)

        painter.end()


def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet(f"background: {theme.BORDER}; max-height: 1px;")
    return f


def _section_lbl(txt: str) -> QLabel:
    l = QLabel(txt)
    l.setStyleSheet(
        f"color: {theme.TEXT_DIM}; font-size: 9px; font-weight: 700; letter-spacing: 1px;"
    )
    return l


class MasterWidget(QWidget):
    """Center master section between the two decks."""

    crossfader_changed = pyqtSignal(float)          # 0.0–1.0
    master_vol_changed = pyqtSignal(float)          # 0.0–2.0
    eq_changed         = pyqtSignal(str, str, float)  # deck_id, band, gain_db

    # FX signals (forwarded from FXPanel)
    fx_enabled_changed       = pyqtSignal(bool)
    fx_type_changed          = pyqtSignal(int)
    fx_target_changed        = pyqtSignal(int)
    fx_wet_changed           = pyqtSignal(float)
    fx_depth_changed         = pyqtSignal(float)
    fx_feedback_changed      = pyqtSignal(float)
    fx_time_division_changed = pyqtSignal(float)
    fx_color_changed         = pyqtSignal(float)
    fx_bank_changed          = pyqtSignal(str)
    wrekk_fx_type_changed    = pyqtSignal(int)
    wrekk_fx_target_changed  = pyqtSignal(int)
    wrekk_fx_stem_target_changed = pyqtSignal(int)
    wrekk_fx_wet_changed     = pyqtSignal(float)
    wrekk_fx_depth_changed   = pyqtSignal(float)
    wrekk_fx_feedback_changed = pyqtSignal(float)
    wrekk_fx_time_division_changed = pyqtSignal(float)
    wrekk_fx_color_changed   = pyqtSignal(float)
    smart_cfx_changed        = pyqtSignal(bool)
    master_cue_pressed       = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(340)
        self.setMaximumWidth(440)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 10, 8, 8)
        root.setSpacing(6)

        # ── MASTER label ──────────────────────────────────────────────────────
        mst = QLabel("MASTER")
        mst.setStyleSheet(
            f"color: {theme.TEXT_MED}; font-size: 10px; font-weight: 800; letter-spacing: 2px;"
        )
        mst.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(mst)

        root.addWidget(_sep())

        # ── Phase correlator ──────────────────────────────────────────────────
        phase_row = QHBoxLayout()
        phase_row.setSpacing(6)
        phase_lbl = _section_lbl("PHASE")
        phase_lbl.setFixedWidth(40)
        phase_row.addWidget(phase_lbl)
        self._phase_bar = PhaseBarWidget()
        phase_row.addWidget(self._phase_bar, stretch=1)
        self._phase_val = QLabel("0.00")
        self._phase_val.setStyleSheet(
            f"color: {theme.TEXT_MED}; font-size: 10px; font-family: monospace; min-width: 36px;"
        )
        self._phase_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        phase_row.addWidget(self._phase_val)
        root.addLayout(phase_row)

        # ── Master loudness (K-weighted) ─────────────────────────────────────
        loud_row = QHBoxLayout()
        loud_row.setSpacing(6)
        loud_lbl = _section_lbl("LOUD")
        loud_lbl.setFixedWidth(40)
        loud_row.addWidget(loud_lbl)
        self._loud_val = QLabel("—")
        self._loud_val.setStyleSheet(
            f"color: {theme.TEXT_MED}; font-size: 10px; font-family: monospace;"
        )
        loud_row.addWidget(self._loud_val, stretch=1)
        root.addLayout(loud_row)

        # ── Key compatibility ─────────────────────────────────────────────────
        key_row = QHBoxLayout()
        key_lbl = _section_lbl("KEY")
        key_lbl.setFixedWidth(40)
        key_row.addWidget(key_lbl)
        self._key_compat = QLabel("—")
        self._key_compat.setStyleSheet(
            f"font-size: 16px; font-weight: 800; color: {theme.TEXT_MED};"
        )
        key_row.addWidget(self._key_compat)
        self._key_detail = QLabel("")
        self._key_detail.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 9px;")
        key_row.addWidget(self._key_detail)
        key_row.addStretch()
        root.addLayout(key_row)

        # ── Headphone MST CUE ────────────────────────────────────────────────
        cue_row = QHBoxLayout()
        cue_row.setSpacing(6)
        cue_row.addStretch()
        self._mst_cue_btn = QPushButton("MST CUE")
        self._mst_cue_btn.setCheckable(True)
        self._mst_cue_btn.setFixedHeight(22)
        self._mst_cue_btn.setMinimumWidth(86)
        self._mst_cue_btn.setMaximumWidth(110)
        self._mst_cue_btn.setStyleSheet(self._cue_btn_ss(False))
        self._mst_cue_btn.clicked.connect(self._on_mst_cue_clicked)
        cue_row.addWidget(self._mst_cue_btn)
        cue_row.addStretch()
        root.addLayout(cue_row)

        root.addWidget(_sep())

        # ── EQ A | A/M/B meters | EQ B ──────────────────────────────────────
        center_row = QHBoxLayout()
        center_row.setSpacing(6)
        center_row.setContentsMargins(0, 0, 0, 0)

        self._eq_a = EQWidget("A")
        self._eq_a.eq_changed.connect(self.eq_changed)
        center_row.addWidget(self._eq_a)

        self._vol_panel = VolumePanelWidget()
        center_row.addWidget(self._vol_panel, stretch=1)

        self._eq_b = EQWidget("B")
        self._eq_b.eq_changed.connect(self.eq_changed)
        center_row.addWidget(self._eq_b)

        root.addLayout(center_row)

        root.addWidget(_sep())

        # ── Master volume ─────────────────────────────────────────────────────
        mv_row = QHBoxLayout()
        mv_row.setSpacing(8)
        mv_row.addWidget(_section_lbl("VOL"))
        self._master_vol = QSlider(Qt.Orientation.Horizontal)
        self._master_vol.setRange(0, 200)
        self._master_vol.setValue(100)
        self._master_vol.setStyleSheet(
            f"QSlider::sub-page:horizontal {{ background: {theme.TEXT_BRIGHT}; border-radius: 2px; }}"
        )
        self._master_vol.valueChanged.connect(
            lambda v: self.master_vol_changed.emit(v / 100.0)
        )
        mv_row.addWidget(self._master_vol, stretch=1)
        root.addLayout(mv_row)

        # ── Crossfader ────────────────────────────────────────────────────────
        xf_row = QHBoxLayout()
        xf_row.setSpacing(6)
        a_lbl = QLabel("A")
        a_lbl.setStyleSheet(f"color: {theme.DECK_A}; font-size: 10px; font-weight: 700;")
        b_lbl = QLabel("B")
        b_lbl.setStyleSheet(f"color: {theme.DECK_B}; font-size: 10px; font-weight: 700;")
        self._crossfader = QSlider(Qt.Orientation.Horizontal)
        self._crossfader.setRange(0, 100)
        self._crossfader.setValue(50)
        self._crossfader.setStyleSheet(
            f"QSlider::groove:horizontal {{ background: qlineargradient("
            f"  x1:0, y1:0, x2:1, y2:0,"
            f"  stop:0 {theme.DECK_A}55, stop:0.5 {theme.BORDER}, stop:1 {theme.DECK_B}55"
            f"); height: 5px; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {theme.WHITE}; width: 16px; height: 16px;"
            f"  margin: -6px 0; border-radius: 8px; }}"
        )
        self._crossfader.valueChanged.connect(
            lambda v: self.crossfader_changed.emit(v / 100.0)
        )
        xf_row.addWidget(a_lbl)
        xf_row.addWidget(self._crossfader, stretch=1)
        xf_row.addWidget(b_lbl)
        root.addLayout(xf_row)

        # ── Pre-fader loudness delta A/B (trim matching) ─────────────────────
        self._loud_delta = QLabel("")
        self._loud_delta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loud_delta.setToolTip(
            "Pre-fader short-term loudness difference between the decks.\n"
            "Match with the TRIM knobs so both tracks mix at equal loudness."
        )
        self._loud_delta.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 9px; font-weight: 700; font-family: monospace;"
        )
        root.addWidget(self._loud_delta)

        root.addWidget(_sep())

        # ── Live oscilloscopes: A | Master | B ────────────────────────────────
        scope_hdr = _section_lbl("LIVE")
        scope_hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(scope_hdr)

        scope_row = QHBoxLayout()
        scope_row.setSpacing(4)
        scope_row.setContentsMargins(0, 2, 0, 0)

        self._scope_a      = _LiveScopeWidget("#18d8ff", "A")
        self._scope_master = _LiveScopeWidget("#e6e8ea", "M")
        self._scope_b      = _LiveScopeWidget("#ff2b65", "B")

        for sc in (self._scope_a, self._scope_master, self._scope_b):
            scope_row.addWidget(sc, stretch=1)
        root.addLayout(scope_row)

        # ── Smart CFX / WREKK toggle ─────────────────────────────────────────
        self._wrekk_btn = QPushButton("WREKK")
        self._wrekk_btn.setCheckable(True)
        self._wrekk_btn.setFixedHeight(24)
        self._wrekk_btn.setStyleSheet(self._wrekk_ss(False))
        self._wrekk_btn.toggled.connect(self._on_wrekk_toggled)
        root.addWidget(self._wrekk_btn)

        self._wrekk_hint = QLabel("CFX: VOC/OTH ← → DRM/BSS")
        self._wrekk_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._wrekk_hint.setVisible(False)
        self._wrekk_hint.setStyleSheet(
            f"color: {theme.STATUS_WARN}; font-size: 9px; font-weight: 700;"
        )
        root.addWidget(self._wrekk_hint)

        # ── FX toggle button ──────────────────────────────────────────────────
        fx_btn_row = QHBoxLayout()
        self._fx_btn = QPushButton("FX")
        self._fx_btn.setCheckable(True)
        self._fx_btn.setFixedHeight(22)
        self._fx_btn.setStyleSheet(self._fx_btn_ss(False))
        self._fx_btn.clicked.connect(self._on_fx_btn_clicked)
        self._fx_enabled_btn = QPushButton("ON")
        self._fx_enabled_btn.setCheckable(True)
        self._fx_enabled_btn.setFixedHeight(22)
        self._fx_enabled_btn.setFixedWidth(38)
        self._fx_enabled_btn.setStyleSheet(self._fx_on_ss(False))
        self._fx_enabled_btn.clicked.connect(self._on_fx_enabled)
        fx_btn_row.addWidget(self._fx_btn, stretch=1)
        fx_btn_row.addWidget(self._fx_enabled_btn)
        root.addLayout(fx_btn_row)

        # ── FX panel (collapsible) ────────────────────────────────────────────
        self._fx_panel = FXPanel(self)
        self._fx_panel.setVisible(False)
        self._fx_panel.enabled_changed.connect(self._on_fx_enabled)
        self._fx_panel.type_changed.connect(self.fx_type_changed)
        self._fx_panel.target_changed.connect(self.fx_target_changed)
        self._fx_panel.wet_changed.connect(self.fx_wet_changed)
        self._fx_panel.depth_changed.connect(self.fx_depth_changed)
        self._fx_panel.feedback_changed.connect(self.fx_feedback_changed)
        self._fx_panel.time_division_changed.connect(self.fx_time_division_changed)
        self._fx_panel.color_changed.connect(self.fx_color_changed)
        self._fx_panel.bank_changed.connect(self.fx_bank_changed)
        self._fx_panel.wrekk_type_changed.connect(self.wrekk_fx_type_changed)
        self._fx_panel.wrekk_target_changed.connect(self.wrekk_fx_target_changed)
        self._fx_panel.wrekk_stem_target_changed.connect(self.wrekk_fx_stem_target_changed)
        self._fx_panel.wrekk_wet_changed.connect(self.wrekk_fx_wet_changed)
        self._fx_panel.wrekk_depth_changed.connect(self.wrekk_fx_depth_changed)
        self._fx_panel.wrekk_feedback_changed.connect(self.wrekk_fx_feedback_changed)
        self._fx_panel.wrekk_time_division_changed.connect(self.wrekk_fx_time_division_changed)
        self._fx_panel.wrekk_color_changed.connect(self.wrekk_fx_color_changed)
        root.addWidget(self._fx_panel)

        root.addWidget(_sep())

        # ── FLX4 status ───────────────────────────────────────────────────────
        self._flx4_status = QLabel("◯  DDJ-FLX4 not connected")
        self._flx4_status.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 10px;"
        )
        self._flx4_status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        root.addWidget(self._flx4_status)

        root.addStretch()

    # ── public API ────────────────────────────────────────────────────────────

    def _fx_btn_ss(self, open_: bool) -> str:
        bg = theme.BG_CTRL
        col = theme.DECK_A if open_ else theme.TEXT_MED
        border = f"1px solid {theme.DECK_A}" if open_ else f"1px solid {theme.BORDER}"
        return (
            f"QPushButton {{ background:{bg}; color:{col}; border:{border}; border-radius:3px;"
            f"  font-size:10px; font-weight:700; letter-spacing:1px; }}"
            f"QPushButton:hover {{ color:{theme.WHITE}; }}"
        )

    def _fx_on_ss(self, on: bool) -> str:
        bg  = theme.DECK_A if on else theme.BG_CTRL
        col = theme.BG_DEEP if on else theme.TEXT_DIM
        return (
            f"QPushButton {{ background:{bg}; color:{col}; border:none; border-radius:3px;"
            f"  font-size:9px; font-weight:700; }}"
        )

    def _cue_btn_ss(self, active: bool) -> str:
        from wrekker.ui import theme as _t
        col    = _t.STATUS_WARN if active else _t.TEXT_DIM
        border = f"1px solid {_t.STATUS_WARN}" if active else f"1px solid {_t.BORDER}"
        bg     = f"{_t.STATUS_WARN}22" if active else _t.BG_CTRL
        return (
            f"QPushButton {{ background:{bg}; color:{col}; border:{border}; border-radius:3px;"
            f"  font-size:9px; font-weight:700; letter-spacing:1px; }}"
        )

    def _on_mst_cue_clicked(self) -> None:
        active = self._mst_cue_btn.isChecked()
        self._mst_cue_btn.setStyleSheet(self._cue_btn_ss(active))
        self.master_cue_pressed.emit()

    def set_monitor_cue_active(self, active: bool) -> None:
        """Reflect transport/hardware MST CUE state on the button."""
        self._mst_cue_btn.blockSignals(True)
        self._mst_cue_btn.setChecked(active)
        self._mst_cue_btn.blockSignals(False)
        self._mst_cue_btn.setStyleSheet(self._cue_btn_ss(active))

    def set_master_volume(self, gain: float) -> None:
        """Push hardware master-volume value (0-2) into slider without re-emitting."""
        self._master_vol.blockSignals(True)
        self._master_vol.setValue(int(round(max(0.0, min(2.0, gain)) * 100)))
        self._master_vol.blockSignals(False)

    def _wrekk_ss(self, on: bool) -> str:
        bg = theme.STATUS_WARN if on else theme.BG_CTRL
        col = theme.BG_DEEP if on else theme.TEXT_MED
        border = f"1px solid {theme.STATUS_WARN}" if on else f"1px solid {theme.BORDER}"
        return (
            f"QPushButton {{ background:{bg}; color:{col}; border:{border}; border-radius:3px;"
            f"  font-size:12px; font-weight:900; letter-spacing:2px; }}"
        )

    def _on_wrekk_toggled(self, enabled: bool) -> None:
        self.set_smart_cfx_enabled(enabled)
        self.smart_cfx_changed.emit(enabled)

    def set_smart_cfx_enabled(self, enabled: bool) -> None:
        self._wrekk_btn.blockSignals(True)
        self._wrekk_btn.setChecked(enabled)
        self._wrekk_btn.blockSignals(False)
        self._wrekk_btn.setStyleSheet(self._wrekk_ss(enabled))
        self._wrekk_hint.setVisible(enabled)
        self._eq_a.set_wrekk_mode(enabled)
        self._eq_b.set_wrekk_mode(enabled)

    def _on_fx_btn_clicked(self, checked: bool) -> None:
        self._fx_panel.setVisible(checked)
        self._fx_btn.setStyleSheet(self._fx_btn_ss(checked))

    def _on_fx_enabled(self, enabled: bool) -> None:
        self._fx_enabled_btn.setChecked(enabled)
        self._fx_enabled_btn.setStyleSheet(self._fx_on_ss(enabled))
        self.fx_enabled_changed.emit(enabled)

    def set_fx_on_state(self, enabled: bool) -> None:
        """Sync ON button from external source (hardware, session load)."""
        self._fx_enabled_btn.setChecked(enabled)
        self._fx_enabled_btn.setStyleSheet(self._fx_on_ss(enabled))

    def update_fx_state(self, fx_state) -> None:
        """Push full FX state from hardware/transport into UI (no signal re-emission)."""
        self.set_fx_on_state(fx_state.active_enabled)
        self._fx_panel.apply_fx_state(fx_state)

    def open_fx_panel(self) -> None:
        """Open the FX panel programmatically (e.g. hardware FX SELECT pressed)."""
        if not self._fx_btn.isChecked():
            self._fx_btn.setChecked(True)
            self._fx_panel.setVisible(True)
            self._fx_btn.setStyleSheet(self._fx_btn_ss(True))

    def set_flx4_connected(self, connected: bool) -> None:
        if connected:
            self._flx4_status.setText("●  DDJ-FLX4")
            self._flx4_status.setStyleSheet(f"color: {theme.STATUS_OK}; font-size: 10px;")
        else:
            self._flx4_status.setText("◯  DDJ-FLX4 not connected")
            self._flx4_status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 10px;")

    def update_states(
        self,
        phase_corr: float,
        state_a:    Optional[DeckState],
        state_b:    Optional[DeckState],
    ) -> None:
        # Phase
        self._phase_bar.set_value(phase_corr)
        sign = "+" if phase_corr >= 0 else ""
        self._phase_val.setText(f"{sign}{phase_corr:.2f}")
        if phase_corr < 0:
            col = theme.STATUS_ERR
        elif phase_corr > 0.7:
            col = theme.STATUS_OK
        else:
            col = theme.TEXT_MED
        self._phase_val.setStyleSheet(
            f"color: {col}; font-size: 10px; font-family: monospace; min-width: 36px;"
        )

        # Key compat
        compat: Optional[float] = None
        key_a_str = key_b_str = ""
        if state_a and state_b:
            compat = state_a.harmonic_compatibility(state_b)
            if state_a.track and state_a.track.key:
                key_a_str = str(state_a.track.key)
            if state_b.track and state_b.track.key:
                key_b_str = str(state_b.track.key)

        if compat is not None and not math.isnan(compat):
            pct = int(compat * 100)
            if compat >= 0.85:   c = theme.STATUS_OK
            elif compat >= 0.5:  c = theme.STATUS_WARN
            else:                c = theme.STATUS_ERR
            self._key_compat.setText(f"{pct}%")
            self._key_compat.setStyleSheet(
                f"font-size: 16px; font-weight: 800; color: {c};"
            )
            detail = f"{key_a_str} ↔ {key_b_str}" if key_a_str and key_b_str else ""
            self._key_detail.setText(detail)
        else:
            self._key_compat.setText("—")
            self._key_compat.setStyleSheet(
                f"font-size: 16px; font-weight: 800; color: {theme.TEXT_MED};"
            )
            self._key_detail.setText("")

    def update_loudness(
        self,
        master_m:  float,
        master_st: float,
        master_tp: float,
        deck_a_st: float,
        deck_b_st: float,
    ) -> None:
        """Master LUFS/TP readout + pre-fader deck loudness delta."""
        if math.isinf(master_st) and math.isinf(master_m):
            self._loud_val.setText("—")
            self._loud_val.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-size: 10px; font-family: monospace;"
            )
        else:
            m_txt  = f"{master_m:.1f}"  if not math.isinf(master_m)  else "—"
            st_txt = f"{master_st:.1f}" if not math.isinf(master_st) else "—"
            tp_txt = f"{master_tp:+.1f}" if not math.isinf(master_tp) else "—"
            # Warn from the short-term level: sustained loudness is what cooks a PA
            if master_st > -8.0 or master_tp > -1.0:
                col = theme.STATUS_ERR
            elif master_st > -12.0:
                col = theme.STATUS_WARN
            else:
                col = theme.TEXT_BRIGHT
            self._loud_val.setText(f"M {m_txt}  S {st_txt} LUFS   TP {tp_txt} dB")
            self._loud_val.setStyleSheet(
                f"color: {col}; font-size: 10px; font-family: monospace;"
            )

        if math.isinf(deck_a_st) or math.isinf(deck_b_st):
            self._loud_delta.setText("")
            return
        delta = deck_b_st - deck_a_st
        if abs(delta) < 0.5:
            text = f"A ≈ B  (Δ {abs(delta):.1f} LU)"
            col = theme.STATUS_OK
        else:
            louder = "B" if delta > 0 else "A"
            text = f"{louder} +{abs(delta):.1f} LU louder — match trim"
            col = theme.STATUS_WARN if abs(delta) < 3.0 else theme.STATUS_ERR
        self._loud_delta.setText(text)
        self._loud_delta.setStyleSheet(
            f"color: {col}; font-size: 9px; font-weight: 700; font-family: monospace;"
        )

    def update_levels(
        self,
        peak_a:  tuple[float, float],
        peak_b:  tuple[float, float],
        peak_m:  tuple[float, float],
        clip_a:  tuple[bool, bool],
        clip_b:  tuple[bool, bool],
        clip_m:  tuple[bool, bool],
    ) -> None:
        self._vol_panel.update_levels(peak_a, peak_b, peak_m, clip_a, clip_b, clip_m)

    def connect_clip_resets(self, reset_a, reset_b, reset_m) -> None:
        self._vol_panel.connect_clip_reset(reset_a, reset_b, reset_m)

    def update_oscilloscopes(
        self,
        audio_a:      "list[float]",
        audio_b:      "list[float]",
        audio_master: "list[float]",
    ) -> None:
        self._scope_a.set_audio(audio_a)
        self._scope_b.set_audio(audio_b)
        self._scope_master.set_audio(audio_master)

    def set_mixer_state(
        self,
        crossfader: float,
        eq_a:  tuple[float, float, float],
        eq_b:  tuple[float, float, float],
    ) -> None:
        """
        Push hardware-sourced mixer values into the UI without emitting signals.
        crossfader: 0.0 (full A) … 1.0 (full B)
        eq_X: (low_db, mid_db, high_db)
        """
        self._crossfader.blockSignals(True)
        self._crossfader.setValue(int(round(crossfader * 100)))
        self._crossfader.blockSignals(False)

        self._eq_a.set_gains(*eq_a)
        self._eq_b.set_gains(*eq_b)

    def set_wrekk_stem_state(
        self,
        stems_a: dict,
        stems_b: dict,
    ) -> None:
        self._eq_a.set_stem_gains(
            stems_a["vocals"].gain, stems_a["drums"].gain, stems_a["bass"].gain
        )
        self._eq_b.set_stem_gains(
            stems_b["vocals"].gain, stems_b["drums"].gain, stems_b["bass"].gain
        )
