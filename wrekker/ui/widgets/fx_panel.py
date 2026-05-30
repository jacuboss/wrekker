"""
FXPanel — inline FX control section for the MasterWidget.

Layout (top → bottom inside a collapsible QWidget):
  [A] [B] [Both]          ← target selector
  [Filter][Echo][Delay]...  ← FX type grid (2 rows × 5)
  Wet ──────────────────○   ← slider
  Depth ────────────────○
  [1/16][1/8][1/4][1/2][1][2][4]  ← time division (for beat-sync FX)
  Color ○──────────────    ← LP→HP or LFO bias
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QSlider,
)

from wrekker.ui import theme
from wrekker.core.transport import (
    FX_BANK_NORMAL, FX_BANK_WREKK, FXState, FX_NAMES, FX_TIME_DIVISIONS,
    FX_TARGET_A, FX_TARGET_B, FX_TARGET_BOTH,
    WREKK_FX_NAMES, WREKK_STEM_TARGETS,
)


def _micro_lbl(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 9px;")
    return lbl


class FXPanel(QWidget):
    """Inline FX control section; toggled by the FX button in MasterWidget."""

    # Emitted when any param changes; parent connects to transport methods
    enabled_changed       = pyqtSignal(bool)
    type_changed          = pyqtSignal(int)
    target_changed        = pyqtSignal(int)
    wet_changed           = pyqtSignal(float)
    depth_changed         = pyqtSignal(float)
    feedback_changed      = pyqtSignal(float)
    time_division_changed = pyqtSignal(float)
    color_changed         = pyqtSignal(float)
    bank_changed          = pyqtSignal(str)
    wrekk_type_changed    = pyqtSignal(int)
    wrekk_target_changed  = pyqtSignal(int)
    wrekk_stem_target_changed = pyqtSignal(int)
    wrekk_wet_changed     = pyqtSignal(float)
    wrekk_depth_changed   = pyqtSignal(float)
    wrekk_feedback_changed = pyqtSignal(float)
    wrekk_time_division_changed = pyqtSignal(float)
    wrekk_color_changed   = pyqtSignal(float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._fx_type = 0
        self._bank = FX_BANK_NORMAL
        self._wrekk_type = 0
        self._wrekk_stem_target = 1
        self._wrekk_stems_status = ""
        self._target  = FX_TARGET_A
        self._wrekk_target = FX_TARGET_A
        self._enabled = False
        self._td_idx  = 3   # default: 1/2

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        bank_row = QHBoxLayout()
        bank_row.setSpacing(2)
        self._btn_bank_normal = self._pill_btn("NORMAL", lambda: self._set_bank(FX_BANK_NORMAL))
        self._btn_bank_wrekk = self._pill_btn("WREKK FX", lambda: self._set_bank(FX_BANK_WREKK))
        bank_row.addWidget(self._btn_bank_normal, stretch=1)
        bank_row.addWidget(self._btn_bank_wrekk, stretch=1)
        root.addLayout(bank_row)

        self._subtitle = QLabel("NORMAL BEAT FX")
        self._subtitle.setStyleSheet(f"color:{theme.TEXT_DIM}; font-size:9px; font-weight:700;")
        root.addWidget(self._subtitle)

        # ── Target: A / B / Both ─────────────────────────────────────────────
        tgt_row = QHBoxLayout()
        tgt_row.setSpacing(2)
        self._btn_tgt_a    = self._pill_btn("A",    lambda: self._set_target(FX_TARGET_A))
        self._btn_tgt_b    = self._pill_btn("B",    lambda: self._set_target(FX_TARGET_B))
        self._btn_tgt_both = self._pill_btn("Both", lambda: self._set_target(FX_TARGET_BOTH))
        for btn in (self._btn_tgt_a, self._btn_tgt_b, self._btn_tgt_both):
            tgt_row.addWidget(btn, stretch=1)
        root.addLayout(tgt_row)

        # ── FX type grid: 2 rows × 5 ─────────────────────────────────────────
        self._type_grid = QGridLayout()
        self._type_grid.setSpacing(2)
        self._type_btns: list[QPushButton] = []
        for i, name in enumerate(FX_NAMES):
            btn = self._pill_btn(name, lambda _=None, idx=i: self._set_type(idx))
            btn.setFixedHeight(20)
            self._type_grid.addWidget(btn, i // 5, i % 5)
            self._type_btns.append(btn)
        root.addLayout(self._type_grid)

        self._wrekk_grid = QGridLayout()
        self._wrekk_grid.setSpacing(2)
        self._wrekk_type_btns: list[QPushButton] = []
        wrekk_tips = [
            "Remove the dry vocal, leave its echo behind.",
            "Wash vocals and texture while rhythm remains clean.",
            "Destroy drums without damaging bass or vocals.",
            "Gate drums and bass to the beat.",
            "Loop-roll one musical layer while the track continues.",
            "Hold the low-end while the rest falls apart.",
            "Break the track into disappearing layers.",
            "Bring the track back together live.",
        ]
        for i, name in enumerate(WREKK_FX_NAMES):
            btn = self._pill_btn(name, lambda _=None, idx=i: self._set_wrekk_type(idx))
            btn.setToolTip(wrekk_tips[i])
            btn.setFixedHeight(20)
            self._wrekk_grid.addWidget(btn, i // 2, i % 2)
            self._wrekk_type_btns.append(btn)
        root.addLayout(self._wrekk_grid)

        self._stem_target_row = QHBoxLayout()
        self._stem_target_row.setSpacing(2)
        self._stem_target_row.addWidget(_micro_lbl("Layer"))
        self._stem_target_btns: list[QPushButton] = []
        for label, value in WREKK_STEM_TARGETS:
            btn = self._pill_btn(label, lambda _=None, v=value: self._set_wrekk_stem_target(v))
            btn.setFixedHeight(18)
            self._stem_target_row.addWidget(btn, stretch=1)
            self._stem_target_btns.append(btn)
        root.addLayout(self._stem_target_row)

        # ── Wet ──────────────────────────────────────────────────────────────
        root.addLayout(self._slider_row("Wet", "wet_sl", self._emit_wet))

        # ── Depth ─────────────────────────────────────────────────────────────
        root.addLayout(self._slider_row("Depth", "depth_sl", self._emit_depth))

        # ── Feedback ──────────────────────────────────────────────────────────
        root.addLayout(self._slider_row("Feedbk", "fb_sl", self._emit_feedback))

        # ── Time division ─────────────────────────────────────────────────────
        td_row = QHBoxLayout()
        td_row.setSpacing(2)
        td_lbl = _micro_lbl("Beat")
        td_lbl.setFixedWidth(30)
        td_row.addWidget(td_lbl)
        self._td_btns: list[QPushButton] = []
        for idx, (label, _value) in enumerate(FX_TIME_DIVISIONS):
            btn = self._pill_btn(label, lambda _=None, i=idx: self._set_td(i))
            btn.setFixedHeight(18)
            td_row.addWidget(btn, stretch=1)
            self._td_btns.append(btn)
        root.addLayout(td_row)

        # ── Color (LP ← 0 → HP) ──────────────────────────────────────────────
        root.addLayout(self._slider_row("Color", "color_sl",
                                        self._emit_color,
                                        lo=-100, hi=100, default=0))

        self._refresh_highlights()

    def _pill_btn(self, text: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setFixedHeight(22)
        btn.setStyleSheet(self._pill_ss(False))
        btn.clicked.connect(slot)
        return btn

    def _pill_ss(self, active: bool) -> str:
        accent = theme.STATUS_WARN if self._bank == FX_BANK_WREKK else theme.DECK_A
        bg  = accent if active else theme.BG_CTRL
        col = theme.BG_DEEP if active else theme.TEXT_MED
        return (
            f"QPushButton {{ background:{bg}; color:{col}; border:none; border-radius:3px;"
            f"  font-size:9px; font-weight:{'700' if active else '400'}; }}"
            f"QPushButton:hover {{ background:{accent if active else theme.BORDER}; }}"
        )

    def _slider_row(self, label: str, attr: str, slot, lo=0, hi=100, default=70) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        lbl = _micro_lbl(label)
        lbl.setFixedWidth(30)
        row.addWidget(lbl)
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(lo, hi)
        sl.setValue(default)
        sl.setFixedHeight(14)
        sl.setStyleSheet(
            f"QSlider::groove:horizontal {{ background:{theme.BG_CTRL}; height:4px; border-radius:2px; }}"
            f"QSlider::handle:horizontal {{ background:{theme.DECK_A}; width:10px; height:10px;"
            f"  margin:-3px 0; border-radius:5px; }}"
            f"QSlider::sub-page:horizontal {{ background:{theme.DECK_A}55; border-radius:2px; }}"
        )
        sl.valueChanged.connect(slot)
        setattr(self, attr, sl)
        row.addWidget(sl, stretch=1)
        return row

    def _emit_wet(self, v: int) -> None:
        value = v / 100.0
        (self.wrekk_wet_changed if self._bank == FX_BANK_WREKK else self.wet_changed).emit(value)

    def _emit_depth(self, v: int) -> None:
        value = v / 100.0
        (self.wrekk_depth_changed if self._bank == FX_BANK_WREKK else self.depth_changed).emit(value)

    def _emit_feedback(self, v: int) -> None:
        value = v / 100.0 * 0.95
        (self.wrekk_feedback_changed if self._bank == FX_BANK_WREKK else self.feedback_changed).emit(value)

    def _emit_color(self, v: int) -> None:
        value = v / 100.0
        (self.wrekk_color_changed if self._bank == FX_BANK_WREKK else self.color_changed).emit(value)

    # ── State helpers ─────────────────────────────────────────────────────────

    def _set_target(self, t: int) -> None:
        if self._bank == FX_BANK_WREKK:
            self._wrekk_target = t
            self.wrekk_target_changed.emit(t)
        else:
            self._target = t
            self.target_changed.emit(t)
        self._refresh_highlights()

    def _set_bank(self, bank: str) -> None:
        self._bank = bank
        self.bank_changed.emit(bank)
        self._refresh_highlights()

    def _set_type(self, idx: int) -> None:
        self._fx_type = idx
        self.type_changed.emit(idx)
        self._refresh_highlights()

    def _set_wrekk_type(self, idx: int) -> None:
        self._wrekk_type = idx
        self.wrekk_type_changed.emit(idx)
        self._refresh_highlights()

    def _set_wrekk_stem_target(self, value: int) -> None:
        self._wrekk_stem_target = value
        self.wrekk_stem_target_changed.emit(value)
        self._refresh_highlights()

    def _set_td(self, idx: int) -> None:
        self._td_idx = idx
        _, value = FX_TIME_DIVISIONS[idx]
        if self._bank == FX_BANK_WREKK:
            self.wrekk_time_division_changed.emit(value)
        else:
            self.time_division_changed.emit(value)
        self._refresh_highlights()

    def _refresh_highlights(self) -> None:
        self._btn_bank_normal.setChecked(self._bank == FX_BANK_NORMAL)
        self._btn_bank_normal.setStyleSheet(self._pill_ss(self._bank == FX_BANK_NORMAL))
        self._btn_bank_wrekk.setChecked(self._bank == FX_BANK_WREKK)
        self._btn_bank_wrekk.setStyleSheet(self._pill_ss(self._bank == FX_BANK_WREKK))
        self._subtitle.setText(
            (self._wrekk_stems_status or "WREKK FX · STEM DECONSTRUCTION EFFECTS")
            if self._bank == FX_BANK_WREKK else "NORMAL BEAT FX"
        )
        self._subtitle.setStyleSheet(
            f"color:{theme.STATUS_WARN if self._bank == FX_BANK_WREKK else theme.TEXT_DIM};"
            " font-size:9px; font-weight:700;"
        )

        show_wrekk = self._bank == FX_BANK_WREKK
        for i in range(self._wrekk_grid.count()):
            widget = self._wrekk_grid.itemAt(i).widget()
            if widget:
                widget.setVisible(show_wrekk)
        for i in range(self._type_grid.count()):
            widget = self._type_grid.itemAt(i).widget()
            if widget:
                widget.setVisible(not show_wrekk)
        for i in range(self._stem_target_row.count()):
            widget = self._stem_target_row.itemAt(i).widget()
            if widget:
                widget.setVisible(show_wrekk and self._wrekk_type == 4)

        tgt_btns = (self._btn_tgt_a, self._btn_tgt_b, self._btn_tgt_both)
        active_target = self._wrekk_target if show_wrekk else self._target
        for i, btn in enumerate(tgt_btns):
            btn.setChecked(i == active_target)
            btn.setStyleSheet(self._pill_ss(i == active_target))

        for i, btn in enumerate(self._type_btns):
            btn.setChecked(i == self._fx_type)
            btn.setStyleSheet(self._pill_ss(i == self._fx_type))

        for i, btn in enumerate(self._wrekk_type_btns):
            btn.setChecked(i == self._wrekk_type)
            btn.setStyleSheet(self._pill_ss(i == self._wrekk_type))

        for i, btn in enumerate(self._stem_target_btns):
            _, value = WREKK_STEM_TARGETS[i]
            btn.setChecked(value == self._wrekk_stem_target)
            btn.setStyleSheet(self._pill_ss(value == self._wrekk_stem_target))

        for i, btn in enumerate(self._td_btns):
            btn.setChecked(i == self._td_idx)
            btn.setStyleSheet(self._pill_ss(i == self._td_idx))

    # ── Public API ────────────────────────────────────────────────────────────

    def apply_fx_state(self, state: FXState) -> None:
        """Sync UI to an FXState (e.g. after loading a session or hardware event)."""
        self._bank = state.fx_bank
        self._target = state.target
        self._fx_type = state.fx_type
        self._wrekk_target = state.wrekk_target
        self._wrekk_type = state.wrekk_fx_type
        self._wrekk_stem_target = state.wrekk_stem_target
        self._wrekk_stems_status = state.wrekk_stems_status
        wet = state.wrekk_wet if self._bank == FX_BANK_WREKK else state.wet
        depth = state.wrekk_depth if self._bank == FX_BANK_WREKK else state.depth
        feedback = state.wrekk_feedback if self._bank == FX_BANK_WREKK else state.feedback
        color = state.wrekk_color if self._bank == FX_BANK_WREKK else state.color
        time_division = state.wrekk_time_division if self._bank == FX_BANK_WREKK else state.time_division
        self.wet_sl.blockSignals(True)
        self.wet_sl.setValue(int(wet * 100))
        self.wet_sl.blockSignals(False)
        self.depth_sl.blockSignals(True)
        self.depth_sl.setValue(int(depth * 100))
        self.depth_sl.blockSignals(False)
        self.fb_sl.blockSignals(True)
        self.fb_sl.setValue(int(feedback / 0.95 * 100))
        self.fb_sl.blockSignals(False)
        self.color_sl.blockSignals(True)
        self.color_sl.setValue(int(color * 100))
        self.color_sl.blockSignals(False)
        # Find closest time division index
        closest = min(
            range(len(FX_TIME_DIVISIONS)),
            key=lambda i: abs(FX_TIME_DIVISIONS[i][1] - time_division),
        )
        self._td_idx = closest
        self._refresh_highlights()
