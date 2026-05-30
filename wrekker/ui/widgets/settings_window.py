"""Professional Settings window for Wrekker."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from typing import Any, Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from wrekker.settings import SettingsStore
from wrekker.settings.store import ENV_SETTING_MAP, DEFAULT_SETTINGS_PATH
from wrekker.ui.branding import logo_label
from wrekker.ui import theme


class SettingsWindow(QDialog):
    settings_saved = pyqtSignal()

    _SECTIONS = [
        ("audio", "Audio & Routing", "Audio engine, master output and headphones/CUE routing."),
        ("controller", "Controller & MIDI", "DDJ-FLX4 detection, MIDI ports, jog and feedback behavior."),
        ("library", "Library & Storage", "Music source folders, source status and core data locations."),
        ("wrekked", "WREKKED & Fastload", "Prepared .wrk storage, fastload cache and cleanup actions."),
        ("analysis", "Analysis & Preparation", "Preparation quality, processing devices and auto-marker rules."),
        ("playback", "Playback & Mixing", "Mixer defaults and track-load reset behavior."),
        ("sync", "Sync & Quantize", "BPM, phase and phrase sync defaults."),
        ("stems", "Stems & WREKK", "Stem defaults, WREKK mode and WREKK FX product rules."),
        ("fx", "FX", "Normal FX and WREKK FX defaults."),
        ("waveforms", "Waveforms & Display", "DJ-facing display and advanced renderer choices."),
        ("lab", "WREKKER LAB", "Analysis correction workspace defaults and preview behavior."),
        ("profiles", "Profiles", "Create, duplicate, import and export setup profiles."),
        ("advanced", "Advanced", "Configuration management and processing overrides."),
        ("diagnostics", "Diagnostics", "Runtime state, logs and developer diagnostics."),
    ]

    def __init__(self, store: SettingsStore, *, engine=None, transport=None, db=None, prepared_db=None, flx4=None, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self._engine = engine
        self._transport = transport
        self._db = db
        self._prepared_db = prepared_db
        self._flx4 = flx4
        self._working = SettingsStore()
        self._load_working_from_store()
        self._controls: dict[str, QWidget] = {}
        self._section_pages: dict[str, QWidget] = {}
        self._dirty = False
        self._overrides = store.runtime_overrides()

        self.setWindowTitle("SETTINGS")
        self.setMinimumSize(1180, 760)
        self.resize(1280, 820)
        self.setStyleSheet(theme.GLOBAL_SS + self._local_styles())
        self._build_ui()
        self._populate_profiles()
        self._select_section("audio")
        self._set_dirty(False)

    def _load_working_from_store(self) -> None:
        payload = self._store.to_dict()
        self._working.schema_version = payload["schema_version"]
        self._working.active_profile_id = payload["active_profile_id"]
        self._working.startup_profile_id = payload["startup_profile_id"]
        self._working.profiles = {
            pid: type(self._store.profiles[pid])(
                id=pid,
                name=data["name"],
                protected=bool(data.get("protected", False)),
                settings=deepcopy(data["settings"]),
            )
            for pid, data in payload["profiles"].items()
        }

    def _local_styles(self) -> str:
        return f"""
        QDialog {{ background: {theme.BG_DEEP}; }}
        QLineEdit, QComboBox, QSpinBox {{
            background: #070b0e; color: {theme.TEXT_BRIGHT};
            border: 1px solid #26323a; border-radius: 4px; padding: 5px 7px;
        }}
        QListWidget {{
            background: #07090b; border: 1px solid #1e262c; color: {theme.TEXT_MED};
        }}
        QListWidget::item {{ padding: 9px 10px; border-bottom: 1px solid #11191e; }}
        QListWidget::item:selected {{ background: #101820; color: #f7fbff; border-left: 3px solid #ffb000; }}
        QTableWidget {{
            background: #07090b; color: {theme.TEXT_MED}; border: 1px solid #1e262c;
            gridline-color: #172128;
        }}
        """

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.addWidget(logo_label(28))
        title = QLabel("SETTINGS")
        title.setStyleSheet("color: #f7fbff; font-size: 20px; font-weight: 900; letter-spacing: 2px;")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(QLabel("Profile"))
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(240)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_combo)
        header.addWidget(self._profile_combo)
        self._dirty_label = QLabel("")
        self._dirty_label.setStyleSheet("color: #ffb000; font-weight: 800;")
        header.addWidget(self._dirty_label)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(10)
        root.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search settings: headphones, fastload, waveform...")
        self._search.textChanged.connect(self._filter_sections)
        left.addWidget(self._search)
        self._nav = QListWidget()
        self._nav.setFixedWidth(250)
        self._nav.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for key, label, desc in self._SECTIONS:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setToolTip(desc)
            self._nav.addItem(item)
        self._nav.currentItemChanged.connect(lambda cur, _old: self._select_section(cur.data(Qt.ItemDataRole.UserRole)) if cur else None)
        left.addWidget(self._nav, 1)
        body.addLayout(left)

        self._stack = QStackedWidget()
        for key, label, desc in self._SECTIONS:
            page = self._make_page(key, label, desc)
            self._section_pages[key] = page
            self._stack.addWidget(page)
        body.addWidget(self._stack, 1)

        footer = QHBoxLayout()
        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color: {theme.TEXT_DIM};")
        footer.addWidget(self._summary, 1)
        reset_section = QPushButton("Reset Section")
        reset_all = QPushButton("Restore Defaults")
        cancel = QPushButton("Cancel")
        apply = QPushButton("Apply")
        save = QPushButton("Save")
        save.setStyleSheet("background: #ffb000; color: #121212; font-weight: 900;")
        reset_section.clicked.connect(self._reset_section)
        reset_all.clicked.connect(self._reset_all)
        cancel.clicked.connect(self.reject)
        apply.clicked.connect(self._apply)
        save.clicked.connect(self._save)
        footer.addWidget(reset_section)
        footer.addWidget(reset_all)
        footer.addWidget(cancel)
        footer.addWidget(apply)
        footer.addWidget(save)
        root.addLayout(footer)

    def _make_page(self, key: str, title: str, desc: str) -> QWidget:
        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(4, 0, 14, 14)
        lay.setSpacing(10)
        h = QLabel(title)
        h.setStyleSheet("color: #f7fbff; font-size: 18px; font-weight: 900;")
        d = QLabel(desc)
        d.setWordWrap(True)
        d.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(h)
        lay.addWidget(d)
        getattr(self, f"_build_{key}_page")(lay)
        lay.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)
        return outer

    # ── sections ─────────────────────────────────────────────────────────────

    def _build_audio_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Audio Engine", [
            self._combo("Audio Status", "audio.engine_enabled", ["Enabled", "Disabled"], bool_labels=True, restart="audio"),
            self._readonly("Audio Backend", "CPAL via Rust engine."),
            self._combo("Main Output Device", "audio.main_output_device", self._audio_devices(), note="Saved as preferred output. Current engine may still use CPAL default until explicit device binding is available.", restart="audio"),
            self._combo("Sample Rate", "audio.sample_rate", [44100, 48000, 96000], restart="audio"),
            self._combo("Buffer Size", "audio.buffer_size", [64, 128, 256, 512, 1024], restart="audio"),
            self._combo("Latency Preset", "audio.latency_preset", ["Low Latency", "Balanced", "Safe", "Custom"]),
            self._latency_label(),
        ])
        self._group(lay, "Master Output Routing", [
            self._readonly("Current Routing", "Master bus is routed to channels 0/1 of the CPAL output stream."),
            self._combo("Master Left Channel", "audio.master_left_channel", [0], disabled=True),
            self._combo("Master Right Channel", "audio.master_right_channel", [1], disabled=True),
            self._actions_row([("Test Master L", self._test_unavailable), ("Test Master R", self._test_unavailable), ("Test Stereo", self._test_unavailable)]),
        ])
        self._group(lay, "Headphones / CUE", [
            self._check("Headphones / CUE Output Enabled", "audio.cue_enabled", restart="audio"),
            self._combo("CUE Output Device", "audio.cue_device", self._cue_devices(), note="CUE is available on FLX4 or compatible multichannel outputs. Separate-device sync is not promised.", restart="audio"),
            self._combo("CUE Channel Pair", "audio.cue_channel_pair", self._cue_channel_pairs(), restart="audio"),
            self._readonly("CUE Routing Status", "Available when a FLX4 or compatible >=4-channel output is detected by the engine."),
            self._actions_row([("Test CUE L", self._test_unavailable), ("Test CUE R", self._test_unavailable), ("Test Separation", self._test_unavailable)]),
        ])
        self._group(lay, "Monitor Defaults", [
            self._slider("Default Headphone Mix: CUE ↔ MASTER", "audio.headphone_mix", 0, 100, scale=100.0, live=True),
            self._slider("Default Headphone Level", "audio.headphone_level", 0, 200, scale=100.0, live=True),
            self._check("MST CUE On Startup", "audio.mst_cue_on_startup"),
            self._check("Restore CUE Routing On Restart", "audio.restore_cue_routing"),
            self._check("Auto-reconnect Preferred Audio Device", "audio.auto_reconnect"),
            self._check("Warn Before Changing Output During Playback", "audio.warn_before_output_change"),
            self._combo("Fallback Device Policy", "audio.fallback_policy", ["Ask User", "Use System Default", "Keep Audio Disabled"]),
        ])

    def _build_controller_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Device and Ports", [
            self._check("Enable Controller Support", "controller.enabled", restart="app"),
            self._readonly("Detected Controller", "DDJ-FLX4 connected" if self._flx4 is not None else "No DDJ-FLX4 detected"),
            self._combo("Active Controller Profile", "controller.profile", ["DDJ-FLX4"], disabled=True),
            self._combo("MIDI Input Port", "controller.midi_input_port", self._midi_ports("input"), note="Auto is recommended for DDJ-FLX4.", restart="app"),
            self._combo("MIDI Output Port", "controller.midi_output_port", self._midi_ports("output"), note="Auto is recommended for DDJ-FLX4 LED/VU feedback.", restart="app"),
            self._check("Auto Detect DDJ-FLX4", "controller.auto_detect_flx4", restart="app"),
            self._check("Reconnect Automatically", "controller.reconnect_automatically", restart="app"),
        ])
        self._group(lay, "Feedback and Jog", [
            self._check("LED Feedback Enabled", "controller.led_feedback"),
            self._check("VU Feedback Enabled", "controller.vu_feedback"),
            self._check("Vinyl Mode Default", "controller.vinyl_mode"),
            self._slider("Scratch Sensitivity", "controller.scratch_sensitivity", 20, 200, scale=100.0),
            self._slider("Nudge Sensitivity", "controller.nudge_sensitivity", 20, 200, scale=100.0),
            self._combo("Jog Release / Response", "controller.jog_response", ["Tight", "Natural", "Loose"]),
            self._readonly("SHIFT + FX", "Not yet available. Hardware access to WREKK FX is planned but not mapped."),
        ])

    def _build_library_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Music Library Sources", [self._library_sources_widget()])
        self._group(lay, "General Storage", [
            self._path("Library Database Location", "storage.library_db_path", disabled=True, note="Safe migration is not implemented; location shown for diagnostics."),
            self._path("PreparedDB Location", "storage.prepared_db_path", disabled=True, note="Safe migration is not implemented; location shown for diagnostics."),
            self._actions_row([("Open Config Folder", lambda: self._open_path(DEFAULT_SETTINGS_PATH.parent)), ("Open Data Folder", lambda: self._open_path(Path(self._value('storage.library_db_path')).parent)), ("Open Cache Folder", lambda: self._open_path(Path(self._value('storage.fastload_cache_root'))))]),
        ])

    def _build_wrekked_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Prepared Storage Paths", [
            self._path("WREKKED Prepared .wrk Root Folder", "storage.wrekked_root", restart="app"),
            self._path(".wrk Storage Folder", "storage.wrk_storage_root", restart="app"),
            self._path("Fastload Cache Root Folder", "storage.fastload_cache_root", restart="app"),
            self._path("Temporary Stem Cache Folder", "storage.temp_stem_cache_root", restart="app"),
            self._storage_dashboard(),
        ])
        self._group(lay, "Fastload Behavior", [
            self._check("Build Fastload Automatically After Prepare", "storage.build_fastload_after_prepare"),
            self._check("Cache Stems in Fastload", "storage.cache_stems_in_fastload"),
            self._spin("Warn When Free Disk Falls Below (GB)", "storage.disk_warning_gb", 1, 500),
            self._combo("Cache Cleanup Policy", "storage.cache_cleanup_policy", ["Manual only", "Remove unused cache older than N days", "Keep all prepared-set cache"]),
            self._actions_row([("Clear Temporary Stem Cache", self._clear_temp_stem_cache), ("Validate .wrk Files", self._validate_wrks), ("Clean Orphan Fastload Entries", self._not_yet)]),
            self._readonly("Safety Rule", "Deleting fastload cache never deletes persistent .wrk files. Deleting .wrk requires a separate confirmation in WREKKED management."),
        ])

    def _build_analysis_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Preparation Profile", [
            self._combo("Preparation Quality", "preparation.quality", ["fast", "balanced", "archive"], labels={"fast": "FAST", "balanced": "BALANCED", "archive": "ARCHIVE"}, restart="app"),
            self._readonly("Quality Meaning", "FAST: quickest set building. BALANCED: moderate time/storage. ARCHIVE: slower, archival .wrk compression."),
            self._combo("Stem Separation Device", "analysis.stem_device", ["Auto", "CPU", "GPU"], restart="app"),
            self._combo("Beat Analysis Device", "analysis.beat_device", ["auto", "cpu", "cuda"], labels={"auto": "Auto", "cpu": "CPU", "cuda": "GPU"}, restart="app"),
            self._combo("GPU Scheduling Policy", "analysis.gpu_policy", ["safe_serialized", "parallel_gpu", "beat_cpu"], labels={"safe_serialized": "Safe / Serialized", "parallel_gpu": "Parallel GPU Processing", "beat_cpu": "Beat Analysis on CPU while stems use GPU"}, restart="app"),
        ])
        self._group(lay, "Analysis Modules", [
            self._check("Beatgrid Analysis", "analysis.beatgrid"),
            self._check("Downbeat Detection", "analysis.downbeats"),
            self._check("Phrase Detection", "analysis.phrases"),
            self._check("Key Analysis", "analysis.key"),
            self._check("LUFS Analysis", "analysis.lufs"),
            self._check("Stem Separation", "analysis.stems"),
            self._check("Stem Energy Analysis", "analysis.stem_energy"),
            self._check("Auto Marker Generation", "analysis.auto_markers"),
            self._check("Primary Markers Enabled", "analysis.primary_markers_enabled"),
            self._check("WREKK Markers Enabled", "analysis.wrekk_markers_enabled"),
            self._slider("Minimum Auto Marker Confidence", "analysis.auto_marker_confidence", 0, 100, scale=100.0),
            self._combo("Default General Marker Visibility", "analysis.marker_display_mode", ["OFF", "PRIMARY", "PRIMARY + WREKK", "FULL", "DEBUG"]),
            self._combo("Default WREKK Visibility", "analysis.wrekk_visibility", ["OPPORTUNITIES ONLY", "STRUCTURAL + OPPORTUNITIES", "ALL / DEBUG"]),
            self._slider("Structural W LAB Review Threshold", "analysis.wrekk_structural_lab_threshold", 0, 100, scale=100.0),
            self._slider("Structural W Live Threshold", "analysis.wrekk_structural_live_threshold", 0, 100, scale=100.0),
            self._slider("WREKK Opportunity Live Threshold", "analysis.wrekk_opportunity_live_threshold", 0, 100, scale=100.0),
            self._check("Group Nearby Marker Events", "analysis.marker_grouping_enabled"),
            self._combo("Marker Cooldown Policy", "analysis.marker_cooldown_policy", ["Minimal", "Balanced", "Strict"]),
            self._readonly("Marker Safety", "Default WREKK live view shows high-confidence opportunities only. Manual or locked markers are never modified by threshold changes."),
        ])

    def _build_playback_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Mixer Defaults", [
            self._slider("Default Master Gain", "playback.default_master_gain", 0, 200, scale=100.0, live=True),
            self._check("Reset Channel Fader on New Track", "playback.reset_channel_fader_on_load"),
            self._check("Reset EQ on New Track", "playback.reset_eq_on_load"),
            self._check("Reset Filter on New Track", "playback.reset_filter_on_load"),
            self._check("Reset Stems on New Track", "playback.reset_stems_on_load"),
            self._check("Reset FX on New Track", "playback.reset_fx_on_load"),
            self._check("Preserve Manual Stem Balance on Track Load", "playback.preserve_manual_stem_balance"),
            self._combo("Crossfader Curve", "playback.crossfader_curve", ["Equal Power"], disabled=True),
            self._combo("Tempo Range", "playback.tempo_range", ["±6%", "±10%", "±16%", "Wide"], disabled=True, note="Displayed as a planned preference; active tempo range is still fixed in current deck controls."),
        ])

    def _build_sync_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Sync Defaults", [
            self._combo("Default Sync Mode", "sync.default_mode", ["BPM only", "BPM + Phase", "BPM + Phase + Phrase"]),
            self._check("Phase-align Follower on Resume", "sync.phase_align_on_resume"),
            self._combo("Default Master Selection", "sync.master_selection", ["Manual Master Selection"], disabled=True),
            self._combo("When Pitch is Moved on Synced Follower", "sync.pitch_move_on_follower", ["Disable Sync"], disabled=True),
        ])
        self._group(lay, "Quantize", [
            self._check("Quantize Enabled", "sync.quantize_enabled", disabled=True, note="Quantize triggers are not implemented in the active performance path yet."),
            self._combo("Quantize Resolution", "sync.quantize_resolution", ["1 beat", "1/2 beat", "1/4 beat"], disabled=True),
            self._check("Quantize Hot Cues", "sync.quantize_hot_cues", disabled=True),
            self._check("Quantize Loops", "sync.quantize_loops", disabled=True),
        ])

    def _build_stems_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Stem Behavior", [
            self._check("Reset Stem Gains on Track Load", "stems.reset_gains_on_load"),
            self._check("Preserve Stem Balance Between Tracks", "stems.preserve_balance_between_tracks"),
            self._slider("Default VOCALS Gain", "stems.default_gains.vocals", 0, 200, scale=100.0),
            self._slider("Default DRUMS Gain", "stems.default_gains.drums", 0, 200, scale=100.0),
            self._slider("Default BASS Gain", "stems.default_gains.bass", 0, 200, scale=100.0),
            self._slider("Default OTHER Gain", "stems.default_gains.other", 0, 200, scale=100.0),
            self._check("Stem Meters Visible", "stems.meters_visible"),
            self._combo("Stem Loading/Status Visibility", "stems.status_visibility", ["Compact", "Detailed"]),
        ])
        self._group(lay, "Stem Horizon", [
            self._check("Stem Horizon Enabled", "stems.horizon_enabled", live=True),
            self._combo("Stem Horizon Display", "stems.horizon_display_mode", ["Off", "LED Blocks", "Future Bars", "Stem Waveforms"]),
            self._combo("Live Horizon Range", "stems.horizon_range_bars", [4, 8, 16, 32]),
            self._check("Show Next Change Countdown", "stems.horizon_show_countdown", live=True),
            self._check("Show WREKK Opportunity Flag", "stems.horizon_show_w_flag", live=True),
            self._combo("Show Stem Dominance Levels", "stems.horizon_dominance_levels", ["Binary Active/Inactive", "Three Levels"]),
            self._combo("Display When", "stems.horizon_display_when", ["Always for prepared .wrk tracks", "Only when WREKK Mode is active", "Only when W marker display is enabled"]),
            self._combo("Live Stem Horizon Detail", "stems.horizon_detail", ["Minimal", "Balanced", "Detailed"]),
            self._slider("Stem Horizon Brightness", "stems.horizon_intensity", 0, 200, scale=100.0, live=True),
            self._check("Dim Inactive Segments", "stems.horizon_dim_inactive", live=True),
            self._check("Highlight Upcoming Change", "stems.horizon_highlight_upcoming_change", live=True),
        ])
        self._group(lay, "WREKK Mode and WREKK FX", [
            self._check("WREKK Mode Active on Startup", "wrekk.mode_on_startup"),
            self._combo("WREKK Macro Curve", "wrekk.macro_curve", ["Gentle", "Performance", "Aggressive"], disabled=True, note="Macro curve presets are reserved until runtime macro curves are parameterized."),
            self._check("Reset WREKK Macro on Track Load", "wrekk.reset_macro_on_load"),
            self._combo("Default FX Bank on Startup", "wrekk.default_fx_bank", ["NORMAL", "WREKK FX"]),
            self._combo("Default WREKK FX Target", "wrekk.default_wrekk_fx_target", ["Deck A", "Deck B", "Both", "Last Used"]),
            self._check("Remember Last Selected WREKK FX", "wrekk.remember_last_wrekk_fx"),
            self._check("Remember WREKK FX Parameter Values", "wrekk.remember_wrekk_fx_parameters"),
            self._readonly("Product Rule", "WREKK FX only operate when prepared stems are available. They never silently process the full mix."),
        ])

    def _build_fx_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "General FX Defaults", [
            self._combo("Startup FX Bank", "normal_fx.startup_fx_bank", ["NORMAL", "WREKK FX"]),
            self._combo("Default FX Target", "normal_fx.default_target", ["Master", "Deck A", "Deck B"]),
            self._combo("Default Beat Division", "normal_fx.default_beat_division", ["1/8", "1/4", "1/2", "1", "2"]),
            self._slider("Default Wet", "normal_fx.default_wet", 0, 100, scale=100.0),
            self._slider("Default Feedback", "normal_fx.default_feedback", 0, 100, scale=100.0),
            self._combo("Tail Behavior on Disable", "normal_fx.tail_behavior", ["Natural Decay"]),
            self._check("Reset FX on Track Load", "normal_fx.reset_on_track_load"),
            self._check("Remember Last Normal FX", "normal_fx.remember_last"),
            self._check("Remember Normal FX Parameters", "normal_fx.remember_parameters"),
            self._readonly("Preset Safety", "FX presets control FX parameters only. They do not overwrite stem gains or track analysis."),
        ])

    def _build_waveforms_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "DJ-Facing Display", [
            self._combo("Waveform Color Mode", "waveforms.color_mode", ["Spectral", "Deck Identity", "Stem Identity"]),
            self._combo("Zoom Waveform Detail", "waveforms.zoom_detail", ["Smooth", "Balanced", "Detailed"]),
            self._combo("Playhead Color", "waveforms.playhead_color", ["White / Magenta", "Amber", "Cyan", "White"]),
            self._check("Show Beat Grid", "waveforms.show_beat_grid"),
            self._check("Show Downbeats", "waveforms.show_downbeats"),
            self._check("Show Phrase Regions", "waveforms.show_phrase_regions"),
            self._combo("Default Auto Marker Display", "waveforms.auto_marker_display", ["OFF", "ESSENTIAL", "FULL", "DEBUG"]),
            self._check("Cross-Deck Beat Overlay", "waveforms.cross_deck_overlay", live=True),
            self._slider("Stem Overlay Opacity", "waveforms.stem_overlay_opacity", 0, 100, scale=100.0),
            self._check("Show Oscilloscopes", "waveforms.show_oscilloscopes"),
            self._check("Show Spectrum", "waveforms.show_spectrum"),
        ])
        self._group(lay, "Visual Performance", [
            self._combo("Visual Performance Preset", "waveforms.visual_performance", ["SAFE", "BALANCED", "HIGH DETAIL", "CUSTOM"], labels={"Balanced": "BALANCED"}),
            self._combo("Deck Zoom Renderer", "waveforms.deck_renderer", ["texture", "classic", "qml"], labels={"texture": "Stable Texture Renderer", "classic": "Classic QWidget Fallback", "qml": "Experimental QML Waveforms"}, restart="app"),
            self._readonly("Experimental Renderer Warning", "Experimental QML may reduce interface performance during live playback. It is never selected by default."),
            self._spin("Texture Cache Scale", "waveforms.texture_zoom_cache_scale", 1, 8, restart="app"),
            self._spin("Texture Peak Smoothing", "waveforms.texture_zoom_peak_smooth", 0, 15, restart="app"),
            self._check("Request Experimental QML Deck Waveforms", "waveforms.experimental_qml_requested", restart="app"),
            self._check("Force Experimental QML Deck Waveforms", "waveforms.experimental_qml_enabled", restart="app"),
        ])

    def _build_lab_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Editing Defaults", [
            self._combo("Default Waveform Source", "lab.default_waveform_source", ["DRUMS", "FULL MIX", "Last Used"]),
            self._check("Default Compare Mode: Auto vs Active", "lab.default_compare_mode"),
            self._combo("Default Phrase Length", "lab.default_phrase_length", [8, 16, 32]),
            self._check("Snap to Drum Transient by Default", "lab.snap_to_drum_transient"),
            self._combo("Regenerate Unlocked Auto Markers After Grid Edit", "lab.regenerate_unlocked_auto_markers", ["Ask", "Always", "Never"]),
            self._check("Require Save Before Mark Manual Verified", "lab.require_save_before_manual_verified"),
        ])
        self._group(lay, "Preview Defaults", [
            self._combo("Preview Starts In", "lab.preview_starts_in", ["Full Mix", "Selected Stem", "Last Used"]),
            self._check("Default Metronome", "lab.default_metronome"),
            self._slider("Metronome Level", "lab.metronome_level", 0, 100, scale=100.0),
            self._check("Accent Downbeats", "lab.accent_downbeats"),
            self._combo("Default Stem Monitoring", "lab.stem_monitoring", ["None", "Isolate Selected Stem", "Mute Selected Stem"]),
            self._check("Restore Previous LAB Preview State", "lab.restore_preview_state"),
        ])
        self._group(lay, "LAB Audio and Renderer", [
            self._combo("LAB Preview Output", "lab.preview_output", ["Follow Main Audio Output"], disabled=True, note="CUE/dedicated LAB routing is not implemented safely yet."),
            self._combo("LAB Preview Buffer Size", "lab.preview_buffer_size", [128, 256, 512, 1024]),
            self._check("Force Widget Timeline", "lab.force_widget_timeline", restart="app"),
            self._combo("LAB Timeline Renderer", "lab.waveform_renderer", ["texture", "classic"], labels={"texture": "Stable Texture Renderer", "classic": "Classic QWidget Fallback"}, restart="app"),
            self._spin("LAB Texture Cache Scale", "lab.texture_cache_scale", 1, 8, restart="app"),
            self._spin("LAB Texture Resolution (px/s)", "lab.texture_px_per_second", 64, 1024, restart="app"),
            self._spin("LAB Texture Peak Smoothing", "lab.texture_peak_smooth", 0, 15, restart="app"),
        ])

    def _build_profiles_page(self, lay: QVBoxLayout) -> None:
        self._profiles_table = QTableWidget(0, 4)
        self._profiles_table.setHorizontalHeaderLabels(["Active", "Startup", "Profile", "Protected"])
        self._profiles_table.verticalHeader().setVisible(False)
        lay.addWidget(self._profiles_table)
        lay.addWidget(self._actions_row([
            ("Create Profile", self._create_profile),
            ("Duplicate Profile", self._duplicate_profile),
            ("Rename Profile", self._rename_profile),
            ("Delete Profile", self._delete_profile),
            ("Set Startup Profile", self._set_startup_profile),
            ("Export Profile", self._export_profile),
            ("Import Profile", self._import_profile),
        ]))
        self._refresh_profiles_table()

    def _build_advanced_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Configuration Management", [
            self._readonly("Config File", str(self._store.path)),
            self._actions_row([("Open Config Folder", lambda: self._open_path(self._store.path.parent)), ("Export Full Settings", self._export_settings), ("Import Full Settings", self._import_settings), ("Copy Runtime Overrides", self._copy_overrides)]),
        ])
        self._group(lay, "Processing Overrides", [
            self._spin("FLAC Compression Level", "preparation.flac_compression_level", 0, 8, allow_none=True, restart="app"),
            self._spin("CPU Worker Count", "preparation.cpu_workers", 0, 6, restart="app"),
            self._spin("Audio Encode Threads", "preparation.audio_encode_threads", 1, 4, restart="app"),
            self._combo("Beat This Checkpoint", "analysis.beat_checkpoint", ["final0"], restart="app"),
            self._check("DBN Postprocessing", "analysis.beat_use_dbn", restart="app"),
            self._check("Keep Prepare Temporary Files", "analysis.keep_prepare_temp", restart="app"),
        ])
        self._group(lay, "Destructive Operations", [
            self._readonly("Safety", "Destructive cache actions explain what is deleted and preserve .wrk files unless explicitly deleted elsewhere."),
            self._actions_row([("Clear Temporary Stem Cache", self._clear_temp_stem_cache), ("Clean Fastload Cache", self._confirm_fastload_clean), ("Validate .wrk Files", self._validate_wrks)]),
        ])

    def _build_diagnostics_page(self, lay: QVBoxLayout) -> None:
        self._group(lay, "Current Runtime State", [
            self._readonly("Application Version", QGuiApplication.applicationVersion() or "unknown"),
            self._readonly("Active Settings Profile", self._working.active_profile.name),
            self._readonly("Audio Engine Status", "Running" if getattr(self._engine, "_running", False) else "Stopped / unavailable"),
            self._readonly("Controller Status", "DDJ-FLX4 connected" if self._flx4 is not None else "Not connected"),
            self._readonly("Current Waveform Renderer", str(self._value("waveforms.deck_renderer"))),
            self._readonly("Fastload Root", str(self._value("storage.fastload_cache_root"))),
            self._readonly("WREKKED Root", str(self._value("storage.wrekked_root"))),
            self._readonly("Runtime Overrides", self._overrides_text() or "None"),
        ])
        self._group(lay, "Debug Toggles", [
            self._check("Enable UI Tick Logging", "diagnostics.ui_tick_log", restart="app"),
            self._check("Enable UI Tick Profiling", "diagnostics.ui_tick_profile", restart="app"),
            self._check("Enable Zoom FPS Logging", "diagnostics.zoom_fps_log", restart="app"),
            self._check("Enable Waveform Position Debug", "diagnostics.waveform_position_debug", restart="app"),
            self._check("Enable Waveform Renderer Debug", "diagnostics.waveform_renderer_debug", restart="app"),
            self._check("Enable UI Platform Log", "diagnostics.ui_platform_log", restart="app"),
            self._check("Disable Cross Overlay Temporarily", "diagnostics.disable_cross_overlay", restart="app"),
            self._check("Disable Deck Realtime Temporarily", "diagnostics.disable_deck_realtime", restart="app"),
            self._actions_row([("Copy Diagnostics Summary", self._copy_diagnostics), ("Open Logs Folder", lambda: self._open_path(DEFAULT_SETTINGS_PATH.parent))]),
        ])

    # ── controls ─────────────────────────────────────────────────────────────

    def _group(self, lay: QVBoxLayout, title: str, rows: list[QWidget]) -> None:
        box = QFrame()
        box.setObjectName("settingsGroup")
        box.setStyleSheet("#settingsGroup { background: #080d10; border: 1px solid #1e262c; border-radius: 6px; }")
        glay = QVBoxLayout(box)
        glay.setContentsMargins(12, 10, 12, 12)
        lbl = QLabel(title)
        lbl.setStyleSheet("color: #ffb000; font-size: 11px; font-weight: 900; letter-spacing: 1px;")
        glay.addWidget(lbl)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        for row in rows:
            if row.property("fullRow"):
                form.addRow(row)
            else:
                form.addRow(row.property("label") or "", row)
        glay.addLayout(form)
        lay.addWidget(box)

    def _value(self, path: str) -> Any:
        return self._working.get(path, include_env=False)

    def _set_value(self, path: str, value: Any) -> None:
        self._working.set(path, value)
        self._set_dirty(True)
        self._show_override(path)

    def _decorate(self, widget: QWidget, label: str, path: str | None = None, note: str | None = None, restart: str | None = None) -> QWidget:
        wrapper = QWidget()
        wrapper.setProperty("label", label)
        lay = QVBoxLayout(wrapper)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        lay.addWidget(widget)
        meta: list[str] = []
        if restart == "audio":
            meta.append("Requires audio restart")
        elif restart == "app":
            meta.append("Requires application restart")
        if note:
            meta.append(note)
        override = self._overrides.label_for(path) if path else ""
        if override:
            meta.append(override)
        if meta:
            m = QLabel(" · ".join(meta))
            m.setWordWrap(True)
            m.setStyleSheet("color: #7e8a92; font-size: 10px;")
            lay.addWidget(m)
        if path:
            self._controls[path] = widget
        return wrapper

    def _combo(self, label: str, path: str, values: list[Any], labels: dict[Any, str] | None = None, *, disabled: bool = False, note: str | None = None, restart: str | None = None, bool_labels: bool = False) -> QWidget:
        cb = QComboBox()
        if bool_labels:
            values = [True, False]
            labels = {True: "Enabled", False: "Disabled"}
        for value in values:
            cb.addItem((labels or {}).get(value, str(value)), value)
        cur = self._value(path)
        idx = cb.findData(cur)
        if idx < 0 and values:
            idx = 0
        cb.setCurrentIndex(idx)
        cb.setEnabled(not disabled)
        cb.currentIndexChanged.connect(lambda _=0, c=cb, p=path: self._set_value(p, c.currentData()))
        return self._decorate(cb, label, path, note, restart)

    def _check(self, label: str, path: str, *, disabled: bool = False, note: str | None = None, restart: str | None = None, live: bool = False) -> QWidget:
        chk = QCheckBox()
        chk.setChecked(bool(self._value(path)))
        chk.setEnabled(not disabled)
        chk.toggled.connect(lambda v, p=path: self._set_value(p, bool(v)))
        return self._decorate(chk, label, path, note, restart)

    def _slider(self, label: str, path: str, lo: int, hi: int, *, scale: float = 1.0, live: bool = False) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setValue(int(round(float(self._value(path) or 0.0) * scale)))
        value_lbl = QLabel(f"{s.value() / scale:.2f}")
        value_lbl.setMinimumWidth(48)
        s.valueChanged.connect(lambda v, p=path, l=value_lbl: (l.setText(f"{v / scale:.2f}"), self._set_value(p, v / scale)))
        row.addWidget(s, 1)
        row.addWidget(value_lbl)
        return self._decorate(w, label, path)

    def _spin(self, label: str, path: str, lo: int, hi: int, *, allow_none: bool = False, restart: str | None = None) -> QWidget:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        value = self._value(path)
        sp.setValue(lo if value is None else int(value))
        sp.valueChanged.connect(lambda v, p=path: self._set_value(p, int(v)))
        return self._decorate(sp, label, path, "0 means Auto" if allow_none else None, restart)

    def _path(self, label: str, path: str, *, disabled: bool = False, note: str | None = None, restart: str | None = None) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(str(self._value(path) or ""))
        edit.setEnabled(not disabled)
        edit.textChanged.connect(lambda text, p=path: self._set_value(p, text))
        browse = QPushButton("Browse")
        browse.setEnabled(not disabled)
        browse.clicked.connect(lambda _=False, e=edit: self._browse_into(e))
        reveal = QPushButton("Open")
        reveal.clicked.connect(lambda _=False, e=edit: self._open_path(Path(e.text()).expanduser()))
        row.addWidget(edit, 1)
        row.addWidget(browse)
        row.addWidget(reveal)
        return self._decorate(w, label, path, note, restart)

    def _readonly(self, label: str, text: str) -> QWidget:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {theme.TEXT_MED};")
        return self._decorate(lbl, label)

    def _actions_row(self, actions: list[tuple[str, Callable]]) -> QWidget:
        w = QWidget()
        w.setProperty("fullRow", True)
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        for text, callback in actions:
            b = QPushButton(text)
            b.clicked.connect(callback)
            row.addWidget(b)
        row.addStretch()
        return w

    # ── special widgets/actions ──────────────────────────────────────────────

    def _latency_label(self) -> QWidget:
        sr = int(self._value("audio.sample_rate") or 44100)
        bs = int(self._value("audio.buffer_size") or 256)
        return self._readonly("Estimated Callback Latency", f"{(bs / max(1, sr)) * 1000.0:.2f} ms")

    def _library_sources_widget(self) -> QWidget:
        w = QWidget()
        w.setProperty("fullRow", True)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["Name", "Path", "Type", "Status", "Last Scan"])
        roots = []
        try:
            roots = self._db.get_roots() if self._db is not None else []
        except Exception:
            roots = []
        table.setRowCount(len(roots))
        for row, root in enumerate(roots):
            p = Path(root.path)
            values = [root.label or p.name, str(p), "Local" if p.exists() else "External", "Available" if p.exists() else "Offline", root.last_scanned_at or "never"]
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(str(value)))
        table.setMinimumHeight(180)
        lay.addWidget(table)
        lay.addWidget(self._actions_row([("Add Local Folder", self._add_library_root), ("Remove Source", self._remove_library_root), ("Rescan Source", self._not_yet), ("Relink Offline Source", self._not_yet)]))
        note = QLabel("Source offline is informative, not fatal: valid .wrk tracks remain playable from prepared media.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(note)
        return w

    def _storage_dashboard(self) -> QWidget:
        roots = [
            ("WREKKED .wrk", Path(str(self._value("storage.wrekked_root") or ""))),
            ("Fastload", Path(str(self._value("storage.fastload_cache_root") or ""))),
            ("Temp stems", Path(str(self._value("storage.temp_stem_cache_root") or ""))),
        ]
        lines = []
        total = 0
        for label, path in roots:
            size = self._dir_size(path)
            total += size
            free = self._free_space(path)
            lines.append(f"{label}: {self._fmt_bytes(size)} used · {self._fmt_bytes(free)} free · {path}")
        lines.append(f"Total Wrekker storage footprint: {self._fmt_bytes(total)}")
        return self._readonly("Storage Usage", "\n".join(lines))

    def _profiles_current_id(self) -> str:
        return self._working.active_profile_id

    def _refresh_profiles_table(self) -> None:
        if not hasattr(self, "_profiles_table"):
            return
        profiles = list(self._working.profiles.values())
        self._profiles_table.setRowCount(len(profiles))
        for row, profile in enumerate(profiles):
            values = [
                "Yes" if profile.id == self._working.active_profile_id else "",
                "Yes" if profile.id == self._working.startup_profile_id else "",
                profile.name,
                "Protected" if profile.protected else "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, profile.id)
                self._profiles_table.setItem(row, col, item)

    # ── profile actions ──────────────────────────────────────────────────────

    def _populate_profiles(self) -> None:
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for pid, profile in self._working.profiles.items():
            self._profile_combo.addItem(profile.name, pid)
        idx = self._profile_combo.findData(self._working.active_profile_id)
        self._profile_combo.setCurrentIndex(max(0, idx))
        self._profile_combo.blockSignals(False)
        self._refresh_profiles_table()

    def _on_profile_combo(self, _idx: int) -> None:
        pid = self._profile_combo.currentData()
        if pid and pid != self._working.active_profile_id:
            self._working.select_profile(pid)
            self._set_dirty(True)
            self._rebuild_pages()

    def _create_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "Create Profile", "Profile name:")
        if ok and name.strip():
            pid = self._working.create_profile(name.strip())
            self._working.select_profile(pid)
            self._populate_profiles()
            self._rebuild_pages()
            self._set_dirty(True)

    def _duplicate_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "Duplicate Profile", "New profile name:", text=f"{self._working.active_profile.name} Copy")
        if ok and name.strip():
            pid = self._working.duplicate_profile(self._working.active_profile_id, name.strip())
            self._working.select_profile(pid)
            self._populate_profiles()
            self._rebuild_pages()
            self._set_dirty(True)

    def _rename_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "Rename Profile", "Profile name:", text=self._working.active_profile.name)
        if ok and name.strip():
            self._working.rename_profile(self._working.active_profile_id, name.strip())
            self._populate_profiles()
            self._set_dirty(True)

    def _delete_profile(self) -> None:
        profile = self._working.active_profile
        if profile.protected:
            QMessageBox.information(self, "Delete Profile", "The Default profile is protected.")
            return
        if QMessageBox.question(self, "Delete Profile", f"Delete profile '{profile.name}'?") == QMessageBox.StandardButton.Yes:
            self._working.delete_profile(profile.id)
            self._populate_profiles()
            self._rebuild_pages()
            self._set_dirty(True)

    def _set_startup_profile(self) -> None:
        self._working.set_startup_profile(self._working.active_profile_id)
        self._refresh_profiles_table()
        self._set_dirty(True)

    def _export_profile(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Profile", str(Path.home() / f"{self._working.active_profile.name}.wrekker-profile.json"), "JSON (*.json)")
        if path:
            self._working.export_profile(self._working.active_profile_id, path)

    def _import_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Profile", str(Path.home()), "JSON (*.json)")
        if path:
            try:
                pid = self._working.import_profile(path)
                self._working.select_profile(pid)
                self._populate_profiles()
                self._rebuild_pages()
                self._set_dirty(True)
            except Exception as exc:
                QMessageBox.warning(self, "Import Profile", str(exc))

    # ── apply/save/reset ─────────────────────────────────────────────────────

    def _apply(self) -> None:
        validation = self._working.validate()
        if not validation.valid:
            QMessageBox.warning(self, "Settings Validation", "\n".join(validation.errors))
            return
        impact = self._impact_summary()
        QMessageBox.information(self, "Apply Settings", impact)
        self._apply_live_safe()

    def _save(self) -> None:
        validation = self._working.validate()
        if not validation.valid:
            QMessageBox.warning(self, "Settings Validation", "\n".join(validation.errors))
            return
        self._store.schema_version = self._working.schema_version
        self._store.active_profile_id = self._working.active_profile_id
        self._store.startup_profile_id = self._working.startup_profile_id
        self._store.profiles = deepcopy(self._working.profiles)
        self._store.save()
        self._store.apply_environment_defaults()
        self._store.sync_prepared_db_settings(self._prepared_db)
        self._apply_live_safe()
        self._set_dirty(False)
        self.settings_saved.emit()
        QMessageBox.information(self, "Settings Saved", self._impact_summary())

    def _apply_live_safe(self) -> None:
        if self._engine is not None:
            try:
                self._engine.set_headphone_mix(float(self._value("audio.headphone_mix")))
                self._engine.set_headphone_level(float(self._value("audio.headphone_level")))
                self._engine.set_master_gain(float(self._value("playback.default_master_gain")))
            except Exception:
                pass

    def _impact_summary(self) -> str:
        return (
            "Safe UI/default changes are applied now where supported.\n\n"
            "Audio device, sample rate, buffer, routing, renderer and diagnostics changes are saved for the next startup or require an audio/application restart. "
            "Wrekker will not stop playback silently."
        )

    def _reset_section(self) -> None:
        item = self._nav.currentItem()
        if not item:
            return
        section = item.data(Qt.ItemDataRole.UserRole)
        if section == "wrekked":
            section = "storage"
        if QMessageBox.question(self, "Reset Section", f"Reset {item.text()} to defaults?") == QMessageBox.StandardButton.Yes:
            self._working.reset_section(section)
            self._rebuild_pages()
            self._set_dirty(True)

    def _reset_all(self) -> None:
        if QMessageBox.question(self, "Restore Defaults", "Reset the active profile to default settings?") == QMessageBox.StandardButton.Yes:
            self._working.reset_all()
            self._rebuild_pages()
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = bool(dirty)
        self._dirty_label.setText("UNSAVED" if dirty else "")
        self._summary.setText(f"Config: {self._store.path} · Active profile: {self._working.active_profile.name}")

    def _rebuild_pages(self) -> None:
        current = self._nav.currentItem().data(Qt.ItemDataRole.UserRole) if self._nav.currentItem() else "audio"
        while self._stack.count():
            widget = self._stack.widget(0)
            self._stack.removeWidget(widget)
            widget.deleteLater()
        self._controls.clear()
        self._section_pages.clear()
        for key, label, desc in self._SECTIONS:
            page = self._make_page(key, label, desc)
            self._section_pages[key] = page
            self._stack.addWidget(page)
        self._select_section(current)

    # ── utility actions ──────────────────────────────────────────────────────

    def _select_section(self, key: str) -> None:
        for i in range(self._nav.count()):
            if self._nav.item(i).data(Qt.ItemDataRole.UserRole) == key:
                self._nav.setCurrentRow(i)
                self._stack.setCurrentIndex(i)
                return

    def _filter_sections(self, text: str) -> None:
        q = text.strip().lower()
        for i, (key, label, desc) in enumerate(self._SECTIONS):
            hay = f"{label} {desc} {key}".lower()
            self._nav.item(i).setHidden(bool(q) and q not in hay)

    def _browse_into(self, edit: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose Folder", edit.text() or str(Path.home()))
        if folder:
            edit.setText(folder)

    def _open_path(self, path: Path) -> None:
        try:
            p = Path(path).expanduser()
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
        except Exception as exc:
            QMessageBox.warning(self, "Open Folder", str(exc))

    def _call_with_timeout(self, fn: Callable[[], list[str]], fallback: list[str], timeout_s: float = 0.75) -> list[str]:
        result: list[list[str]] = []

        def runner() -> None:
            try:
                result.append(fn())
            except Exception:
                result.append([])

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout_s)
        if not result:
            return fallback
        values = [v for v in result[0] if str(v).strip()]
        return values or fallback

    def _audio_devices(self) -> list[str]:
        def scan() -> list[str]:
            import sounddevice as sd

            devices = ["System Default"]
            for idx, dev in enumerate(sd.query_devices()):
                try:
                    out_ch = int(dev.get("max_output_channels", 0))
                except Exception:
                    out_ch = 0
                if out_ch <= 0:
                    continue
                name = str(dev.get("name") or f"Output {idx}")
                label = f"{name} ({out_ch} ch)"
                if label not in devices:
                    devices.append(label)
            return devices

        return self._call_with_timeout(scan, ["System Default"])

    def _cue_devices(self) -> list[str]:
        def scan() -> list[str]:
            import sounddevice as sd

            devices = ["Auto multichannel / FLX4"]
            for idx, dev in enumerate(sd.query_devices()):
                try:
                    out_ch = int(dev.get("max_output_channels", 0))
                except Exception:
                    out_ch = 0
                if out_ch < 4:
                    continue
                name = str(dev.get("name") or f"Output {idx}")
                label = f"{name} ({out_ch} ch)"
                if label not in devices:
                    devices.append(label)
            return devices

        return self._call_with_timeout(scan, ["Auto multichannel / FLX4"])

    def _cue_channel_pairs(self) -> list[str]:
        return ["2/3"]

    def _midi_ports(self, direction: str) -> list[str]:
        def scan() -> list[str]:
            import mido

            names = mido.get_input_names() if direction == "input" else mido.get_output_names()
            ports = ["Auto"]
            for name in names:
                text = str(name)
                if text not in ports:
                    ports.append(text)
            return ports

        return self._call_with_timeout(scan, ["Auto"])

    def _show_override(self, _path: str) -> None:
        pass

    def _test_unavailable(self) -> None:
        QMessageBox.information(self, "Audio Test", "Direct test tones are not exposed by the current Rust engine API yet. Routing status is shown without interrupting playback.")

    def _not_yet(self) -> None:
        QMessageBox.information(self, "Not Yet Available", "This action is reserved in Settings but is not implemented safely in the current runtime path.")

    def _add_library_root(self) -> None:
        if self._db is None:
            self._not_yet()
            return
        folder = QFileDialog.getExistingDirectory(self, "Add Music Source Folder", str(Path.home()))
        if folder:
            self._db.add_root(Path(folder))
            self._rebuild_pages()

    def _remove_library_root(self) -> None:
        QMessageBox.information(self, "Remove Source", "Use WREKKED management for row-specific removal until the Settings source table has selection actions.")

    def _clear_temp_stem_cache(self) -> None:
        root = Path(str(self._value("storage.temp_stem_cache_root") or "")).expanduser()
        if QMessageBox.question(self, "Clear Temporary Stem Cache", f"Delete temporary stem cache at:\n{root}\n\n.wrk files and source audio are preserved.") != QMessageBox.StandardButton.Yes:
            return
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        QMessageBox.information(self, "Temporary Stem Cache", "Temporary stem cache cleared. Persistent .wrk files were not touched.")

    def _confirm_fastload_clean(self) -> None:
        root = Path(str(self._value("storage.fastload_cache_root") or "")).expanduser()
        if QMessageBox.question(self, "Clean Fastload Cache", f"Delete fastload cache at:\n{root}\n\nPersistent .wrk files are preserved.") != QMessageBox.StandardButton.Yes:
            return
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        QMessageBox.information(self, "Fastload Cache", "Fastload cache cleared. Persistent .wrk files were not touched.")

    def _validate_wrks(self) -> None:
        root = Path(str(self._value("storage.wrekked_root") or "")).expanduser()
        count = len(list(root.rglob("*.wrk"))) if root.exists() else 0
        QMessageBox.information(self, "Validate .wrk Files", f"Found {count} .wrk file(s). Deep validation remains in WREKKED management.")

    def _export_settings(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Settings", str(Path.home() / "wrekker-settings.json"), "JSON (*.json)")
        if path:
            temp = SettingsStore()
            temp.schema_version = self._working.schema_version
            temp.active_profile_id = self._working.active_profile_id
            temp.startup_profile_id = self._working.startup_profile_id
            temp.profiles = deepcopy(self._working.profiles)
            temp.export_settings(path)

    def _import_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Settings", str(Path.home()), "JSON (*.json)")
        if path:
            result = self._working.import_settings(path)
            if result.valid:
                self._populate_profiles()
                self._rebuild_pages()
                self._set_dirty(True)
            else:
                QMessageBox.warning(self, "Import Settings", "\n".join(result.errors))

    def _copy_overrides(self) -> None:
        QGuiApplication.clipboard().setText(self._overrides_text() or "No runtime overrides")

    def _copy_diagnostics(self) -> None:
        QGuiApplication.clipboard().setText(self._diagnostics_text())

    def _overrides_text(self) -> str:
        lines = []
        for path, env in self._overrides.values.items():
            lines.append(f"{path}: {env}={os.environ.get(env, '')}")
        return "\n".join(lines)

    def _diagnostics_text(self) -> str:
        return "\n".join([
            f"Wrekker version: {QGuiApplication.applicationVersion() or 'unknown'}",
            f"Settings profile: {self._working.active_profile.name}",
            f"Config: {self._store.path}",
            f"Audio running: {getattr(self._engine, '_running', False)}",
            f"Controller: {'DDJ-FLX4' if self._flx4 is not None else 'not connected'}",
            f"Waveform renderer: {self._value('waveforms.deck_renderer')}",
            f"Fastload root: {self._value('storage.fastload_cache_root')}",
            f"WREKKED root: {self._value('storage.wrekked_root')}",
            "Overrides:",
            self._overrides_text() or "none",
        ])

    @staticmethod
    def _dir_size(path: Path) -> int:
        try:
            if not path.exists():
                return 0
            total = 0
            count = 0
            for p in path.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
                    count += 1
                    if count >= 5000:
                        break
            return total
        except Exception:
            return 0

    @staticmethod
    def _free_space(path: Path) -> int:
        try:
            p = path if path.exists() else path.parent
            return shutil.disk_usage(p).free
        except Exception:
            return 0

    @staticmethod
    def _fmt_bytes(n: int) -> str:
        value = float(max(0, n))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024.0 or unit == "TB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024.0
