"""
MainWindow — top-level Wrekker window.

Update loop (16ms / 60Hz):
  - Fast path (every tick): state, position, waveform, cross-deck overlay, realtime deck UI
  - Visual meters path (≈45Hz): spectrum, oscilloscopes, peak meters, mini meters
  - Slow path (every 6th tick ≈10Hz): LUFS/metrics and full deck labels
"""
from __future__ import annotations
import time
import math
import os
import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore    import Qt, QSocketNotifier, QThread, QTimer, pyqtSignal
from PyQt6.QtGui     import QFont, QColor, QAction, QKeySequence
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSplitter, QFrame, QMenuBar, QStackedWidget, QPushButton,
    QToolButton,
)

from wrekker.config.paths import data_dir
from wrekker.core.engine_v2 import AudioEngine
from wrekker.core.transport import Transport
from wrekker.library.database import LibraryDB
from wrekker.sync import PhraseLockSync
from wrekker.ui.branding import logo_label
from wrekker.ui import theme
from wrekker.ui.widgets.deck    import DeckWidget
from wrekker.ui.widgets.master  import MasterWidget
from wrekker.ui.widgets.library import LibraryWidget
from wrekker.ui.widgets.wrekked import WrekkedWidget
from wrekker.library.wrekked_scanner import WrekkedScanner


class _UiTickThread(QThread):
    """Steady visual clock for the main UI.

    QTimer in the main thread can be quantized by the Qt/platform dispatcher to
    ~25-30 ms on some Linux setups. This thread emits at a target cadence and
    coalesces ticks so the main thread never builds a backlog.
    """

    tick = pyqtSignal()

    def __init__(self, fps: float = 60.0, parent=None) -> None:
        super().__init__(parent)
        self._period = 1.0 / max(30.0, min(240.0, float(fps)))
        self._running = threading.Event()
        self._running.set()
        self._lock = threading.Lock()
        self._pending = False

    def run(self) -> None:
        next_t = time.perf_counter()
        while self._running.is_set():
            now = time.perf_counter()
            if now >= next_t:
                with self._lock:
                    if not self._pending:
                        self._pending = True
                        self.tick.emit()
                next_t += self._period
                if now - next_t > self._period * 2.0:
                    next_t = now + self._period
            sleep_s = max(0.001, min(0.005, next_t - time.perf_counter()))
            time.sleep(sleep_s)

    def acknowledge(self) -> None:
        with self._lock:
            self._pending = False

    def stop(self) -> None:
        self._running.clear()
        self.wait(1000)


class _UiPipeClock:
    """File-descriptor driven visual clock.

    On Linux, waking the Qt dispatcher through a readable fd is often steadier
    than QTimer or queued cross-thread signals when the platform dispatcher is
    quantizing visual timers.
    """

    def __init__(self, callback, fps: float, parent) -> None:
        self._callback = callback
        self._period = 1.0 / max(30.0, min(240.0, float(fps)))
        self._running = threading.Event()
        self._running.set()
        self._rfd, self._wfd = os.pipe()
        os.set_blocking(self._rfd, False)
        os.set_blocking(self._wfd, False)
        self._notifier = QSocketNotifier(self._rfd, QSocketNotifier.Type.Read, parent)
        self._notifier.activated.connect(self._activated)
        self._thread = threading.Thread(target=self._run, name="wrekker-ui-pipe-clock", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        next_t = time.perf_counter()
        while self._running.is_set():
            now = time.perf_counter()
            if now >= next_t:
                try:
                    os.write(self._wfd, b"x")
                except BlockingIOError:
                    pass
                except OSError:
                    return
                next_t += self._period
                if now - next_t > self._period * 2.0:
                    next_t = now + self._period
            sleep_s = max(0.001, min(0.004, next_t - time.perf_counter()))
            time.sleep(sleep_s)

    def _activated(self, _fd) -> None:
        try:
            while os.read(self._rfd, 4096):
                pass
        except BlockingIOError:
            pass
        except OSError:
            return
        self._callback()

    def stop(self) -> None:
        self._running.clear()
        self._notifier.setEnabled(False)
        self._thread.join(timeout=1.0)
        for fd in (self._rfd, self._wfd):
            try:
                os.close(fd)
            except OSError:
                pass


class MainWindow(QMainWindow):
    def __init__(
        self,
        transport:   Transport,
        engine:      AudioEngine,
        db:          LibraryDB,
        flx4=None,
        debug:       bool = False,
        prepared_db=None,    # PreparedDB | None
        stem_model=None,     # HTDemucsModel | None (for prepare worker)
        settings_store=None, # SettingsStore | None
        degraded_mode: bool = False,
    ) -> None:
        super().__init__()
        self._transport    = transport
        self._engine       = engine
        self._db           = db
        self._flx4         = flx4
        self._debug        = debug
        self._prepared_db  = prepared_db
        self._stem_model   = stem_model
        self._settings_store = settings_store
        self._degraded_mode = degraded_mode
        self._settings_window = None
        self._dbg_tick_times: list[float] = []
        self._dbg_tick_count  = 0
        self._wf_seq_a: int = -1
        self._wf_seq_b: int = -1
        self._last_fx_state = None
        self._last_smart_cfx_ui = None
        self._last_mixer_ui_state = None
        self._last_monitor_ui_state = None
        self._tick_log_enabled = os.environ.get("WREKKER_UI_TICK_LOG", "0") == "1"
        self._tick_profile_enabled = os.environ.get("WREKKER_UI_TICK_PROFILE", "0") == "1"
        self._disable_cross_overlay = os.environ.get("WREKKER_DISABLE_CROSS_OVERLAY", "0") == "1"
        self._disable_deck_realtime = os.environ.get("WREKKER_DISABLE_DECK_REALTIME", "0") == "1"
        self._tick_log_count = 0
        self._tick_log_last = time.perf_counter()
        self._tick_log_late = 0
        self._tick_profile: dict[str, list[float]] = {}
        self._timer: QTimer | None = None
        self._tick_thread: _UiTickThread | None = None
        self._pipe_clock: _UiPipeClock | None = None
        self._heavy_tick_n:  int = 0   # counter: heavy ops run every 3rd tick
        self._last_ref_key = None      # HarmonicKey | None — for library compat dots
        self._phrase_sync = PhraseLockSync()
        self._lab_windows: dict[str, QMainWindow] = {}

        self.setWindowTitle("Wrekker")
        self.setMinimumSize(1100, 700)
        self.resize(1400, 840)
        self.setStyleSheet(theme.GLOBAL_SS)

        self._build_ui()
        self._build_menu()
        self._connect_signals()
        self._log_ui_platform()
        self._start_update_loop()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root_widget = QWidget()
        root_layout = QVBoxLayout(root_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root_widget)

        # ── top bar ───────────────────────────────────────────────────────────
        top_bar = self._build_top_bar()
        root_layout.addWidget(top_bar)

        top_line = QFrame()
        top_line.setFrameShape(QFrame.Shape.HLine)
        top_line.setStyleSheet(f"background: {theme.BORDER}; max-height: 1px;")
        root_layout.addWidget(top_line)

        if self._degraded_mode:
            banner = QLabel(
                "AI setup incomplete: stem separation and Beat This! preparation are disabled. "
                "Prepared .wrk playback remains available."
            )
            banner.setStyleSheet(
                f"background: rgba(255, 149, 0, 38); color: {theme.TEXT_BRIGHT}; "
                f"border-bottom: 1px solid {theme.STATUS_WARN}; padding: 6px 16px; "
                "font-weight: 700;"
            )
            root_layout.addWidget(banner)

        # ── main splitter (decks top, library bottom) ─────────────────────────
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(3)
        root_layout.addWidget(splitter, stretch=1)

        # Decks row
        decks_widget = self._build_decks_row()
        splitter.addWidget(decks_widget)

        # WREKKED browser panel. General Library now lives inside WREKKED.
        browser_container = QWidget()
        browser_layout = QVBoxLayout(browser_container)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        browser_layout.setSpacing(0)

        self._browser_stack = QStackedWidget()
        self._library = None

        if self._prepared_db is not None:
            prepared_root = self._prepared_path("wrekked_library_path")
            scanner = WrekkedScanner(self._prepared_db, prepared_root=prepared_root)
            self._wrekked = WrekkedWidget(self._prepared_db, scanner, library_db=self._db)
            self._wrekked.rescan_done.connect(self._wrekked.on_rescan_done)
            self._browser_stack.addWidget(self._wrekked)
        else:
            self._wrekked = None
            self._library = LibraryWidget(self._db, prepared_db=self._prepared_db)
            self._browser_stack.addWidget(self._library)

        browser_layout.addWidget(self._browser_stack, 1)
        splitter.addWidget(browser_container)

        self._browser_stack.setCurrentIndex(0)

        # Favor the browser at startup: deck internals are compact enough now
        # to leave the DJ more rows for choosing the next track.
        splitter.setSizes([480, 360])

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(38)
        bar.setStyleSheet(f"background: {theme.BG_PANEL};")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)

        # Brand
        layout.addWidget(logo_label(24))
        logo = QLabel("WREKKER")
        logo.setStyleSheet(
            f"color: {theme.WHITE}; font-size: 15px; font-weight: 800; letter-spacing: 3px;"
        )
        layout.addWidget(logo)
        layout.addStretch()

        # Clock
        self._clock = QLabel("00:00:00")
        self._clock.setStyleSheet(
            f"color: {theme.TEXT_MED}; font-size: 12px; font-family: monospace;"
        )
        layout.addWidget(self._clock)

        settings_btn = QToolButton()
        settings_btn.setText("SET")
        settings_btn.setToolTip("Settings")
        settings_btn.setFixedSize(30, 26)
        settings_btn.clicked.connect(self._open_settings)
        layout.addWidget(settings_btn)

        return bar

    def _build_menu(self) -> None:
        app_menu = self.menuBar().addMenu("Wrekker")
        settings_action = QAction("Settings", self)
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.triggered.connect(self._open_settings)
        app_menu.addAction(settings_action)

    def _open_settings(self) -> None:
        if self._settings_store is None:
            from wrekker.settings import SettingsStore
            self._settings_store = SettingsStore.load_or_create()
        if self._settings_window is not None:
            self._settings_window.raise_()
            self._settings_window.activateWindow()
            return
        from wrekker.ui.widgets.settings_window import SettingsWindow
        self._settings_window = SettingsWindow(
            self._settings_store,
            engine=self._engine,
            transport=self._transport,
            db=self._db,
            prepared_db=self._prepared_db,
            flx4=self._flx4,
            parent=self,
        )
        self._settings_window.finished.connect(lambda _=0: setattr(self, "_settings_window", None))
        self._settings_window.settings_saved.connect(self._on_settings_saved)
        self._settings_window.show()

    def _on_settings_saved(self) -> None:
        if self._prepared_db is not None and self._settings_store is not None:
            self._settings_store.sync_prepared_db_settings(self._prepared_db)
        self._apply_stem_horizon_settings()
        if self._wrekked is not None:
            self._wrekked.refresh()

    def _build_decks_row(self) -> QWidget:
        w  = QWidget()
        h  = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # Deck A
        self._deck_a = DeckWidget("A")
        h.addWidget(self._deck_a)

        div_a = QFrame()
        div_a.setFrameShape(QFrame.Shape.VLine)
        div_a.setStyleSheet(f"background: {theme.BORDER}; max-width: 1px;")
        h.addWidget(div_a)

        # Master (center)
        self._master = MasterWidget()
        self._master.set_flx4_connected(self._flx4 is not None)
        h.addWidget(self._master)

        div_b = QFrame()
        div_b.setFrameShape(QFrame.Shape.VLine)
        div_b.setStyleSheet(f"background: {theme.BORDER}; max-width: 1px;")
        h.addWidget(div_b)

        # Deck B
        self._deck_b = DeckWidget("B")
        h.addWidget(self._deck_b)
        self._apply_stem_horizon_settings()

        return w

    def _apply_stem_horizon_settings(self) -> None:
        if not hasattr(self, "_deck_a") or self._settings_store is None:
            return
        enabled = bool(self._settings_store.get("stems.horizon_enabled", True, include_env=False))
        mode = self._settings_store.get("stems.horizon_display_mode", "LED Blocks", include_env=False)
        if not enabled:
            mode = "Off"
        kwargs = {
            "mode": mode,
            "range_bars": int(self._settings_store.get("stems.horizon_range_bars", 8, include_env=False) or 8),
            "show_countdown": bool(self._settings_store.get("stems.horizon_show_countdown", True, include_env=False)),
            "show_w_flag": bool(self._settings_store.get("stems.horizon_show_w_flag", True, include_env=False)),
        }
        self._deck_a.configure_stem_horizon(**kwargs)
        self._deck_b.configure_stem_horizon(**kwargs)

    # ── signal wiring ─────────────────────────────────────────────────────────

    def _connect_signals(self) -> None:
        # Deck A controls
        self._deck_a.play_pause.connect(self._on_play_pause)
        self._deck_a.cue_pressed.connect(self._transport.cue)
        self._deck_a.cue_released.connect(self._transport.cue_release)
        self._deck_a.loop_in.connect(self._transport.loop_in)
        self._deck_a.loop_out.connect(self._transport.loop_out)
        self._deck_a.loop_toggle.connect(self._transport.loop_toggle)
        self._deck_a.sync_pressed.connect(self._transport.sync)
        self._deck_a.sync_master_pressed.connect(self._transport.set_sync_master)
        self._deck_a.seek.connect(self._transport.seek)
        self._deck_a.stem_gain.connect(self._transport.set_stem_gain)
        self._deck_a.stem_mute.connect(self._transport.mute_stem)
        self._deck_a.stem_solo.connect(self._transport.solo_stem)
        self._deck_a.channel_volume.connect(self._transport.set_channel_volume)
        self._deck_a.pregain_changed.connect(self._transport.set_pregain)

        # Deck B controls
        self._deck_b.play_pause.connect(self._on_play_pause)
        self._deck_b.cue_pressed.connect(self._transport.cue)
        self._deck_b.cue_released.connect(self._transport.cue_release)
        self._deck_b.loop_in.connect(self._transport.loop_in)
        self._deck_b.loop_out.connect(self._transport.loop_out)
        self._deck_b.loop_toggle.connect(self._transport.loop_toggle)
        self._deck_b.sync_pressed.connect(self._transport.sync)
        self._deck_b.sync_master_pressed.connect(self._transport.set_sync_master)
        self._deck_b.seek.connect(self._transport.seek)
        self._deck_b.stem_gain.connect(self._transport.set_stem_gain)
        self._deck_b.stem_mute.connect(self._transport.mute_stem)
        self._deck_b.stem_solo.connect(self._transport.solo_stem)
        self._deck_b.channel_volume.connect(self._transport.set_channel_volume)
        self._deck_b.pregain_changed.connect(self._transport.set_pregain)

        # Master
        self._master.crossfader_changed.connect(self._transport.set_crossfader)
        self._master.master_vol_changed.connect(self._transport.set_master_gain)
        self._master.eq_changed.connect(self._transport.set_eq)
        self._master.smart_cfx_changed.connect(self._transport.set_smart_cfx_enabled)
        self._master.smart_cfx_changed.connect(self._deck_a.set_smart_cfx_enabled)
        self._master.smart_cfx_changed.connect(self._deck_b.set_smart_cfx_enabled)

        # Monitor CUE / PFL
        self._deck_a.monitor_cue_pressed.connect(
            lambda: self._transport.toggle_monitor_cue("A")
        )
        self._deck_b.monitor_cue_pressed.connect(
            lambda: self._transport.toggle_monitor_cue("B")
        )

        # Auto-marker user actions
        self._deck_a.clear_auto_markers_sig.connect(self._transport.clear_auto_markers)
        self._deck_b.clear_auto_markers_sig.connect(self._transport.clear_auto_markers)
        self._deck_a.remove_auto_marker_sig.connect(self._transport.remove_auto_marker)
        self._deck_b.remove_auto_marker_sig.connect(self._transport.remove_auto_marker)
        self._deck_a.regenerate_markers_sig.connect(self._transport.regenerate_markers_bg)
        self._deck_b.regenerate_markers_sig.connect(self._transport.regenerate_markers_bg)
        self._deck_a.convert_marker_to_cue_sig.connect(self._on_convert_marker_to_cue)
        self._deck_b.convert_marker_to_cue_sig.connect(self._on_convert_marker_to_cue)
        self._deck_a.edit_analysis_sig.connect(self._open_wrekker_lab)
        self._deck_b.edit_analysis_sig.connect(self._open_wrekker_lab)
        self._master.master_cue_pressed.connect(
            lambda: self._transport.toggle_monitor_cue("master")
        )

        # FX
        self._master.fx_enabled_changed.connect(self._transport.set_fx_enabled)
        self._master.fx_type_changed.connect(self._transport.set_fx_type)
        self._master.fx_target_changed.connect(self._transport.set_fx_target)
        self._master.fx_wet_changed.connect(self._transport.set_fx_wet)
        self._master.fx_depth_changed.connect(self._transport.set_fx_depth)
        self._master.fx_feedback_changed.connect(self._transport.set_fx_feedback)
        self._master.fx_time_division_changed.connect(self._transport.set_fx_time_division)
        self._master.fx_color_changed.connect(self._transport.set_fx_color)
        self._master.fx_bank_changed.connect(self._transport.set_fx_bank)
        self._master.wrekk_fx_type_changed.connect(self._transport.set_wrekk_fx_type)
        self._master.wrekk_fx_target_changed.connect(self._transport.set_wrekk_fx_target)
        self._master.wrekk_fx_stem_target_changed.connect(self._transport.set_wrekk_fx_stem_target)
        self._master.wrekk_fx_wet_changed.connect(self._transport.set_wrekk_fx_wet)
        self._master.wrekk_fx_depth_changed.connect(self._transport.set_wrekk_fx_depth)
        self._master.wrekk_fx_feedback_changed.connect(self._transport.set_wrekk_fx_feedback)
        self._master.wrekk_fx_time_division_changed.connect(self._transport.set_wrekk_fx_time_division)
        self._master.wrekk_fx_color_changed.connect(self._transport.set_wrekk_fx_color)

        # Clip reset callbacks
        self._master.connect_clip_resets(
            lambda: self._engine.reset_clip("A"),
            lambda: self._engine.reset_clip("B"),
            self._engine.reset_master_clip,
        )

        # Browser → load/prepare
        if self._library is not None:
            self._library.load_track.connect(self._on_load_track)
            self._library.prepare_tracks.connect(self._on_prepare_tracks)
            self._library.root_added.connect(self._on_root_added)

        if self._wrekked is not None:
            self._wrekked.load_wrk_track.connect(self._on_load_wrk_track)
            self._wrekked.load_source_track.connect(self._on_load_track)
            self._wrekked.prepare_library_tracks.connect(self._on_prepare_tracks)
            self._wrekked.open_lab_requested.connect(self._open_wrekker_lab)

        # Hardware LOAD A/B buttons → active browser panel
        if self._flx4 is not None and hasattr(self._flx4, "set_load_callbacks"):
            self._flx4.set_load_callbacks(
                load_a=lambda: self._load_selected_to_deck("A"),
                load_b=lambda: self._load_selected_to_deck("B"),
            )

        # Hardware browse knob → active browser panel (dispatched at call time)
        if self._flx4 is not None and hasattr(self._flx4, "set_browse_callbacks"):
            self._flx4.set_browse_callbacks(
                scroll=self._hw_browse_scroll,
                click=self._hw_browse_click,
            )

    # ── transport helpers ─────────────────────────────────────────────────────

    def _switch_browser(self, index: int) -> None:
        self._browser_stack.setCurrentIndex(index)


    def _on_load_wrk_track(self, deck_id: str, wrk_path: str) -> None:
        self._transport.load_wrk_track(deck_id, wrk_path)

    def _open_wrekker_lab(self, wrk_path: str) -> None:
        from wrekker.ui.widgets.lab import WrekkerLabWindow
        key = str(Path(wrk_path).absolute())
        win = self._lab_windows.get(key)
        if win is not None:
            win.raise_()
            win.activateWindow()
            return
        win = WrekkerLabWindow(key, db=self._prepared_db, settings_store=self._settings_store, parent=None)
        win.saved.connect(lambda _p: self._refresh_after_lab_save())
        win.destroyed.connect(lambda _=None, k=key: self._lab_windows.pop(k, None))
        self._lab_windows[key] = win
        win.show()

    def _refresh_after_lab_save(self) -> None:
        if self._wrekked is not None:
            self._wrekked._refresh_sets()
            self._wrekked._load_tracks()

    def _on_convert_marker_to_cue(self, deck_id: str, marker) -> None:
        """Convert an AutoMarker to a hot cue point at its position."""
        from wrekker.core.deck import MarkerType
        mtype = getattr(marker, "type", None)
        label = (mtype.value.upper().replace("_", " ")
                 if mtype is not None else "AUTO")
        # Pick a color matching the marker type
        _MKRS_CUE_COLORS: dict[str, str] = {
            "mix_in":        "#2ecc71",
            "mix_out":       "#e74c3c",
            "drop":          "#f1c40f",
            "breakdown":     "#3498db",
            "vocal_in":      "#ff6baf",
            "vocal_out":     "#d85a9b",
            "bass_in":       "#ffd23f",
            "bass_out":      "#d9a600",
            "kick_in":       "#18d8ff",
            "kick_out":      "#1192b0",
            "top_in":        "#9b7cff",
            "top_out":       "#7252c7",
            "vocal_ghost":   "#f39c12",
            "deconstruct":   "#f39c12",
            "rebuild":       "#f39c12",
            "switch_point":  "#fdcb6e",
        }
        mval  = mtype.value if mtype else ""
        color = _MKRS_CUE_COLORS.get(mval, "#00d4ff")
        pos   = getattr(marker, "position_s", 0.0)
        added = self._transport.add_cue_at(deck_id, pos, label=label, color=color)
        if not added:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Hot Cue", "All 8 hot cue slots are in use."
            )

    def _on_play_pause(self, deck_id: str) -> None:
        from wrekker.core.deck import DeckStatus
        state = self._transport.get_state(deck_id)
        if state.status == DeckStatus.PLAYING:
            self._transport.pause(deck_id)
        else:
            self._transport.play(deck_id)

    def _on_load_track(self, track, deck_id: str) -> None:
        self._transport.load_track(deck_id, track.path)

    def _hw_browse_scroll(self, delta: int) -> None:
        """Route hardware knob rotation to the active browser tab."""
        if self._wrekked is not None:
            self._wrekked.browse_scroll(delta)
        elif self._library is not None:
            self._library.browse_scroll(delta)

    def _hw_browse_click(self) -> None:
        """Route hardware knob press to the active browser tab."""
        if self._wrekked is not None:
            self._wrekked.browse_click()
        elif self._library is not None:
            self._library.browse_click()

    def _load_selected_to_deck(self, deck_id: str) -> None:
        if self._wrekked is not None:
            self._wrekked.load_selected_to_deck(deck_id)
        elif self._library is not None:
            track = self._library.get_selected_track()
            if track:
                self._transport.load_track(deck_id, track.path)

    def _on_prepare_tracks(self, tracks, set_id: int | None = None) -> None:
        if not tracks or self._prepared_db is None:
            return
        from wrekker.formats.wrk import wrk_id_for
        prepared_root = self._prepared_path("wrekked_library_path")
        existing = self._prepared_db.get_set_wrk_ids(set_id) if set_id else set()
        filtered = []
        seen: set[str] = set()
        for track in tracks:
            try:
                wid = wrk_id_for(track.path)
            except Exception:
                wid = str(track.path.absolute())
            if wid in seen or wid in existing:
                continue
            seen.add(wid)
            filtered.append(track)
        tracks = filtered
        if not tracks:
            return
        from wrekker.ui.workers.prepare_worker import PrepareWorker
        from wrekker.ui.widgets.prepare_dialog import PrepareDialog
        worker = PrepareWorker(
            tracks        = list(tracks),
            prepared_db   = self._prepared_db,
            prepared_root = prepared_root,
            stem_model    = self._stem_model,
            parent        = self,
        )
        titles = [t.display_title for t in tracks]
        dlg = PrepareDialog(worker, titles, parent=self)
        dlg.show()
        def _after_done(*_):
            if set_id:
                existing = self._prepared_db.get_set_wrk_ids(set_id)
                next_position = self._prepared_db.next_set_position(set_id)
                for t in tracks:
                    rec = self._prepared_db.find_wrk(t.path)
                    if rec and rec.wrk_ready and rec.wrk_id not in existing:
                        self._prepared_db.upsert_set_track(
                            set_id=set_id, wrk_id=rec.wrk_id, wrk_path=rec.wrk_path,
                            title=rec.title or t.display_title,
                            artist=rec.artist or t.display_artist,
                            duration_s=rec.duration_s or t.duration,
                            bpm=rec.bpm or t.bpm,
                            key=rec.key or t.key_str,
                            stems_ready=rec.stems_ready,
                            wrk_ready=rec.wrk_ready,
                            source_available=t.path.exists(),
                            position=next_position,
                        )
                        next_position += 1
                        existing.add(rec.wrk_id)
                self._prepared_db.update_set_track_count(set_id)
                self._prepared_db.update_set_total_duration(set_id)
            if self._library is not None:
                self._library.refresh_statuses()
            if self._wrekked is not None:
                self._wrekked.refresh()

        worker.all_done.connect(_after_done)
        worker.start()

    def _prepared_path(self, key: str) -> Path:
        default = data_dir() / "prepared"
        if self._prepared_db is None:
            return default
        value = self._prepared_db.get_setting(key, str(default))
        return Path(value).expanduser() if value else default

    def _get_reference_key(self, state_a, state_b):
        """Return the HarmonicKey of the reference deck for compat dots."""
        from wrekker.core.deck import DeckStatus
        # Prefer sync master
        for state in (state_a, state_b):
            if state and state.sync_master and state.track and state.track.key:
                return state.track.key
        # Prefer playing deck
        for state in (state_a, state_b):
            if (state and state.status == DeckStatus.PLAYING
                    and state.track and state.track.key):
                return state.track.key
        # Fallback: any loaded deck with a key
        for state in (state_a, state_b):
            if state and state.track and state.track.key:
                return state.track.key
        return None

    def _on_root_added(self, path) -> None:
        self._library.refresh()

    # ── update loop ───────────────────────────────────────────────────────────

    def _log_ui_platform(self) -> None:
        if not (self._debug or os.environ.get("WREKKER_UI_PLATFORM_LOG", "0") == "1"):
            return
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            platform = app.platformName() if app is not None else "unknown"
            screens = []
            for s in (app.screens() if app is not None else []):
                geo = s.geometry()
                screens.append(
                    f"{s.name()} {geo.width()}x{geo.height()} "
                    f"dpr={s.devicePixelRatio():.2f} refresh={s.refreshRate():.1f}Hz"
                )
            print(
                "[ui-platform] "
                f"platform={platform} clock={os.environ.get('WREKKER_UI_CLOCK', 'pipe')} "
                f"screens={' | '.join(screens) if screens else 'none'}",
                flush=True,
            )
        except Exception as exc:
            print(f"[ui-platform] unavailable: {exc}", flush=True)

    def _start_update_loop(self) -> None:
        clock_mode = os.environ.get("WREKKER_UI_CLOCK", "pipe").lower()
        if clock_mode == "qtimer":
            self._timer = QTimer(self)
            self._timer.setTimerType(Qt.TimerType.PreciseTimer)
            self._timer.setInterval(int(os.environ.get("WREKKER_UI_TICK_MS", "8")))
            self._timer.timeout.connect(self._tick)
            self._timer.start()
        elif clock_mode == "thread":
            try:
                fps = float(os.environ.get("WREKKER_UI_TARGET_FPS", "60"))
            except ValueError:
                fps = 60.0
            self._tick_thread = _UiTickThread(fps=fps, parent=self)
            self._tick_thread.tick.connect(self._tick)
            self._tick_thread.start()
        else:
            try:
                fps = float(os.environ.get("WREKKER_UI_TARGET_FPS", "60"))
            except ValueError:
                fps = 60.0
            self._pipe_clock = _UiPipeClock(self._tick, fps=fps, parent=self)
        self._last_tick_t: float = time.perf_counter()
        self._last_meter_t: float = self._last_tick_t
        self._last_heavy_t: float = self._last_tick_t
        self._last_control_t: float = self._last_tick_t
        self._last_stem_lufs_t: float = 0.0
        self._stem_lufs_cache_a: dict[str, float] = {}
        self._stem_lufs_cache_b: dict[str, float] = {}

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start()
        self._update_clock()

    def _tick(self) -> None:
        _t0 = time.perf_counter()
        _profile_last = _t0

        def _profile_mark(name: str) -> None:
            nonlocal _profile_last
            if not self._tick_profile_enabled:
                return
            now = time.perf_counter()
            self._tick_profile.setdefault(name, []).append((now - _profile_last) * 1000.0)
            _profile_last = now

        dt  = _t0 - self._last_tick_t
        self._last_tick_t = _t0
        if self._tick_log_enabled:
            self._tick_log_count += 1
            if dt > 0.020:
                self._tick_log_late += 1
            elapsed_log = _t0 - self._tick_log_last
            if elapsed_log >= 3.0:
                fps = self._tick_log_count / max(elapsed_log, 1e-6)
                print(f"[ui-tick] fps={fps:.1f} late>20ms={self._tick_log_late}", flush=True)
                self._tick_log_count = 0
                self._tick_log_late = 0
                self._tick_log_last = _t0
                if self._tick_profile_enabled and self._tick_profile:
                    parts = []
                    for name, vals in self._tick_profile.items():
                        if not vals:
                            continue
                        parts.append(f"{name}={sum(vals) / len(vals):.2f}/{max(vals):.2f}ms")
                    print("[ui-profile] " + "  ".join(parts), flush=True)
                    self._tick_profile.clear()

        late_tick = dt > 0.020

        # Heavy-path counter: expensive ops run every 6th tick (~10 Hz at 60 Hz base).
        # If the UI loop is already late, defer heavy work briefly so the
        # position/waveform path can recover cadence instead of locking around
        # the slower meters/heavy cadence.
        self._heavy_tick_n = (self._heavy_tick_n + 1) % 6
        heavy_due = (self._heavy_tick_n == 0)
        heavy = heavy_due and (not late_tick or (_t0 - self._last_heavy_t) >= 0.25)
        if heavy:
            self._last_heavy_t = _t0

        meter_due = (_t0 - self._last_meter_t) >= (1.0 / 45.0)
        # Meters and oscilloscopes are visual feedback, not transport truth. If
        # the 16ms tick is late, cap this path near 20Hz so it cannot keep the
        # whole UI stuck at ~35Hz.
        visual_meters = meter_due and (not late_tick or (_t0 - self._last_meter_t) >= 0.050)
        if visual_meters:
            self._last_meter_t = _t0

        control_due = (_t0 - self._last_control_t) >= (1.0 / 30.0)
        control_feedback = control_due and (not late_tick or (_t0 - self._last_control_t) >= 0.066)
        if control_feedback:
            self._last_control_t = _t0

        if not self._debug:
            _t0 = 0.0

        # PLL sync tick — every tick is fine (uses dt, self-corrects)
        self._transport.tick_sync(dt)
        _profile_mark("sync")

        # ── Always at 60 Hz ───────────────────────────────────────────────────

        # State snapshots (cheap: reads Python dicts)
        state_a = self._transport.get_state("A")
        state_b = self._transport.get_state("B")
        _profile_mark("state")
        smart_cfx = self._transport.get_smart_cfx_enabled()
        if smart_cfx != self._last_smart_cfx_ui:
            self._master.set_smart_cfx_enabled(smart_cfx)
            self._deck_a.set_smart_cfx_enabled(smart_cfx)
            self._deck_b.set_smart_cfx_enabled(smart_cfx)
            self._last_smart_cfx_ui = smart_cfx
        _profile_mark("smart_cfx")

        # Real-time positions (direct atomic read from Rust)
        pos_a = self._engine.deck_a.position_s
        pos_b = self._engine.deck_b.position_s
        _profile_mark("pos")

        # Waveform — update when data changes (load + beat detection complete)
        seq_a = self._engine.get_waveform_seq("A")
        if seq_a != self._wf_seq_a:
            self._wf_seq_a = seq_a
            self._deck_a.set_waveform(self._engine.get_waveform("A"))

        seq_b = self._engine.get_waveform_seq("B")
        if seq_b != self._wf_seq_b:
            self._wf_seq_b = seq_b
            self._deck_b.set_waveform(self._engine.get_waveform("B"))
        _profile_mark("wf_seq")

        from wrekker.core.deck import STEM_NAMES, DeckStatus

        # Cross-deck beat overlay — isolated so an exception never breaks update_state
        try:
            a_playing = state_a.status == DeckStatus.PLAYING
            b_playing = state_b.status == DeckStatus.PLAYING
            if not self._disable_cross_overlay:
                bg_b = state_b.beatgrid
                self._deck_a.set_other_deck_overlay(
                    pos_s        = pos_b,
                    beats        = bg_b.beats if bg_b else (),
                    bpm          = (state_b.bpm_live
                                    or (state_b.track.bpm if state_b.track else 0.0)
                                    or (bg_b.bpm if bg_b else 0.0)),
                    first_beat_s = bg_b.first_beat_s if bg_b else 0.0,
                    source_playing = b_playing,
                )
                bg_a = state_a.beatgrid
                self._deck_b.set_other_deck_overlay(
                    pos_s        = pos_a,
                    beats        = bg_a.beats if bg_a else (),
                    bpm          = (state_a.bpm_live
                                    or (state_a.track.bpm if state_a.track else 0.0)
                                    or (bg_a.bpm if bg_a else 0.0)),
                    first_beat_s = bg_a.first_beat_s if bg_a else 0.0,
                    source_playing = a_playing,
                )
        except Exception:
            pass
        _profile_mark("cross")

        # Push only the position-sensitive controls at 60 Hz. Full deck repaint
        # stays on the heavy path so waveform scrolling is not gated by labels,
        # artwork, stem widgets, or metric formatting.
        if not self._disable_deck_realtime:
            self._deck_a.update_realtime_state(state_a, pos_a)
            self._deck_b.update_realtime_state(state_b, pos_b)
        _profile_mark("deck_rt")

        if control_feedback:
            if state_a and state_a.stems:
                self._deck_a.update_stem_gains([state_a.stems[s].effective_gain for s in STEM_NAMES])
            if state_b and state_b.stems:
                self._deck_b.update_stem_gains([state_b.stems[s].effective_gain for s in STEM_NAMES])

            self._update_phrase_meters(state_a, state_b, pos_a, pos_b)

            # Hardware/control feedback does not need to run at waveform cadence.
            self._sync_mixer_ui()

            fx_state = self._transport.get_fx_state()
            if fx_state != self._last_fx_state:
                self._master.update_fx_state(fx_state)
                if (self._last_fx_state is not None
                        and fx_state.fx_type != self._last_fx_state.fx_type):
                    self._master.open_fx_panel()
                self._last_fx_state = fx_state
            _profile_mark("control")

        # ── Heavy path: ~10 Hz ────────────────────────────────────────────────
        if heavy:
            # Metrics
            metrics_a = self._engine.get_deck_metrics("A")
            metrics_b = self._engine.get_deck_metrics("B")
            phase     = self._engine.get_phase_correlation()

            # Per-stem LUFS — only poll when playing with stems loaded
            def _stem_lufs(deck_id: str, state) -> dict[str, float]:
                neg_inf = float("-inf")
                playing = state and state.status == DeckStatus.PLAYING
                deck_pb = self._engine.deck_a if deck_id == "A" else self._engine.deck_b
                has_stems = deck_pb.stems is not None
                if not (playing and has_stems):
                    return {s: neg_inf for s in STEM_NAMES}
                result = {}
                for stem in STEM_NAMES:
                    try:
                        lufs = self._engine.get_stem_meters(deck_id, stem)
                        result[stem] = lufs.momentary_lufs
                    except Exception:
                        result[stem] = neg_inf
                return result

            # Native per-stem LUFS is a handful of atomic reads — poll at the
            # heavy-path rate (~10 Hz) so the stem meters move like meters.
            stem_lufs_a = self._stem_lufs_cache_a = _stem_lufs("A", state_a)
            stem_lufs_b = self._stem_lufs_cache_b = _stem_lufs("B", state_b)
            self._last_stem_lufs_t = _t0

            # Stem gains → waveform overlay
            if state_a and state_a.stems:
                gains_a = [state_a.stems[s].effective_gain for s in STEM_NAMES]
                self._deck_a.update_stem_gains(gains_a)
            if state_b and state_b.stems:
                gains_b = [state_b.stems[s].effective_gain for s in STEM_NAMES]
                self._deck_b.update_stem_gains(gains_b)

            # Push full metrics update (overwrites the position-only update above)
            self._deck_a.update_state(state_a, pos_a, metrics_a, stem_lufs_a)
            self._deck_b.update_state(state_b, pos_b, metrics_b, stem_lufs_b)
            self._master.update_states(phase, state_a, state_b)

            # Master loudness + pre-fader A/B loudness delta (gain staging)
            try:
                mst_m, mst_st = self._engine.get_master_lufs()
                mpl, mpr = self._engine.get_master_peak()
                mpk = max(float(mpl), float(mpr))
                mst_tp = 20.0 * math.log10(mpk) if mpk > 1e-6 else float("-inf")
                st_a = metrics_a.lufs.short_term_lufs if metrics_a else float("-inf")
                st_b = metrics_b.lufs.short_term_lufs if metrics_b else float("-inf")
                self._master.update_loudness(mst_m, mst_st, mst_tp, st_a, st_b)
            except Exception:
                pass

            if self._flx4 is not None and hasattr(self._flx4, "sync_leds"):
                self._flx4.sync_leds()

            # Harmonic compat dots in library — update reference key at 10 Hz
            ref_key = self._get_reference_key(state_a, state_b)
            if ref_key != self._last_ref_key:
                self._last_ref_key = ref_key
                if self._library is not None:
                    self._library.set_reference_key(ref_key)
                if self._wrekked is not None:
                    self._wrekked.set_reference_key(ref_key)
            _profile_mark("heavy")

        # ── Visual meters path: ~45 Hz ───────────────────────────────────────
        if visual_meters:
            try:
                self._deck_a.update_spectrum(self._engine.get_spectrum("A"))
                self._deck_b.update_spectrum(self._engine.get_spectrum("B"))
            except Exception:
                pass
            _profile_mark("meters")

            try:
                live_a = self._engine.get_live_audio("A")
                live_b = self._engine.get_live_audio("B")
                live_m = self._engine.get_master_live_audio()
                self._master.update_oscilloscopes(live_a, live_b, live_m)
            except Exception:
                pass

            try:
                peak_a = self._engine.get_peak_levels("A")
                peak_b = self._engine.get_peak_levels("B")
                peak_m = self._engine.get_master_peak()
                clip_a = self._engine.get_clip_flags("A")
                clip_b = self._engine.get_clip_flags("B")
                clip_m = self._engine.get_master_clip()
                self._master.update_levels(peak_a, peak_b, peak_m, clip_a, clip_b, clip_m)
                if self._flx4 is not None and hasattr(self._flx4, "update_level_meters"):
                    self._flx4.update_level_meters(peak_a, peak_b)
            except Exception:
                pass

            try:
                def _peak_db(level: float) -> float:
                    return 20.0 * math.log10(level) - 3.0 if level > 1e-5 else float("-inf")

                def _stem_levels(deck_id: str, state) -> dict[str, float]:
                    if not state or not state.stems:
                        return {}
                    deck_pb = self._engine.deck_a if deck_id == "A" else self._engine.deck_b
                    if deck_pb.stems is None:
                        return {}
                    return {
                        stem: _peak_db(self._engine.get_stem_peak(deck_id, stem))
                        for stem in STEM_NAMES
                    }

                pa = self._engine.get_peak_levels("A")
                pb = self._engine.get_peak_levels("B")
                self._deck_a.update_live_levels(
                    deck_level=_peak_db(max(pa)),
                    stem_levels=_stem_levels("A", state_a),
                )
                self._deck_b.update_live_levels(
                    deck_level=_peak_db(max(pb)),
                    stem_levels=_stem_levels("B", state_b),
                )
            except Exception:
                pass

        if self._debug:
            elapsed_ms = (time.perf_counter() - _t0) * 1000.0
            self._dbg_tick_times.append(elapsed_ms)
            self._dbg_tick_count += 1
            if self._dbg_tick_count % 180 == 0:  # every 3 s at 60 Hz
                avg  = sum(self._dbg_tick_times) / len(self._dbg_tick_times)
                peak = max(self._dbg_tick_times)
                print(f"[ui-tick] avg={avg:.1f}ms  peak={peak:.1f}ms  ({len(self._dbg_tick_times)} ticks)")
                self._dbg_tick_times.clear()
        if self._tick_thread is not None:
            self._tick_thread.acknowledge()

    def _update_phrase_meters(self, state_a, state_b, pos_a: float, pos_b: float) -> None:
        """Update phrase progress at the visual tick rate."""
        from wrekker.core.deck import DeckStatus

        def _status_for_pair() -> str:
            if not state_a.beatgrid or not state_b.beatgrid:
                return "idle"
            sync_active = bool(state_a.sync_enabled or state_b.sync_enabled)
            if not sync_active:
                return "off"

            phase_errors = [
                abs(st.sync_phase_error)
                for st in (state_a, state_b)
                if st.sync_phase_error is not None
            ]
            beat_locked = bool(phase_errors) and min(phase_errors) < 0.05
            if not beat_locked:
                return "off"

            master, master_pos = (state_a, pos_a) if state_a.sync_master else (state_b, pos_b)
            slave, slave_pos = (state_b, pos_b) if state_a.sync_master else (state_a, pos_a)
            if self._phrase_sync.is_phrase_locked_at(master, master_pos, slave, slave_pos):
                return "locked"
            return "beat"

        try:
            status = _status_for_pair()
            for deck, state, pos in (
                (self._deck_a, state_a, pos_a),
                (self._deck_b, state_b, pos_b),
            ):
                if not state.beatgrid or state.status == DeckStatus.EMPTY:
                    deck.update_phrase_meter(0.0, 0, 32, "idle")
                    continue
                beats_total = self._phrase_sync.phrase_length_beats(state, pos)
                beats_done = self._phrase_sync.phrase_progress_beats(state, pos)
                progress = self._phrase_sync.phrase_progress_fraction(state, pos)
                deck.update_phrase_meter(progress, beats_done, beats_total, status)
        except Exception:
            self._deck_a.update_phrase_meter(0.0, 0, 32, "idle")
            self._deck_b.update_phrase_meter(0.0, 0, 32, "idle")

    def _sync_mixer_ui(self) -> None:
        """Push current engine mixer values into UI widgets (no signal re-emission)."""
        xf = self._engine.get_crossfader()
        eq_a = (
            self._engine.get_eq("A", "low"),
            self._engine.get_eq("A", "mid"),
            self._engine.get_eq("A", "high"),
        )
        eq_b = (
            self._engine.get_eq("B", "low"),
            self._engine.get_eq("B", "mid"),
            self._engine.get_eq("B", "high"),
        )
        master_gain = self._engine.get_master_gain()
        ch_a = self._engine.get_channel_gain("A")
        ch_b = self._engine.get_channel_gain("B")
        pre_a = self._engine.get_pregain("A")
        pre_b = self._engine.get_pregain("B")
        mixer_key = (
            round(xf, 3),
            tuple(round(v, 3) for v in eq_a),
            tuple(round(v, 3) for v in eq_b),
            round(master_gain, 3),
            round(ch_a, 3),
            round(ch_b, 3),
            round(pre_a, 3),
            round(pre_b, 3),
        )
        if mixer_key != self._last_mixer_ui_state:
            self._master.set_mixer_state(xf, eq_a, eq_b)
            self._master.set_master_volume(master_gain)
            self._deck_a.set_channel_volume(ch_a)
            self._deck_b.set_channel_volume(ch_b)
            self._deck_a.set_pregain(pre_a)
            self._deck_b.set_pregain(pre_b)
            self._last_mixer_ui_state = mixer_key

        mon = self._transport.get_monitor_state()
        mon_key = (mon.cue_deck_a, mon.cue_deck_b, mon.cue_master)
        if mon_key != self._last_monitor_ui_state:
            self._deck_a.set_monitor_cue_active(mon.cue_deck_a)
            self._deck_b.set_monitor_cue_active(mon.cue_deck_b)
            self._master.set_monitor_cue_active(mon.cue_master)
            self._last_monitor_ui_state = mon_key

    def _update_clock(self) -> None:
        from datetime import datetime
        self._clock.setText(datetime.now().strftime("%H:%M:%S"))

    def refresh_prepared_statuses(self) -> None:
        """Refresh the library STATUS column after an external scan or prepare run."""
        if self._library is not None:
            self._library.refresh_statuses()
        if self._wrekked is not None:
            self._wrekked.refresh()

    # ── cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, ev) -> None:
        if self._timer is not None:
            self._timer.stop()
        if self._tick_thread is not None:
            self._tick_thread.stop()
        if self._pipe_clock is not None:
            self._pipe_clock.stop()
        self._clock_timer.stop()
        ev.accept()
