"""Styled WREKKED library manager."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QTableView, QTextEdit, QVBoxLayout, QWidget,
)

from wrekker.config.paths import data_dir, fastload_dir
from wrekker.library.prepared_db import PreparedDB, PreparedSet, WrekkedTrack
from wrekker.library.models import LibraryTrack, SearchQuery
from wrekker.ui import theme

__all__ = ["ManageWrekkedLibraryDialog", "MetadataEditDialog", "WrekkedPathSettingsDialog"]


def _size_str(n: int) -> str:
    units = ("B", "KB", "MB", "GB")
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return f"{n} B"


def _status_for(t: WrekkedTrack, fastload: bool) -> tuple[str, str, str, str]:
    wrk = "WRK READY" if t.wrk_ready and Path(t.wrk_path).exists() else "BROKEN"
    stems = "STEMS READY" if t.stems_ready else "NO STEMS"
    fl = "FASTLOAD READY" if fastload else "NO CACHE"
    src = "SOURCE OK" if t.source_available else "SOURCE OFFLINE"
    return wrk, stems, fl, src


class _TrackModel(QAbstractTableModel):
    _COLS = ["Title", "Artist", "BPM", "Key", "WRK", "STEMS", "FASTLOAD", "Source", "Dur", "..."]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tracks: list[WrekkedTrack] = []
        self.fastload: dict[str, bool] = {}

    def set_tracks(self, tracks: list[WrekkedTrack], fastload: dict[str, bool] | None = None) -> None:
        self.beginResetModel()
        self.tracks = tracks
        self.fastload = fastload or {}
        self.endResetModel()

    def set_fastload(self, fastload: dict[str, bool]) -> None:
        self.fastload = fastload
        if self.tracks:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.tracks) - 1, len(self._COLS) - 1))

    def track_at(self, row: int) -> WrekkedTrack | None:
        return self.tracks[row] if 0 <= row < len(self.tracks) else None

    def rowCount(self, _=QModelIndex()) -> int:
        return len(self.tracks)

    def columnCount(self, _=QModelIndex()) -> int:
        return len(self._COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self._COLS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        t = self.tracks[index.row()]
        fl = self.fastload.get(t.wrk_path, False)
        wrk, stems, fastload, source = _status_for(t, fl)
        if role == Qt.ItemDataRole.DisplayRole:
            m, s = divmod(int(t.duration_s), 60)
            return [
                t.title or Path(t.wrk_path).stem,
                t.artist or "",
                f"{t.bpm:.1f}" if t.bpm else "",
                t.key or "",
                wrk, stems, fastload, source, f"{m}:{s:02d}", "...",
            ][index.column()]
        if role == Qt.ItemDataRole.ForegroundRole:
            val = self.data(index, Qt.ItemDataRole.DisplayRole) or ""
            if "BROKEN" in val or "OFFLINE" in val:
                return QColor(theme.STATUS_ERR if "BROKEN" in val else theme.STATUS_WARN)
            if "READY" in val:
                return QColor(theme.STATUS_OK if "FASTLOAD" in val or "WRK" in val else theme.DECK_A)
            if "NO CACHE" in val:
                return QColor(theme.STATUS_WARN)
            return QColor(theme.TEXT_BRIGHT if index.column() == 0 else theme.TEXT_MED)
        if role == Qt.ItemDataRole.FontRole and 4 <= index.column() <= 7:
            f = QFont()
            f.setPointSize(8)
            f.setBold(True)
            return f
        if role == Qt.ItemDataRole.UserRole:
            return t
        return None


class _LibrarySearchModel(QAbstractTableModel):
    _COLS = ["Title", "Artist", "BPM", "Key", "Prepared", "Source"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tracks: list[LibraryTrack] = []
        self.statuses: dict[str, object] = {}

    def set_results(self, tracks: list[LibraryTrack], statuses: dict[str, object]) -> None:
        self.beginResetModel()
        self.tracks = tracks
        self.statuses = statuses
        self.endResetModel()

    def track_at(self, row: int) -> LibraryTrack | None:
        return self.tracks[row] if 0 <= row < len(self.tracks) else None

    def rowCount(self, _=QModelIndex()) -> int:
        return len(self.tracks)

    def columnCount(self, _=QModelIndex()) -> int:
        return len(self._COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self._COLS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        t = self.tracks[index.row()]
        rec = self.statuses.get(str(t.path.absolute()))
        prepared = "WRK READY" if rec and rec.wrk_ready and Path(rec.wrk_path).exists() else "NOT PREPARED"
        if role == Qt.ItemDataRole.DisplayRole:
            return [
                t.display_title,
                t.display_artist,
                f"{t.bpm:.1f}" if t.bpm else "",
                t.key_str,
                prepared,
                "SOURCE OK" if t.path.exists() else "SOURCE MISSING",
            ][index.column()]
        if role == Qt.ItemDataRole.ForegroundRole:
            if index.column() == 4:
                return QColor(theme.STATUS_OK if prepared == "WRK READY" else theme.STATUS_WARN)
            if index.column() == 5 and not t.path.exists():
                return QColor(theme.STATUS_ERR)
            return QColor(theme.TEXT_BRIGHT if index.column() == 0 else theme.TEXT_MED)
        if role == Qt.ItemDataRole.UserRole:
            return t
        return None


class MetadataEditDialog(QDialog):
    def __init__(self, db: PreparedDB, track: WrekkedTrack, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self._track = track
        self.setWindowTitle("Edit Metadata")
        self.setMinimumWidth(420)
        self.setStyleSheet(theme.GLOBAL_SS)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.title = QLineEdit(track.title or "")
        self.artist = QLineEdit(track.artist or "")
        self.album = QLineEdit("")
        self.genre = QLineEdit("")
        self.comments = QTextEdit()
        self.comments.setFixedHeight(70)
        self.bpm = QLineEdit(f"{track.bpm:.2f}" if track.bpm else "")
        self.key = QLineEdit(track.key or "")
        for label, widget in (
            ("Title", self.title), ("Artist", self.artist), ("Album", self.album),
            ("Genre", self.genre), ("Comments", self.comments),
            ("BPM override", self.bpm), ("Key override", self.key),
        ):
            form.addRow(label, widget)
        lay.addLayout(form)
        note = QLabel("PreparedDB display metadata is the source of truth here; the original source file is not required.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 10px;")
        lay.addWidget(note)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        save = QPushButton("Save")
        save.setStyleSheet(f"color: {theme.STATUS_OK}; border-color: {theme.STATUS_OK}66;")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(save)
        lay.addLayout(row)

    def _save(self) -> None:
        try:
            bpm = float(self.bpm.text()) if self.bpm.text().strip() else None
        except ValueError:
            QMessageBox.warning(self, "Invalid BPM", "BPM must be a number.")
            return
        self._db.update_track_metadata(
            self._track.wrk_id,
            title=self.title.text().strip(),
            artist=self.artist.text().strip(),
            bpm=bpm,
            key=self.key.text().strip() or None,
        )
        self.accept()


class WrekkedPathSettingsDialog(QDialog):
    _PATH_KEYS = [
        ("wrekked_library_path", "WREKKED Library Path"),
        ("wrk_storage_path", ".wrk Storage Path"),
        ("fastload_cache_path", "Fastload Cache Path"),
        ("temporary_stem_cache_path", "Temporary Stem Cache Path"),
        ("backup_export_path", "Backup/Export Location"),
    ]
    _PATH_DEFAULTS: dict[str, str] = {
        "wrekked_library_path":      str(data_dir() / "prepared"),
        "wrk_storage_path":          str(data_dir() / "prepared" / "tracks"),
        "fastload_cache_path":       str(fastload_dir()),
        "temporary_stem_cache_path": str(Path("/tmp") / "wrekker-stems"),
        "backup_export_path":        "",
    }

    def __init__(self, db: PreparedDB, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("Library Options")
        self.setMinimumWidth(740)
        self.setStyleSheet(theme.GLOBAL_SS)
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        # ── Music Sources ──────────────────────────────────────────────────────
        lay.addWidget(self._section_lbl("MUSIC SOURCES"))
        self._roots_list = QListWidget()
        self._roots_list.setMaximumHeight(110)
        self._roots_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        lay.addWidget(self._roots_list)
        self._load_roots()
        src_row = QHBoxLayout()
        add_btn    = QPushButton("Add Folder")
        remove_btn = QPushButton("Remove Selected")
        reveal_src = QPushButton("Reveal")
        add_btn.clicked.connect(self._add_root)
        remove_btn.clicked.connect(self._remove_root)
        reveal_src.clicked.connect(self._reveal_root)
        src_row.addWidget(add_btn)
        src_row.addWidget(remove_btn)
        src_row.addWidget(reveal_src)
        src_row.addStretch()
        lay.addLayout(src_row)

        # ── Source Mode ────────────────────────────────────────────────────────
        lay.addWidget(self._section_lbl("SOURCE MODE"))
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Music Source Mode"))
        self._source_mode = QComboBox()
        self._source_mode.addItems(["Local Folders", "SMB Network Share"])
        saved_mode = self._db.get_setting("source_mode", "Local Folders") or "Local Folders"
        self._source_mode.setCurrentText(saved_mode)
        self._source_mode.currentTextChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._source_mode)
        mode_row.addStretch()
        lay.addLayout(mode_row)

        # ── SMB Configuration (shown only in SMB mode) ─────────────────────────
        self._smb_widget = QWidget()
        smb_lay = QVBoxLayout(self._smb_widget)
        smb_lay.setContentsMargins(0, 4, 0, 0)
        smb_lay.addWidget(self._section_lbl("SMB CONFIGURATION"))
        smb_form = QFormLayout()
        self._smb_host  = QLineEdit(); self._smb_host.setPlaceholderText("e.g. 192.168.1.10 or myserver")
        self._smb_share = QLineEdit(); self._smb_share.setPlaceholderText("e.g. music")
        self._smb_user  = QLineEdit()
        self._smb_pass  = QLineEdit(); self._smb_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._smb_domain = QLineEdit(); self._smb_domain.setPlaceholderText("optional")
        self._smb_mount  = QLineEdit(); self._smb_mount.setPlaceholderText("optional — leave blank for GVFS auto-mount")
        smb_form.addRow("Host / IP",    self._smb_host)
        smb_form.addRow("Share Name",   self._smb_share)
        smb_form.addRow("Username",     self._smb_user)
        smb_form.addRow("Password",     self._smb_pass)
        smb_form.addRow("Domain",       self._smb_domain)
        smb_form.addRow("Mount Point",  self._smb_mount)
        smb_lay.addLayout(smb_form)
        test_row = QHBoxLayout()
        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self._test_smb)
        test_row.addWidget(test_btn)
        test_row.addStretch()
        smb_lay.addLayout(test_row)
        lay.addWidget(self._smb_widget)
        self._load_smb_fields()

        # ── Storage Paths ──────────────────────────────────────────────────────
        lay.addWidget(self._section_lbl("STORAGE PATHS"))
        self._path_rows: dict[str, QLineEdit] = {}
        paths_lay = QVBoxLayout()
        for key, label in self._PATH_KEYS:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setMinimumWidth(190)
            edit = QLineEdit(self._db.get_setting(key, self._PATH_DEFAULTS.get(key, "")) or "")
            self._path_rows[key] = edit
            browse_btn = QPushButton("Browse")
            reveal_btn = QPushButton("Reveal")
            browse_btn.clicked.connect(lambda _=False, k=key: self._browse(k))
            reveal_btn.clicked.connect(lambda _=False, k=key: self._reveal_path(Path(self._path_rows[k].text()).expanduser()))
            row.addWidget(lbl, 0)
            row.addWidget(edit, 1)
            row.addWidget(browse_btn)
            row.addWidget(reveal_btn)
            paths_lay.addLayout(row)
        lay.addLayout(paths_lay)

        # ── Fastload Options ───────────────────────────────────────────────────
        lay.addWidget(self._section_lbl("FASTLOAD"))
        self._fastload_mode = QComboBox()
        self._fastload_mode.addItems(["Disabled", "Mix Only", "WREKKED Sets", "Selected Tracks", "Auto/LRU"])
        self._fastload_mode.setCurrentText(self._db.get_setting("fastload_mode", "Mix Only") or "Mix Only")
        self._fastload_fmt = QComboBox()
        self._fastload_fmt.addItems(["PCM16", "F32"])
        self._fastload_fmt.setCurrentText(self._db.get_setting("fastload_format", "PCM16") or "PCM16")
        fl_form = QFormLayout()
        fl_form.addRow("Fastload Mode",   self._fastload_mode)
        fl_form.addRow("Fastload Format", self._fastload_fmt)
        lay.addLayout(fl_form)

        # ── Status + Buttons ───────────────────────────────────────────────────
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self._status)
        btn_row = QHBoxLayout()
        validate_btn = QPushButton("Validate")
        save_btn = QPushButton("Save")
        save_btn.setStyleSheet(f"color: {theme.STATUS_OK}; border-color: {theme.STATUS_OK}66;")
        validate_btn.clicked.connect(self._validate)
        save_btn.clicked.connect(self._save)
        btn_row.addStretch()
        btn_row.addWidget(validate_btn)
        btn_row.addWidget(save_btn)
        lay.addLayout(btn_row)

        self._on_mode_changed(saved_mode)

    # ── helpers ────────────────────────────────────────────────────────────────

    def _section_lbl(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {theme.DECK_A}; font-size: 9px; font-weight: 800; "
            f"letter-spacing: 1px; margin-top: 6px;"
        )
        return lbl

    # ── music sources ──────────────────────────────────────────────────────────

    def _load_roots(self) -> None:
        self._roots_list.clear()
        try:
            roots = self._db.get_roots()
        except Exception:
            roots = []
        for r in roots:
            scanned = r.last_scanned_at or "never"
            item = QListWidgetItem(f"{r.label or Path(r.path).name}  —  {r.path}  (scanned: {scanned})")
            item.setData(Qt.ItemDataRole.UserRole, r.path)
            item.setForeground(QColor(theme.TEXT_MED if r.enabled else theme.TEXT_DIM))
            self._roots_list.addItem(item)

    def _add_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add Music Source Folder", str(Path.home()))
        if folder:
            self._db.add_root(Path(folder))
            self._load_roots()

    def _remove_root(self) -> None:
        item = self._roots_list.currentItem()
        if not item:
            return
        path_str = item.data(Qt.ItemDataRole.UserRole)
        if path_str and QMessageBox.question(
            self, "Remove Root",
            f"Remove '{path_str}' from library sources?\nTracks already in the DB are kept.",
        ) == QMessageBox.StandardButton.Yes:
            self._db.remove_root(Path(path_str))
            self._load_roots()

    def _reveal_root(self) -> None:
        item = self._roots_list.currentItem()
        if item:
            self._reveal_path(Path(item.data(Qt.ItemDataRole.UserRole)))

    # ── source mode / SMB ──────────────────────────────────────────────────────

    def _on_mode_changed(self, mode: str) -> None:
        self._smb_widget.setVisible(mode == "SMB Network Share")

    def _load_smb_fields(self) -> None:
        from wrekker.library.smb import load_smb_config
        shares = load_smb_config()
        if not shares:
            return
        s = next(iter(shares.values()))
        self._smb_host.setText(s.host)
        self._smb_share.setText(s.share)
        self._smb_user.setText(s.username)
        self._smb_pass.setText(s.password)
        self._smb_domain.setText(s.domain)
        self._smb_mount.setText(str(s.mount_point) if s.mount_point else "")

    def _test_smb(self) -> None:
        from wrekker.library.smb import SMBShare
        host  = self._smb_host.text().strip()
        share = self._smb_share.text().strip()
        if not host or not share:
            QMessageBox.warning(self, "SMB Test", "Host and Share are required.")
            return
        self._status.setText("Testing SMB connection…")
        mp_text = self._smb_mount.text().strip()
        s = SMBShare(
            host=host, share=share,
            username=self._smb_user.text().strip(),
            password=self._smb_pass.text(),
            domain=self._smb_domain.text().strip(),
            mount_point=Path(mp_text).expanduser() if mp_text else None,
        )
        try:
            existing = s.local_path()
            if existing:
                self._set_status(f"Already mounted at {existing}", ok=True)
            else:
                path = s.mount()
                self._set_status(f"Mounted at {path}", ok=True)
        except Exception as e:
            self._set_status(f"SMB test failed: {e}", ok=False)

    # ── storage paths ──────────────────────────────────────────────────────────

    def _browse(self, key: str) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Folder", self._path_rows[key].text() or str(Path.home())
        )
        if folder:
            self._path_rows[key].setText(folder)

    def _reveal_path(self, path: Path) -> None:
        if path:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])

    # ── validate / save ────────────────────────────────────────────────────────

    def _validate(self) -> bool:
        bad: list[str] = []
        for key, edit in self._path_rows.items():
            text = edit.text().strip()
            if not text:
                continue
            p = Path(text).expanduser()
            if not p.exists():
                bad.append(f"{key}: missing")
            elif not p.is_dir():
                bad.append(f"{key}: not a folder")
        msg = " · ".join(bad) if bad else "All existing paths validated."
        self._set_status(msg, ok=not bad)
        return not bad

    def _save(self) -> None:
        for key, edit in self._path_rows.items():
            value = edit.text().strip()
            if value:
                self._db.set_setting(key, value)
                if key == "fastload_cache_path":
                    os.environ["WREKKER_FASTLOAD_CACHE"] = value
        self._db.set_setting("fastload_mode",   self._fastload_mode.currentText())
        self._db.set_setting("fastload_format",  self._fastload_fmt.currentText())
        self._db.set_setting("source_mode",      self._source_mode.currentText())
        if self._source_mode.currentText() == "SMB Network Share":
            from wrekker.library.smb import save_smb_config
            save_smb_config(
                host=self._smb_host.text().strip(),
                share=self._smb_share.text().strip(),
                username=self._smb_user.text().strip(),
                password=self._smb_pass.text(),
                domain=self._smb_domain.text().strip(),
                mount_point=self._smb_mount.text().strip(),
            )
        self.accept()

    def _set_status(self, text: str, ok: bool = True) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(
            f"color: {theme.STATUS_OK if ok else theme.STATUS_ERR};"
        )


class ManageWrekkedLibraryDialog(QDialog):
    _fastload_ready = pyqtSignal(dict)
    _filtered_tracks_ready = pyqtSignal(object, object)
    _refresh_tracks_requested = pyqtSignal()
    _progress = pyqtSignal(str)
    prepare_and_add_tracks = pyqtSignal(object, int)
    open_lab_requested = pyqtSignal(str)

    def __init__(self, db: PreparedDB, library_db=None, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self._library_db = library_db
        self._sets: list[PreparedSet] = []
        self._active_set_id: int | None = None
        self._library_scope_paths: list[Path] = []
        self.setWindowTitle("Manage WREKKED Library")
        self.setMinimumSize(980, 620)
        self.setStyleSheet(theme.GLOBAL_SS)
        self._fastload_ready.connect(self._on_fastload_ready)
        self._refresh_tracks_requested.connect(self._load_tracks)
        self._progress.connect(self._set_status)
        self._build_ui()
        self._filtered_tracks_ready.connect(self._model.set_tracks)
        self._load_sets()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        top = QHBoxLayout()
        title = QLabel("Manage WREKKED Library")
        title.setStyleSheet(f"color: {theme.STATUS_WARN}; font-size: 14px; font-weight: 800; letter-spacing: 1px;")
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search prepared tracks")
        self._search.textChanged.connect(self._load_tracks)
        for label, cb in (
            ("New Set", self._new_set), ("Rename", self._rename_set),
            ("Fastload Set", lambda: self._fastload_selected_set(False)),
            ("Validate", self._validate_selected), ("Settings", self._open_settings),
        ):
            b = QPushButton(label)
            b.clicked.connect(cb)
            top.addWidget(b)
        top.insertWidget(0, self._search, 1)
        top.insertWidget(0, title)
        root.addLayout(top)
        body = QHBoxLayout()
        self._set_list = QListWidget()
        self._set_list.setMaximumWidth(240)
        self._set_list.currentRowChanged.connect(self._on_set_row)
        self._set_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._set_list.customContextMenuRequested.connect(self._set_menu)
        body.addWidget(self._set_list)
        right = QVBoxLayout()
        lib_box = QWidget()
        lib_box.setStyleSheet(f"background: {theme.BG_PANEL}; border: 1px solid {theme.BORDER};")
        lib_lay = QVBoxLayout(lib_box)
        lib_lay.setContentsMargins(8, 6, 8, 8)
        lib_lay.setSpacing(6)
        lib_top = QHBoxLayout()
        lib_lbl = QLabel("GENERAL LIBRARY")
        lib_lbl.setStyleSheet(f"color: {theme.DECK_A}; font-size: 9px; font-weight: 800; letter-spacing: 1px;")
        self._library_search = QLineEdit()
        self._library_search.setPlaceholderText("Search full library to add tracks to the selected WREKKED set")
        self._library_search.textChanged.connect(self._search_library)
        self._add_library_btn = QPushButton("Add to Set")
        self._add_library_btn.clicked.connect(self._add_library_results_to_set)
        self._add_library_btn.setEnabled(self._library_db is not None)
        self._import_scope_btn = QPushButton("Import Scope")
        self._import_scope_btn.clicked.connect(self._import_library_scope_as_set)
        self._import_scope_btn.setEnabled(False)
        lib_top.addWidget(lib_lbl)
        lib_top.addWidget(self._library_search, 1)
        lib_top.addWidget(self._add_library_btn)
        lib_top.addWidget(self._import_scope_btn)
        lib_lay.addLayout(lib_top)
        self._library_scope = QComboBox()
        self._library_scope.currentIndexChanged.connect(self._search_library)
        lib_lay.addWidget(self._library_scope)
        self._library_model = _LibrarySearchModel(self)
        self._library_table = QTableView()
        self._library_table.setModel(self._library_model)
        self._library_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._library_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._library_table.verticalHeader().setVisible(False)
        self._library_table.setMaximumHeight(150)
        self._library_table.horizontalHeader().setDefaultSectionSize(92)
        self._library_table.setColumnWidth(0, 180)
        self._library_table.setColumnWidth(1, 140)
        self._library_table.setColumnWidth(4, 110)
        lib_lay.addWidget(self._library_table)
        if self._library_db is None:
            self._library_search.setEnabled(False)
            self._library_search.setPlaceholderText("General library search unavailable in this context")
        self._populate_library_scopes()
        right.addWidget(lib_box)
        self._model = _TrackModel(self)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._track_menu)
        self._table.doubleClicked.connect(lambda i: self._edit_track(self._model.track_at(i.row())))
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setDefaultSectionSize(86)
        self._table.setColumnWidth(0, 180)
        self._table.setColumnWidth(1, 150)
        self._table.setColumnWidth(9, 34)
        right.addWidget(self._table, 1)
        self._details = QLabel("")
        self._details.setWordWrap(True)
        self._details.setStyleSheet(f"background: {theme.BG_CARD}; color: {theme.TEXT_MED}; border: 1px solid {theme.BORDER}; padding: 8px;")
        right.addWidget(self._details)
        self._table.selectionModel().selectionChanged.connect(lambda *_: self._update_details())
        body.addLayout(right, 1)
        root.addLayout(body, 1)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        root.addWidget(self._status)

    def _load_sets(self) -> None:
        self._sets = self._db.list_wrekked_sets()
        self._set_list.blockSignals(True)
        self._set_list.clear()
        for label, data in (("All Prepared Tracks", None), ("Recently Prepared", "__recent__"), ("Source Offline", "__offline__"), ("No Fastload Cache", "__nocache__"), ("Broken Items", "__broken__")):
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, data)
            it.setForeground(QColor(theme.TEXT_DIM))
            self._set_list.addItem(it)
        sep = QListWidgetItem("── WREKKED SETS ──")
        sep.setFlags(sep.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        sep.setForeground(QColor(theme.TEXT_DIM))
        self._set_list.addItem(sep)
        for s in self._sets:
            it = QListWidgetItem(f"{s.name}  ({s.track_count})")
            it.setData(Qt.ItemDataRole.UserRole, s.id)
            it.setForeground(QColor(theme.STATUS_WARN))
            self._set_list.addItem(it)
        self._set_list.blockSignals(False)
        if self._set_list.currentRow() < 0:
            self._set_list.setCurrentRow(0)
        self._load_tracks()

    def _on_set_row(self, row: int) -> None:
        it = self._set_list.item(row)
        data = it.data(Qt.ItemDataRole.UserRole) if it else None
        self._active_set_id = data if isinstance(data, int) else None
        self._add_library_btn.setEnabled(self._library_db is not None and self._active_set_id is not None)
        self._load_tracks()

    def _load_tracks(self) -> None:
        it = self._set_list.currentItem()
        data = it.data(Qt.ItemDataRole.UserRole) if it else None
        q = self._search.text().strip()
        if q:
            tracks = self._db.search_wrekked(q, self._active_set_id, limit=1000)
        elif data == "__recent__":
            tracks = self._db.list_recently_wrekked(500)
        elif isinstance(data, int):
            tracks = self._db.list_tracks_in_wrekked_set(data)
        else:
            tracks = self._db.list_all_wrekked_tracks()
        if data == "__offline__":
            tracks = [t for t in tracks if not t.source_available]
        elif data == "__broken__":
            tracks = [t for t in tracks if not t.wrk_ready or not Path(t.wrk_path).exists()]
        self._model.set_tracks(tracks)
        threading.Thread(target=self._check_fastload, args=(tracks, data), daemon=True).start()
        self._set_status(f"{len(tracks)} tracks")

    def _check_fastload(self, tracks: list[WrekkedTrack], view_data=None) -> None:
        from wrekker.formats.fastload import FastloadCache
        cache = FastloadCache()
        result = {t.wrk_path: cache.is_valid(Path(t.wrk_path)) for t in tracks}
        if view_data == "__nocache__":
            tracks = [t for t in tracks if not result.get(t.wrk_path)]
            self._filtered_tracks_ready.emit(tracks, result)
        self._fastload_ready.emit(result)

    def _on_fastload_ready(self, result: dict) -> None:
        self._model.set_fastload(result)
        self._update_details()

    def _selected_tracks(self) -> list[WrekkedTrack]:
        rows = sorted({i.row() for i in self._table.selectedIndexes()})
        return [t for t in (self._model.track_at(r) for r in rows) if t]

    def _selected_library_tracks(self) -> list[LibraryTrack]:
        rows = sorted({i.row() for i in self._library_table.selectedIndexes()})
        return [t for t in (self._library_model.track_at(r) for r in rows) if t]

    def _populate_library_scopes(self) -> None:
        self._library_scope.clear()
        if self._library_db is None:
            return
        self._library_scope.addItem("All Library", ("all", ""))
        try:
            folders = self._library_db.get_folder_paths()
        except Exception:
            folders = []
        for folder in folders[:300]:
            p = Path(folder)
            self._library_scope.addItem(f"Folder: {p.name or str(p)}", ("folder", str(p)))
        for playlist in self._find_library_playlists(folders)[:200]:
            self._library_scope.addItem(f"Playlist: {playlist.stem}", ("playlist", str(playlist)))

    def _find_library_playlists(self, folders: list[str]) -> list[Path]:
        roots: set[Path] = set()
        try:
            roots.update(Path(r.path) for r in self._db.get_roots())
        except Exception:
            pass
        for folder in folders:
            roots.add(Path(folder))
        playlists: set[Path] = set()
        for root in roots:
            if not root.exists():
                continue
            try:
                for ext in ("*.m3u", "*.m3u8"):
                    playlists.update(p for p in root.rglob(ext) if p.is_file())
            except Exception:
                pass
        return sorted(playlists, key=lambda p: str(p).lower())

    def _tracks_from_playlist(self, playlist: Path) -> list[LibraryTrack]:
        if self._library_db is None:
            return []
        tracks: list[LibraryTrack] = []
        seen: set[str] = set()
        try:
            lines = playlist.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            path = Path(line)
            if not path.is_absolute():
                path = (playlist.parent / path).resolve()
            track = self._library_db.get_by_path(path)
            if track and str(track.path) not in seen:
                seen.add(str(track.path))
                tracks.append(track)
        return tracks

    def _tracks_for_library_scope(self) -> list[LibraryTrack]:
        if self._library_db is None:
            return []
        scope = self._library_scope.currentData()
        if not scope:
            return []
        kind, value = scope
        if kind == "folder":
            return self._library_db.get_tracks_in_folder(Path(value))
        if kind == "playlist":
            return self._tracks_from_playlist(Path(value))
        return self._library_db.search(SearchQuery(text="", limit=2000))

    def _search_library(self) -> None:
        if self._library_db is None:
            return
        q = self._library_search.text().strip()
        scope = self._library_scope.currentData()
        self._import_scope_btn.setEnabled(bool(scope and scope[0] == "playlist"))
        if q:
            if scope and scope[0] == "folder":
                tracks = self._library_db.search_in_folder(q, Path(scope[1]), limit=120)
            elif scope and scope[0] == "playlist":
                ql = q.lower()
                tracks = [
                    t for t in self._tracks_from_playlist(Path(scope[1]))
                    if ql in t.display_title.lower()
                    or ql in t.display_artist.lower()
                    or ql in t.album.lower()
                    or ql in str(t.path).lower()
                ][:120]
            else:
                tracks = self._library_db.search(SearchQuery(text=q, limit=120))
        else:
            tracks = self._tracks_for_library_scope()[:500]
        statuses = self._db.get_batch_status([t.path for t in tracks]) if tracks else {}
        self._library_model.set_results(tracks, statuses)

    def _import_library_scope_as_set(self) -> None:
        scope = self._library_scope.currentData()
        if not scope or scope[0] != "playlist":
            return
        playlist = Path(scope[1])
        tracks = self._tracks_from_playlist(playlist)
        if not tracks:
            QMessageBox.information(self, "Import Playlist", "No library tracks from this playlist were found in LibraryDB.")
            return
        name, ok = QInputDialog.getText(self, "Import Playlist as WREKKED Set", "Set name:", text=playlist.stem)
        if not ok or not name.strip():
            return
        set_id = self._db.create_wrekked_set(name.strip())
        self._active_set_id = set_id
        self._add_library_tracks_to_set(tracks, set_id)

    def _add_library_results_to_set(self) -> None:
        if self._active_set_id is None:
            QMessageBox.information(self, "Select Set", "Select a WREKKED set first.")
            return
        tracks = self._selected_library_tracks()
        if not tracks:
            return
        self._add_library_tracks_to_set(tracks, self._active_set_id)

    def _add_library_tracks_to_set(self, tracks: list[LibraryTrack], set_id: int) -> None:
        ready = []
        needs_prepare = []
        skipped_duplicates = 0
        existing = self._db.get_set_wrk_ids(set_id)
        statuses = self._db.get_batch_status([t.path for t in tracks])
        seen_sources: set[str] = set()
        for t in tracks:
            source_key = str(t.path.absolute())
            if source_key in seen_sources:
                skipped_duplicates += 1
                continue
            seen_sources.add(source_key)
            rec = statuses.get(str(t.path.absolute()))
            if rec and rec.wrk_ready and Path(rec.wrk_path).exists():
                if rec.wrk_id in existing:
                    skipped_duplicates += 1
                else:
                    ready.append((t, rec))
                    existing.add(rec.wrk_id)
            elif t.path.exists():
                try:
                    from wrekker.formats.wrk import wrk_id_for
                    future_id = wrk_id_for(t.path)
                except Exception:
                    future_id = ""
                if future_id and future_id in existing:
                    skipped_duplicates += 1
                else:
                    needs_prepare.append(t)
                    if future_id:
                        existing.add(future_id)
        next_position = self._db.next_set_position(set_id)
        for t, rec in ready:
            self._db.upsert_set_track(
                set_id=set_id, wrk_id=rec.wrk_id, wrk_path=rec.wrk_path,
                title=rec.title or t.display_title, artist=rec.artist or t.display_artist,
                duration_s=rec.duration_s or t.duration, bpm=rec.bpm or t.bpm,
                key=rec.key or t.key_str, stems_ready=rec.stems_ready,
                wrk_ready=rec.wrk_ready, source_available=t.path.exists(),
                position=next_position,
            )
            next_position += 1
        if ready:
            self._db.update_set_track_count(set_id)
            self._db.update_set_total_duration(set_id)
        if needs_prepare:
            self.prepare_and_add_tracks.emit(needs_prepare, set_id)
            final_status = f"Added {len(ready)} ready · preparing {len(needs_prepare)} · skipped {skipped_duplicates} duplicate(s)."
        else:
            final_status = f"Added {len(ready)} ready · skipped {skipped_duplicates} duplicate(s)."
        self._load_sets()
        self._search_library()
        self._set_status(final_status)

    def _set_menu(self, pos) -> None:
        it = self._set_list.itemAt(pos)
        data = it.data(Qt.ItemDataRole.UserRole) if it else None
        menu = QMenu(self)
        menu.addAction("Manage WREKKED Library").triggered.connect(lambda: None)
        if isinstance(data, int):
            menu.addAction("Edit Set Order...").triggered.connect(lambda: self._edit_set_order(data))
            menu.addAction("Build Fastload for Set").triggered.connect(lambda: self._fastload_set(data, False))
            menu.addAction("Rebuild Fastload for Set").triggered.connect(lambda: self._fastload_set(data, True))
            menu.addAction("Delete Fastload for Set").triggered.connect(lambda: self._delete_fastload_set(data))
            menu.addAction("Validate Fastload for Set").triggered.connect(lambda: self._validate_set(data))
            menu.addAction("Show Fastload Status").triggered.connect(lambda: self._show_set_status(data))
            menu.addSeparator()
            menu.addAction("Duplicate Set").triggered.connect(lambda: self._duplicate_set(data))
            menu.addAction("Delete Set...").triggered.connect(lambda: self._delete_set(data))
        menu.exec(self._set_list.viewport().mapToGlobal(pos))

    def _edit_set_order(self, set_id: int) -> None:
        from wrekker.ui.widgets.wrekked_set_dialog import WrekkedSetDialog
        if WrekkedSetDialog(self._db, set_id, self).exec():
            self._load_sets()

    def _track_menu(self, pos) -> None:
        idx = self._table.indexAt(pos)
        if idx.isValid() and not self._table.selectionModel().isSelected(idx):
            self._table.selectRow(idx.row())
        tracks = self._selected_tracks()
        if not tracks:
            return
        menu = QMenu(self)
        if len(tracks) == 1 and tracks[0].wrk_ready:
            menu.addAction("Open Selected Track in WREKKER LAB").triggered.connect(
                lambda: self.open_lab_requested.emit(tracks[0].wrk_path)
            )
            menu.addSeparator()
        menu.addAction("Edit Metadata").triggered.connect(lambda: self._edit_track(tracks[0]))
        menu.addAction("Build Fastload Cache").triggered.connect(lambda: self._fastload_tracks(tracks, False))
        menu.addAction("Rebuild Fastload Cache").triggered.connect(lambda: self._fastload_tracks(tracks, True))
        menu.addAction("Delete Fastload Cache...").triggered.connect(lambda: self._delete_fastload_tracks(tracks))
        menu.addAction("Validate Fastload Caches").triggered.connect(self._validate_selected)
        menu.addSeparator()
        set_menu = menu.addMenu("Move to WREKKED Set")
        copy_menu = menu.addMenu("Copy to WREKKED Set")
        for s in self._sets:
            move_action = set_menu.addAction(s.name)
            move_action.setEnabled(self._active_set_id is None or s.id != self._active_set_id)
            move_action.triggered.connect(lambda _=False, sid=s.id: self._move_to_set(tracks, sid))
            copy_action = copy_menu.addAction(s.name)
            copy_action.triggered.connect(lambda _=False, sid=s.id: self._copy_to_set(tracks, sid))
        if self._active_set_id is not None:
            menu.addAction("Remove from Current WREKKED Set...").triggered.connect(lambda: self._remove_tracks(tracks))
        menu.addSeparator()
        menu.addAction("Reveal .wrk File").triggered.connect(lambda: self._reveal(Path(tracks[0].wrk_path)))
        menu.addAction("Reveal Fastload Cache Folder").triggered.connect(lambda: self._reveal_fastload(tracks[0]))
        menu.addSeparator()
        menu.addAction("Delete .wrk...").triggered.connect(lambda: self._delete_wrk_tracks(tracks))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _new_set(self) -> None:
        name, ok = QInputDialog.getText(self, "New WREKKED Set", "Set name:")
        if ok and name.strip():
            self._db.create_wrekked_set(name.strip())
            self._load_sets()

    def _rename_set(self) -> None:
        if self._active_set_id is None:
            return
        s = next((x for x in self._sets if x.id == self._active_set_id), None)
        name, ok = QInputDialog.getText(self, "Rename Set", "New name:", text=s.name if s else "")
        if ok and name.strip():
            self._db.rename_wrekked_set(self._active_set_id, name.strip())
            self._load_sets()

    def _duplicate_set(self, set_id: int) -> None:
        s = next((x for x in self._sets if x.id == set_id), None)
        if not s:
            return
        new_id = self._db.create_wrekked_set(f"{s.name} (copy)")
        self._db.copy_tracks_to_set(self._db.list_tracks_in_wrekked_set(set_id), new_id)
        self._load_sets()

    def _delete_set(self, set_id: int) -> None:
        s = next((x for x in self._sets if x.id == set_id), None)
        if QMessageBox.question(self, "Delete Set", f'Delete "{s.name if s else "this set"}"? .wrk files are kept.') == QMessageBox.StandardButton.Yes:
            self._db.remove_wrekked_set(set_id)
            self._load_sets()

    def _move_to_set(self, tracks: list[WrekkedTrack], set_id: int) -> None:
        if self._active_set_id == set_id:
            self._set_status("Already in selected set.")
            return
        if self._active_set_id is not None:
            if QMessageBox.question(
                self, "Move Tracks",
                f"Move {len(tracks)} track(s) out of the current set?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            ) != QMessageBox.StandardButton.Yes:
                return
            self._db.move_tracks_to_set(tracks, self._active_set_id, set_id)
        else:
            self._db.copy_tracks_to_set(tracks, set_id)
        self._load_sets()

    def _copy_to_set(self, tracks: list[WrekkedTrack], set_id: int) -> None:
        self._db.copy_tracks_to_set(tracks, set_id)
        self._load_sets()

    def _remove_tracks(self, tracks: list[WrekkedTrack]) -> None:
        if self._active_set_id is None:
            return
        if QMessageBox.question(self, "Remove Tracks", f"Remove {len(tracks)} track(s) from this set?") != QMessageBox.StandardButton.Yes:
            return
        for t in tracks:
            self._db.remove_set_track(self._active_set_id, t.wrk_id)
        self._load_sets()

    def _edit_track(self, track: WrekkedTrack | None) -> None:
        if not track:
            return
        if MetadataEditDialog(self._db, track, self).exec():
            self._load_tracks()

    def _fastload_selected_set(self, rebuild: bool) -> None:
        if self._active_set_id is not None:
            self._fastload_set(self._active_set_id, rebuild)

    def _fastload_set(self, set_id: int, rebuild: bool) -> None:
        self._fastload_tracks(self._db.list_tracks_in_wrekked_set(set_id), rebuild)

    def _fastload_tracks(self, tracks: list[WrekkedTrack], rebuild: bool) -> None:
        def worker():
            from wrekker.formats.fastload import FastloadCache, FORMAT_PCM16
            from wrekker.formats.wrk import load_wrk_metadata, load_wrk_mix
            cache = FastloadCache()
            ok = skip = fail = size = 0
            total = len(tracks)
            for i, t in enumerate(tracks, 1):
                self._progress.emit(f"Fastload {i}/{total}: {t.title or Path(t.wrk_path).name} · ok {ok} · skipped {skip} · failed {fail} · {_size_str(size)}")
                p = Path(t.wrk_path)
                if not t.wrk_ready or not p.exists():
                    skip += 1
                    continue
                try:
                    if rebuild:
                        cache.invalidate(p)
                    elif cache.is_valid(p):
                        skip += 1
                        continue
                    before = cache.entry_size_bytes(p)
                    meta = load_wrk_metadata(p)
                    audio, sr = load_wrk_mix(p)
                    cache.build(p, meta, audio, sr, stems_raw=None, audio_format=FORMAT_PCM16)
                    size += max(0, cache.entry_size_bytes(p) - before)
                    ok += 1
                except Exception:
                    fail += 1
            self._progress.emit(f"Fastload done · {ok} success · {skip} skipped · {fail} failed · {_size_str(size)}")
            self._refresh_tracks_requested.emit()
        threading.Thread(target=worker, daemon=True, name="wrekked-fastload-batch").start()

    def _delete_fastload_set(self, set_id: int) -> None:
        self._delete_fastload_tracks(self._db.list_tracks_in_wrekked_set(set_id))

    def _delete_fastload_tracks(self, tracks: list[WrekkedTrack]) -> None:
        if QMessageBox.question(self, "Delete Fastload Cache", f"Delete cache for {len(tracks)} track(s)? .wrk files are kept.") != QMessageBox.StandardButton.Yes:
            return
        from wrekker.formats.fastload import FastloadCache
        cache = FastloadCache()
        size = 0
        for t in tracks:
            p = Path(t.wrk_path)
            size += cache.entry_size_bytes(p)
            cache.invalidate(p)
        self._set_status(f"Deleted {_size_str(size)} fastload cache")
        self._load_tracks()

    def _delete_wrk_tracks(self, tracks: list[WrekkedTrack]) -> None:
        if QMessageBox.question(self, "Delete .wrk", f"Delete {len(tracks)} .wrk file(s)? This cannot be undone.") != QMessageBox.StandardButton.Yes:
            return
        for t in tracks:
            Path(t.wrk_path).unlink(missing_ok=True)
            if t.wrk_id:
                self._db.remove_prepared_track(t.wrk_id)
            self._db.remove_by_wrk_path(t.wrk_path)
        self._load_sets()

    def _validate_selected(self) -> None:
        tracks = self._selected_tracks()
        from wrekker.formats.fastload import FastloadCache
        cache = FastloadCache()
        broken = [t for t in tracks if not Path(t.wrk_path).exists()]
        bad_cache = [t for t in tracks if Path(t.wrk_path).exists() and not cache.is_valid(Path(t.wrk_path))]
        self._set_status(f"Validation: {len(broken)} broken .wrk · {len(bad_cache)} missing/outdated cache")

    def _validate_set(self, set_id: int) -> None:
        self._table.clearSelection()
        self._model.set_tracks(self._db.list_tracks_in_wrekked_set(set_id), self._model.fastload)
        self._validate_selected()

    def _show_set_status(self, set_id: int) -> None:
        tracks = self._db.list_tracks_in_wrekked_set(set_id)
        from wrekker.formats.fastload import FastloadCache
        cache = FastloadCache()
        fl = sum(1 for t in tracks if cache.is_valid(Path(t.wrk_path)))
        no = len(tracks) - fl
        wrk_size = sum(Path(t.wrk_path).stat().st_size for t in tracks if Path(t.wrk_path).exists())
        fl_size = sum(cache.entry_size_bytes(Path(t.wrk_path)) for t in tracks)
        QMessageBox.information(self, "Fastload Status", f"{len(tracks)} tracks\n{fl} FASTLOAD READY\n{no} NO CACHE\n.wrk {_size_str(wrk_size)}\nFastload {_size_str(fl_size)}")

    def _update_details(self) -> None:
        tracks = self._selected_tracks()
        if not tracks:
            self._details.setText("No track selected")
            return
        t = tracks[0]
        from wrekker.formats.fastload import FastloadCache
        cache = FastloadCache()
        p = Path(t.wrk_path)
        fl_ok = cache.is_valid(p)
        cdir = cache.cache_dir(p)
        expected = "FASTLOAD" if fl_ok else ("WRK FLAC FALLBACK" if p.exists() and t.wrk_ready else "UNAVAILABLE")
        size = p.stat().st_size if p.exists() else 0
        csize = cache.entry_size_bytes(p)
        created = ""
        flag = cdir / "ready.flag"
        if flag.exists():
            try:
                created = json.loads(flag.read_text()).get("created_at", "")
            except Exception:
                created = ""
        self._details.setText(
            f"{t.title or p.stem} · {t.artist or 'Unknown'}\n"
            f".wrk: {p} ({_size_str(size)}) · Fastload: {cdir} ({_size_str(csize)})\n"
            f"Cache status: {'FASTLOAD READY' if fl_ok else 'NO CACHE'} · mode: Mix Only · format: PCM16 · created: {created or 'unknown'}\n"
            f"Source: {'available' if t.source_available else 'offline'} · expected load path: {expected}"
        )

    def _open_settings(self) -> None:
        WrekkedPathSettingsDialog(self._db, self).exec()

    def _reveal(self, path: Path) -> None:
        target = path if path.exists() and path.is_dir() else path.parent
        if not target.exists():
            QMessageBox.warning(self, "Reveal Failed", f"Path does not exist:\n{path}")
            return
        subprocess.Popen(
            ["xdg-open", str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _reveal_fastload(self, track: WrekkedTrack) -> None:
        from wrekker.formats.fastload import FastloadCache
        self._reveal(FastloadCache().cache_dir(Path(track.wrk_path)))

    def _set_status(self, text: str) -> None:
        self._status.setText(text)
