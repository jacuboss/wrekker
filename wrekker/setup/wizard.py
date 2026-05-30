"""First-time model setup wizard."""

from __future__ import annotations

import threading

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from wrekker.setup.model_registry import MODELS, install_missing, missing_models, models_ready
from wrekker.ui import theme


class _InstallThread(QThread):
    progress = pyqtSignal(str, int, object, float)
    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self) -> None:
        super().__init__()
        self.cancel_event = threading.Event()

    def run(self) -> None:
        try:
            results = install_missing(
                progress=lambda name, done, total, speed: self.progress.emit(name, done, total, speed),
                log=self.log.emit,
                cancel_event=self.cancel_event,
            )
            failed = [r for r in results if not r.ready]
            if failed:
                self.done.emit(False, failed[-1].message or "installation failed")
            else:
                self.done.emit(models_ready(), "ready")
        except Exception as exc:
            self.done.emit(False, str(exc))

    def cancel(self) -> None:
        self.cancel_event.set()


class FirstTimeWizard(QDialog):
    """
    First-time setup wizard shown when AI models are not present.

    If the user cancels, the caller may launch Wrekker in degraded mode:
    existing prepared tracks still load, but new stem separation and Beat This!
    preparation are unavailable until setup completes.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Wrekker First-Time Setup")
        self.setModal(True)
        self.resize(720, 520)
        self.setStyleSheet(theme.GLOBAL_SS)
        self._thread: _InstallThread | None = None
        self._bars: dict[str, QProgressBar] = {}
        self._status: dict[str, QLabel] = {}
        self._build_ui()
        self._refresh_state()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(14)

        title = QLabel("FIRST-TIME SETUP")
        title.setStyleSheet(f"font-size: 22px; font-weight: 900; color: {theme.STATUS_WARN};")
        layout.addWidget(title)

        intro = QLabel(
            "Wrekker keeps the base installer lightweight. AI models and CPU "
            "wheels are downloaded once and cached locally for stem separation "
            "and Beat This! analysis. Required space is approximately 1.2 GB."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        missing = QLabel("")
        missing.setObjectName("missingLabel")
        layout.addWidget(missing)
        self._missing_label = missing

        for name, spec in MODELS.items():
            row = QWidget()
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            label = QLabel(str(spec.get("label") or name))
            label.setMinimumWidth(130)
            bar = QProgressBar()
            bar.setRange(0, 100)
            status = QLabel("Pending")
            status.setMinimumWidth(130)
            row_l.addWidget(label)
            row_l.addWidget(bar, 1)
            row_l.addWidget(status)
            layout.addWidget(row)
            self._bars[name] = bar
            self._status[name] = status

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        self._log.setStyleSheet("font-family: monospace;")
        layout.addWidget(self._log, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._skip_btn = QPushButton("Launch Degraded")
        self._install_btn = QPushButton("Download & Install")
        self._cancel_btn = QPushButton("Cancel Download")
        self._cancel_btn.setEnabled(False)
        buttons.addWidget(self._skip_btn)
        buttons.addWidget(self._cancel_btn)
        buttons.addWidget(self._install_btn)
        layout.addLayout(buttons)

        self._skip_btn.clicked.connect(self.reject)
        self._install_btn.clicked.connect(self._start_install)
        self._cancel_btn.clicked.connect(self._cancel_install)

    def _refresh_state(self) -> None:
        missing = missing_models()
        if not missing:
            self.accept()
            return
        labels = [str(MODELS[name].get("label") or name) for name in missing]
        self._missing_label.setText("Missing: " + ", ".join(labels))
        for name in MODELS:
            if name not in missing:
                self._bars[name].setValue(100)
                self._status[name].setText("Ready")

    def _start_install(self) -> None:
        self._install_btn.setEnabled(False)
        self._skip_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._thread = _InstallThread()
        self._thread.progress.connect(self._on_progress)
        self._thread.log.connect(self._append_log)
        self._thread.done.connect(self._on_done)
        self._thread.start()

    def _cancel_install(self) -> None:
        if self._thread is not None:
            self._thread.cancel()
        self._append_log("Cancelling setup...")

    def _on_progress(self, name: str, done: int, total: object, speed_bps: float) -> None:
        bar = self._bars.get(name)
        status = self._status.get(name)
        if bar is None or status is None:
            return
        if isinstance(total, int) and total > 0:
            bar.setRange(0, 100)
            bar.setValue(max(0, min(100, int(done / total * 100))))
            speed = speed_bps / 1024 / 1024 if speed_bps else 0.0
            remaining = max(total - done, 0)
            eta = remaining / speed_bps if speed_bps else 0.0
            status.setText(f"{speed:.1f} MB/s  ETA {eta:.0f}s")
        else:
            bar.setRange(0, 0)
            status.setText("Installing")

    def _append_log(self, text: str) -> None:
        self._log.appendPlainText(text)

    def _on_done(self, ok: bool, message: str) -> None:
        self._cancel_btn.setEnabled(False)
        if self._thread is not None:
            self._thread.wait()
            self._thread = None
        if ok:
            self._append_log("Setup complete.")
            self.accept()
            return
        self._append_log(f"Setup incomplete: {message}")
        self._install_btn.setEnabled(True)
        self._skip_btn.setEnabled(True)
        for bar in self._bars.values():
            if bar.minimum() == 0 and bar.maximum() == 0:
                bar.setRange(0, 100)
