"""
PrepareDialog — shows .wrk preparation progress for a batch of tracks.

Opens as a non-modal window; can be closed (background continues) or cancelled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QCloseEvent
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QScrollArea, QWidget, QFrame, QSizePolicy,
)

from wrekker.ui import theme

__all__ = ["PrepareDialog"]

_PHASE_LABEL = {
    "audio":    "Loading",
    "analysis": "Analyzing",
    "stems":    "Separating stems",
    "packing":  "Packing .wrk",
    "fastload": "Caching fastload",
}

_STATUS_COLORS = {
    "done":       "#2ecc71",
    "failed":     "#e74c3c",
    "skipped":    "#95a5a6",
    "processing": "#f39c12",
    "waiting":    "#7f8c8d",
}

_TASK_LABELS = {
    "audio": "Audio",
    "mix_flac": "Mix",
    "metadata": "Meta",
    "waveform": "Wave",
    "zoom_waveform": "Zoom",
    "lufs": "LUFS",
    "key": "Key",
    "beatgrid": "Grid",
    "stems": "Stems",
    "stem_flacs": "StemFLAC",
    "stem_energy": "Energy",
    "stem_horizon": "Horizon",
    "markers": "Markers",
    "packing": "Pack",
    "fastload": "Fastload",
}

_TASK_MARKS = {
    "pending": ".",
    "waiting": ".",
    "running": "*",
    "done": "ok",
    "ready": "ok",
    "skipped": "skip",
    "failed": "fail",
}


class _TrackRow(QWidget):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(8)
        self._tasks: dict[str, str] = {}

        self._status = QLabel("—")
        self._status.setFixedWidth(80)
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 9px; font-weight: 700; "
            f"border: 1px solid {theme.BORDER}; border-radius: 3px;"
        )
        layout.addWidget(self._status)

        self._title = QLabel(title)
        self._title.setStyleSheet(f"color: {theme.TEXT_MED}; font-size: 11px;")
        self._title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._title, stretch=1)

        self._bar = QProgressBar()
        self._bar.setFixedWidth(160)
        self._bar.setFixedHeight(6)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                background: {theme.BORDER};
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background: {theme.STATUS_OK};
                border-radius: 3px;
            }}
        """)
        layout.addWidget(self._bar)

        self._phase_lbl = QLabel("")
        self._phase_lbl.setFixedWidth(260)
        self._phase_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 9px;")
        layout.addWidget(self._phase_lbl)

    def set_processing(self, phase: str, frac: float) -> None:
        self._bar.setValue(int(frac * 100))
        self._phase_lbl.setText(_PHASE_LABEL.get(phase, phase))
        self._status.setText("…")
        self._status.setStyleSheet(
            f"color: {_STATUS_COLORS['processing']}; font-size: 9px; font-weight: 700; "
            f"border: 1px solid {_STATUS_COLORS['processing']}44; border-radius: 3px;"
        )
        self._title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 11px;")

    def set_task_status(self, task: str, status: str, detail: str = "") -> None:
        shown = _TASK_MARKS.get(status, status)
        self._tasks[task] = shown
        active = [f"{_TASK_LABELS.get(k, k)}:{v}" for k, v in self._tasks.items()]
        if detail and status == "failed":
            active.append(detail[:36])
        self._phase_lbl.setText("  ".join(active[-5:]))
        if status == "failed":
            self._phase_lbl.setToolTip(detail)

    def set_done(self) -> None:
        self._bar.setValue(100)
        self._phase_lbl.setText("")
        col = _STATUS_COLORS["done"]
        self._status.setText("WRK")
        self._status.setStyleSheet(
            f"color: {col}; font-size: 9px; font-weight: 700; "
            f"border: 1px solid {col}44; border-radius: 3px;"
        )
        self._title.setStyleSheet(f"color: {theme.TEXT_MED}; font-size: 11px;")

    def set_skipped(self) -> None:
        self._phase_lbl.setText("")
        col = _STATUS_COLORS["skipped"]
        self._status.setText("SKIP")
        self._status.setStyleSheet(
            f"color: {col}; font-size: 9px; font-weight: 700; "
            f"border: 1px solid {col}44; border-radius: 3px;"
        )
        self._title.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")

    def set_failed(self, error: str) -> None:
        col = _STATUS_COLORS["failed"]
        self._status.setText("FAIL")
        self._status.setStyleSheet(
            f"color: {col}; font-size: 9px; font-weight: 700; "
            f"border: 1px solid {col}44; border-radius: 3px;"
        )
        self._title.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        # Show first line of error inline; full traceback on hover
        first_line = error.splitlines()[-1] if error else "unknown error"
        self._phase_lbl.setText(first_line[:40])
        self._phase_lbl.setStyleSheet(f"color: {col}; font-size: 9px;")
        self._phase_lbl.setToolTip(error)
        self._title.setToolTip(error)


class PrepareDialog(QDialog):
    def __init__(
        self,
        worker,        # PrepareWorker
        titles: list[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._worker  = worker
        self._rows:   list[_TrackRow] = []
        self._paused  = False
        self._finished = False
        self._cancel_requested = False

        self.setWindowTitle("Preparing Set")
        self.setMinimumWidth(600)
        self.setMinimumHeight(400)
        self.setStyleSheet(theme.GLOBAL_SS)
        self.setModal(False)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {theme.BG_PANEL};")
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(16, 10, 16, 10)
        self._hdr_lbl = QLabel(f"Preparing {len(titles)} tracks…")
        self._hdr_lbl.setStyleSheet(
            f"color: {theme.TEXT_BRIGHT}; font-size: 13px; font-weight: 700;"
        )
        hdr_layout.addWidget(self._hdr_lbl)
        hdr_layout.addStretch()
        root.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {theme.BORDER}; max-height: 1px;")
        root.addWidget(sep)

        # Overall progress
        overall_w = QWidget()
        overall_w.setStyleSheet(f"background: {theme.BG_DEEP};")
        overall_layout = QHBoxLayout(overall_w)
        overall_layout.setContentsMargins(16, 8, 16, 8)
        self._overall_bar = QProgressBar()
        self._overall_bar.setRange(0, max(len(titles), 1))
        self._overall_bar.setValue(0)
        self._overall_bar.setTextVisible(False)
        self._overall_bar.setFixedHeight(8)
        self._overall_bar.setStyleSheet(f"""
            QProgressBar {{ background: {theme.BORDER}; border-radius: 4px; }}
            QProgressBar::chunk {{ background: {theme.STATUS_OK}; border-radius: 4px; }}
        """)
        overall_layout.addWidget(self._overall_bar, stretch=1)
        self._overall_lbl = QLabel("0 / 0")
        self._overall_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 10px;")
        self._overall_lbl.setFixedWidth(60)
        overall_layout.addWidget(self._overall_lbl)
        root.addWidget(overall_w)

        # Track list scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"border: none; background: {theme.BG_DEEP};")
        inner = QWidget()
        inner.setStyleSheet(f"background: {theme.BG_DEEP};")
        self._list_layout = QVBoxLayout(inner)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)

        for i, title in enumerate(titles):
            row = _TrackRow(title)
            if i % 2 == 0:
                row.setStyleSheet(f"background: {theme.BG_DEEP};")
            else:
                row.setStyleSheet(f"background: {theme.BG_PANEL};")
            self._rows.append(row)
            self._list_layout.addWidget(row)

        self._list_layout.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=1)

        # Footer buttons
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"background: {theme.BORDER}; max-height: 1px;")
        root.addWidget(sep2)

        btn_w = QWidget()
        btn_w.setStyleSheet(f"background: {theme.BG_PANEL};")
        btn_layout = QHBoxLayout(btn_w)
        btn_layout.setContentsMargins(12, 8, 12, 8)
        btn_layout.setSpacing(8)
        btn_layout.addStretch()

        self._pause_btn = QPushButton("Pause")
        self._pause_btn.clicked.connect(self._on_pause)
        btn_layout.addWidget(self._pause_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self._cancel_btn)

        root.addWidget(btn_w)

        # Wire worker signals
        worker.track_started.connect(self._on_track_started)
        worker.track_progress.connect(self._on_track_progress)
        if hasattr(worker, "track_task_status"):
            worker.track_task_status.connect(self._on_track_task_status)
        worker.track_done.connect(self._on_track_done)
        worker.track_failed.connect(self._on_track_failed)
        worker.track_skipped.connect(self._on_track_skipped)
        worker.all_done.connect(self._on_all_done)

        self._n_done = 0
        self._total  = len(titles)
        self._update_overall(0)

    # ── slot handlers ─────────────────────────────────────────────────────────

    def _on_track_started(self, idx: int, title: str, total: int) -> None:
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_processing("audio", 0.0)

    def _on_track_progress(self, idx: int, phase: str, frac: float) -> None:
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_processing(phase, frac)

    def _on_track_task_status(self, idx: int, task: str, status: str, detail: str) -> None:
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_task_status(task, status, detail)

    def _on_track_done(self, idx: int, title: str) -> None:
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_done()
        self._n_done += 1
        self._update_overall(self._n_done)

    def _on_track_failed(self, idx: int, title: str, error: str) -> None:
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_failed(error)
        self._n_done += 1
        self._update_overall(self._n_done)

    def _on_track_skipped(self, idx: int, title: str) -> None:
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_skipped()
        self._n_done += 1
        self._update_overall(self._n_done)

    def _on_all_done(self, n_ok: int, n_failed: int, n_skipped: int) -> None:
        self._finished = True
        self._hdr_lbl.setText(
            f"Done - {n_ok} prepared, {n_skipped} skipped, {n_failed} failed"
        )
        self._pause_btn.setEnabled(False)
        self._cancel_btn.setText("Close")
        self._cancel_btn.setEnabled(True)

    def _update_overall(self, done: int) -> None:
        self._overall_bar.setValue(done)
        self._overall_lbl.setText(f"{done} / {self._total}")

    def _on_pause(self) -> None:
        if self._finished or self._cancel_requested:
            return
        self._paused = not self._paused
        if self._paused:
            self._worker.pause()
            self._pause_btn.setText("Resume")
            self._hdr_lbl.setText("Paused")
        else:
            self._worker.resume()
            self._pause_btn.setText("Pause")
            self._hdr_lbl.setText(f"Preparing {self._total} tracks...")

    def _on_cancel(self) -> None:
        if self._cancel_btn.text() == "Close":
            self.accept()
            return
        self._request_cancel()

    def _request_cancel(self) -> None:
        if self._cancel_requested or self._finished:
            return
        self._cancel_requested = True
        self._worker.cancel()
        self._pause_btn.setEnabled(False)
        self._cancel_btn.setText("Close")
        self._cancel_btn.setEnabled(True)
        self._hdr_lbl.setText("Cancelling after current phase...")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._finished:
            event.accept()
            return
        self._request_cancel()
        event.accept()
