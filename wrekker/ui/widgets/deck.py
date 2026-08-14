"""
DeckWidget — full panel for one DJ deck.
Assembles: track info, position bar, metrics strip, stems, transport controls.
"""
from __future__ import annotations
import math
import os
import time
from typing import Optional

from PyQt6.QtCore    import QRectF, Qt, pyqtSignal, QTimer
from PyQt6.QtGui     import QColor, QFont, QPainter, QPixmap, QCursor, QAction
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QSizePolicy, QSlider, QMenu,
)

from wrekker.core.deck import (
    MARKER_MIN_CONFIDENCE,
    DeckState,
    DeckStatus,
    DeckMetrics,
    STEM_NAMES,
)
from wrekker.ui import theme
from wrekker.ui.widgets.meters   import LUFSMeterWidget, SpectrumBarsWidget
from wrekker.ui.widgets.stems    import StemsWidget
from wrekker.ui.widgets.waveform      import PositionBarWidget
from wrekker.ui.widgets.zoom_waveform import ZoomWaveformWidget
from wrekker.ui.widgets.texture_zoom_waveform import TextureZoomWaveformWidget
from wrekker.ui.widgets.deck_timeline_quick import DeckTimelineQuick
from wrekker.ui.widgets.marker_style import (
    MarkerDisplayMode,
    coerce_marker_display_mode,
    marker_color,
    marker_label,
    marker_tier,
    marker_tooltip,
    marker_value,
)

_NEXT_MARKER_MIN_CONF = MARKER_MIN_CONFIDENCE
_HUD_MARKER_CATEGORIES = (
    ("primary", "P"),
    ("wrekk", "W"),
    ("guide", "G"),
)


def _make_zoom_waveform(deck_id: str) -> ZoomWaveformWidget:
    renderer = os.environ.get("WREKKER_ZOOM_RENDERER", "texture").strip().lower()
    if renderer in {"classic", "legacy", "qwidget"}:
        return ZoomWaveformWidget(deck_id)
    return TextureZoomWaveformWidget(deck_id)


def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet(f"background: {theme.BORDER}; max-height: 1px;")
    return f


def _lbl_small(text: str) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:9px;font-weight:700;")
    return l


class _MetricLabel(QLabel):
    """Small labelled value pair."""

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self._label = label
        self._value = "—"
        self.setFixedHeight(16)
        self.setMinimumWidth(58)
        self._refresh()

    def set_value(self, value: str) -> None:
        self._value = value
        self._refresh()

    def _refresh(self) -> None:
        self.setText(
            f'<span style="color:{theme.TEXT_DIM};font-size:9px;">{self._label} </span>'
            f'<span style="color:{theme.TEXT_BRIGHT};font-size:13px;font-weight:700;">{self._value}</span>'
        )


class PhraseMeterWidget(QWidget):
    """Compact 8/16-bar phrase progress indicator."""

    _COLORS = {
        "locked": "#2ecc71",
        "beat": "#f39c12",
        "off": "#e74c3c",
        "idle": theme.BORDER_LIT,
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._progress = 0.0
        self._beats_done = 0
        self._beats_total = 32
        self._status = "idle"
        self.setFixedHeight(18)
        self.setMinimumWidth(120)

    def set_phrase(
        self,
        progress: float,
        beats_done: int,
        beats_total: int,
        status: str,
    ) -> None:
        self._progress = max(0.0, min(1.0, progress))
        self._beats_done = max(0, beats_done)
        self._beats_total = max(1, beats_total)
        self._status = status if status in self._COLORS else "idle"
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = self.width()
        h = self.height()
        bar_h = 5
        y = (h - bar_h) // 2
        p.fillRect(0, y, w, bar_h, QColor(theme.BORDER))

        col = QColor(self._COLORS[self._status])
        fill_w = int(w * self._progress)
        if fill_w > 0:
            p.fillRect(0, y, fill_w, bar_h, col)

        beats = 32 if self._beats_total <= 32 else 64
        step = max(1, beats // 8)
        for i in range(1, 8):
            x = int(w * (i * step / beats))
            marker_col = QColor(theme.BG_DEEP if i % 2 else theme.TEXT_DIM)
            p.fillRect(x, y - 2, 1, bar_h + 4, marker_col)

        head_x = max(0, min(w - 1, fill_w))
        p.fillRect(QRectF(head_x - 1, y - 4, 3, bar_h + 8), col)
        p.end()


class DeckWidget(QWidget):
    """Complete deck panel."""

    # Signals forwarded to Transport
    play_pause          = pyqtSignal(str)          # deck_id
    cue_pressed         = pyqtSignal(str)          # deck_id
    cue_released        = pyqtSignal(str)          # deck_id — for hold-to-preview
    loop_in             = pyqtSignal(str)
    loop_out            = pyqtSignal(str)
    loop_toggle         = pyqtSignal(str)
    sync_pressed        = pyqtSignal(str)          # deck_id → sync to master
    sync_master_pressed = pyqtSignal(str)          # deck_id → set as sync master
    seek                = pyqtSignal(str, float)   # deck_id, position_s
    stem_gain           = pyqtSignal(str, str, float)
    stem_mute           = pyqtSignal(str, str, bool)
    stem_solo           = pyqtSignal(str, str, bool)
    channel_volume      = pyqtSignal(str, float)   # deck_id, 0.0–1.0
    pregain_changed     = pyqtSignal(str, float)   # deck_id, 0.0–2.0
    monitor_cue_pressed       = pyqtSignal(str)          # deck_id
    # Auto-marker user actions
    clear_auto_markers_sig    = pyqtSignal(str)          # deck_id
    remove_auto_marker_sig    = pyqtSignal(str, str)     # deck_id, marker_id
    convert_marker_to_cue_sig = pyqtSignal(str, object)  # deck_id, AutoMarker
    regenerate_markers_sig    = pyqtSignal(str)          # deck_id
    edit_analysis_sig         = pyqtSignal(str)          # wrk_path

    def __init__(self, deck_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._deck_id = deck_id
        self._state:            Optional[DeckState]   = None
        self._metrics:          Optional[DeckMetrics] = None
        self._marker_mode = MarkerDisplayMode.ESSENTIAL
        self._markers_visible:  bool = True
        self._next_marker_last_t: float = 0.0
        self._next_marker_key: dict[str, tuple] = {}
        self._marker_cache_source: tuple | None = None
        self._markers_by_tier: dict[str, tuple] = {"primary": (), "wrekk": (), "guide": ()}
        self._track_info_key: tuple | None = None

        accent = theme.deck_color(deck_id)
        self.setMinimumWidth(340)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ── deck label ────────────────────────────────────────────────────────
        deck_lbl = QLabel(f"DECK {deck_id}")
        deck_lbl.setStyleSheet(
            f"color: {accent}; font-size: 10px; font-weight: 800;"
            f"letter-spacing: 2px;"
        )
        root.addWidget(deck_lbl)

        # ── track info + artwork ──────────────────────────────────────────────
        track_row = QHBoxLayout()
        track_row.setSpacing(8)
        track_row.setContentsMargins(0, 0, 0, 0)

        # Artwork thumbnail
        self._artwork_lbl = QLabel()
        self._artwork_lbl.setFixedSize(48, 48)
        self._artwork_lbl.setStyleSheet(
            f"background: {theme.BG_CTRL}; border: 1px solid {theme.BORDER};"
            f"border-radius: 2px;"
        )
        self._artwork_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._artwork_lbl.setText("♪")
        self._artwork_lbl.setStyleSheet(
            f"background: {theme.BG_CTRL}; border: 1px solid {theme.BORDER};"
            f"border-radius: 2px; color: {theme.TEXT_DIM}; font-size: 18px;"
        )
        self._artwork_pixmap: Optional[QPixmap] = None
        track_row.addWidget(self._artwork_lbl)

        # Artist + title stacked
        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        info_col.setContentsMargins(0, 0, 0, 0)

        self._artist_lbl = QLabel("—")
        self._artist_lbl.setStyleSheet(
            f"color: {theme.TEXT_MED}; font-size: 11px;"
        )
        self._artist_lbl.setWordWrap(False)
        info_col.addWidget(self._artist_lbl)

        self._title_lbl = QLabel("No track loaded")
        self._title_lbl.setStyleSheet(
            f"color: {theme.WHITE}; font-size: 13px; font-weight: 700;"
        )
        self._title_lbl.setWordWrap(False)
        info_col.addWidget(self._title_lbl)
        info_col.addStretch()

        track_row.addLayout(info_col, stretch=1)
        root.addLayout(track_row)

        root.addWidget(_sep())

        # ── moving waveforms / timeline overlays ─────────────────────────────
        self._timeline_qml = DeckTimelineQuick(deck_id)
        if self._timeline_qml.available:
            self._timeline_qml.seek_requested.connect(lambda s: self.seek.emit(deck_id, s))
            self._timeline_qml.marker_right_clicked.connect(self._on_marker_right_clicked)
            root.addWidget(self._timeline_qml)
            self._zoom_wf = None
            self._pos_bar = None
        else:
            # Fallback kept for environments where Qt Quick cannot initialise.
            self._zoom_wf = _make_zoom_waveform(deck_id)
            self._zoom_wf.seek_requested.connect(lambda s: self.seek.emit(deck_id, s))
            self._zoom_wf.marker_right_clicked.connect(self._on_marker_right_clicked)
            root.addWidget(self._zoom_wf)

            self._pos_bar = PositionBarWidget(deck_id)
            self._pos_bar.seek_requested.connect(lambda s: self.seek.emit(deck_id, s))
            self._pos_bar.marker_right_clicked.connect(self._on_marker_right_clicked)
            root.addWidget(self._pos_bar)

        hud_row = QHBoxLayout()
        hud_row.setSpacing(8)
        hud_row.setContentsMargins(0, 0, 0, 0)

        next_marker_col = QVBoxLayout()
        next_marker_col.setSpacing(0)
        next_marker_col.setContentsMargins(0, 0, 0, 0)
        self._next_marker_lbls: dict[str, QLabel] = {}
        self._next_marker_leds: dict[str, QLabel] = {}
        for tier, title in _HUD_MARKER_CATEGORIES:
            row = QHBoxLayout()
            row.setSpacing(6)
            row.setContentsMargins(0, 0, 0, 0)
            led = QLabel()
            led.setFixedSize(7, 7)
            self._set_marker_confidence_led(led, None)
            self._next_marker_leds[tier] = led
            row.addWidget(led, alignment=Qt.AlignmentFlag.AlignVCenter)

            lbl = QLabel(f"{title}: —")
            lbl.setFixedHeight(14)
            lbl.setStyleSheet(self._hud_marker_style(tier))
            self._next_marker_lbls[tier] = lbl
            row.addWidget(lbl, stretch=1)
            next_marker_col.addLayout(row)
        hud_row.addLayout(next_marker_col, stretch=1)

        # ── compact deck metrics, kept beside approaching markers ───────────
        metrics_wrap = QHBoxLayout()
        metrics_wrap.setSpacing(6)
        metrics_wrap.setContentsMargins(0, 0, 0, 0)

        metrics_col = QVBoxLayout()
        metrics_col.setSpacing(0)
        metrics_col.setContentsMargins(0, 0, 0, 0)
        metrics_top = QHBoxLayout()
        metrics_top.setSpacing(8)
        metrics_top.setContentsMargins(0, 0, 0, 0)
        metrics_bottom = QHBoxLayout()
        metrics_bottom.setSpacing(8)
        metrics_bottom.setContentsMargins(0, 0, 0, 0)

        self._bpm_lbl  = _MetricLabel("BPM")
        self._key_lbl  = _MetricLabel("KEY")
        self._lufs_lbl = _MetricLabel("LUFS")
        self._pitch_lbl = _MetricLabel("PITCH")
        self._wrekk_badge = QLabel("WREKK")
        self._wrekk_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._wrekk_badge.setVisible(False)
        self._wrekk_badge.setStyleSheet(
            f"background:{theme.STATUS_WARN}; color:{theme.BG_DEEP};"
            f"font-size:10px; font-weight:900; letter-spacing:2px; padding:2px 6px;"
        )

        metrics_top.addWidget(self._bpm_lbl)
        metrics_top.addWidget(self._key_lbl)
        metrics_bottom.addWidget(self._lufs_lbl)
        metrics_bottom.addWidget(self._pitch_lbl)
        metrics_col.addLayout(metrics_top)
        metrics_col.addLayout(metrics_bottom)
        metrics_wrap.addLayout(metrics_col)
        metrics_wrap.addWidget(self._wrekk_badge)

        # LUFS bar (mini vertical, placed inline)
        self._lufs_bar = LUFSMeterWidget()
        metrics_wrap.addWidget(self._lufs_bar)

        hud_row.addLayout(metrics_wrap)
        root.addLayout(hud_row)

        # ── phrase meter: 8/16-bar progress, color-coded by sync quality ─────
        phrase_row = QHBoxLayout()
        phrase_row.setSpacing(8)
        phrase_row.setContentsMargins(0, 0, 0, 0)
        phrase_row.addWidget(_lbl_small("PHRASE"))
        self._phrase_meter = PhraseMeterWidget()
        phrase_row.addWidget(self._phrase_meter, stretch=1)
        root.addLayout(phrase_row)

        # ── channel fader + pregain row ───────────────────────────────────────
        gain_row = QHBoxLayout()
        gain_row.setSpacing(8)
        gain_row.setContentsMargins(0, 0, 0, 0)

        # VOL label + slider (0–100, maps to 0.0–1.0)
        gain_row.addWidget(_lbl_small("VOL"))
        self._ch_fader = QSlider(Qt.Orientation.Horizontal)
        self._ch_fader.setRange(0, 100)
        self._ch_fader.setValue(100)
        self._ch_fader.setFixedHeight(18)
        self._ch_fader.setStyleSheet(
            f"QSlider::groove:horizontal {{height:4px;background:{theme.BORDER};border-radius:2px;}}"
            f"QSlider::sub-page:horizontal {{background:{accent};border-radius:2px;}}"
            f"QSlider::handle:horizontal {{background:{theme.WHITE};width:12px;height:12px;"
            f"  margin:-4px 0;border-radius:6px;}}"
        )
        self._ch_fader.valueChanged.connect(
            lambda v: self.channel_volume.emit(deck_id, v / 100.0)
        )
        gain_row.addWidget(self._ch_fader, stretch=2)

        # GAIN label + slider (0–200, maps to 0.0–2.0)
        self._pregain_label = _lbl_small("GAIN")
        gain_row.addWidget(self._pregain_label)
        self._pregain_slider = QSlider(Qt.Orientation.Horizontal)
        self._pregain_slider.setRange(0, 200)
        self._pregain_slider.setValue(100)
        self._pregain_slider.setFixedHeight(18)
        self._pregain_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{height:4px;background:{theme.BORDER};border-radius:2px;}}"
            f"QSlider::sub-page:horizontal {{background:{theme.TEXT_MED};border-radius:2px;}}"
            f"QSlider::handle:horizontal {{background:{theme.WHITE};width:12px;height:12px;"
            f"  margin:-4px 0;border-radius:6px;}}"
        )
        self._pregain_slider.valueChanged.connect(
            lambda v: self.pregain_changed.emit(deck_id, v / 100.0)
        )
        gain_row.addWidget(self._pregain_slider, stretch=1)

        root.addLayout(gain_row)

        # ── stems status label ────────────────────────────────────────────────
        self._stems_status = QLabel("")
        self._stems_status.setStyleSheet(
            f"color: {theme.STATUS_WARN}; font-size: 10px;"
        )
        root.addWidget(self._stems_status)

        root.addWidget(_sep())

        # ── stem faders ───────────────────────────────────────────────────────
        self._stems_widget = StemsWidget()
        self._stems_widget.gain_changed.connect(
            lambda stem, gain: self.stem_gain.emit(deck_id, stem, gain))
        self._stems_widget.mute_changed.connect(
            lambda stem, muted: self.stem_mute.emit(deck_id, stem, muted))
        self._stems_widget.solo_changed.connect(
            lambda stem, solo: self.stem_solo.emit(deck_id, stem, solo))
        root.addWidget(self._stems_widget)

        root.addWidget(_sep())

        # ── spectrum ──────────────────────────────────────────────────────────
        self._spectrum = SpectrumBarsWidget(color=accent)
        root.addWidget(self._spectrum)

        root.addWidget(_sep())

        # ── transport controls ────────────────────────────────────────────────
        transport_row = QHBoxLayout()
        transport_row.setSpacing(6)

        def _btn(txt: str, checked: bool = False, w: int = 0) -> QPushButton:
            b = QPushButton(txt)
            b.setCheckable(checked)
            b.setFixedHeight(34)
            if w:
                b.setFixedWidth(w)
            return b

        self._play_btn     = _btn("▶", checked=True, w=44)
        self._cue_btn      = _btn("CUE", w=44)
        self._loop_in_btn  = _btn("IN",  w=36)
        self._loop_out_btn = _btn("OUT", w=40)
        self._loop_btn     = _btn("⟳", checked=True, w=36)
        self._sync_btn     = _btn("SYNC", checked=True, w=48)
        self._master_btn   = _btn("M",    checked=True, w=26)
        self._pfl_btn      = _btn("PFL", checked=True, w=36)
        self._mkrs_btn     = _btn("MKRS", checked=True, w=44)

        self._play_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BG_CTRL}; border: 1px solid {theme.BORDER}; color: {theme.TEXT_MED}; }}"
            f"QPushButton:checked {{ background: {accent}22; border-color: {accent}; color: {accent}; }}"
        )
        self._sync_btn_default_ss = (
            f"QPushButton {{ background: {theme.BG_CTRL}; border: 1px solid {theme.BORDER};"
            f"  color: {theme.TEXT_DIM}; font-size: 9px; }}"
            f"QPushButton:hover {{ border-color: #00d4ff; color: #00d4ff; }}"
        )
        self._sync_btn.setStyleSheet(self._sync_btn_default_ss)
        self._master_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BG_CTRL}; border: 1px solid {theme.BORDER}; color: {theme.TEXT_DIM}; font-size: 9px; font-weight: 700; }}"
            f"QPushButton:checked {{ background: #ffaa0033; border-color: #ffaa00; color: #ffaa00; }}"
        )
        self._master_btn.setToolTip("Set as sync tempo master")
        self._pfl_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BG_CTRL}; border: 1px solid {theme.BORDER}; color: {theme.TEXT_DIM}; font-size: 9px; font-weight: 700; }}"
            f"QPushButton:checked {{ background: {accent}33; border-color: {accent}; color: {accent}; }}"
        )
        self._pfl_btn.setToolTip("Headphone CUE / PFL (Pre-Fader Listen)")
        self._mkrs_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BG_CTRL}; border: 1px solid {theme.BORDER}; color: {theme.TEXT_DIM}; font-size: 9px; font-weight: 700; }}"
            f"QPushButton:checked {{ background: #a78bfa33; border-color: #a78bfa; color: #a78bfa; }}"
        )
        self._mkrs_btn.setToolTip("Auto markers: ESSENTIAL\nRight-click for modes/actions")
        self._mkrs_btn.setChecked(True)
        self._mkrs_btn.toggled.connect(self._on_markers_toggled)
        self._mkrs_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._mkrs_btn.customContextMenuRequested.connect(self._on_mkrs_btn_context)

        self._play_btn.toggled.connect(lambda _: self.play_pause.emit(deck_id))

        # CUE: press → cue_pressed, release → cue_released (hold-to-preview)
        self._cue_btn.pressed.connect(lambda: self.cue_pressed.emit(deck_id))
        self._cue_btn.released.connect(lambda: self.cue_released.emit(deck_id))

        self._loop_in_btn.clicked.connect(lambda: self.loop_in.emit(deck_id))
        self._loop_out_btn.clicked.connect(lambda: self.loop_out.emit(deck_id))
        self._loop_btn.toggled.connect(lambda _: self.loop_toggle.emit(deck_id))
        self._sync_btn.clicked.connect(lambda: self.sync_pressed.emit(deck_id))
        self._master_btn.clicked.connect(lambda: self.sync_master_pressed.emit(deck_id))
        self._pfl_btn.clicked.connect(lambda: self.monitor_cue_pressed.emit(deck_id))

        for btn in (self._play_btn, self._cue_btn,
                    self._loop_in_btn, self._loop_out_btn,
                    self._loop_btn, self._sync_btn, self._master_btn,
                    self._pfl_btn, self._mkrs_btn):
            transport_row.addWidget(btn)

        root.addLayout(transport_row)
        root.addStretch()

    # ── public update API ─────────────────────────────────────────────────────

    def set_waveform(self, data) -> None:
        """Call whenever WaveformData changes (load + beat analysis + stems)."""
        if self._timeline_qml.available:
            self._timeline_qml.set_waveform(data)
        else:
            self._pos_bar.set_waveform(data)
            self._zoom_wf.set_waveform(data)
        self._stems_widget.set_horizon(getattr(data, "stem_horizon", None) if data else None)

    def configure_stem_horizon(self, **kwargs) -> None:
        self._stems_widget.configure_horizon(**kwargs)

    def set_markers(self, markers) -> None:
        """Push auto-detected markers to both waveform views."""
        if self._timeline_qml.available:
            self._timeline_qml.set_markers(markers)
        else:
            self._pos_bar.set_markers(markers)
            self._zoom_wf.set_markers(markers)
        self._refresh_marker_cache(markers)
        self._stems_widget.set_horizon_markers(markers)
        self._next_marker_key = {}

    def _set_marker_display_mode(self, mode: MarkerDisplayMode | str) -> None:
        self._marker_mode = coerce_marker_display_mode(mode)
        self._markers_visible = self._marker_mode != MarkerDisplayMode.OFF
        if self._timeline_qml.available:
            self._timeline_qml.set_marker_display_mode(self._marker_mode)
        else:
            self._pos_bar.set_marker_display_mode(self._marker_mode)
            self._zoom_wf.set_marker_display_mode(self._marker_mode)
        self._mkrs_btn.blockSignals(True)
        self._mkrs_btn.setChecked(self._markers_visible)
        self._mkrs_btn.blockSignals(False)
        self._mkrs_btn.setToolTip(
            f"Auto markers: {self._marker_mode.value.upper()}\nRight-click for modes/actions"
        )
        if self._state and hasattr(self._state, "auto_markers"):
            markers = self._state.auto_markers if self._markers_visible else ()
            if self._timeline_qml.available:
                self._timeline_qml.set_markers(markers)
            else:
                self._pos_bar.set_markers(markers)
                self._zoom_wf.set_markers(markers)
        if self._state and hasattr(self._state, "auto_markers"):
            self._stems_widget.set_horizon_markers(self._state.auto_markers)

    def set_other_deck_overlay(
        self,
        pos_s:        float,
        beats:        tuple,
        bpm:          float,
        first_beat_s: float,
        source_playing: bool = True,
    ) -> None:
        target = self._timeline_qml if self._timeline_qml.available else self._zoom_wf
        target.set_other_deck(pos_s, beats, bpm, first_beat_s, source_playing)

    def update_spectrum(self, bands: tuple[float, ...]) -> None:
        """Push raw dBFS spectrum bands at high rate (independent of LUFS/metrics)."""
        self._spectrum.set_spectrum(bands)

    def update_live_levels(
        self,
        deck_level: float | None = None,
        stem_levels: dict[str, float] | None = None,
    ) -> None:
        """Push fast peak-derived meter levels without rebuilding full deck UI."""
        if deck_level is not None:
            self._lufs_bar.set_levels(deck_level, deck_level)
        if stem_levels:
            self._stems_widget.update_levels(stem_levels)

    def update_stem_gains(self, gains: list[float]) -> None:
        """Called every UI tick with current fader values (vocals/drums/bass/other)."""
        if self._timeline_qml.available:
            self._timeline_qml.set_stem_gains(gains)
        else:
            self._pos_bar.set_stem_gains(gains)

    def update_phrase_meter(
        self,
        progress: float,
        beats_done: int,
        beats_total: int,
        status: str,
    ) -> None:
        self._phrase_meter.set_phrase(progress, beats_done, beats_total, status)

    def update_realtime_state(self, state: DeckState, position_s: float) -> None:
        """Cheap 60 Hz update: only controls that must track audio position smoothly."""
        if state.track:
            dur = state.track.duration_s
        else:
            dur = 0.0

        cue_positions = [c.position_s for c in state.cue_points]
        bg = state.beatgrid
        if self._timeline_qml.available:
            self._timeline_qml.update_position(
                pos_s         = position_s,
                duration_s    = dur,
                beats         = bg.beats if bg else (),
                bpm           = state.bpm_live or (state.track.bpm if state.track else 0.0) or (bg.bpm if bg else 120.0),
                first_beat_s  = bg.first_beat_s if bg else 0.0,
                cue_positions = cue_positions,
                loop          = state.loop,
                playing       = state.status == DeckStatus.PLAYING,
                sync_enabled  = state.sync_enabled,
                phase_err     = state.sync_phase_error,
            )
        else:
            self._pos_bar.update_position(position_s, dur, cue_positions, state.loop)
            self._zoom_wf.update_position(
                pos_s         = position_s,
                duration_s    = dur,
                beats         = bg.beats if bg else (),
                bpm           = state.bpm_live or (state.track.bpm if state.track else 0.0) or (bg.bpm if bg else 120.0),
                first_beat_s  = bg.first_beat_s if bg else 0.0,
                cue_positions = cue_positions,
                loop          = state.loop,
                sync_enabled  = state.sync_enabled,
                phase_err     = state.sync_phase_error,
                playing       = state.status == DeckStatus.PLAYING,
            )
            self._zoom_wf.animate_frame()

        playing = state.status == DeckStatus.PLAYING
        self._play_btn.blockSignals(True)
        self._play_btn.setChecked(playing)
        self._play_btn.setText("⏸" if playing else "▶")
        self._play_btn.blockSignals(False)
        self._update_next_marker_label(state, position_s, force=False)
        self._stems_widget.set_horizon_position(position_s)

    def set_channel_volume(self, vol: float) -> None:
        """Push hardware channel-fader value (0–1) into slider without re-emitting."""
        self._ch_fader.blockSignals(True)
        self._ch_fader.setValue(int(round(vol * 100)))
        self._ch_fader.blockSignals(False)

    def set_pregain(self, gain: float) -> None:
        """Push hardware pregain value (0–2) into slider without re-emitting."""
        self._pregain_slider.blockSignals(True)
        self._pregain_slider.setValue(int(round(gain * 100)))
        self._pregain_slider.blockSignals(False)

    def set_smart_cfx_enabled(self, enabled: bool) -> None:
        self._wrekk_badge.setVisible(enabled)

    def set_monitor_cue_active(self, active: bool) -> None:
        """Reflect hardware/transport PFL state on the PFL button."""
        self._pfl_btn.blockSignals(True)
        self._pfl_btn.setChecked(active)
        self._pfl_btn.blockSignals(False)

    # ── marker user-action handlers ───────────────────────────────────────────

    def _on_markers_toggled(self, checked: bool) -> None:
        if checked:
            if self._marker_mode == MarkerDisplayMode.OFF:
                self._set_marker_display_mode(MarkerDisplayMode.ESSENTIAL)
            else:
                self._set_marker_display_mode(self._marker_mode)
        else:
            self._set_marker_display_mode(MarkerDisplayMode.OFF)

    def _on_mkrs_btn_context(self, _pos) -> None:
        """Right-click on MKRS button — show marker action menu."""
        self._show_marker_menu(None)

    def _on_marker_right_clicked(self, marker) -> None:
        """Right-click on a marker in waveform — context menu for that marker."""
        self._show_marker_menu(marker)

    def _show_marker_menu(self, marker) -> None:
        menu = QMenu(self)

        if marker is not None:
            mtype = getattr(marker, "type", None)
            mval  = mtype.value.upper().replace("_", " ") if mtype else "MARKER"
            pos   = getattr(marker, "position_s", 0.0)
            mins, secs_f = divmod(pos, 60)
            menu.addSection(f"{mval}  {int(mins)}:{secs_f:05.2f}")

            act_to_cue = QAction("Convert to Hot Cue", menu)
            act_to_cue.triggered.connect(
                lambda: self.convert_marker_to_cue_sig.emit(self._deck_id, marker)
            )
            menu.addAction(act_to_cue)

            act_seek = QAction("Seek to Marker", menu)
            act_seek.triggered.connect(
                lambda: self.seek.emit(self._deck_id, marker.position_s)
            )
            menu.addAction(act_seek)

            act_del = QAction("Delete This Marker", menu)
            act_del.triggered.connect(
                lambda: self.remove_auto_marker_sig.emit(self._deck_id, marker.id)
            )
            menu.addAction(act_del)
            menu.addSeparator()

        mode_labels = {
            MarkerDisplayMode.OFF: "OFF",
            MarkerDisplayMode.PRIMARY: "PRIMARY",
            MarkerDisplayMode.ESSENTIAL: "ESSENTIAL",
            MarkerDisplayMode.PRIMARY_WREKK: "PRIMARY + WREKK",
            MarkerDisplayMode.FULL: "FULL",
            MarkerDisplayMode.DEBUG: "DEBUG",
        }
        mode_menu = menu.addMenu("Marker Display")
        for mode in (
            MarkerDisplayMode.OFF,
            MarkerDisplayMode.PRIMARY,
            MarkerDisplayMode.ESSENTIAL,
            MarkerDisplayMode.PRIMARY_WREKK,
            MarkerDisplayMode.FULL,
            MarkerDisplayMode.DEBUG,
        ):
            act_mode = QAction(mode_labels[mode], mode_menu)
            act_mode.setCheckable(True)
            act_mode.setChecked(self._marker_mode == mode)
            act_mode.triggered.connect(lambda _checked=False, m=mode: self._set_marker_display_mode(m))
            mode_menu.addAction(act_mode)
        menu.addSeparator()

        act_regen = QAction("Regenerate Markers", menu)
        act_regen.triggered.connect(
            lambda: self.regenerate_markers_sig.emit(self._deck_id)
        )
        menu.addAction(act_regen)

        act_clear = QAction("Clear All Auto Markers", menu)
        act_clear.triggered.connect(
            lambda: self.clear_auto_markers_sig.emit(self._deck_id)
        )
        menu.addAction(act_clear)

        # "Convert Best MIX_IN / MIX_OUT to Hot Cues"
        if self._state and self._state.auto_markers:
            menu.addSeparator()
            act_best = QAction("Convert Best MIX-IN & MIX-OUT to Hot Cues", menu)
            act_best.triggered.connect(self._convert_best_mix_points)
            menu.addAction(act_best)
            act_all = QAction("Accept All High-Confidence Markers as Hot Cues", menu)
            act_all.triggered.connect(self._accept_all_high_confidence)
            menu.addAction(act_all)

        if self._state and self._state.track and str(self._state.track.path).lower().endswith(".wrk"):
            menu.addSeparator()
            act_lab = QAction("Edit Analysis in WREKKER LAB", menu)
            act_lab.triggered.connect(lambda: self.edit_analysis_sig.emit(str(self._state.track.path)))
            menu.addAction(act_lab)

        menu.exec(QCursor.pos())

    def _convert_best_mix_points(self) -> None:
        if not (self._state and self._state.auto_markers):
            return
        for mtype_val in ("mix_in", "mix_out"):
            candidates = [
                m for m in self._state.auto_markers
                if getattr(getattr(m, "type", None), "value", "") == mtype_val
            ]
            if candidates:
                best = max(candidates, key=lambda m: m.confidence)
                self.convert_marker_to_cue_sig.emit(self._deck_id, best)

    def _accept_all_high_confidence(self) -> None:
        if not (self._state and self._state.auto_markers):
            return
        for m in self._state.auto_markers:
            if getattr(m, "confidence", 0.0) >= 0.70:
                self.convert_marker_to_cue_sig.emit(self._deck_id, m)

    def update_state(
        self,
        state:        DeckState,
        position_s:   float,
        metrics:      Optional[DeckMetrics],
        stem_lufs:    dict[str, float],
    ) -> None:
        self._state   = state
        self._metrics = metrics

        # Track info + artwork. Artwork decode/scale is expensive; only do it
        # when the loaded track/artwork object actually changes.
        if state.track:
            title = state.track.title or state.track.path.stem
            artist = state.track.artist or "Unknown Artist"
            dur = state.track.duration_s
            art = state.track.artwork_data
            track_key = (str(state.track.path), title, artist, id(art))
            if track_key != self._track_info_key:
                self._track_info_key = track_key
                self._title_lbl.setText(title)
                self._artist_lbl.setText(artist)
                if art is not None:
                    try:
                        px = QPixmap()
                        px.loadFromData(art)
                        if not px.isNull():
                            self._artwork_lbl.setPixmap(
                                px.scaled(48, 48,
                                          Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                          Qt.TransformationMode.SmoothTransformation)
                            )
                            self._artwork_lbl.setText("")
                    except Exception:
                        self._artwork_lbl.setText("♪")
                else:
                    self._artwork_lbl.setText("♪")
                    self._artwork_lbl.setPixmap(QPixmap())
        else:
            if self._track_info_key is not None:
                self._track_info_key = None
                self._title_lbl.setText("No track loaded")
                self._artist_lbl.setText("—")
                self._artwork_lbl.setText("♪")
                self._artwork_lbl.setPixmap(QPixmap())
            dur = 0.0

        # Position bar (with loop overlay)
        cue_positions = [c.position_s for c in state.cue_points]
        bg = state.beatgrid
        if self._timeline_qml.available:
            self._timeline_qml.update_position(
                pos_s         = position_s,
                duration_s    = dur,
                beats         = bg.beats if bg else (),
                bpm           = state.bpm_live or (state.track.bpm if state.track else 0.0) or (bg.bpm if bg else 120.0),
                first_beat_s  = bg.first_beat_s if bg else 0.0,
                cue_positions = cue_positions,
                loop          = state.loop,
                playing       = state.status == DeckStatus.PLAYING,
                sync_enabled  = state.sync_enabled,
                phase_err     = state.sync_phase_error,
            )
        else:
            self._pos_bar.update_position(position_s, dur, cue_positions, state.loop)
            self._zoom_wf.update_position(
                pos_s         = position_s,
                duration_s    = dur,
                beats         = bg.beats if bg else (),
                bpm           = state.bpm_live or (state.track.bpm if state.track else 0.0) or (bg.bpm if bg else 120.0),
                first_beat_s  = bg.first_beat_s if bg else 0.0,
                cue_positions = cue_positions,
                loop          = state.loop,
                sync_enabled  = state.sync_enabled,
                phase_err     = state.sync_phase_error,
                playing       = state.status == DeckStatus.PLAYING,
            )

        # BPM / Key / Pitch
        bpm = state.bpm_live or (state.track.bpm if state.track else None)
        self._bpm_lbl.set_value(f"{bpm:.2f}" if bpm else "—")
        if state.track and state.track.key:
            k = state.track.key
            self._key_lbl.set_value(f"{k.key_name} {k}")
        else:
            self._key_lbl.set_value("—")
        pitch = state.pitch_pct
        self._pitch_lbl.set_value(f"{pitch:+.1f}st" if pitch else "0.0st")

        # LUFS — only update when we have fresh metrics; hold last value otherwise
        if metrics:
            m  = metrics.lufs.momentary_lufs
            st = metrics.lufs.short_term_lufs
            if math.isinf(m) and math.isinf(st):
                self._lufs_lbl.set_value("—")
            else:
                m_txt  = f"{m:.1f}"  if not math.isinf(m)  else "—"
                st_txt = f"{st:.1f}" if not math.isinf(st) else "—"
                self._lufs_lbl.set_value(f"{m_txt}M {st_txt}S")
            self._lufs_bar.set_levels(m, st)

        # Spectral
        if metrics and metrics.spectrum:
            self._spectrum.set_spectrum(metrics.spectrum)

        # Stems — pass stem_lufs only when populated; avoids meter flicker on fast ticks
        self._stems_widget.update_stems(state.stems, stem_lufs if stem_lufs else None)

        # Stems status badge
        ss = state.stems_status
        if ss in ("queued", "analyzing"):
            pct = ""
            self._stems_status.setText(f"⟳ Analyzing stems{'...' if ss == 'analyzing' else ' (queued)'}")
        elif ss == "ready":
            self._stems_status.setText("")
        else:
            self._stems_status.setText("")

        # Sync master indicator
        self._master_btn.blockSignals(True)
        self._master_btn.setChecked(state.sync_master)
        self._master_btn.blockSignals(False)

        # SYNC button: reflect enabled state and phase-lock quality
        self._sync_btn.blockSignals(True)
        self._sync_btn.setChecked(state.sync_enabled)
        if state.sync_enabled:
            err = abs(state.sync_phase_error or 0.0)
            if err < 0.05:
                col = "#2ecc71"   # green — phase locked
            elif err < 0.20:
                col = "#f39c12"   # orange — correcting
            else:
                col = "#e74c3c"   # red — large drift
            self._sync_btn.setStyleSheet(
                f"QPushButton {{ background: {col}22; border: 1px solid {col};"
                f"  color: {col}; font-size: 9px; font-weight: 700; }}"
            )
        else:
            self._sync_btn.setStyleSheet(self._sync_btn_default_ss)
        self._sync_btn.blockSignals(False)

        # Auto markers — push to waveform views, respecting show/hide toggle
        if hasattr(state, "auto_markers"):
            shown = state.auto_markers if self._marker_mode != MarkerDisplayMode.OFF else ()
            if self._timeline_qml.available:
                self._timeline_qml.set_marker_display_mode(self._marker_mode)
                self._timeline_qml.set_markers(shown)
            else:
                self._pos_bar.set_marker_display_mode(self._marker_mode)
                self._zoom_wf.set_marker_display_mode(self._marker_mode)
                self._pos_bar.set_markers(shown)
                self._zoom_wf.set_markers(shown)
            self._update_next_marker_label(state, position_s, force=True)

        # Play button state
        playing = state.status == DeckStatus.PLAYING
        self._play_btn.blockSignals(True)
        self._play_btn.setChecked(playing)
        self._play_btn.setText("⏸" if playing else "▶")
        self._play_btn.blockSignals(False)

    # ── next auto-marker display ─────────────────────────────────────────────

    @staticmethod
    def _fmt_countdown(seconds: float) -> str:
        seconds = max(0.0, seconds)
        if seconds < 10.0:
            return f"{seconds:.1f}s"
        m, s = divmod(int(round(seconds)), 60)
        return f"{m}:{s:02d}"

    @staticmethod
    def _fmt_marker_pos(seconds: float) -> str:
        m, s = divmod(max(0.0, seconds), 60.0)
        return f"{int(m)}:{s:04.1f}"

    @staticmethod
    def _hud_marker_style(tier: str, color: str | None = None, uncertain: bool = False) -> str:
        if color is None:
            color = theme.TEXT_DIM
        if uncertain:
            color = theme.TEXT_MED
        weight = 850 if tier == "primary" else 780 if tier == "wrekk" else 700
        size = 10 if tier == "primary" else 9
        return f"color:{color}; font-size:{size}px; font-weight:{weight};"

    def _refresh_marker_cache(self, markers) -> None:
        source = tuple(markers or ())
        if source == self._marker_cache_source:
            return
        self._marker_cache_source = source
        grouped = {"primary": [], "wrekk": [], "guide": []}
        for m in source:
            conf = float(getattr(m, "confidence", 0.0) or 0.0)
            if conf < _NEXT_MARKER_MIN_CONF:
                continue
            value = marker_value(m)
            tier = marker_tier(value)
            if tier not in grouped:
                continue
            if value not in {"phrase"} and tier == "guide":
                continue
            grouped[tier].append(m)
        self._markers_by_tier = {
            tier: tuple(sorted(items, key=lambda m: float(getattr(m, "position_s", 0.0) or 0.0)))
            for tier, items in grouped.items()
        }

    def _next_marker_for_tier(self, state: DeckState, position_s: float, tier: str):
        self._refresh_marker_cache(getattr(state, "auto_markers", ()) or ())
        markers = self._markers_by_tier.get(tier, ())
        if not markers:
            return None, None

        def choose(candidates: list[tuple[object, float]]):
            if not candidates:
                return None, None
            return min(candidates, key=lambda item: item[1])

        loop = state.loop
        if loop and loop.active and loop.start_s < loop.end_s:
            inside = [
                m for m in markers
                if loop.start_s <= getattr(m, "position_s", 0.0) <= loop.end_s
            ]
            if not inside:
                return None, None
            candidates: list[tuple[object, float]] = []
            for m in inside:
                mpos = getattr(m, "position_s", 0.0)
                if mpos > position_s + 0.05:
                    remaining = mpos - position_s
                else:
                    remaining = max(0.0, loop.end_s - position_s) + max(0.0, mpos - loop.start_s)
                candidates.append((m, remaining))
            return choose(candidates)

        candidates = [
            (m, getattr(m, "position_s", 0.0) - position_s)
            for m in markers
            if getattr(m, "position_s", 0.0) > position_s + 0.05
        ]
        return choose(candidates)

    def _update_next_marker_label(
        self,
        state: DeckState,
        position_s: float,
        force: bool = False,
    ) -> None:
        now = time.perf_counter()
        if not force and now - self._next_marker_last_t < 0.10:
            return
        self._next_marker_last_t = now

        for tier, title in _HUD_MARKER_CATEGORIES:
            marker, remaining = self._next_marker_for_tier(state, position_s, tier)
            lbl = self._next_marker_lbls[tier]
            led = self._next_marker_leds[tier]
            if marker is None:
                key = ("empty",)
                if self._next_marker_key.get(tier) != key:
                    self._next_marker_key[tier] = key
                    lbl.setText(f"{title}  —")
                    lbl.setToolTip("")
                    lbl.setStyleSheet(self._hud_marker_style(tier))
                    led.setToolTip("")
                    self._set_marker_confidence_led(led, None)
                continue

            mval = marker_value(marker)
            label = marker_label(mval, compact=False)
            conf = float(getattr(marker, "confidence", 0.0))
            uncertain = conf < 0.50
            shown_label = f"{label}?" if uncertain else label
            countdown = self._fmt_countdown(float(remaining or 0.0))
            key = (getattr(marker, "id", id(marker)), shown_label, countdown, uncertain)
            if self._next_marker_key.get(tier) != key:
                self._next_marker_key[tier] = key
                color = marker_color(mval, conf)
                lbl.setText(f"{title}  {shown_label:<8}  {countdown}")
                lbl.setStyleSheet(self._hud_marker_style(tier, color, uncertain))
                lbl.setToolTip(marker_tooltip(marker))
                led.setToolTip(marker_tooltip(marker))
                self._set_marker_confidence_led(led, conf)

    def _set_marker_confidence_led(self, led: QLabel, confidence: float | None) -> None:
        if confidence is None:
            col = theme.BORDER_LIT
        elif confidence >= 0.85:
            col = "#2ecc71"
        elif confidence >= MARKER_MIN_CONFIDENCE:
            col = "#f1c40f"
        else:
            col = "#e74c3c"
        led.setStyleSheet(
            f"background:{col}; border-radius:4px; border:1px solid {col};"
        )
