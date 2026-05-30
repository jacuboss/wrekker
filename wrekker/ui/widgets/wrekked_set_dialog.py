"""
WrekkedSetDialog — lightweight playlist editor for a single WREKKED set.

Features:
- Rename the set
- Reorder tracks via drag-and-drop or Up/Down buttons
- Remove individual tracks
- See per-track FASTLOAD / WRK / SRC OFF badges
- See set summary (total tracks, duration, FASTLOAD count)
- Delete the entire set
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui  import QColor, QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QListWidgetItem, QAbstractItemView,
    QFrame, QWidget, QMessageBox, QSizePolicy,
)

from wrekker.library.prepared_db import PreparedDB, WrekkedTrack
from wrekker.ui import theme

__all__ = ["WrekkedSetDialog"]


def _dur_str(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _track_label(t: WrekkedTrack, fastload: bool) -> str:
    badge = "FASTLOAD" if fastload else ("SRC OFF" if not t.source_available else "WRK")
    artist = t.artist or "Unknown"
    title  = t.title or Path(t.wrk_path).stem
    return f"{title}  —  {artist}  [{badge}]"


class WrekkedSetDialog(QDialog):
    """Edit name, track order, and membership of one WREKKED set."""

    def __init__(self, db: PreparedDB, set_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit WREKKED Set")
        self.setMinimumSize(500, 480)
        self.setStyleSheet(theme.GLOBAL_SS)

        self._db     = db
        self._set_id = set_id
        self._tracks: list[WrekkedTrack] = []
        self._fastload: dict[str, bool]  = {}

        self._build_ui()
        self._load()

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        # Set name
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit()
        name_row.addWidget(self._name_edit, stretch=1)
        lay.addLayout(name_row)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {theme.BORDER};")
        sep.setFixedHeight(1)
        lay.addWidget(sep)

        # Track list
        self._list = QListWidget()
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: {theme.BG_CARD};
                border: 1px solid {theme.BORDER};
                border-radius: 4px;
                color: {theme.TEXT_MED};
                font-size: 11px;
                outline: none;
            }}
            QListWidget::item {{ padding: 4px 8px; }}
            QListWidget::item:selected {{
                background: {theme.BG_CTRL};
                color: {theme.TEXT_BRIGHT};
            }}
        """)
        lay.addWidget(self._list, stretch=1)

        # Reorder / remove buttons
        btn_row = QHBoxLayout()
        self._up_btn   = QPushButton("↑ Up")
        self._down_btn = QPushButton("↓ Down")
        self._rm_btn   = QPushButton("Remove")
        self._rm_btn.setStyleSheet(
            f"color: {theme.STATUS_ERR}; border-color: {theme.STATUS_ERR}44;"
        )
        self._up_btn.clicked.connect(self._move_up)
        self._down_btn.clicked.connect(self._move_down)
        self._rm_btn.clicked.connect(self._remove_selected)
        btn_row.addWidget(self._up_btn)
        btn_row.addWidget(self._down_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._rm_btn)
        lay.addLayout(btn_row)

        # Stats label
        self._stats_lbl = QLabel("")
        self._stats_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 10px;")
        lay.addWidget(self._stats_lbl)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"background: {theme.BORDER};")
        sep2.setFixedHeight(1)
        lay.addWidget(sep2)

        # Action buttons
        action_row = QHBoxLayout()
        self._delete_btn = QPushButton("Delete Set…")
        self._delete_btn.setStyleSheet(
            f"color: {theme.STATUS_ERR}; border-color: {theme.STATUS_ERR}44;"
        )
        self._delete_btn.clicked.connect(self._delete_set)
        action_row.addWidget(self._delete_btn)
        action_row.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._save_btn   = QPushButton("Save")
        self._save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {theme.STATUS_OK}18;
                border: 1px solid {theme.STATUS_OK}66;
                color: {theme.STATUS_OK};
            }}
            QPushButton:hover {{
                background: {theme.STATUS_OK}33;
                border-color: {theme.STATUS_OK};
            }}
        """)
        self._cancel_btn.clicked.connect(self.reject)
        self._save_btn.clicked.connect(self._save)
        action_row.addWidget(self._cancel_btn)
        action_row.addWidget(self._save_btn)
        lay.addLayout(action_row)

    # ── Load / populate ────────────────────────────────────────────────────────

    def _load(self) -> None:
        sets = {s.id: s for s in self._db.list_wrekked_sets()}
        s    = sets.get(self._set_id)
        if s:
            self._name_edit.setText(s.name)

        self._tracks = self._db.list_tracks_in_wrekked_set(self._set_id)
        self._populate_list()
        self._update_stats()

        # Fastload check in background
        threading.Thread(
            target=self._check_fastload_bg,
            args=(list(self._tracks),),
            daemon=True,
        ).start()

    def _populate_list(self) -> None:
        self._list.clear()
        for t in self._tracks:
            fl   = self._fastload.get(t.wrk_path, False)
            item = QListWidgetItem(_track_label(t, fl))
            item.setData(Qt.ItemDataRole.UserRole, t.wrk_id)
            if not t.wrk_ready:
                item.setForeground(QColor(theme.STATUS_ERR))
            elif fl:
                item.setForeground(QColor(theme.STATUS_OK))
            elif not t.source_available:
                item.setForeground(QColor(theme.STATUS_WARN))
            self._list.addItem(item)

    def _check_fastload_bg(self, tracks: list[WrekkedTrack]) -> None:
        from wrekker.formats.fastload import FastloadCache
        cache = FastloadCache()
        result = {}
        for t in tracks:
            try:
                result[t.wrk_path] = cache.is_valid(Path(t.wrk_path))
            except Exception:
                result[t.wrk_path] = False
        self._fastload = result
        # Refresh list from main thread
        QTimer.singleShot(0, self._populate_list)
        QTimer.singleShot(0, self._update_stats)

    def _update_stats(self) -> None:
        total    = len(self._tracks)
        fl_count = sum(1 for v in self._fastload.values() if v)
        wrk_ok   = sum(1 for t in self._tracks if t.wrk_ready)
        offline  = sum(1 for t in self._tracks if not t.source_available)
        dur      = sum(t.duration_s for t in self._tracks)

        parts = [f"{total} tracks"]
        if dur > 0:
            parts.append(_dur_str(dur))
        if wrk_ok:
            parts.append(f"{wrk_ok} WRK")
        if fl_count:
            parts.append(f"{fl_count} FASTLOAD")
        if offline:
            parts.append(f"{offline} SRC OFF")
        self._stats_lbl.setText("  ·  ".join(parts))

    # ── Track reorder / remove ─────────────────────────────────────────────────

    def _current_row(self) -> int:
        return self._list.currentRow()

    def _move_up(self) -> None:
        row = self._current_row()
        if row <= 0:
            return
        self._tracks.insert(row - 1, self._tracks.pop(row))
        self._populate_list()
        self._list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self._current_row()
        if row < 0 or row >= len(self._tracks) - 1:
            return
        self._tracks.insert(row + 1, self._tracks.pop(row))
        self._populate_list()
        self._list.setCurrentRow(row + 1)

    def _remove_selected(self) -> None:
        row = self._current_row()
        if row < 0 or row >= len(self._tracks):
            return
        self._tracks.pop(row)
        self._populate_list()
        self._update_stats()

    # ── Save / Delete ──────────────────────────────────────────────────────────

    def _save(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            return

        # Rename if changed
        sets = {s.id: s for s in self._db.list_wrekked_sets()}
        current = sets.get(self._set_id)
        if current and current.name != name:
            if not self._db.rename_wrekked_set(self._set_id, name):
                QMessageBox.warning(self, "Rename Failed",
                                    "A set with that name already exists.")
                return

        # Reorder: read order from list widget (respects drag-drop)
        n = self._list.count()
        ordered_ids = [
            self._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(n)
        ]
        for pos, wrk_id in enumerate(ordered_ids):
            self._db.reorder_set_track(self._set_id, wrk_id, pos)

        # Remove tracks that were deleted in the dialog
        saved_ids = set(ordered_ids)
        original_ids = {t.wrk_id for t in self._db.list_tracks_in_wrekked_set(self._set_id)}
        for wid in original_ids - saved_ids:
            self._db.remove_set_track(self._set_id, wid)

        self._db.update_set_track_count(self._set_id)
        self._db.update_set_total_duration(self._set_id)
        self.accept()

    def _delete_set(self) -> None:
        sets = {s.id: s for s in self._db.list_wrekked_sets()}
        s    = sets.get(self._set_id)
        name = s.name if s else "this set"
        btn  = QMessageBox.question(
            self, "Delete Set",
            f'Delete the set "{name}"?\n\n(The .wrk files are not deleted.)',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        self._db.remove_wrekked_set(self._set_id)
        self.done(2)   # special code so caller knows set was deleted
