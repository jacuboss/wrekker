"""
Wrekker application bootstrap.

Usage:
    python -m wrekker.ui.app
    python -m wrekker.ui.app --no-audio      # UI-only mode (library browsing)
    python -m wrekker.ui.app --no-controller # skip FLX4 detection
    python -m wrekker.ui.app --debug         # audio callback + UI tick timing
"""
from __future__ import annotations

import sys
import argparse

from PyQt6.QtCore import QThread
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QDialog

from wrekker.config.paths import data_dir

from wrekker.ui.branding import LOGO_PATH
from wrekker.ui import theme


class _StartupScanThread(QThread):
    """Background thread that checks all prepared records for file-system changes."""

    def __init__(self, prepared_db) -> None:
        super().__init__()
        self._db = prepared_db

    def run(self) -> None:
        n_out, n_miss = self._db.scan_for_changes()
        if n_out or n_miss:
            print(
                f"[startup] scan: {n_out} outdated, {n_miss} missing prepared tracks",
                flush=True,
            )


def launch(
    no_audio:      bool = False,
    no_controller: bool = False,
    debug:         bool = False,
) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Wrekker")
    app.setApplicationVersion("0.1.1")
    if LOGO_PATH.exists():
        app.setWindowIcon(QIcon(str(LOGO_PATH)))
    app.setStyleSheet(theme.GLOBAL_SS)

    try:
        from wrekker.setup.model_registry import ensure_user_python_path
        ensure_user_python_path()
    except Exception as exc:
        print(f"[setup] user python path unavailable: {exc}", flush=True)

    from wrekker.settings import SettingsStore
    settings_store = SettingsStore.load_or_create()
    settings_store.apply_environment_defaults()

    # ── First-time AI setup ──────────────────────────────────────────────────
    degraded_mode = False
    try:
        from wrekker.setup.model_registry import models_ready
        if not models_ready():
            from wrekker.setup.wizard import FirstTimeWizard
            wizard = FirstTimeWizard(parent=None)
            if wizard.exec() != QDialog.DialogCode.Accepted:
                degraded_mode = True
    except Exception as exc:
        degraded_mode = True
        print(f"[setup] AI setup unavailable, launching degraded: {exc}", flush=True)

    # ── Library DB ────────────────────────────────────────────────────────────
    wrekker_data = data_dir()
    from pathlib import Path
    db_path      = Path(settings_store.get("storage.library_db_path", wrekker_data / "library.db", include_env=False)).expanduser()
    from wrekker.library import LibraryDB
    db = LibraryDB(db_path)

    # ── Prepared library DB ───────────────────────────────────────────────────
    from wrekker.library.prepared_db import PreparedDB
    prepared_db = PreparedDB(Path(settings_store.get("storage.prepared_db_path", wrekker_data / "prepared.db", include_env=False)).expanduser())
    settings_store.sync_prepared_db_settings(prepared_db)

    # ── Audio engine + Transport ──────────────────────────────────────────────
    from wrekker.core.engine_v2 import AudioEngine
    from wrekker.core.transport import Transport
    from wrekker.stems.analyzer import StemAnalyzer
    from wrekker.stems.models   import HTDemucsModel, UnavailableStemModel

    # Callback runs entirely in Rust — GIL is never held in the hot path.
    # 256-frame blocksize (~5.8 ms at 44100 Hz) gives low latency without xruns.
    sys.setswitchinterval(0.001)

    # Cap torch/numpy thread pools so Demucs analysis doesn't starve the audio thread.
    # Use half the available cores (min 1, max 6) — leaves headroom for audio + UI.
    import os
    _n_workers = max(1, min(6, (os.cpu_count() or 4) // 2))
    try:
        import torch
        torch.set_num_threads(_n_workers)
    except ImportError:
        pass
    try:
        from threadpoolctl import threadpool_limits
        threadpool_limits(limits=_n_workers, user_api="blas")
    except Exception:
        pass

    stem_model = UnavailableStemModel("First-time AI setup was skipped") if degraded_mode else HTDemucsModel()
    engine     = AudioEngine(
        sr=int(settings_store.get("audio.sample_rate", 44100, include_env=False)),
        blocksize=int(settings_store.get("audio.buffer_size", 256, include_env=False)),
    )
    analyzer   = StemAnalyzer(stem_model)
    transport  = Transport(engine, analyzer, prepared_db=prepared_db)
    if not no_audio:
        if debug:
            engine.enable_debug()
        engine.start()

    # ── FLX4 controller ───────────────────────────────────────────────────────
    flx4 = None
    if not no_controller:
        try:
            from wrekker.hardware.flx4 import FLX4Driver
            flx4 = FLX4Driver(transport=transport, engine=engine)
            flx4.start()
        except Exception:
            flx4 = None   # controller not connected — non-fatal

    # ── Main window ───────────────────────────────────────────────────────────
    from wrekker.ui.main_window import MainWindow
    win = MainWindow(
        transport=transport, engine=engine, db=db, flx4=flx4, debug=debug,
        prepared_db=prepared_db, stem_model=stem_model, settings_store=settings_store,
        degraded_mode=degraded_mode,
    )
    win.show()

    # Scan prepared records for file-system changes in the background.
    # QThread.finished is a queued cross-thread signal → slot runs on the main thread.
    _scan = _StartupScanThread(prepared_db)
    _scan.finished.connect(win.refresh_prepared_statuses)
    _scan.start()

    exit_code = app.exec()

    # ── Cleanup ───────────────────────────────────────────────────────────────
    _scan.wait()
    if flx4:
        flx4.stop()
    if not no_audio:
        engine.stop()

    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Wrekker DJ software")
    parser.add_argument("--no-audio",      action="store_true",
                        help="Skip audio engine (library browsing only)")
    parser.add_argument("--no-controller", action="store_true",
                        help="Skip DDJ-FLX4 detection")
    parser.add_argument("--debug",         action="store_true",
                        help="Print audio callback timing and UI tick stats")
    args = parser.parse_args()
    sys.exit(launch(
        no_audio=args.no_audio,
        no_controller=args.no_controller,
        debug=args.debug,
    ))


if __name__ == "__main__":
    main()
