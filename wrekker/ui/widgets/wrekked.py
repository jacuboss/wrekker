"""
WREKKED browser — prepared-set list on the left, track table on the right.

Mirrors LibraryWidget in structure and style: same top bar, same separator,
same tree-style left panel, same table settings.  Works offline — SOURCE OFFLINE
is informational, not fatal.
"""
from __future__ import annotations

import subprocess
import threading
import os
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex,
    QTimer, pyqtSignal, QItemSelectionModel,
)
from PyQt6.QtGui  import QColor, QAction, QBrush, QFont, QPainter
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QTableView, QAbstractItemView, QHeaderView, QMenu, QPushButton,
    QFrame, QSplitter, QListWidget, QListWidgetItem,
    QMessageBox, QInputDialog, QStyledItemDelegate, QApplication, QStyle,
    QDialog, QTextEdit,
)

from wrekker.library.prepared_db import PreparedDB, PreparedSet, WrekkedTrack
from wrekker.library.models import LibraryTrack, SearchQuery
from wrekker.library.wrekked_scanner import WrekkedScanner
from wrekker.ui import theme

__all__ = ["WrekkedWidget"]


# ── Status badge helpers ───────────────────────────────────────────────────────

def _badge_text(t: WrekkedTrack, fastload: bool = False) -> str:
    if t.wrk_id.startswith("source:") and not t.wrk_ready:
        return "NO WRK"
    if not t.wrk_ready:
        return "BROKEN"
    if fastload:
        return "FASTLOAD"
    if not t.source_available:
        return "SRC OFF"
    if t.stems_ready:
        return "STEMS"
    return "WRK"


_STATUS_FG = {
    "FASTLOAD": "#00e87a",   # bright green — best state
    "WRK":      "#2ecc71",
    "STEMS":    "#3498db",
    "SRC OFF":  "#e67e22",
    "BROKEN":   "#e74c3c",
    "NO WRK":   "#ffa726",
}

_MARKER_STATUS_TEXT = {
    "ready":    "AUTO MARKERS READY",
    "low_conf": "LOW CONFIDENCE",
    "none":     "NO MARKERS",
}
_MARKER_STATUS_FG = {
    "ready":    "#a78bfa",
    "low_conf": "#fbbf24",
    "none":     "#505050",
}

_VIRTUAL_ALL    = "__all__"
_VIRTUAL_RECENT = "__recent__"
_VIRTUAL_LIBRARY_ALL = "__library_all__"
_VIRTUAL_LIBRARY_FOLDER = "__library_folder__"
_VIRTUAL_LIBRARY_PLAYLIST = "__library_playlist__"
_SECTION_SETS = "__section_sets__"
_SECTION_LIBRARY = "__section_library__"
_COMPAT_ROLE = Qt.ItemDataRole.UserRole + 11
_COMPAT_GREEN  = QColor("#00e87a")
_COMPAT_YELLOW = QColor("#ffa726")
_COMPAT_RED    = QColor("#ff4444")
_COMPAT_NONE   = QColor("#505050")


class DuplicateLibraryDialog(QDialog):
    """Review likely duplicate LibraryDB rows after a library rescan."""

    add_to_set_requested = pyqtSignal(object, int)

    def __init__(self, groups: list[list[LibraryTrack]], sets: list[PreparedSet], library_db, parent=None) -> None:
        super().__init__(parent)
        self._groups = groups
        self._sets = sets
        self._library_db = library_db
        self.setWindowTitle("Library Duplicates")
        self.setMinimumSize(760, 480)
        self.setStyleSheet(theme.GLOBAL_SS)
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        title = QLabel("Duplicate tracks found in Library")
        title.setStyleSheet(f"color: {theme.STATUS_WARN}; font-weight: 800;")
        lay.addWidget(title)
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: {theme.BG_CARD};
                border: 1px solid {theme.BORDER};
                color: {theme.TEXT_MED};
            }}
            QListWidget::item {{ padding: 4px 6px; }}
            QListWidget::item:selected {{
                background: {theme.BG_CTRL};
                color: {theme.TEXT_BRIGHT};
            }}
        """)
        lay.addWidget(self._list, 1)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self._status)
        row = QHBoxLayout()
        remove = QPushButton("Remove Selected from Library Index")
        keep_first = QPushButton("Keep First in Each Group")
        add = QPushButton("Add Selected to Set")
        close = QPushButton("Close")
        remove.clicked.connect(self._remove_selected)
        keep_first.clicked.connect(self._keep_first)
        add.clicked.connect(self._add_selected_to_set)
        close.clicked.connect(self.accept)
        row.addWidget(remove)
        row.addWidget(keep_first)
        row.addStretch()
        row.addWidget(add)
        row.addWidget(close)
        lay.addLayout(row)

    def _populate(self) -> None:
        self._list.clear()
        for gi, group in enumerate(self._groups, 1):
            head = QListWidgetItem(f"Group {gi} · {group[0].display_artist} - {group[0].display_title} · {len(group)} copies")
            head.setData(Qt.ItemDataRole.UserRole, None)
            head.setForeground(QColor(theme.STATUS_WARN))
            font = head.font()
            font.setBold(True)
            head.setFont(font)
            head.setFlags(head.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._list.addItem(head)
            for t in group:
                item = QListWidgetItem(f"  {t.duration_str} · {t.path}")
                item.setData(Qt.ItemDataRole.UserRole, t)
                item.setToolTip(str(t.path))
                self._list.addItem(item)
        total = sum(len(g) for g in self._groups)
        self._status.setText(f"{len(self._groups)} duplicate group(s) · {total} tracks")

    def _selected_tracks(self) -> list[LibraryTrack]:
        tracks = []
        for item in self._list.selectedItems():
            t = item.data(Qt.ItemDataRole.UserRole)
            if t is not None:
                tracks.append(t)
        return tracks

    def _remove_selected(self) -> None:
        tracks = self._selected_tracks()
        if not tracks:
            return
        if QMessageBox.question(
            self,
            "Remove Duplicates",
            f"Remove {len(tracks)} selected duplicate row(s) from LibraryDB?\n\nAudio files are not deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        removed = self._library_db.remove_tracks([t.id for t in tracks])
        self._status.setText(f"Removed {removed} duplicate row(s) from LibraryDB")
        self.accept()

    def _keep_first(self) -> None:
        to_remove: list[LibraryTrack] = []
        for group in self._groups:
            to_remove.extend(group[1:])
        if not to_remove:
            return
        if QMessageBox.question(
            self,
            "Keep First",
            f"Keep the first path in each group and remove {len(to_remove)} duplicate row(s) from LibraryDB?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        removed = self._library_db.remove_tracks([t.id for t in to_remove])
        self._status.setText(f"Removed {removed} duplicate row(s)")
        self.accept()

    def _add_selected_to_set(self) -> None:
        tracks = self._selected_tracks()
        if not tracks or not self._sets:
            return
        names = [s.name for s in self._sets]
        name, ok = QInputDialog.getItem(self, "Add to WREKKED Set", "Set:", names, 0, False)
        if not ok:
            return
        set_id = next((s.id for s in self._sets if s.name == name), None)
        if set_id is not None:
            self.add_to_set_requested.emit(tracks, set_id)
            self.accept()


class LibraryScanResultsDialog(QDialog):
    """Compact styled summary shown after a LibraryDB rescan."""

    def __init__(self, result, duplicate_count: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Library Scan Results")
        self.setMinimumSize(460, 280)
        self.setStyleSheet(theme.GLOBAL_SS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        title = QLabel("Library scan complete")
        title.setStyleSheet(f"color: {theme.STATUS_OK}; font-weight: 800;")
        lay.addWidget(title)
        body = QTextEdit()
        body.setReadOnly(True)
        body.setStyleSheet(f"""
            QTextEdit {{
                background: {theme.BG_CARD};
                border: 1px solid {theme.BORDER};
                color: {theme.TEXT_MED};
                padding: 8px;
            }}
        """)
        if result is None:
            text = "No scan result was returned.\n\nCheck that at least one Library root exists and is available."
        else:
            text = (
                f"Tracks found: {result.total_found}\n"
                f"Processed: {result.processed}\n"
                f"New/updated: {result.new_tracks}\n"
                f"Skipped current: {result.skipped}\n"
                f"Errors: {result.errors}\n"
                f"Duplicate groups: {duplicate_count}"
            )
        body.setText(text)
        lay.addWidget(body, 1)
        row = QHBoxLayout()
        row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)


class _KeyCompatDelegate(QStyledItemDelegate):
    _DOT = 7

    def paint(self, painter: QPainter, option, index) -> None:
        compat = index.data(_COMPAT_ROLE)
        key_str = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if compat is None:
            dot = _COMPAT_NONE
        elif compat >= 0.75:
            dot = _COMPAT_GREEN
        elif compat >= 0.50:
            dot = _COMPAT_YELLOW
        else:
            dot = _COMPAT_RED
        painter.save()
        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget)
        r = option.rect.adjusted(4, 0, -4, 0)
        cy = r.top() + r.height() // 2
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(r.left(), cy - self._DOT // 2, self._DOT, self._DOT)
        fm = option.fontMetrics
        painter.setPen(QColor(theme.TEXT_MED))
        painter.drawText(r.left() + self._DOT + 4, r.top() + (r.height() - fm.height()) // 2 + fm.ascent(), key_str)
        painter.restore()


# ── Track table model ──────────────────────────────────────────────────────────

class _WrekkedTableModel(QAbstractTableModel):
    _COLS = ["Title", "Artist", "Duration", "BPM", "Key", "STATUS", "..."]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tracks:        list[WrekkedTrack] = []
        self._fastload:      dict[str, bool]    = {}   # wrk_path → is_fastload_ready
        self._marker_status: dict[str, str]     = {}   # wrk_path → "ready"|"low_conf"|"none"
        self._lab_status:    dict[str, dict]    = {}   # wrk_path → LAB summary
        self._ref_key = None

    def set_tracks(self, tracks: list[WrekkedTrack]) -> None:
        self.beginResetModel()
        self._tracks        = tracks
        self._fastload      = {}
        self._marker_status = {}
        self._lab_status    = {}
        self.endResetModel()

    def set_fastload(self, fastload: dict[str, bool]) -> None:
        self._fastload = fastload
        if not self._tracks:
            return
        col = len(self._COLS) - 1
        self.dataChanged.emit(self.index(0, col), self.index(len(self._tracks) - 1, col))

    def set_marker_status(self, status: dict[str, str]) -> None:
        self._marker_status = status
        if not self._tracks:
            return
        self.dataChanged.emit(self.index(0, 5), self.index(len(self._tracks) - 1, 5))

    def set_lab_status(self, status: dict[str, dict]) -> None:
        self._lab_status = status
        if self._tracks:
            self.dataChanged.emit(self.index(0, 5), self.index(len(self._tracks) - 1, 5))

    def set_reference_key(self, key) -> None:
        self._ref_key = key
        if self._tracks:
            self.dataChanged.emit(self.index(0, 4), self.index(len(self._tracks) - 1, 4))

    def track_at(self, row: int) -> Optional[WrekkedTrack]:
        return self._tracks[row] if 0 <= row < len(self._tracks) else None

    def rowCount(self, _=QModelIndex()) -> int:
        return len(self._tracks)

    def columnCount(self, _=QModelIndex()) -> int:
        return len(self._COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (orientation == Qt.Orientation.Horizontal
                and role == Qt.ItemDataRole.DisplayRole):
            return self._COLS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        t   = self._tracks[index.row()]
        col = index.column()
        fl  = self._fastload.get(t.wrk_path, False)

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return t.title or Path(t.wrk_path).stem
            if col == 1:
                return t.artist or "—"
            if col == 2:
                m, s = divmod(int(t.duration_s), 60)
                return f"{m}:{s:02d}"
            if col == 3:
                return f"{t.bpm:.1f}" if t.bpm else ""
            if col == 4:
                return t.key or ""
            if col == 5:
                lab = self._lab_status.get(t.wrk_path) or {}
                lab_label = lab.get("badge")
                return lab_label or _badge_text(t, fl)
            if col == 6:
                return "⋯"

        if role == _COMPAT_ROLE and col == 4:
            if self._ref_key is None or not t.key:
                return None
            try:
                from wrekker.core.deck import HarmonicKey
                track_key = HarmonicKey.from_camelot(t.key) or HarmonicKey.from_key_name(t.key)
                return self._ref_key.compatibility(track_key) if track_key else None
            except Exception:
                return None

        if role == Qt.ItemDataRole.ToolTipRole and col == 4:
            compat = self.data(index, _COMPAT_ROLE)
            if compat is None:
                return "No harmonic reference or key data"
            return f"Harmonic compatibility score: {compat:.2f}"

        if role == Qt.ItemDataRole.ToolTipRole and col == 5:
            badge = _badge_text(t, fl)
            ms = self._marker_status.get(t.wrk_path, "")
            marker_text = _MARKER_STATUS_TEXT.get(ms, "")
            lab = self._lab_status.get(t.wrk_path) or {}
            bits = [badge]
            if marker_text:
                bits.append(marker_text)
            if lab:
                bits.append(
                    f"LAB rev {lab.get('revision', 0)} · {lab.get('status', '')} · "
                    f"{lab.get('hot_cues', 0)} cues · {lab.get('loops', 0)} loops"
                )
            return "  ·  ".join(bits)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 5:
                lab = self._lab_status.get(t.wrk_path) or {}
                label = lab.get("badge") or _badge_text(t, fl)
                if label in ("VERIFIED", "CUES READY"):
                    return QColor("#00e87a")
                if label in ("NEEDS REVIEW", "LOW CONF", "DYN TODO"):
                    return QColor("#fbbf24")
                if label in ("LAB EDITED", "GRID EDITED", "MARKERS EDITED"):
                    return QColor("#ffb000")
                return QColor(_STATUS_FG.get(label, theme.TEXT_DIM))
            if col == 6:
                return QColor(theme.TEXT_DIM)
            return QColor(theme.TEXT_BRIGHT if col == 0 else theme.TEXT_MED)

        if role == Qt.ItemDataRole.FontRole and col in (5, 6):
            f = QFont()
            f.setPointSize(8)
            f.setBold(True)
            return f

        if role == Qt.ItemDataRole.UserRole:
            return t

        return None


# ── WREKKED widget ─────────────────────────────────────────────────────────────

class WrekkedWidget(QWidget):
    """
    Prepared-set browser.  Emits load_wrk_track(deck_id, wrk_path) when the
    user wants to load a track.
    """

    load_wrk_track   = pyqtSignal(str, str)    # deck_id, wrk_path
    load_source_track = pyqtSignal(object, str) # LibraryTrack, deck_id
    prepare_library_tracks = pyqtSignal(object, int) # list[LibraryTrack], set_id
    open_lab_requested = pyqtSignal(str)        # wrk_path
    rescan_done      = pyqtSignal(int)          # n_tracks found
    _fastload_ready  = pyqtSignal(object)       # dict[str, bool]
    _marker_status_ready = pyqtSignal(object)   # dict[str, str]
    _lab_status_ready = pyqtSignal(object)      # dict[str, dict]
    _library_scan_done = pyqtSignal(object, object) # ScanProgress|None, duplicate groups

    def __init__(
        self,
        db:      PreparedDB,
        scanner: WrekkedScanner,
        library_db=None,
        parent:  QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db      = db
        self._scanner = scanner
        self._library_db = library_db
        self._sets: list[PreparedSet] = []
        self._active_set_id: int | None = None
        self._active_virtual: str | None = _VIRTUAL_ALL
        self._active_library_folder: Path | None = None
        self._active_playlist_path: Path | None = None
        self._library_sources: dict[str, LibraryTrack] = {}
        self._playlist_cache_key: tuple[str, ...] | None = None
        self._playlist_cache: list[Path] = []
        self._sets_collapsed = False
        self._library_collapsed = False
        self._browse_in_set_list: bool = False   # knob mode: False=track table, True=set list

        self._fastload_ready.connect(self._on_fastload_ready)
        self._marker_status_ready.connect(self._on_marker_status_ready)
        self._lab_status_ready.connect(self._on_lab_status_ready)
        self._library_scan_done.connect(self._on_library_scan_done)

        self._build_ui()
        self._refresh_sets()

        QTimer.singleShot(1000, self._rescan_bg)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Top bar
        top_widget = QWidget()
        top_widget.setStyleSheet(f"background: {theme.BG_PANEL};")
        top = QHBoxLayout(top_widget)
        top.setContentsMargins(12, 6, 12, 6)
        top.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search title, artist…")
        self._search.textChanged.connect(self._on_search_changed)
        top.addWidget(self._search, stretch=1)

        self._count_lbl = QLabel("0 tracks")
        self._count_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 10px;")
        top.addWidget(self._count_lbl)

        self._manage_btn = QPushButton("MANAGE")
        self._manage_btn.setFixedWidth(74)
        self._manage_btn.setToolTip("Manage WREKKED Library")
        self._manage_btn.clicked.connect(self._open_manage_library)
        top.addWidget(self._manage_btn)

        self._scan_btn = QPushButton("RESCAN")
        self._scan_btn.setFixedWidth(64)
        self._scan_btn.clicked.connect(self._rescan_bg)
        top.addWidget(self._scan_btn)

        root_layout.addWidget(top_widget)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {theme.BORDER}; max-height: 1px;")
        root_layout.addWidget(sep)

        # Main splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(2)
        root_layout.addWidget(splitter, stretch=1)

        # Left: set list
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left.setMinimumWidth(140)
        left.setMaximumWidth(300)

        # New Set button strip
        set_btn_bar = QWidget()
        set_btn_bar.setStyleSheet(f"background: {theme.BG_PANEL};")
        set_btn_lay = QHBoxLayout(set_btn_bar)
        set_btn_lay.setContentsMargins(6, 4, 6, 4)
        set_btn_lay.setSpacing(4)
        lbl_sets = QLabel("SETS")
        lbl_sets.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 9px; font-weight: 700;")
        set_btn_lay.addWidget(lbl_sets)
        set_btn_lay.addStretch()
        new_set_btn = QPushButton("+ New")
        new_set_btn.setFixedHeight(18)
        new_set_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {theme.BORDER};
                color: {theme.TEXT_DIM};
                font-size: 9px;
                padding: 0 6px;
            }}
            QPushButton:hover {{
                border-color: {theme.STATUS_OK};
                color: {theme.STATUS_OK};
            }}
        """)
        new_set_btn.clicked.connect(self._on_new_set)
        set_btn_lay.addWidget(new_set_btn)
        manage_btn = QPushButton("⋯")
        manage_btn.setFixedWidth(22)
        manage_btn.setFixedHeight(18)
        manage_btn.setToolTip("Manage WREKKED Library")
        manage_btn.clicked.connect(self._open_manage_library)
        set_btn_lay.addWidget(manage_btn)
        left_layout.addWidget(set_btn_bar)

        self._set_list = QListWidget()
        self._set_list.setStyleSheet(f"""
            QListWidget {{
                background: {theme.BG_PANEL};
                border: none;
                border-right: 1px solid {theme.BORDER};
                color: {theme.TEXT_MED};
                font-size: 11px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 3px 4px;
            }}
            QListWidget::item:selected {{
                background: {theme.BG_CTRL};
                color: {theme.TEXT_BRIGHT};
            }}
            QListWidget::item:hover:!selected {{
                background: {theme.BORDER};
            }}
        """)
        self._set_list.currentRowChanged.connect(self._on_set_selected)
        self._set_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._set_list.customContextMenuRequested.connect(self._on_set_context)
        left_layout.addWidget(self._set_list, stretch=1)

        # Stats bar under set list
        self._stats_lbl = QLabel("")
        self._stats_lbl.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 9px; padding: 3px 6px;"
        )
        self._stats_lbl.setWordWrap(True)
        left_layout.addWidget(self._stats_lbl)

        splitter.addWidget(left)

        # Right: track table
        self._model = _WrekkedTableModel(self)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(False)
        self._table.setStyleSheet("border: none;")

        self._table.verticalHeader().setDefaultSectionSize(22)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(2, 60)
        self._table.setColumnWidth(3, 60)
        self._table.setColumnWidth(4, 50)
        self._table.setColumnWidth(5, 72)
        self._table.setColumnWidth(6, 34)

        self._table.doubleClicked.connect(self._on_double_click)
        self._table.clicked.connect(self._on_table_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.setItemDelegateForColumn(4, _KeyCompatDelegate(self._table))

        splitter.addWidget(self._table)
        splitter.setSizes([200, 700])

    # ── Set list ───────────────────────────────────────────────────────────────

    def _refresh_sets(self) -> None:
        self._sets = self._db.list_wrekked_sets()
        self._set_list.blockSignals(True)
        self._set_list.clear()

        sets_hdr = QListWidgetItem(("▸" if self._sets_collapsed else "▾") + " SETS")
        sets_hdr.setData(Qt.ItemDataRole.UserRole, _SECTION_SETS)
        sets_hdr.setForeground(QColor(theme.STATUS_WARN))
        font = sets_hdr.font()
        font.setBold(True)
        sets_hdr.setFont(font)
        self._set_list.addItem(sets_hdr)

        if not self._sets_collapsed:
            for label, data in [("All Prepared", _VIRTUAL_ALL),
                                 ("Recently Added", _VIRTUAL_RECENT)]:
                item = QListWidgetItem(f"  {label}")
                item.setData(Qt.ItemDataRole.UserRole, data)
                item.setForeground(QColor(theme.TEXT_DIM))
                self._set_list.addItem(item)

            for s in self._sets:
                tc   = s.track_count
                text = f"  {s.name}  ({tc})" if tc else f"  {s.name}"
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, s.id)
                item.setForeground(QColor(theme.STATUS_OK))
                self._set_list.addItem(item)

        if self._library_db is not None:
            lib_hdr = QListWidgetItem(("▸" if self._library_collapsed else "▾") + " LIBRARY")
            lib_hdr.setData(Qt.ItemDataRole.UserRole, _SECTION_LIBRARY)
            lib_hdr.setForeground(QColor(theme.DECK_A))
            lib_font = lib_hdr.font()
            lib_font.setBold(True)
            lib_hdr.setFont(lib_font)
            self._set_list.addItem(lib_hdr)

            if not self._library_collapsed:
                all_lib = QListWidgetItem("  All Library")
                all_lib.setData(Qt.ItemDataRole.UserRole, _VIRTUAL_LIBRARY_ALL)
                all_lib.setForeground(QColor(theme.DECK_A))
                self._set_list.addItem(all_lib)

                try:
                    folders = self._library_db.get_folder_paths()
                except Exception:
                    folders = []
                for folder in folders[:300]:
                    p = Path(folder)
                    item = QListWidgetItem(f"    {p.name or str(p)}")
                    item.setData(Qt.ItemDataRole.UserRole, (_VIRTUAL_LIBRARY_FOLDER, str(p)))
                    item.setToolTip(str(p))
                    item.setForeground(QColor(theme.TEXT_MED))
                    self._set_list.addItem(item)
                playlists = self._find_library_playlists(folders)
                if playlists:
                    label = QListWidgetItem("  Playlists")
                    label.setData(Qt.ItemDataRole.UserRole, "__playlist_label__")
                    label.setForeground(QColor(theme.TEXT_DIM))
                    label.setFlags(label.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    self._set_list.addItem(label)
                    for playlist in playlists[:200]:
                        item = QListWidgetItem(f"    {playlist.stem}")
                        item.setData(Qt.ItemDataRole.UserRole, (_VIRTUAL_LIBRARY_PLAYLIST, str(playlist)))
                        item.setToolTip(str(playlist))
                        item.setForeground(QColor(theme.STATUS_WARN))
                        self._set_list.addItem(item)

        self._set_list.blockSignals(False)
        self._select_first_content_row()

    def _select_first_content_row(self) -> None:
        for row in range(self._set_list.count()):
            data = self._set_list.item(row).data(Qt.ItemDataRole.UserRole)
            if data not in (_SECTION_SETS, _SECTION_LIBRARY):
                self._set_list.setCurrentRow(row)
                return
        if self._set_list.count():
            self._set_list.setCurrentRow(0)

    def _find_library_playlists(self, folders: list[str]) -> list[Path]:
        cache_key = tuple(sorted(folders))
        if self._playlist_cache_key == cache_key:
            return list(self._playlist_cache)
        roots: set[Path] = set()
        if self._db is not None:
            try:
                roots.update(Path(r.path) for r in self._db.get_roots())
            except Exception:
                pass
        for folder in folders:
            p = Path(folder)
            roots.add(p if p.is_dir() else p.parent)
        playlists: set[Path] = set()
        for root in roots:
            if not root.exists():
                continue
            try:
                for ext in ("*.m3u", "*.m3u8"):
                    playlists.update(p for p in root.rglob(ext) if p.is_file())
            except Exception:
                continue
        self._playlist_cache_key = cache_key
        self._playlist_cache = sorted(playlists, key=lambda p: str(p).lower())
        return list(self._playlist_cache)

    def _tracks_from_playlist(self, playlist: Path) -> list[LibraryTrack]:
        if self._library_db is None:
            return []
        result: list[LibraryTrack] = []
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
            if track is None:
                track = self._library_db.get_by_path(Path(os.path.normpath(str(path))))
            if track and str(track.path) not in seen:
                seen.add(str(track.path))
                result.append(track)
        return result

    def _on_set_selected(self, row: int) -> None:
        item = self._set_list.item(row)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if data == _SECTION_SETS:
            self._sets_collapsed = not self._sets_collapsed
            self._refresh_sets()
            return
        if data == _SECTION_LIBRARY:
            self._library_collapsed = not self._library_collapsed
            self._refresh_sets()
            return
        if data == _VIRTUAL_ALL:
            self._active_virtual = _VIRTUAL_ALL
            self._active_set_id  = None
            self._active_library_folder = None
            self._active_playlist_path = None
        elif data == _VIRTUAL_RECENT:
            self._active_virtual = _VIRTUAL_RECENT
            self._active_set_id  = None
            self._active_library_folder = None
            self._active_playlist_path = None
        elif data == _VIRTUAL_LIBRARY_ALL:
            self._active_virtual = _VIRTUAL_LIBRARY_ALL
            self._active_set_id = None
            self._active_library_folder = None
            self._active_playlist_path = None
        elif isinstance(data, tuple) and data and data[0] == _VIRTUAL_LIBRARY_FOLDER:
            self._active_virtual = _VIRTUAL_LIBRARY_FOLDER
            self._active_set_id = None
            self._active_library_folder = Path(data[1])
            self._active_playlist_path = None
        elif isinstance(data, tuple) and data and data[0] == _VIRTUAL_LIBRARY_PLAYLIST:
            self._active_virtual = _VIRTUAL_LIBRARY_PLAYLIST
            self._active_set_id = None
            self._active_library_folder = None
            self._active_playlist_path = Path(data[1])
        elif isinstance(data, int):
            self._active_set_id  = data
            self._active_virtual = None
            self._active_library_folder = None
            self._active_playlist_path = None
        self._load_tracks()
        self._update_stats()

    def _on_set_context(self, pos) -> None:
        item = self._set_list.itemAt(pos)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if data == _SECTION_SETS:
            self._sets_collapsed = not self._sets_collapsed
            self._refresh_sets()
            return
        if data == _SECTION_LIBRARY:
            self._library_collapsed = not self._library_collapsed
            self._refresh_sets()
            return
        if isinstance(data, tuple) and data and data[0] == _VIRTUAL_LIBRARY_PLAYLIST:
            playlist = Path(data[1])
            menu = QMenu(self)
            act_import = QAction("Import Playlist as WREKKED Set", self)
            act_import.triggered.connect(lambda: self._import_playlist_as_set(playlist))
            menu.addAction(act_import)
            act_reveal = QAction("Reveal Playlist File", self)
            act_reveal.triggered.connect(lambda: self._reveal(playlist))
            menu.addAction(act_reveal)
            menu.exec(self._set_list.viewport().mapToGlobal(pos))
            return
        if data in (None, _VIRTUAL_ALL, _VIRTUAL_RECENT, _VIRTUAL_LIBRARY_ALL):
            return
        if isinstance(data, tuple):
            return
        set_id = data
        s = next((x for x in self._sets if x.id == set_id), None)
        if s is None:
            return

        menu = QMenu(self)

        act_edit = QAction("Edit Set…", self)
        act_edit.triggered.connect(lambda: self._open_set_editor(set_id))
        menu.addAction(act_edit)

        act_manage = QAction("Manage WREKKED Library…", self)
        act_manage.triggered.connect(self._open_manage_library)
        menu.addAction(act_manage)

        act_rename = QAction("Rename…", self)
        act_rename.triggered.connect(lambda: self._rename_set(set_id, s.name))
        menu.addAction(act_rename)

        act_dup = QAction("Duplicate Set", self)
        act_dup.triggered.connect(lambda: self._duplicate_set(set_id, s.name))
        menu.addAction(act_dup)

        menu.addSeparator()
        act_build = QAction("Build Fastload for Set", self)
        act_build.triggered.connect(lambda: self._build_fastload_for_set(set_id, rebuild=False))
        menu.addAction(act_build)
        act_rebuild = QAction("Rebuild Fastload for Set", self)
        act_rebuild.triggered.connect(lambda: self._build_fastload_for_set(set_id, rebuild=True))
        menu.addAction(act_rebuild)
        act_delete_fl = QAction("Delete Fastload for Set…", self)
        act_delete_fl.triggered.connect(lambda: self._delete_fastload_for_set(set_id))
        menu.addAction(act_delete_fl)
        act_validate = QAction("Validate Fastload for Set", self)
        act_validate.triggered.connect(lambda: self._validate_fastload_for_set(set_id))
        menu.addAction(act_validate)
        act_show = QAction("Show Fastload Status", self)
        act_show.triggered.connect(lambda: self._show_fastload_status(set_id))
        menu.addAction(act_show)
        menu.addSeparator()
        act_delete = QAction(f'Delete "{s.name}"…', self)
        act_delete.triggered.connect(lambda: self._delete_set(set_id, s.name))
        menu.addAction(act_delete)

        menu.exec(self._set_list.viewport().mapToGlobal(pos))

    # ── Set CRUD ───────────────────────────────────────────────────────────────

    def _on_new_set(self) -> None:
        name, ok = QInputDialog.getText(self, "New WREKKED Set", "Set name:")
        if not ok or not name.strip():
            return
        self._db.create_wrekked_set(name.strip())
        self._refresh_sets()

    def _rename_set(self, set_id: int, current_name: str) -> None:
        name, ok = QInputDialog.getText(
            self, "Rename Set", "New name:", text=current_name
        )
        if not ok or not name.strip():
            return
        if not self._db.rename_wrekked_set(set_id, name.strip()):
            QMessageBox.warning(self, "Rename Failed",
                                "A set with that name already exists.")
        self._refresh_sets()

    def _duplicate_set(self, set_id: int, name: str) -> None:
        new_name = f"{name} (copy)"
        new_id = self._db.create_wrekked_set(new_name)
        tracks = self._db.list_tracks_in_wrekked_set(set_id)
        for t in tracks:
            self._db.upsert_set_track(
                set_id=new_id, wrk_id=t.wrk_id, wrk_path=t.wrk_path,
                title=t.title, artist=t.artist, duration_s=t.duration_s,
                bpm=t.bpm, key=t.key,
                stems_ready=t.stems_ready, wrk_ready=t.wrk_ready,
                source_available=t.source_available, position=t.position,
            )
        self._db.update_set_track_count(new_id)
        self._refresh_sets()

    def _delete_set(self, set_id: int, name: str) -> None:
        btn = QMessageBox.question(
            self, "Delete Set",
            f'Delete the set "{name}" and remove all its tracks from the set?\n\n'
            f'(The .wrk files are not deleted.)',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        self._db.remove_wrekked_set(set_id)
        if self._active_set_id == set_id:
            self._active_set_id  = None
            self._active_virtual = _VIRTUAL_ALL
        self._refresh_sets()
        self._load_tracks()

    def _open_set_editor(self, set_id: int) -> None:
        from wrekker.ui.widgets.wrekked_set_dialog import WrekkedSetDialog
        dlg = WrekkedSetDialog(self._db, set_id, parent=self)
        dlg.exec()
        self._refresh_sets()
        self._load_tracks()

    def _import_playlist_as_set(self, playlist: Path) -> None:
        tracks = self._tracks_from_playlist(playlist)
        if not tracks:
            QMessageBox.information(self, "Import Playlist", "No library tracks from this playlist were found in LibraryDB.")
            return
        name, ok = QInputDialog.getText(
            self, "Import Playlist as WREKKED Set",
            "Set name:",
            text=playlist.stem,
        )
        if not ok or not name.strip():
            return
        set_id = self._db.create_wrekked_set(name.strip())
        statuses = self._db.get_batch_status([t.path for t in tracks])
        needs_prepare: list[LibraryTrack] = []
        seen_wrk_ids: set[str] = set()
        seen_sources: set[str] = set()
        skipped_duplicates = 0
        added = 0
        next_position = self._db.next_set_position(set_id)
        for pos, t in enumerate(tracks):
            source_key = str(t.path.absolute())
            if source_key in seen_sources:
                skipped_duplicates += 1
                continue
            seen_sources.add(source_key)
            rec = statuses.get(str(t.path.absolute()))
            if rec and rec.wrk_ready and Path(rec.wrk_path).exists():
                if rec.wrk_id in seen_wrk_ids:
                    skipped_duplicates += 1
                    continue
                seen_wrk_ids.add(rec.wrk_id)
                self._db.upsert_set_track(
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
                added += 1
            elif t.path.exists():
                try:
                    from wrekker.formats.wrk import wrk_id_for
                    future_id = wrk_id_for(t.path)
                except Exception:
                    future_id = ""
                if future_id and future_id in seen_wrk_ids:
                    skipped_duplicates += 1
                    continue
                if future_id:
                    seen_wrk_ids.add(future_id)
                needs_prepare.append(t)
        self._db.update_set_track_count(set_id)
        self._db.update_set_total_duration(set_id)
        self._refresh_sets()
        if needs_prepare:
            self.prepare_library_tracks.emit(needs_prepare, set_id)
            self._count_lbl.setText(
                f"Imported {added}; preparing {len(needs_prepare)} · skipped {skipped_duplicates} duplicates"
            )
        else:
            self._count_lbl.setText(f"Imported playlist: {added} tracks · skipped {skipped_duplicates} duplicates")

    # ── Stats ──────────────────────────────────────────────────────────────────

    def _update_stats(self) -> None:
        if self._active_set_id is None:
            self._stats_lbl.setText("")
            return
        try:
            s = self._db.get_set_summary(self._active_set_id)
            fl_count = sum(1 for v in self._model._fastload.values() if v)
            no_cache = max(0, s["wrk_count"] - fl_count)
            parts = [f"{s['total']} tracks"]
            if s["wrk_count"]:
                parts.append(f"{s['wrk_count']} WRK")
            if s["stems_count"]:
                parts.append(f"{s['stems_count']} STEMS")
            if fl_count:
                parts.append(f"{fl_count} FASTLOAD")
            if no_cache:
                parts.append(f"{no_cache} NO CACHE")
            if s["offline_count"]:
                parts.append(f"{s['offline_count']} SOURCE OFFLINE")
            if s["broken_count"]:
                parts.append(f"{s['broken_count']} BROKEN")
            self._stats_lbl.setText(" · ".join(parts))
        except Exception:
            self._stats_lbl.setText("")

    # ── Track loading ──────────────────────────────────────────────────────────

    def _on_search_changed(self, _text: str) -> None:
        self._load_tracks()

    def _load_tracks(self) -> None:
        q = self._search.text().strip()
        self._library_sources = {}
        if self._active_virtual in (_VIRTUAL_LIBRARY_ALL, _VIRTUAL_LIBRARY_FOLDER, _VIRTUAL_LIBRARY_PLAYLIST):
            tracks = self._load_library_rows(q)
            self._model.set_tracks(tracks)
            self._count_lbl.setText(f"{len(tracks)} library track{'s' if len(tracks) != 1 else ''}")
            threading.Thread(
                target=self._check_fastload_bg, args=(list(tracks),),
                daemon=True, name="fl-check",
            ).start()
            return
        if q:
            tracks = self._db.search_wrekked(q, set_id=self._active_set_id)
        elif self._active_virtual == _VIRTUAL_ALL:
            tracks = self._db.list_all_wrekked_tracks()
        elif self._active_virtual == _VIRTUAL_RECENT:
            tracks = self._db.list_recently_wrekked()
        elif self._active_set_id is not None:
            tracks = self._db.list_tracks_in_wrekked_set(self._active_set_id)
        else:
            tracks = []
        self._model.set_tracks(tracks)
        n = len(tracks)
        self._count_lbl.setText(f"{n} track{'s' if n != 1 else ''}")
        # Kick off fastload check in background
        threading.Thread(
            target=self._check_fastload_bg, args=(list(tracks),),
            daemon=True, name="fl-check",
        ).start()

    def _load_library_rows(self, query: str = "") -> list[WrekkedTrack]:
        if self._library_db is None:
            return []
        try:
            if query and self._active_virtual == _VIRTUAL_LIBRARY_FOLDER and self._active_library_folder is not None:
                lib_tracks = self._library_db.search_in_folder(query, self._active_library_folder, limit=1000)
            elif query and self._active_virtual == _VIRTUAL_LIBRARY_PLAYLIST and self._active_playlist_path is not None:
                ql = query.lower()
                lib_tracks = [
                    t for t in self._tracks_from_playlist(self._active_playlist_path)
                    if ql in t.display_title.lower()
                    or ql in t.display_artist.lower()
                    or ql in t.album.lower()
                    or ql in str(t.path).lower()
                ][:1000]
            elif query:
                lib_tracks = self._library_db.search(SearchQuery(text=query, limit=1000))
            elif self._active_virtual == _VIRTUAL_LIBRARY_PLAYLIST and self._active_playlist_path is not None:
                lib_tracks = self._tracks_from_playlist(self._active_playlist_path)
            elif self._active_virtual == _VIRTUAL_LIBRARY_FOLDER and self._active_library_folder is not None:
                lib_tracks = self._library_db.get_tracks_in_folder(self._active_library_folder)
            else:
                lib_tracks = self._library_db.search(SearchQuery(text="", limit=2000))
        except Exception:
            lib_tracks = []
        statuses = self._db.get_batch_status([t.path for t in lib_tracks]) if lib_tracks else {}
        rows: list[WrekkedTrack] = []
        for i, t in enumerate(lib_tracks):
            rec = statuses.get(str(t.path.absolute()))
            wrk_ready = bool(rec and rec.wrk_ready and Path(rec.wrk_path).exists())
            wrk_id = rec.wrk_id if rec else f"source:{t.id}"
            wrk_path = rec.wrk_path if rec else str(t.path)
            self._library_sources[wrk_id] = t
            rows.append(WrekkedTrack(
                id=0, set_id=0, wrk_id=wrk_id, wrk_path=wrk_path,
                title=(rec.title if rec else t.display_title) or t.display_title,
                artist=(rec.artist if rec else t.display_artist) or t.display_artist,
                duration_s=(rec.duration_s if rec else t.duration) or 0.0,
                bpm=(rec.bpm if rec else t.bpm),
                key=(rec.key if rec else t.key_str if t.key_str != "—" else None),
                stems_ready=bool(rec and rec.stems_ready),
                wrk_ready=wrk_ready,
                source_available=t.path.exists(),
                position=i,
                added_at="",
            ))
        return rows

    def _check_fastload_bg(self, tracks: list[WrekkedTrack]) -> None:
        import zipfile
        import json as _json
        from wrekker.formats.fastload import FastloadCache
        cache    = FastloadCache()
        fl_result: dict[str, bool] = {}
        ms_result: dict[str, str]  = {}
        lab_result: dict[str, dict] = {}
        for t in tracks:
            try:
                fl_result[t.wrk_path] = cache.is_valid(Path(t.wrk_path))
            except Exception:
                fl_result[t.wrk_path] = False
            # Check analysis/markers.json in the .wrk archive
            if t.wrk_id.startswith("source:") or not t.wrk_ready:
                ms_result[t.wrk_path] = "none"
                continue
            try:
                p = Path(t.wrk_path)
                if not p.exists():
                    ms_result[t.wrk_path] = "none"
                    continue
                with zipfile.ZipFile(p, "r") as z:
                    names = z.namelist()
                    if "analysis/corrections.json" in names:
                        try:
                            corr = _json.loads(z.read("analysis/corrections.json"))
                            manifest = _json.loads(z.read("manifest.json"))
                            lab = manifest.get("lab", {})
                            status = corr.get("analysis_status") or lab.get("analysis_status") or "LAB EDITED"
                            badge = "LAB EDITED"
                            if corr.get("manual_verified"):
                                badge = "VERIFIED"
                            elif lab.get("dynamic_tempo_todo"):
                                badge = "DYN TODO"
                            elif corr.get("cues_ready"):
                                badge = "CUES READY"
                            elif corr.get("grid_edited"):
                                badge = "GRID EDITED"
                            elif corr.get("markers_edited"):
                                badge = "MARKERS EDITED"
                            elif status == "LOW CONFIDENCE":
                                badge = "LOW CONF"
                            lab_result[t.wrk_path] = {
                                "badge": badge,
                                "status": status,
                                "revision": corr.get("analysis_revision") or lab.get("analysis_revision") or 0,
                                "hot_cues": corr.get("hot_cue_count") or lab.get("hot_cue_count") or 0,
                                "loops": corr.get("loop_count") or lab.get("loop_count") or 0,
                            }
                        except Exception:
                            pass
                    if "analysis/markers.json" not in names:
                        ms_result[t.wrk_path] = "none"
                        continue
                    markers = _json.loads(z.read("analysis/markers.json"))
                if not markers:
                    ms_result[t.wrk_path] = "none"
                    continue
                avg_conf = sum(m.get("confidence", 0.0) for m in markers) / len(markers)
                ms_result[t.wrk_path] = "ready" if avg_conf >= 0.70 else "low_conf"
            except Exception:
                ms_result[t.wrk_path] = "none"
        self._fastload_ready.emit(fl_result)
        self._marker_status_ready.emit(ms_result)
        self._lab_status_ready.emit(lab_result)

    def _on_fastload_ready(self, result: dict) -> None:
        self._model.set_fastload(result)
        self._update_stats()

    def _on_marker_status_ready(self, result: dict) -> None:
        self._model.set_marker_status(result)

    def _on_lab_status_ready(self, result: dict) -> None:
        self._model.set_lab_status(result)

    def _selected_tracks(self) -> list[WrekkedTrack]:
        idxs = self._table.selectedIndexes()
        rows = sorted({i.row() for i in idxs})
        return [t for t in (self._model.track_at(r) for r in rows) if t]

    def get_selected_track(self) -> Optional[WrekkedTrack]:
        tracks = self._selected_tracks()
        return tracks[0] if tracks else None

    def _on_double_click(self, index: QModelIndex) -> None:
        t = self._model.track_at(index.row())
        if index.column() == 6:
            self._show_track_menu_for_index(index)
        elif t and t.wrk_ready:
            self.load_wrk_track.emit("A", t.wrk_path)
        elif t and t.wrk_id.startswith("source:"):
            src = self._library_sources.get(t.wrk_id)
            if src:
                self.load_source_track.emit(src, "A")

    def _on_table_clicked(self, index: QModelIndex) -> None:
        if index.isValid() and index.column() == 6:
            self._show_track_menu_for_index(index)

    def _show_track_menu_for_index(self, index: QModelIndex) -> None:
        self._table.selectionModel().setCurrentIndex(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect |
            QItemSelectionModel.SelectionFlag.Rows,
        )
        rect = self._table.visualRect(index)
        self._on_context_menu(rect.center())

    # ── Context menu ───────────────────────────────────────────────────────────

    def _on_context_menu(self, pos) -> None:
        idx = self._table.indexAt(pos)
        if idx.isValid() and not self._table.selectionModel().isSelected(idx):
            self._table.selectionModel().setCurrentIndex(
                idx,
                QItemSelectionModel.SelectionFlag.ClearAndSelect |
                QItemSelectionModel.SelectionFlag.Rows,
            )
        tracks = self._selected_tracks()
        if not tracks:
            return

        menu   = QMenu(self)
        single = len(tracks) == 1
        n      = len(tracks)
        t0     = tracks[0] if single else None
        fl     = self._model._fastload.get(t0.wrk_path, False) if t0 else False
        source_track = self._library_sources.get(t0.wrk_id) if t0 else None

        # ── Load ─────────────────────────────────────────────────────────────
        if single and (t0.wrk_ready or source_track is not None):
            act_a = QAction("Load to Deck A", self)
            act_b = QAction("Load to Deck B", self)
            if t0.wrk_ready:
                act_a.triggered.connect(lambda: self.load_wrk_track.emit("A", t0.wrk_path))
                act_b.triggered.connect(lambda: self.load_wrk_track.emit("B", t0.wrk_path))
            else:
                act_a.triggered.connect(lambda: self.load_source_track.emit(source_track, "A"))
                act_b.triggered.connect(lambda: self.load_source_track.emit(source_track, "B"))
            menu.addAction(act_a)
            menu.addAction(act_b)
            menu.addSeparator()

        if single and t0.wrk_ready:
            act_lab = QAction("Open in WREKKER LAB", self)
            act_lab.triggered.connect(lambda: self.open_lab_requested.emit(t0.wrk_path))
            menu.addAction(act_lab)
            menu.addSeparator()

        if single and source_track is not None and not t0.wrk_ready:
            act_prepare = QAction("Prepare .wrk", self)
            act_prepare.triggered.connect(lambda: self.prepare_library_tracks.emit([source_track], 0))
            menu.addAction(act_prepare)
            menu.addSeparator()

        upgrade_sources = self._tracks_needing_beatgrid_upgrade(tracks)
        if upgrade_sources:
            label = (
                "Update Beatgrid to Schema v2"
                if len(upgrade_sources) == 1
                else f"Update {len(upgrade_sources)} Beatgrids to Schema v2"
            )
            target_set_id = self._active_set_id or 0
            act_upgrade_bg = QAction(label, self)
            act_upgrade_bg.triggered.connect(
                lambda _=False, srcs=list(upgrade_sources), sid=target_set_id:
                    self.prepare_library_tracks.emit(srcs, sid)
            )
            menu.addAction(act_upgrade_bg)
            menu.addSeparator()

        # ── Fastload cache ────────────────────────────────────────────────────
        if single and t0.wrk_ready:
            ms = self._model._marker_status.get(t0.wrk_path, "")
            ms_label = _MARKER_STATUS_TEXT.get(ms, "NO MARKERS")
            act_markers = QAction(f"Marker Status: {ms_label}", self)
            act_markers.triggered.connect(lambda: self._show_marker_status(t0))
            menu.addAction(act_markers)

        if single and t0.wrk_ready:
            if fl:
                act_rebuild_fl = QAction("Rebuild Fastload Cache", self)
                act_rebuild_fl.triggered.connect(
                    lambda: self._build_fastload(t0.wrk_path, rebuild=True)
                )
                menu.addAction(act_rebuild_fl)
                act_del_fl = QAction("Delete Fastload Cache", self)
                act_del_fl.triggered.connect(
                    lambda: self._delete_fastload(t0.wrk_path)
                )
                menu.addAction(act_del_fl)
                act_reveal_fl = QAction("Reveal Fastload Cache Folder", self)
                act_reveal_fl.triggered.connect(lambda: self._reveal_fastload(t0.wrk_path))
                menu.addAction(act_reveal_fl)
            else:
                act_build_fl = QAction("Build Fastload Cache", self)
                act_build_fl.triggered.connect(
                    lambda: self._build_fastload(t0.wrk_path, rebuild=False)
                )
                menu.addAction(act_build_fl)
            act_validate_fl = QAction("Validate Cache", self)
            act_validate_fl.triggered.connect(lambda: self._validate_fastload(t0.wrk_path))
            menu.addAction(act_validate_fl)
            menu.addSeparator()

        # ── Add to another set ────────────────────────────────────────────────
        wrk_ready_tracks = [t for t in tracks if t.wrk_ready]
        if wrk_ready_tracks:
            other_sets = [s for s in self._sets if s.id != self._active_set_id]
            if other_sets:
                add_menu = menu.addMenu(
                    f"Add {'Track' if single else f'{n} Tracks'} to Set"
                )
                for s in other_sets:
                    act = QAction(s.name, self)
                    act.triggered.connect(
                        lambda _=False, sid=s.id: self._add_to_set(wrk_ready_tracks, sid)
                    )
                    add_menu.addAction(act)
                move_menu = menu.addMenu(
                    f"Move {'Track' if single else f'{n} Tracks'} to Set"
                )
                move_menu.setEnabled(self._active_set_id is not None)
                for s in other_sets:
                    act = QAction(s.name, self)
                    act.triggered.connect(
                        lambda _=False, sid=s.id: self._move_to_set(wrk_ready_tracks, sid)
                    )
                    move_menu.addAction(act)

        if source_track is not None:
            source_tracks = [
                self._library_sources[t.wrk_id]
                for t in tracks
                if t.wrk_id in self._library_sources and not t.wrk_ready
            ]
            if source_tracks and self._sets:
                prep_menu = menu.addMenu(
                    f"Prepare and Add {'Track' if len(source_tracks) == 1 else f'{len(source_tracks)} Tracks'} to Set"
                )
                for s in self._sets:
                    act = QAction(s.name, self)
                    act.triggered.connect(
                        lambda _=False, sid=s.id, srcs=list(source_tracks): self.prepare_library_tracks.emit(srcs, sid)
                    )
                    prep_menu.addAction(act)

        # ── Remove from current set ────────────────────────────────────────────
        if self._active_set_id is not None:
            menu.addSeparator()
            if single:
                act_up = QAction("Move Up in Set", self)
                act_up.triggered.connect(lambda: self._move_track_in_active_set(t0, -1))
                menu.addAction(act_up)
                act_down = QAction("Move Down in Set", self)
                act_down.triggered.connect(lambda: self._move_track_in_active_set(t0, 1))
                menu.addAction(act_down)
            label = f"Remove {'Track' if single else f'{n} Tracks'} from Set"
            act_remove = QAction(label, self)
            act_remove.triggered.connect(lambda: self._remove_from_set(tracks))
            menu.addAction(act_remove)

        # ── Reveal ────────────────────────────────────────────────────────────
        if single:
            menu.addSeparator()
            act_reveal = QAction("Reveal .wrk in Files", self)
            act_reveal.triggered.connect(lambda: self._reveal(Path(t0.wrk_path)))
            menu.addAction(act_reveal)
            act_edit_meta = QAction("Edit Metadata", self)
            act_edit_meta.triggered.connect(lambda: self._edit_metadata(t0))
            menu.addAction(act_edit_meta)

        if single and t0:
            menu.addSeparator()
            act_delete_wrk = QAction("Delete .wrk…", self)
            act_delete_wrk.setEnabled(Path(t0.wrk_path).exists() and not t0.wrk_id.startswith("source:"))
            act_delete_wrk.triggered.connect(lambda: self._delete_wrk(t0))
            menu.addAction(act_delete_wrk)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _tracks_needing_beatgrid_upgrade(
        self,
        tracks: list[WrekkedTrack],
    ) -> list[LibraryTrack]:
        """Return source tracks whose .wrk beatgrid is missing or schema < 2."""
        result: list[LibraryTrack] = []
        seen: set[str] = set()
        for t in tracks:
            if not t.wrk_ready:
                continue
            p = Path(t.wrk_path)
            if not p.exists():
                continue
            try:
                src = self._source_track_for_wrk(p)
                if src is None:
                    continue
                source_key = str(src.path.absolute())
                if source_key in seen:
                    continue
                if self._wrk_needs_beatgrid_upgrade(p):
                    result.append(src)
                    seen.add(source_key)
            except Exception:
                continue
        return result

    def _wrk_needs_beatgrid_upgrade(self, wrk_path: Path) -> bool:
        from wrekker.analysis.beat_tracker import needs_reanalysis
        from wrekker.formats.wrk import load_wrk_metadata

        meta = load_wrk_metadata(wrk_path)
        return needs_reanalysis(meta.beatgrid)

    def _source_track_for_wrk(self, wrk_path: Path) -> LibraryTrack | None:
        from wrekker.formats.wrk import load_wrk_metadata
        from wrekker.library.models import track_id

        meta = load_wrk_metadata(wrk_path)
        manifest = meta.manifest
        source = manifest.get("source", {})
        source_path = (
            source.get("path")
            or manifest.get("source_path")
            or manifest.get("metadata", {}).get("source_path")
            or ""
        )
        if not source_path:
            return None
        path = Path(source_path)
        if not path.exists():
            return None

        key = meta.key or ""
        key_num = None
        key_mode = None
        if len(key) >= 2 and key[-1].upper() in ("A", "B"):
            try:
                key_num = int(key[:-1])
                key_mode = key[-1].upper()
            except ValueError:
                key_num = None
                key_mode = None

        return LibraryTrack(
            id=track_id(path),
            path=path,
            title=meta.title or path.stem,
            artist=meta.artist or "",
            album=meta.album or "",
            genre="",
            duration=float(meta.duration_s or 0.0),
            bpm=meta.bpm,
            key_num=key_num,
            key_mode=key_mode,
            year=None,
            bitrate=None,
            sr=meta.sr,
            channels=meta.channels,
            added_at=time.time(),
        )

    # ── Context helpers ────────────────────────────────────────────────────────

    def _reveal(self, folder: Path) -> None:
        try:
            target = folder if folder.exists() and folder.is_dir() else folder.parent
            if not target.exists():
                QMessageBox.warning(self, "Reveal Failed", f"Path does not exist:\n{folder}")
                return
            subprocess.Popen(
                ["xdg-open", str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _reveal_fastload(self, wrk_path: str) -> None:
        from wrekker.formats.fastload import FastloadCache
        self._reveal(FastloadCache().cache_dir(Path(wrk_path)))

    def _add_to_set(self, tracks: list[WrekkedTrack], set_id: int) -> None:
        existing = self._db.get_set_wrk_ids(set_id)
        added = 0
        skipped = 0
        next_position = self._db.next_set_position(set_id)
        for t in tracks:
            if t.wrk_id in existing:
                skipped += 1
                continue
            self._db.upsert_set_track(
                set_id=set_id, wrk_id=t.wrk_id, wrk_path=t.wrk_path,
                title=t.title, artist=t.artist, duration_s=t.duration_s,
                bpm=t.bpm, key=t.key,
                stems_ready=t.stems_ready, wrk_ready=t.wrk_ready,
                source_available=t.source_available, position=next_position,
            )
            existing.add(t.wrk_id)
            next_position += 1
            added += 1
        self._db.update_set_track_count(set_id)
        self._db.update_set_total_duration(set_id)
        self._count_lbl.setText(f"Added {added} · skipped {skipped} duplicate(s)")

    def _move_track_in_active_set(self, track: WrekkedTrack | None, delta: int) -> None:
        if self._active_set_id is None or track is None:
            return
        tracks = self._db.list_tracks_in_wrekked_set(self._active_set_id)
        idx = next((i for i, t in enumerate(tracks) if t.wrk_id == track.wrk_id), -1)
        if idx < 0:
            return
        new_idx = max(0, min(len(tracks) - 1, idx + int(delta)))
        if new_idx == idx:
            return
        self._db.reorder_set_track(self._active_set_id, track.wrk_id, new_idx)
        self._load_tracks()
        self._table.selectRow(new_idx)

    def _move_to_set(self, tracks: list[WrekkedTrack], set_id: int) -> None:
        if self._active_set_id is None:
            self._add_to_set(tracks, set_id)
            return
        btn = QMessageBox.question(
            self, "Move to WREKKED Set",
            f"Move {len(tracks)} track(s) out of the current set?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        self._db.move_tracks_to_set(tracks, self._active_set_id, set_id)
        self._refresh_sets()
        self._load_tracks()

    def _remove_from_set(self, tracks: list[WrekkedTrack]) -> None:
        if self._active_set_id is None:
            return
        btn = QMessageBox.question(
            self, "Remove from WREKKED Set",
            f"Remove {len(tracks)} track(s) from the current set?\n\nThe .wrk files are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        for t in tracks:
            self._db.remove_set_track(self._active_set_id, t.wrk_id)
        self._refresh_sets()
        self._load_tracks()

    def _build_fastload(self, wrk_path: str, rebuild: bool) -> None:
        def _worker():
            try:
                from wrekker.formats.fastload import FastloadCache, FORMAT_PCM16
                from wrekker.formats.wrk import load_wrk_metadata, load_wrk_mix
                from pathlib import Path as _P
                p = _P(wrk_path)
                if rebuild:
                    FastloadCache().invalidate(p)
                meta  = load_wrk_metadata(p)
                audio, sr = load_wrk_mix(p)
                FastloadCache().build(p, meta, audio, sr,
                                      stems_raw=None, audio_format=FORMAT_PCM16)
                print(f"[wrekked] fastload built: {p.name}")
            except Exception as e:
                print(f"[wrekked] fastload build failed: {e}")
        threading.Thread(target=_worker, daemon=True, name="fl-build").start()
        # Re-check after a short delay to refresh the badge
        QTimer.singleShot(3000, self._load_tracks)

    def _delete_fastload(self, wrk_path: str) -> None:
        btn = QMessageBox.question(
            self, "Delete Fastload Cache",
            "Delete this track's fastload cache? The .wrk file is kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        try:
            from wrekker.formats.fastload import FastloadCache
            FastloadCache().invalidate(Path(wrk_path))
        except Exception:
            pass
        self._load_tracks()

    def _validate_fastload(self, wrk_path: str) -> None:
        from wrekker.formats.fastload import FastloadCache
        ok = FastloadCache().is_valid(Path(wrk_path))
        QMessageBox.information(
            self, "Fastload Cache",
            "FASTLOAD READY" if ok else "NO CACHE / CACHE OUTDATED",
        )

    def _delete_wrk(self, track: WrekkedTrack) -> None:
        btn = QMessageBox.question(
            self, "Delete .wrk",
            f"Delete the prepared package for:\n{track.title or Path(track.wrk_path).stem}\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        Path(track.wrk_path).unlink(missing_ok=True)
        if track.wrk_id:
            self._db.remove_prepared_track(track.wrk_id)
        self._db.remove_by_wrk_path(track.wrk_path)
        self._refresh_sets()
        self._load_tracks()

    def _edit_metadata(self, track: WrekkedTrack) -> None:
        from wrekker.ui.widgets.wrekked_manage_dialog import MetadataEditDialog
        if MetadataEditDialog(self._db, track, self).exec():
            self._load_tracks()

    def _open_manage_library(self) -> None:
        from wrekker.ui.widgets.wrekked_manage_dialog import ManageWrekkedLibraryDialog
        dlg = ManageWrekkedLibraryDialog(self._db, self._library_db, self)
        dlg.prepare_and_add_tracks.connect(
            lambda tracks, set_id: self.prepare_library_tracks.emit(tracks, set_id)
        )
        dlg.open_lab_requested.connect(self.open_lab_requested.emit)
        dlg.exec()
        self._refresh_sets()
        self._load_tracks()

    def _build_fastload_for_set(self, set_id: int, rebuild: bool) -> None:
        tracks = self._db.list_tracks_in_wrekked_set(set_id)
        def _worker():
            ok = skipped = failed = size = 0
            try:
                from wrekker.formats.fastload import FastloadCache, FORMAT_PCM16
                from wrekker.formats.wrk import load_wrk_metadata, load_wrk_mix
                cache = FastloadCache()
                total = len(tracks)
                for i, t in enumerate(tracks, 1):
                    self._count_lbl.setText(f"Fastload {i}/{total} · ok {ok} skip {skipped} fail {failed}")
                    p = Path(t.wrk_path)
                    if not t.wrk_ready or not p.exists():
                        skipped += 1
                        continue
                    if rebuild:
                        cache.invalidate(p)
                    elif cache.is_valid(p):
                        skipped += 1
                        continue
                    before = cache.entry_size_bytes(p)
                    meta = load_wrk_metadata(p)
                    audio, sr = load_wrk_mix(p)
                    cache.build(p, meta, audio, sr, stems_raw=None, audio_format=FORMAT_PCM16)
                    size += max(0, cache.entry_size_bytes(p) - before)
                    ok += 1
            except Exception:
                failed += 1
            self._count_lbl.setText(f"Fastload done · {ok} success · {skipped} skipped · {failed} failed")
            QTimer.singleShot(0, self._load_tracks)
        threading.Thread(target=_worker, daemon=True, name="wrekked-set-fastload").start()

    def _delete_fastload_for_set(self, set_id: int) -> None:
        tracks = self._db.list_tracks_in_wrekked_set(set_id)
        btn = QMessageBox.question(
            self, "Delete Set Fastload",
            f"Delete fastload cache for {len(tracks)} track(s)? .wrk files are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        from wrekker.formats.fastload import FastloadCache
        cache = FastloadCache()
        total = 0
        for t in tracks:
            p = Path(t.wrk_path)
            total += cache.entry_size_bytes(p)
            cache.invalidate(p)
        self._count_lbl.setText(f"Deleted {total / (1024 * 1024):.1f} MB fastload")
        self._load_tracks()

    def _validate_fastload_for_set(self, set_id: int) -> None:
        from wrekker.formats.fastload import FastloadCache
        cache = FastloadCache()
        tracks = self._db.list_tracks_in_wrekked_set(set_id)
        ready = sum(1 for t in tracks if cache.is_valid(Path(t.wrk_path)))
        self._count_lbl.setText(f"{ready} FASTLOAD READY · {len(tracks) - ready} NO CACHE")

    def _show_fastload_status(self, set_id: int) -> None:
        from wrekker.formats.fastload import FastloadCache
        cache = FastloadCache()
        tracks = self._db.list_tracks_in_wrekked_set(set_id)
        ready = sum(1 for t in tracks if cache.is_valid(Path(t.wrk_path)))
        size = sum(cache.entry_size_bytes(Path(t.wrk_path)) for t in tracks)
        QMessageBox.information(
            self, "Fastload Status",
            f"{len(tracks)} tracks\n{ready} FASTLOAD READY\n{len(tracks) - ready} NO CACHE\nCache size: {size / (1024 * 1024):.1f} MB",
        )

    def _show_marker_status(self, track: WrekkedTrack) -> None:
        import zipfile
        import json as _json
        try:
            p = Path(track.wrk_path)
            with zipfile.ZipFile(p, "r") as z:
                if "analysis/markers.json" not in z.namelist():
                    QMessageBox.information(
                        self, "Marker Status",
                        "NO MARKERS\n\nNo analysis/markers.json in .wrk.\n"
                        "Re-prepare the track to generate auto markers.",
                    )
                    return
                markers = _json.loads(z.read("analysis/markers.json"))
        except Exception as e:
            QMessageBox.warning(self, "Marker Status", f"Could not read markers: {e}")
            return
        if not markers:
            QMessageBox.information(
                self, "Marker Status",
                "NO MARKERS\n\nmarkers.json is empty.\n"
                "Re-prepare the track to generate auto markers.",
            )
            return
        avg_conf = sum(m.get("confidence", 0.0) for m in markers) / len(markers)
        headline = "AUTO MARKERS READY" if avg_conf >= 0.70 else "LOW CONFIDENCE"
        by_type: dict[str, int] = {}
        for m in markers:
            t = m.get("type", "?")
            by_type[t] = by_type.get(t, 0) + 1
        lines = [
            headline,
            "",
            f"Total markers: {len(markers)}",
            f"Avg confidence: {avg_conf:.2f}",
            "",
            "By type:",
        ]
        for mtype, count in sorted(by_type.items()):
            lines.append(f"  {mtype}: {count}")
        QMessageBox.information(self, "Marker Status", "\n".join(lines))

    def set_reference_key(self, key) -> None:
        self._model.set_reference_key(key)

    def refresh(self) -> None:
        self._refresh_sets()
        self._load_tracks()

    # ── Hardware browse API ────────────────────────────────────────────────────

    def browse_scroll(self, delta: int) -> None:
        """Hardware knob rotation: scroll track table or set list by delta rows."""
        if self._browse_in_set_list:
            self._set_list_navigate(delta)
        else:
            self._table_navigate(delta)

    def browse_click(self) -> None:
        """
        Hardware knob press:
        - In track mode → switch to set-list mode.
        - In set-list mode → confirm selected set, return to track mode.
        """
        if not self._browse_in_set_list:
            self._browse_in_set_list = True
            self._set_list.setFocus()
            if self._set_list.currentRow() < 0:
                self._set_list.setCurrentRow(0)
            return
        row = self._set_list.currentRow()
        if row >= 0:
            self._on_set_selected(row)
        self._browse_in_set_list = False
        self._table.setFocus()

    def load_selected_to_deck(self, deck_id: str) -> None:
        """Load the currently selected track to deck_id (called by hardware LOAD buttons)."""
        t = self.get_selected_track()
        if t and t.wrk_ready:
            self.load_wrk_track.emit(deck_id, t.wrk_path)
        elif t and t.wrk_id.startswith("source:"):
            src = self._library_sources.get(t.wrk_id)
            if src:
                self.load_source_track.emit(src, deck_id)

    def _table_navigate(self, delta: int) -> None:
        sm    = self._table.selectionModel()
        total = self._model.rowCount()
        if total == 0:
            return
        cur = sm.currentIndex()
        row = cur.row() if cur.isValid() else -1
        new_row = max(0, min(total - 1, row + delta))
        idx = self._model.index(new_row, 0)
        sm.setCurrentIndex(
            idx,
            QItemSelectionModel.SelectionFlag.ClearAndSelect |
            QItemSelectionModel.SelectionFlag.Rows,
        )
        self._table.scrollTo(idx)

    def _set_list_navigate(self, delta: int) -> None:
        count = self._set_list.count()
        if count == 0:
            return
        row = self._set_list.currentRow()
        new_row = max(0, min(count - 1, row + delta))
        item = self._set_list.item(new_row)
        # Skip non-selectable section separators
        attempts = 0
        while item and not (item.flags() & Qt.ItemFlag.ItemIsSelectable) and attempts < count:
            new_row = max(0, min(count - 1, new_row + (1 if delta >= 0 else -1)))
            item = self._set_list.item(new_row)
            attempts += 1
        self._set_list.setCurrentRow(new_row)
        if item:
            self._set_list.scrollToItem(item)

    # ── Rescan ─────────────────────────────────────────────────────────────────

    def _rescan_bg(self) -> None:
        if self._active_virtual in (_VIRTUAL_LIBRARY_ALL, _VIRTUAL_LIBRARY_FOLDER, _VIRTUAL_LIBRARY_PLAYLIST):
            self._rescan_library_bg()
            return
        self._scan_btn.setEnabled(False)
        self._scan_btn.setText("…")
        self._count_lbl.setText("scanning…")
        threading.Thread(
            target=self._rescan_worker, daemon=True, name="wrekked-scan"
        ).start()

    def _rescan_worker(self) -> None:
        try:
            n = self._scanner.scan()
        except Exception:
            n = 0
        self.rescan_done.emit(n)

    def on_rescan_done(self, n: int) -> None:
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("RESCAN")
        self._playlist_cache_key = None
        self._playlist_cache = []
        self._refresh_sets()
        self._load_tracks()
        quarantined = getattr(self._scanner, "last_quarantined_corrupt", 0)
        needs_upgrade = getattr(self._scanner, "last_needs_beatgrid_upgrade", 0)
        if quarantined:
            self._count_lbl.setText(f"Quarantined {quarantined} corrupt .wrk · scanned {n}")
        elif needs_upgrade:
            self._count_lbl.setText(
                f"Scanned {n} · {needs_upgrade} .wrk need beatgrid v2 update"
            )

    def _rescan_library_bg(self) -> None:
        if self._library_db is None:
            return
        self._scan_btn.setEnabled(False)
        self._scan_btn.setText("…")
        self._count_lbl.setText("Scanning Library…")
        threading.Thread(
            target=self._rescan_library_worker,
            daemon=True,
            name="library-rescan",
        ).start()

    def _rescan_library_worker(self) -> None:
        result = None
        try:
            from wrekker.library.scanner import LibraryScanner
            scanner = LibraryScanner(self._library_db)
            roots: list[Path] = []
            if self._active_virtual == _VIRTUAL_LIBRARY_FOLDER and self._active_library_folder is not None:
                roots = [self._active_library_folder]
            else:
                try:
                    roots = [Path(r.path) for r in self._db.get_roots()]
                except Exception:
                    roots = []
            for root in roots:
                if root.exists():
                    result = scanner.scan_blocking(root)
                    try:
                        self._db.update_root_scanned(root)
                    except Exception:
                        pass
            duplicates = self._library_db.find_duplicates()
        except Exception:
            duplicates = []
        self._library_scan_done.emit(result, duplicates)

    def _on_library_scan_done(self, result, duplicates: list[list[LibraryTrack]]) -> None:
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("RESCAN")
        self._playlist_cache_key = None
        self._playlist_cache = []
        self._refresh_sets()
        self._load_tracks()
        if result is not None:
            self._count_lbl.setText(
                f"Library scan · {result.processed}/{result.total_found} · {len(duplicates)} duplicate group(s)"
            )
            LibraryScanResultsDialog(result, len(duplicates), self).exec()
        if duplicates:
            dlg = DuplicateLibraryDialog(duplicates, self._sets, self._library_db, self)
            dlg.add_to_set_requested.connect(
                lambda tracks, set_id: self.prepare_library_tracks.emit(tracks, set_id)
            )
            dlg.exec()
            self._refresh_sets()
            self._load_tracks()
