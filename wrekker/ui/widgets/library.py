"""
Library browser: folder tree on the left, track table on the right.

Left panel   — QTreeWidget showing library roots + folder hierarchy.
Right panel  — QTableView of tracks with status badges (WRK / STEMS / etc.).

Interaction:
  • Click a folder → shows all tracks under it (recursive).
  • Type in search  → global FTS5 search (ignores folder selection).
  • SCAN button     → rescans the selected root (or mounts SMB).
  • Add Folder      → native picker, adds new library root.
  • Prepare Set     → context menu on tracks → opens PrepareDialog.
"""
from __future__ import annotations
import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex,
    QTimer, pyqtSignal,
)
from PyQt6.QtGui  import QColor, QAction, QBrush, QFont, QPainter
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QTableView, QAbstractItemView, QHeaderView, QMenu, QPushButton,
    QFrame, QSplitter, QTreeWidget, QTreeWidgetItem, QFileDialog,
    QStyledItemDelegate, QApplication, QStyle, QMessageBox, QInputDialog,
)
from PyQt6.QtCore import QItemSelectionModel

from wrekker.library.database import LibraryDB
from wrekker.library.models   import LibraryTrack, SearchQuery
from wrekker.ui import theme

__all__ = ["LibraryWidget"]

# ── Custom roles ──────────────────────────────────────────────────────────────

_BPM_DATA_ROLE = Qt.ItemDataRole.UserRole + 1   # (meta_bpm, wrk_bpm) for BPM col
_COMPAT_ROLE   = Qt.ItemDataRole.UserRole + 2   # float|None for Key col

# ── BPM / Key delegate colors ─────────────────────────────────────────────────

_BPM_META_CLR  = QColor("#3498db")   # metadata BPM — blue
_BPM_WRK_CLR   = QColor("#f39c12")   # wrekker BPM — WREKK orange
_COMPAT_GREEN  = QColor("#00e87a")
_COMPAT_YELLOW = QColor("#ffa726")
_COMPAT_RED    = QColor("#ff4444")
_COMPAT_NONE   = QColor("#505050")


class _DualBPMDelegate(QStyledItemDelegate):
    """Renders BPM column: metadata in blue, wrekker-analyzed in orange."""

    def paint(self, painter: QPainter, option, index) -> None:
        data = index.data(_BPM_DATA_ROLE)   # (meta_bpm | None, wrk_bpm | None)

        painter.save()
        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(
            QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget
        )

        if not data or data == (None, None):
            painter.restore()
            return

        meta_bpm, wrk_bpm = data
        r  = option.rect.adjusted(8, 0, -4, 0)
        fm = option.fontMetrics
        y  = r.top() + (r.height() - fm.height()) // 2 + fm.ascent()

        if meta_bpm is not None and wrk_bpm is not None and abs(meta_bpm - wrk_bpm) > 0.5:
            ms  = f"{meta_bpm:.1f}"
            ws  = f"{wrk_bpm:.1f}"
            sep = " · "
            painter.setPen(_BPM_META_CLR)
            painter.drawText(r.left(), y, ms)
            x = r.left() + fm.horizontalAdvance(ms)
            painter.setPen(QColor(theme.BORDER_LIT))
            painter.drawText(x, y, sep)
            x += fm.horizontalAdvance(sep)
            painter.setPen(_BPM_WRK_CLR)
            painter.drawText(x, y, ws)
        elif wrk_bpm is not None:
            painter.setPen(_BPM_WRK_CLR)
            painter.drawText(r.left(), y, f"{wrk_bpm:.1f}")
        elif meta_bpm is not None:
            painter.setPen(_BPM_META_CLR)
            painter.drawText(r.left(), y, f"{meta_bpm:.1f}")

        painter.restore()


class _KeyCompatDelegate(QStyledItemDelegate):
    """Renders Key column: small compat dot (green/yellow/red/gray) + key text."""
    _DOT = 7

    def paint(self, painter: QPainter, option, index) -> None:
        compat  = index.data(_COMPAT_ROLE)   # float | None
        key_str = index.data(Qt.ItemDataRole.DisplayRole) or ""

        if   compat is None:   dot = _COMPAT_NONE
        elif compat >= 0.75:   dot = _COMPAT_GREEN
        elif compat >= 0.50:   dot = _COMPAT_YELLOW
        else:                  dot = _COMPAT_RED

        painter.save()
        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(
            QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget
        )

        r  = option.rect.adjusted(4, 0, -4, 0)
        cy = r.top() + r.height() // 2
        d  = self._DOT

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(r.left(), cy - d // 2, d, d)

        fm = option.fontMetrics
        y  = r.top() + (r.height() - fm.height()) // 2 + fm.ascent()
        painter.setPen(QColor(theme.TEXT_MED))
        painter.drawText(r.left() + d + 4, y, key_str)

        painter.restore()


# ── Status badge helpers ──────────────────────────────────────────────────────

def _status_text(rec) -> str:
    """Short badge text for a PreparedRecord (or None)."""
    if rec is None:
        return ""
    if rec.wrk_ready:
        return "WRK"
    if rec.stems_ready:
        return "STEMS"
    s = rec.analysis_status
    if s == "processing":
        return "PREP…"
    if s == "failed":
        return "FAIL"
    if s == "outdated":
        return "OLD"
    return ""


_STATUS_FG = {
    "WRK":   "#2ecc71",
    "STEMS": "#3498db",
    "PREP…": "#f39c12",
    "FAIL":  "#e74c3c",
    "OLD":   "#e67e22",
}


# ── Track table model ─────────────────────────────────────────────────────────

class _TrackTableModel(QAbstractTableModel):
    _COLS = ["Title", "Artist", "Album", "Duration", "BPM", "Key", "STATUS", "..."]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tracks:   list[LibraryTrack] = []
        self._statuses: dict[str, object]  = {}   # abs_path_str → PreparedRecord
        self._ref_key = None                       # HarmonicKey | None

    def set_tracks(self, tracks: list[LibraryTrack],
                   statuses: dict | None = None) -> None:
        self.beginResetModel()
        self._tracks   = tracks
        self._statuses = statuses or {}
        self.endResetModel()

    def update_statuses(self, statuses: dict) -> None:
        self._statuses = statuses
        if not self._tracks:
            return
        top    = self.index(0, 0)
        bottom = self.index(len(self._tracks) - 1, len(self._COLS) - 1)
        self.dataChanged.emit(top, bottom)

    def set_reference_key(self, key) -> None:
        """Update the harmonic reference key and refresh the Key column."""
        self._ref_key = key
        if not self._tracks:
            return
        top    = self.index(0, 5)
        bottom = self.index(len(self._tracks) - 1, 5)
        self.dataChanged.emit(top, bottom)

    def track_at(self, row: int) -> Optional[LibraryTrack]:
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
        rec = self._statuses.get(str(t.path.absolute()))

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 4:   # BPM — tooltip-friendly combined string
                meta = f"{t.bpm:.1f}" if t.bpm else ""
                wrk  = f"{rec.bpm:.1f}" if rec and rec.bpm else ""
                if meta and wrk and abs(t.bpm - rec.bpm) > 0.5:
                    return f"{meta} · {wrk}"
                return wrk or meta or "—"
            if col == 5:   # Key
                return t.key_str
            if col == 6:
                return _status_text(rec)
            if col == 7:
                return "⋯"
            return [
                t.display_title,
                t.display_artist,
                t.album or "—",
                t.duration_str,
                "", "", "", "",
            ][col]

        if role == _BPM_DATA_ROLE and col == 4:
            return (t.bpm, rec.bpm if rec else None)

        if role == _COMPAT_ROLE and col == 5:
            if self._ref_key is None or t.key_str == "—":
                return None
            try:
                from wrekker.core.deck import HarmonicKey
                track_key = HarmonicKey.from_camelot(t.key_str)
                return self._ref_key.compatibility(track_key) if track_key else None
            except Exception:
                return None

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 6:
                badge   = _status_text(rec)
                hex_col = _STATUS_FG.get(badge, theme.TEXT_DIM)
                return QColor(hex_col)
            if col == 7:
                return QColor(theme.TEXT_DIM)
            return QColor(theme.TEXT_BRIGHT if col == 0 else theme.TEXT_MED)

        if role == Qt.ItemDataRole.FontRole and col in (6, 7):
            f = QFont()
            f.setPointSize(8)
            f.setBold(True)
            return f

        if role == Qt.ItemDataRole.UserRole:
            return t

        return None


# ── Folder tree helpers ───────────────────────────────────────────────────────

def _build_path_tree(folder_paths: list[str]) -> tuple[Optional[Path], dict]:
    if not folder_paths:
        return None, {}
    try:
        common = Path(os.path.commonpath(folder_paths))
    except ValueError:
        return None, {}
    tree: dict = {}
    for folder_str in folder_paths:
        folder = Path(folder_str)
        try:
            rel  = folder.relative_to(common)
        except ValueError:
            continue
        node = tree
        for part in rel.parts:
            node = node.setdefault(part, {})
    return common, tree


def _populate_tree_widget(
    subtree:     dict,
    parent:      QTreeWidgetItem | QTreeWidget,
    parent_path: Path,
) -> None:
    for name in sorted(subtree.keys(), key=str.lower):
        child_path = parent_path / name
        item = QTreeWidgetItem(parent, [name])
        item.setData(0, Qt.ItemDataRole.UserRole, child_path)
        _populate_tree_widget(subtree[name], item, child_path)


# ── Main widget ───────────────────────────────────────────────────────────────

class LibraryWidget(QWidget):
    """
    Library browser with folder tree + track table.

    Signals
    -------
    load_track(LibraryTrack, str): track + deck_id ("A" or "B")
    prepare_tracks(list[LibraryTrack]): launch prepare workflow for tracks
    root_added(Path): a new library root was added
    """

    load_track    = pyqtSignal(object, str)
    prepare_tracks = pyqtSignal(object)   # list[LibraryTrack]
    root_added    = pyqtSignal(object)    # Path
    _scan_progress = pyqtSignal(object)
    _scan_done     = pyqtSignal(object, object)

    def __init__(
        self,
        db:           LibraryDB,
        prepared_db=None,   # PreparedDB | None
        parent:       QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db           = db
        self._prepared_db  = prepared_db
        self._current_folder: Optional[Path] = None
        self._browse_in_tree: bool = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._do_search)
        self._scan_progress.connect(self._on_scan_progress_ui)
        self._scan_done.connect(self._on_scan_done_ui)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── top bar ───────────────────────────────────────────────────────────
        top_widget = QWidget()
        top_widget.setStyleSheet(f"background: {theme.BG_PANEL};")
        top = QHBoxLayout(top_widget)
        top.setContentsMargins(12, 6, 12, 6)
        top.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search tracks, artists, albums…")
        self._search.textChanged.connect(self._on_search_changed)
        top.addWidget(self._search, stretch=1)

        self._count_lbl = QLabel("0 tracks")
        self._count_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 10px;")
        top.addWidget(self._count_lbl)

        self._add_btn = QPushButton("+ Folder")
        self._add_btn.setFixedWidth(64)
        self._add_btn.setToolTip("Add a local folder to the library")
        self._add_btn.clicked.connect(self._on_add_folder)
        top.addWidget(self._add_btn)

        self._prepare_btn = QPushButton("PREPARE")
        self._prepare_btn.setFixedWidth(72)
        self._prepare_btn.setToolTip(
            "Prepare selected tracks (or all visible) into .wrk files"
        )
        self._prepare_btn.setStyleSheet(f"""
            QPushButton {{
                background: {theme.STATUS_OK}18;
                border: 1px solid {theme.STATUS_OK}66;
                color: {theme.STATUS_OK};
                font-size: 9px; font-weight: 700;
            }}
            QPushButton:hover {{
                background: {theme.STATUS_OK}33;
                border-color: {theme.STATUS_OK};
            }}
            QPushButton:disabled {{
                background: transparent;
                border-color: {theme.BORDER};
                color: {theme.TEXT_DIM};
            }}
        """)
        self._prepare_btn.clicked.connect(self._on_prepare_clicked)
        self._prepare_btn.setEnabled(self._prepared_db is not None)
        top.addWidget(self._prepare_btn)

        self._manage_btn = QPushButton("WREKKED")
        self._manage_btn.setFixedWidth(72)
        self._manage_btn.setToolTip("Manage WREKKED Library")
        self._manage_btn.clicked.connect(self._open_manage_library)
        self._manage_btn.setEnabled(self._prepared_db is not None)
        top.addWidget(self._manage_btn)

        self._scan_btn = QPushButton("SCAN")
        self._scan_btn.setFixedWidth(54)
        self._scan_btn.clicked.connect(self._on_scan)
        top.addWidget(self._scan_btn)

        root_layout.addWidget(top_widget)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {theme.BORDER}; max-height: 1px;")
        root_layout.addWidget(sep)

        # ── main splitter ─────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(2)
        root_layout.addWidget(splitter, stretch=1)

        # ── folder tree (left) ────────────────────────────────────────────────
        self._folder_tree = QTreeWidget()
        self._folder_tree.setHeaderHidden(True)
        self._folder_tree.setMinimumWidth(140)
        self._folder_tree.setMaximumWidth(300)
        self._folder_tree.setRootIsDecorated(True)
        self._folder_tree.setIndentation(14)
        self._folder_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._folder_tree.customContextMenuRequested.connect(self._on_tree_context)
        self._folder_tree.setStyleSheet(f"""
            QTreeWidget {{
                background: {theme.BG_PANEL};
                border: none;
                border-right: 1px solid {theme.BORDER};
                color: {theme.TEXT_MED};
                font-size: 11px;
            }}
            QTreeWidget::item {{
                padding: 3px 4px;
            }}
            QTreeWidget::item:selected {{
                background: {theme.BG_CTRL};
                color: {theme.TEXT_BRIGHT};
            }}
            QTreeWidget::item:hover:!selected {{
                background: {theme.BORDER};
            }}
        """)
        self._folder_tree.itemClicked.connect(self._on_folder_clicked)
        splitter.addWidget(self._folder_tree)

        # ── track table (right) ───────────────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._model = _TrackTableModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(False)
        self._table.setStyleSheet("border: none;")

        vh = self._table.verticalHeader()
        vh.setDefaultSectionSize(22)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(3, 60)
        self._table.setColumnWidth(4, 60)
        self._table.setColumnWidth(5, 50)
        self._table.setColumnWidth(6, 60)
        self._table.setColumnWidth(7, 34)

        self._table.doubleClicked.connect(self._on_double_click)
        self._table.clicked.connect(self._on_table_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)

        self._table.setItemDelegateForColumn(4, _DualBPMDelegate(self._table))
        self._table.setItemDelegateForColumn(5, _KeyCompatDelegate(self._table))

        right_layout.addWidget(self._table)
        splitter.addWidget(right)
        splitter.setSizes([200, 700])

        self._refresh_folder_tree()
        self._load_all()

    # ── folder tree ───────────────────────────────────────────────────────────

    def _refresh_folder_tree(self) -> None:
        self._folder_tree.clear()

        # "All Tracks" sentinel
        all_item = QTreeWidgetItem(self._folder_tree, ["All Tracks"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, None)
        all_item.setForeground(0, QColor(theme.TEXT_DIM))

        # Library roots section (from PreparedDB)
        if self._prepared_db is not None:
            roots = self._prepared_db.get_roots()
            if roots:
                roots_hdr = QTreeWidgetItem(self._folder_tree, ["── SOURCES ──"])
                roots_hdr.setData(0, Qt.ItemDataRole.UserRole, "__section__")
                roots_hdr.setForeground(0, QColor(theme.TEXT_DIM))
                roots_hdr.setFlags(roots_hdr.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                for root in roots:
                    rpath = Path(root.path)
                    ri = QTreeWidgetItem(roots_hdr, [root.label or rpath.name])
                    ri.setData(0, Qt.ItemDataRole.UserRole, rpath)
                    ri.setForeground(0, QColor(theme.STATUS_OK))
                roots_hdr.setExpanded(True)

        # Folder tree from tracks in DB
        folder_paths = self._db.get_folder_paths()
        if folder_paths:
            common, tree = _build_path_tree(folder_paths)
            if common is not None:
                folders_hdr = QTreeWidgetItem(self._folder_tree, ["── FOLDERS ──"])
                folders_hdr.setData(0, Qt.ItemDataRole.UserRole, "__section__")
                folders_hdr.setForeground(0, QColor(theme.TEXT_DIM))
                folders_hdr.setFlags(folders_hdr.flags() & ~Qt.ItemFlag.ItemIsSelectable)

                root_item = QTreeWidgetItem(folders_hdr, [common.name or str(common)])
                root_item.setData(0, Qt.ItemDataRole.UserRole, common)
                root_item.setForeground(0, QColor(theme.TEXT_MED))
                _populate_tree_widget(tree, root_item, common)
                root_item.setExpanded(True)
                folders_hdr.setExpanded(True)

        self._folder_tree.setCurrentItem(all_item)

    def _on_folder_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data == "__section__":
            return
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._current_folder = data
        if data is None:
            self._load_all()
        else:
            tracks = self._db.get_tracks_in_folder(data)
            self._show_tracks(tracks, f"{len(tracks)} tracks in {data.name}")

    def _on_tree_context(self, pos) -> None:
        item = self._folder_tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data in (None, "__section__"):
            return
        path = Path(data)
        menu = QMenu(self)

        # Folder-level "Prepare Set"
        act_prepare = QAction(f"Prepare Set ({path.name})", self)
        act_prepare.triggered.connect(lambda: self._prepare_folder(path))
        menu.addAction(act_prepare)

        # If it's a library root, offer Rescan + Remove
        if self._prepared_db is not None:
            roots = {r.path for r in self._prepared_db.get_roots()}
            if str(path.absolute()) in roots:
                menu.addSeparator()
                act_rescan = QAction("Rescan", self)
                act_rescan.triggered.connect(lambda: self._scan_root(path))
                menu.addAction(act_rescan)
                act_remove = QAction("Remove from Library", self)
                act_remove.triggered.connect(lambda: self._remove_root(path))
                menu.addAction(act_remove)

        menu.exec(self._folder_tree.viewport().mapToGlobal(pos))

    # ── search ────────────────────────────────────────────────────────────────

    def _on_search_changed(self, text: str) -> None:
        if text:
            self._current_folder = None
            self._folder_tree.clearSelection()
        self._search_timer.start()

    def _do_search(self) -> None:
        text = self._search.text().strip()
        if not text:
            self._load_all()
            return
        q = SearchQuery(text=text, limit=500)
        try:
            tracks = self._db.search(q)
        except Exception:
            tracks = []
        total = self._db.count()
        self._show_tracks(tracks, f"{len(tracks)} results / {total} total")

    def _load_all(self) -> None:
        q = SearchQuery(text="", limit=2000)
        try:
            tracks = self._db.search(q)
        except Exception:
            tracks = []
        total = self._db.count()
        self._show_tracks(tracks, f"{total} tracks")

    def _show_tracks(self, tracks: list, count_text: str) -> None:
        statuses = {}
        if self._prepared_db is not None and tracks:
            try:
                statuses = self._prepared_db.get_batch_status(
                    [t.path for t in tracks]
                )
            except Exception:
                pass
        self._model.set_tracks(tracks, statuses)
        self._count_lbl.setText(count_text)

    # ── context menu / load ───────────────────────────────────────────────────

    # ── Hardware browse API ───────────────────────────────────────────────────

    def browse_scroll(self, delta: int) -> None:
        """Hardware knob rotation: scroll track table or folder tree by delta rows."""
        if self._browse_in_tree:
            self._tree_navigate(delta)
        else:
            self._table_navigate(delta)

    def browse_click(self) -> None:
        """
        Hardware knob press:
        - In track mode → switch into folder-tree mode.
        - In folder mode → expand/collapse selected folder; leaf folders move
          back to track mode and load that folder's tracks.
        """
        if not self._browse_in_tree:
            self._browse_in_tree = True
            self._folder_tree.setFocus()
            cur = self._folder_tree.currentItem()
            if cur is None:
                first = self._folder_tree.topLevelItem(0)
                if first:
                    self._folder_tree.setCurrentItem(first)
            return

        cur = self._folder_tree.currentItem()
        if cur is None:
            return
        if cur.childCount() > 0:
            cur.setExpanded(not cur.isExpanded())
        else:
            # Leaf item: load its tracks and return to track-browse mode
            self._on_folder_clicked(cur, 0)
            self._browse_in_tree = False
            self._table.setFocus()

    def _tree_navigate(self, delta: int) -> None:
        tree = self._folder_tree
        cur  = tree.currentItem()
        if cur is None:
            item = tree.topLevelItem(0)
            if item:
                tree.setCurrentItem(item)
            return
        for _ in range(abs(delta)):
            nxt = tree.itemBelow(cur) if delta > 0 else tree.itemAbove(cur)
            if nxt:
                cur = nxt
        tree.setCurrentItem(cur)
        tree.scrollToItem(cur)

    def _table_navigate(self, delta: int) -> None:
        sm    = self._table.selectionModel()
        total = self._model.rowCount()
        if total == 0:
            return
        cur_idx = sm.currentIndex()
        row = cur_idx.row() if cur_idx.isValid() else -1
        new_row = max(0, min(total - 1, row + delta))
        idx = self._model.index(new_row, 0)
        sm.setCurrentIndex(
            idx,
            QItemSelectionModel.SelectionFlag.ClearAndSelect |
            QItemSelectionModel.SelectionFlag.Rows,
        )
        self._table.scrollTo(idx)

    def _selected_tracks(self) -> list[LibraryTrack]:
        idxs = self._table.selectedIndexes()
        rows = sorted({i.row() for i in idxs})
        return [t for t in (self._model.track_at(r) for r in rows) if t]

    def get_selected_track(self):
        """Return the first selected LibraryTrack, or None."""
        tracks = self._selected_tracks()
        return tracks[0] if tracks else None

    def _on_double_click(self, index: QModelIndex) -> None:
        track = self._model.track_at(index.row())
        if index.column() == 7:
            self._show_track_menu_for_index(index)
        elif track:
            self.load_track.emit(track, "A")

    def _on_table_clicked(self, index: QModelIndex) -> None:
        if index.isValid() and index.column() == 7:
            self._show_track_menu_for_index(index)

    def _show_track_menu_for_index(self, index: QModelIndex) -> None:
        self._table.selectionModel().setCurrentIndex(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect |
            QItemSelectionModel.SelectionFlag.Rows,
        )
        self._on_context_menu(self._table.visualRect(index).center())

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
        rec    = self._prepared_db.find_wrk(t0.path) if (single and self._prepared_db) else None

        # ── Load ─────────────────────────────────────────────────────────────
        if single:
            act_a = QAction("Load to Deck A", self)
            act_b = QAction("Load to Deck B", self)
            act_a.triggered.connect(lambda: self.load_track.emit(t0, "A"))
            act_b.triggered.connect(lambda: self.load_track.emit(t0, "B"))
            menu.addAction(act_a)
            menu.addAction(act_b)
            menu.addSeparator()

        # ── Prepare / rebuild ─────────────────────────────────────────────────
        if self._prepared_db is not None:
            if single and rec and rec.wrk_ready:
                act_rebuild = QAction("Rebuild .wrk", self)
                act_rebuild.triggered.connect(lambda: self.prepare_tracks.emit([t0]))
                menu.addAction(act_rebuild)
            else:
                label = f"Prepare {'Track' if single else f'{n} Tracks'} (.wrk)"
                act_prepare = QAction(label, self)
                act_prepare.triggered.connect(lambda: self.prepare_tracks.emit(tracks))
                menu.addAction(act_prepare)

        # ── WREKKED set ───────────────────────────────────────────────────────
        if self._prepared_db is not None:
            wrk_tracks = [
                t for t in tracks
                if self._prepared_db.find_wrk(t.path) is not None and
                   self._prepared_db.find_wrk(t.path).wrk_ready
            ]
            if wrk_tracks:
                menu.addSeparator()
                sets = self._prepared_db.list_wrekked_sets()
                set_menu = menu.addMenu(
                    f"Add {'Track' if single else f'{n} Tracks'} to WREKKED Set"
                )
                for s in sets:
                    act = QAction(s.name, self)
                    act.triggered.connect(
                        lambda _=False, sid=s.id: self._add_to_set(wrk_tracks, sid)
                    )
                    set_menu.addAction(act)
                set_menu.addSeparator()
                act_new = QAction("New Set…", self)
                act_new.triggered.connect(
                    lambda: self._add_to_new_set(wrk_tracks)
                )
                set_menu.addAction(act_new)

        # ── Fastload cache ────────────────────────────────────────────────────
        if single and rec and rec.wrk_ready:
            menu.addSeparator()
            from wrekker.formats.fastload import FastloadCache
            from pathlib import Path as _Path
            cache_valid = FastloadCache().is_valid(_Path(rec.wrk_path))
            if cache_valid:
                act_rebuild_fl = QAction("Rebuild Fastload Cache", self)
                act_rebuild_fl.triggered.connect(
                    lambda: self._build_fastload(rec.wrk_path, rebuild=True)
                )
                menu.addAction(act_rebuild_fl)
                act_del_fl = QAction("Delete Fastload Cache", self)
                act_del_fl.triggered.connect(
                    lambda: self._delete_fastload(rec.wrk_path)
                )
                menu.addAction(act_del_fl)
                act_reveal_fl = QAction("Reveal Fastload Cache Folder", self)
                act_reveal_fl.triggered.connect(lambda: self._reveal_fastload(rec.wrk_path))
                menu.addAction(act_reveal_fl)
            else:
                act_build_fl = QAction("Build Fastload Cache", self)
                act_build_fl.triggered.connect(
                    lambda: self._build_fastload(rec.wrk_path, rebuild=False)
                )
                menu.addAction(act_build_fl)
            act_validate_fl = QAction("Validate Cache", self)
            act_validate_fl.triggered.connect(lambda: self._validate_fastload(rec.wrk_path))
            menu.addAction(act_validate_fl)

        # ── Reveal ───────────────────────────────────────────────────────────
        if single:
            menu.addSeparator()
            act_reveal_src = QAction("Reveal Source File", self)
            act_reveal_src.triggered.connect(lambda: self._reveal(t0.path.parent))
            menu.addAction(act_reveal_src)
            if rec and rec.wrk_ready and rec.wrk_path:
                act_reveal_wrk = QAction("Reveal .wrk File", self)
                act_reveal_wrk.triggered.connect(
                    lambda: self._reveal(Path(rec.wrk_path))
                )
                menu.addAction(act_reveal_wrk)
            if rec:
                act_edit = QAction("Edit Metadata", self)
                act_edit.triggered.connect(lambda: self._edit_metadata(t0, rec))
                menu.addAction(act_edit)

        # ── Destructive ───────────────────────────────────────────────────────
        if single and rec and rec.wrk_path and self._prepared_db:
            menu.addSeparator()
            act_del_wrk = QAction("Delete .wrk…", self)
            act_del_wrk.setEnabled(Path(rec.wrk_path).exists())
            act_del_wrk.triggered.connect(lambda: self._delete_wrk(t0, rec))
            menu.addAction(act_del_wrk)

        if single and self._prepared_db:
            act_remove = QAction("Remove from Library…", self)
            act_remove.triggered.connect(lambda: self._remove_from_library(t0, rec))
            menu.addAction(act_remove)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    # ── context menu helpers ──────────────────────────────────────────────────

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

    def _add_to_set(self, tracks, set_id: int) -> None:
        if not self._prepared_db:
            return
        next_position = self._prepared_db.next_set_position(set_id)
        for t in tracks:
            r = self._prepared_db.find_wrk(t.path)
            if r and r.wrk_ready:
                self._prepared_db.upsert_set_track(
                    set_id=set_id, wrk_id=r.wrk_id, wrk_path=r.wrk_path,
                    title=r.title, artist=r.artist, duration_s=r.duration_s,
                    bpm=r.bpm, key=r.key,
                    stems_ready=r.stems_ready, wrk_ready=r.wrk_ready,
                    source_available=t.path.exists(),
                    position=next_position,
                )
                next_position += 1
        self._prepared_db.update_set_track_count(set_id)
        self._prepared_db.update_set_total_duration(set_id)

    def _add_to_new_set(self, tracks) -> None:
        if not self._prepared_db:
            return
        name, ok = QInputDialog.getText(self, "New WREKKED Set", "Set name:")
        if not ok or not name.strip():
            return
        set_id = self._prepared_db.create_wrekked_set(name.strip())
        self._add_to_set(tracks, set_id)

    def _build_fastload(self, wrk_path: str, rebuild: bool) -> None:
        def _worker():
            try:
                from wrekker.formats.fastload import FastloadCache, FORMAT_PCM16
                from wrekker.formats.wrk import load_wrk_metadata, load_wrk_mix
                from pathlib import Path as _P
                if rebuild:
                    FastloadCache().invalidate(_P(wrk_path))
                meta  = load_wrk_metadata(_P(wrk_path))
                audio, sr = load_wrk_mix(_P(wrk_path))
                FastloadCache().build(_P(wrk_path), meta, audio, sr,
                                      stems_raw=None, audio_format=FORMAT_PCM16)
                print(f"[library] fastload built: {_P(wrk_path).name}")
            except Exception as e:
                print(f"[library] fastload build failed: {e}")
        threading.Thread(target=_worker, daemon=True, name="fl-build").start()

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

    def _validate_fastload(self, wrk_path: str) -> None:
        from wrekker.formats.fastload import FastloadCache
        ok = FastloadCache().is_valid(Path(wrk_path))
        QMessageBox.information(
            self, "Fastload Cache",
            "FASTLOAD READY" if ok else "NO CACHE / CACHE OUTDATED",
        )

    def _delete_wrk(self, track, rec) -> None:
        btn = QMessageBox.question(
            self, "Delete .wrk",
            f"Delete the prepared package for:\n{track.display_title}\n\n"
            f"This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        try:
            Path(rec.wrk_path).unlink(missing_ok=True)
        except Exception:
            pass
        if self._prepared_db:
            self._prepared_db.remove_by_wrk_path(rec.wrk_path)
            self._prepared_db.mark_outdated(track.path)
        self.refresh_statuses()

    def _edit_metadata(self, track, rec) -> None:
        if not self._prepared_db or not rec:
            return
        from wrekker.library.prepared_db import WrekkedTrack
        from wrekker.ui.widgets.wrekked_manage_dialog import MetadataEditDialog
        wt = WrekkedTrack(
            id=0, set_id=0, wrk_id=rec.wrk_id, wrk_path=rec.wrk_path,
            title=rec.title or track.display_title,
            artist=rec.artist or track.display_artist,
            duration_s=rec.duration_s or getattr(track, "duration_s", 0.0),
            bpm=rec.bpm or track.bpm,
            key=rec.key or track.key_str,
            stems_ready=rec.stems_ready,
            wrk_ready=rec.wrk_ready,
            source_available=track.path.exists(),
            position=0,
            added_at=rec.created_at,
        )
        if MetadataEditDialog(self._prepared_db, wt, self).exec():
            self.refresh_statuses()

    def _remove_from_library(self, track, rec) -> None:
        if not rec or not self._prepared_db:
            return
        btn = QMessageBox.question(
            self, "Remove from Library",
            f"Remove PreparedDB entry for {track.display_title}?\n\nSource files and .wrk files are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        self._prepared_db.remove_prepared_track(rec.wrk_id)
        self.refresh_statuses()

    def _open_manage_library(self) -> None:
        if not self._prepared_db:
            return
        from wrekker.ui.widgets.wrekked_manage_dialog import ManageWrekkedLibraryDialog
        dlg = ManageWrekkedLibraryDialog(self._prepared_db, self._db, self)
        dlg.exec()
        self.refresh()

    # ── scan / add folder ─────────────────────────────────────────────────────

    def _on_prepare_clicked(self) -> None:
        """Prepare selected tracks, or all currently visible tracks if none selected."""
        tracks = self._selected_tracks()
        if not tracks:
            # Fall back to all visible tracks
            tracks = [
                self._model.track_at(r)
                for r in range(self._model.rowCount())
            ]
            tracks = [t for t in tracks if t]
        if tracks:
            self.prepare_tracks.emit(tracks)

    def _on_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Add Library Folder",
            str(Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not folder:
            return
        folder_path = Path(folder)
        if self._prepared_db is not None:
            self._prepared_db.add_root(folder_path)
        self.root_added.emit(folder_path)
        self._scan_root(folder_path)

    def _on_scan(self) -> None:
        # Scan the currently selected library root, or fall back to SMB
        selected = None
        item = self._folder_tree.currentItem()
        if item:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, Path):
                selected = data

        if selected and selected.exists():
            self._scan_root(selected)
            return

        # Legacy SMB scan
        self._smb_scan()

    def _scan_root(self, root: Path) -> None:
        from wrekker.library.scanner import LibraryScanner
        self._scan_btn.setEnabled(False)
        self._scan_btn.setText("…")

        scanner = LibraryScanner(self._db)

        def _on_progress(prog):
            self._scan_progress.emit(prog)

        def _on_done(prog):
            self._scan_done.emit(root, prog)

        scanner.scan_async(root, on_progress=_on_progress, on_done=_on_done)

    def _on_scan_progress_ui(self, prog) -> None:
        self._count_lbl.setText(
            f"Scanning… {prog.processed}/{prog.total_found}"
        )

    def _on_scan_done_ui(self, root: Path, _prog) -> None:
        if self._prepared_db is not None:
            try:
                self._prepared_db.update_root_scanned(root)
            except Exception:
                pass
        self._refresh_folder_tree()
        self._load_all()
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("SCAN")

    def _remove_root(self, root: Path) -> None:
        if self._prepared_db is not None:
            self._prepared_db.remove_root(root)
        self._refresh_folder_tree()

    def _smb_scan(self) -> None:
        from wrekker.library.smb     import load_smb_config
        from wrekker.library.scanner import LibraryScanner

        self._scan_btn.setEnabled(False)
        self._scan_btn.setText("…")

        shares = load_smb_config()
        if not shares:
            self._count_lbl.setText("No SMB config in Wrekker config folder")
            self._scan_btn.setEnabled(True)
            self._scan_btn.setText("SCAN")
            return

        share = next(iter(shares.values()))
        try:
            scan_root = share.mount()
        except Exception as e:
            self._count_lbl.setText(f"Mount failed: {e}")
            self._scan_btn.setEnabled(True)
            self._scan_btn.setText("SCAN")
            return

        self._scan_root(scan_root)

    def _prepare_folder(self, folder: Path) -> None:
        tracks = self._db.get_tracks_in_folder(folder)
        if tracks:
            self.prepare_tracks.emit(tracks)

    # ── public API ────────────────────────────────────────────────────────────

    def set_reference_key(self, key) -> None:
        """Set the harmonic reference key for the compat dot on all visible rows."""
        self._model.set_reference_key(key)

    def refresh(self) -> None:
        self._refresh_folder_tree()
        self._do_search()

    def refresh_statuses(self) -> None:
        """Re-query PreparedDB for current tracks and update badges."""
        tracks = [self._model.track_at(r) for r in range(self._model.rowCount())]
        tracks = [t for t in tracks if t]
        if not tracks or self._prepared_db is None:
            return
        try:
            statuses = self._prepared_db.get_batch_status([t.path for t in tracks])
            self._model.update_statuses(statuses)
        except Exception:
            pass
