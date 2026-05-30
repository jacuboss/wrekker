"""
PrepareWorker — QThread that builds .wrk files for a batch of tracks.

Signals (emitted from the worker thread):
    track_started(index: int, title: str, total: int)
    track_progress(index: int, phase: str, frac: float)
        phase: "audio" | "analysis" | "stems" | "packing" | "fastload"
    track_task_status(index: int, task: str, status: str, detail: str)
    track_done(index: int, title: str)
    track_failed(index: int, title: str, error: str)
    track_skipped(index: int, title: str)
    all_done(n_ok: int, n_failed: int, n_skipped: int)

The worker can be paused and cancelled via pause() / cancel().
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import traceback

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from wrekker.library.prepared_db import PreparedDB, TrackStatus
from wrekker.library.models import LibraryTrack
from wrekker.formats.wrk import (
    STEM_NAMES, create_wrk, encode_flac_bytes, encode_stem_flacs,
    validate_wrk, wrk_id_for, wrk_path_for, update_folder_index,
)

__all__ = ["PrepareWorker"]


_GPU_SEMAPHORE = threading.Semaphore(1)


@dataclass
class _PrepResult:
    value: object = None
    status: str = TrackStatus.READY
    error: Optional[str] = None


@dataclass
class _PrepContext:
    idx: int
    track: LibraryTrack
    path: Path
    wrk_path: Path
    wrk_id: str
    src_hash: str
    src_mtime: int
    audio: np.ndarray
    sr: int
    file_meta: dict
    started_at: float
    results: dict[str, _PrepResult] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    timings_ms: dict[str, int] = field(default_factory=dict)
    temp_paths: list[Path] = field(default_factory=list)


def _source_hash_and_mtime(path: Path) -> tuple[str, int]:
    stat = path.stat()
    payload = f"{path.absolute()}:{stat.st_mtime_ns}"
    return hashlib.sha256(payload.encode()).hexdigest(), stat.st_mtime_ns


class PrepareWorker(QThread):
    track_started  = pyqtSignal(int, str, int)       # index, title, total
    track_progress = pyqtSignal(int, str, float)     # index, phase, fraction
    track_task_status = pyqtSignal(int, str, str, str)  # index, task, status, detail
    track_done     = pyqtSignal(int, str)            # index, title
    track_failed   = pyqtSignal(int, str, str)       # index, title, error
    track_skipped  = pyqtSignal(int, str)            # index, title
    all_done       = pyqtSignal(int, int, int)       # n_ok, n_failed, n_skipped

    def __init__(
        self,
        tracks:       list[LibraryTrack],
        prepared_db:  PreparedDB,
        prepared_root: Path,
        stem_model=None,   # HTDemucsModel or None (skip stems)
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._tracks       = tracks
        self._db           = prepared_db
        self._root         = prepared_root
        self._stem_model   = stem_model

        self._cancel_event = threading.Event()
        self._pause_event  = threading.Event()   # set = paused
        self._stem_cancel  = threading.Event()   # per-stem cancel

    # ── public control ────────────────────────────────────────────────────────

    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()

    def cancel(self) -> None:
        self._cancel_event.set()
        self._stem_cancel.set()
        self._pause_event.clear()

    # ── thread entry ─────────────────────────────────────────────────────────

    def run(self) -> None:
        n_ok = n_failed = n_skipped = 0
        total = len(self._tracks)

        for idx, track in enumerate(self._tracks):
            if self._cancel_event.is_set():
                break

            # Pause wait
            while self._pause_event.is_set():
                if self._cancel_event.is_set():
                    break
                time.sleep(0.1)
            if self._cancel_event.is_set():
                break

            title = track.display_title
            self.track_started.emit(idx, title, total)

            try:
                skipped = self._process_track(idx, track)
                if skipped:
                    n_skipped += 1
                    self.track_skipped.emit(idx, title)
                else:
                    n_ok += 1
                    self.track_done.emit(idx, title)
            except InterruptedError:
                n_skipped += 1
                self.track_skipped.emit(idx, title)
                break
            except Exception as exc:
                n_failed += 1
                tb = traceback.format_exc()
                print(f"[prepare] FAILED {title!r}:\n{tb}", flush=True)
                self.track_failed.emit(idx, title, str(exc))
                try:
                    self._db.mark_failed(track.path, tb[:2000])
                except Exception:
                    pass

        self.all_done.emit(n_ok, n_failed, n_skipped)

    def _checkpoint(self) -> None:
        """Honor pause/cancel requests between expensive preparation phases."""
        while self._pause_event.is_set():
            if self._cancel_event.is_set():
                raise InterruptedError("cancelled")
            time.sleep(0.1)
        if self._cancel_event.is_set():
            raise InterruptedError("cancelled")

    # ── per-track processing ──────────────────────────────────────────────────

    def _process_track(self, idx: int, track: LibraryTrack) -> bool:
        """Process one track. Returns True if skipped (already up-to-date)."""
        path = track.path
        self._checkpoint()

        # Check if already prepared and current
        rec = self._db.find_wrk(path)
        if rec and rec.wrk_ready and Path(rec.wrk_path).exists():
            if rec.is_current(path):
                from wrekker.analysis.beat_tracker import needs_reanalysis
                from wrekker.formats.wrk import load_wrk_metadata

                try:
                    meta = load_wrk_metadata(rec.wrk_path)
                    if not needs_reanalysis(meta.beatgrid):
                        return True   # up-to-date schema-v2 beatgrid, skip
                except Exception:
                    pass
                # Existing .wrk is current but has an old/missing beatgrid schema.
                # Rebuild so WREKKED gets Beat This! beat/downbeat/phrase data.
            # Source changed or analysis schema is old → rebuild
            self._db.mark_outdated(path)

        wrk_id   = wrk_id_for(path)
        wrk_path = wrk_path_for(path, self._root)
        src_hash, src_mtime = _source_hash_and_mtime(path)

        # ── Register as processing ────────────────────────────────────────────
        self._db.upsert_record(
            source_path=path, wrk_path=wrk_path, wrk_id=wrk_id,
            title=track.display_title, artist=track.display_artist,
            album=track.album or "", duration_s=track.duration or 0.0,
            bpm=track.bpm, source_hash=src_hash, source_mtime_ns=src_mtime,
            analysis_status=TrackStatus.PROCESSING,
        )

        statuses = {
            "audio": "pending", "mix_flac": "pending", "metadata": "pending", "waveform": "pending",
            "zoom_waveform": "pending", "lufs": "pending", "key": "pending", "beatgrid": "pending",
            "stems": "pending", "stem_flacs": "waiting", "stem_energy": "waiting",
            "stem_horizon": "waiting", "markers": "waiting", "packing": "waiting", "fastload": "waiting",
        }
        errors: dict[str, str] = {}
        timings_ms: dict[str, int] = {}
        total_started = time.perf_counter()

        # ── Phase 0: decode once ──────────────────────────────────────────────
        self.track_progress.emit(idx, "audio", 0.0)
        self._task_update(idx, statuses, "audio", "running", "decode")
        audio_started = time.perf_counter()
        try:
            from wrekker.audio import load_audio
            audio, sr = load_audio(path)          # (samples, channels) float32
        except Exception as exc:
            statuses["audio"] = TrackStatus.FAILED
            errors["audio"] = str(exc)
            self._persist_details(path, statuses, errors, timings_ms, total_started)
            raise
        timings_ms["audio_decode"] = self._elapsed_ms(audio_started)
        print(f"[prepare] audio decode: {timings_ms['audio_decode']}ms", flush=True)
        self._task_update(idx, statuses, "audio", "done", f"{sr} Hz")
        self.track_progress.emit(idx, "audio", 1.0)
        self._checkpoint()

        ctx = _PrepContext(
            idx=idx, track=track, path=path, wrk_path=wrk_path, wrk_id=wrk_id,
            src_hash=src_hash, src_mtime=src_mtime, audio=audio, sr=sr,
            file_meta={}, started_at=total_started,
            statuses=statuses, errors=errors, timings_ms=timings_ms,
        )

        try:
            self._run_parallel_analysis(ctx)
            self._checkpoint()

            ctx.file_meta = self._result_value(ctx, "metadata") or {}
            beatgrid = self._result_value(ctx, "beatgrid")
            beatgrid_dict = beatgrid.to_dict() if beatgrid is not None and beatgrid.beats else None
            bpm = (
                (beatgrid.bpm if beatgrid is not None and beatgrid.bpm else None)
                or track.bpm
                or ctx.file_meta.get("bpm")
            )
            key_str = self._result_value(ctx, "key")
            waveform = self._result_value(ctx, "waveform")
            if waveform is None:
                raise RuntimeError("waveform generation failed")
            lufs = self._result_value(ctx, "lufs")
            stems = self._result_value(ctx, "stems")
            mix_flac = self._result_value(ctx, "packing.mix_flac_encode")
            stem_flacs = self._result_value(ctx, "packing.stems_flac_encode")
            stems_for_pack = stems if stem_flacs else None
            stem_energy = self._result_value(ctx, "stem_energy") if stem_flacs else None
            markers_list = self._result_value(ctx, "markers")
            stem_horizon = self._result_value(ctx, "stem_horizon")

            if markers_list:
                avg_conf = sum(m.get("confidence", 0.0) for m in markers_list) / len(markers_list)
                marker_status_val = "ready" if avg_conf >= 0.70 else "low_conf"
            else:
                marker_status_val = "failed" if ctx.statuses.get("markers") == TrackStatus.FAILED else "none"
            marker_count_val = len(markers_list or [])

            # ── Phase 3: pack .wrk after required artifacts are ready ─────────────
            self._task_update(idx, statuses, "packing", "running", ".wrk")
            self.track_progress.emit(idx, "packing", 0.0)
            packing_started = time.perf_counter()
            try:
                create_wrk(
                source_path      = path,
                output_path      = wrk_path,
                audio            = audio,
                sr               = sr,
                stems            = stems_for_pack,
                waveform_peaks   = waveform.peaks,
                waveform_colors  = waveform.colors,
                stem_energy      = stem_energy,
                beatgrid         = beatgrid_dict,
                cues             = [],
                loops            = [],
                artwork          = ctx.file_meta.get("artwork"),
                markers          = markers_list,
                stem_horizon     = stem_horizon,
                title            = ctx.file_meta.get("title") or track.display_title,
                artist           = ctx.file_meta.get("artist") or track.display_artist,
                album            = track.album or "",
                bpm              = bpm,
                key              = key_str,
                lufs             = lufs,
                mix_flac         = mix_flac,
                stem_flacs       = stem_flacs,
                prepare_mode     = os.environ.get("WREKKER_PREPARE_MODE", "fast"),
                timing_ms        = timings_ms,
                )
                validate_started = time.perf_counter()
                report = validate_wrk(wrk_path)
                timings_ms["packing.validate"] = self._elapsed_ms(validate_started)
                print(f"[prepare] packing.validate: {timings_ms['packing.validate']}ms", flush=True)
                if not report.valid:
                    raise ValueError(f".wrk validation failed: {report.errors}")
            except Exception as exc:
                self._task_update(idx, statuses, "packing", TrackStatus.FAILED, str(exc))
                errors["packing"] = str(exc)
                self._persist_details(path, statuses, errors, timings_ms, total_started)
                raise
            timings_ms["packing"] = self._elapsed_ms(packing_started)
            for _k in sorted(k for k in timings_ms if k.startswith("packing.")):
                if _k != "packing.validate":
                    print(f"[prepare] {_k}: {timings_ms[_k]}ms", flush=True)
            print(f"[prepare] packing: {timings_ms['packing']}ms", flush=True)
            self._task_update(idx, statuses, "packing", "done", "validated")
            self.track_progress.emit(idx, "packing", 1.0)
            self._checkpoint()

            # Update human-readable per-folder index.json (non-fatal)
            try:
                update_folder_index(wrk_path, path, src_hash)
            except Exception as _e:
                print(f"[prepare] index.json update skipped ({_e})", flush=True)

            # ── Phase 4: fastload mix cache after validation ──────────────────────
            self._task_update(idx, statuses, "fastload", "running", "cache")
            self.track_progress.emit(idx, "fastload", 0.0)
            fastload_started = time.perf_counter()
            fastload_size = 0
            try:
                from wrekker.formats.fastload import FastloadCache, FORMAT_PCM16
                from wrekker.formats.wrk import load_wrk_metadata
                wrk_meta = load_wrk_metadata(wrk_path)
                fastload_cache = FastloadCache()
                fastload_cache.build(
                    wrk_path=wrk_path, meta=wrk_meta,
                    mix_audio=audio, mix_sr=sr,
                    stems_raw=None, audio_format=FORMAT_PCM16,
                )
                fastload_size = fastload_cache.entry_size_bytes(wrk_path)
                print(f"[prepare] fastload cache built: {wrk_path.name}", flush=True)
            except Exception as _e:
                errors["fastload"] = str(_e)
                self._task_update(idx, statuses, "fastload", TrackStatus.FAILED, str(_e))
                print(f"[prepare] fastload cache skipped ({_e})", flush=True)
            else:
                self._task_update(idx, statuses, "fastload", "done", "ready")
            timings_ms["fastload"] = self._elapsed_ms(fastload_started)
            print(f"[prepare] fastload: {timings_ms['fastload']}ms", flush=True)
            wrk_size = wrk_path.stat().st_size if wrk_path.exists() else 0
            print(
                "[prepare] sizes: "
                f"wrk={self._fmt_bytes(wrk_size)} "
                f"fastload={self._fmt_bytes(fastload_size)} "
                f"total={self._fmt_bytes(wrk_size + fastload_size)}",
                flush=True,
            )
            self.track_progress.emit(idx, "fastload", 1.0)
            self._checkpoint()

            total_ms = self._elapsed_ms(total_started)
            timings_ms["total"] = total_ms
            print(f"[prepare] total: {total_ms}ms", flush=True)
            self._persist_details(path, statuses, errors, timings_ms, total_started)

            # ── Register in DB ────────────────────────────────────────────────────
            self._db.upsert_record(
            source_path=path, wrk_path=wrk_path, wrk_id=wrk_id,
            title=ctx.file_meta.get("title") or track.display_title,
            artist=ctx.file_meta.get("artist") or track.display_artist,
            album=track.album or "", duration_s=audio.shape[0] / sr,
            bpm=bpm, key=key_str, lufs=lufs,
            stems_ready=(stem_flacs is not None),
            wrk_ready=True,
            analysis_status=(TrackStatus.READY if beatgrid_dict else TrackStatus.FAILED),
            stem_status=(TrackStatus.READY if stem_flacs else TrackStatus.PENDING),
            wrk_version=1,
            source_hash=src_hash, source_mtime_ns=src_mtime,
            marker_status=marker_status_val,
            marker_count=marker_count_val,
            warnings=self._warnings_for(ctx),
            task_statuses=statuses,
            task_errors=errors,
            task_timings_ms=timings_ms,
            total_preparation_ms=total_ms,
            )
            return False
        finally:
            self._cleanup_temp_paths(ctx)

    # ── preparation scheduler ────────────────────────────────────────────────

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    @staticmethod
    def _fmt_bytes(n: int) -> str:
        value = float(max(0, n))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024.0 or unit == "TB":
                if unit == "B":
                    return f"{int(value)} B"
                return f"{value:.1f} {unit}"
            value /= 1024.0
        return f"{int(n)} B"

    def _task_update(
        self,
        idx: int,
        statuses: dict[str, str],
        task: str,
        status: str,
        detail: str = "",
    ) -> None:
        statuses[task] = status
        self.track_task_status.emit(idx, task, status, detail)

    def _persist_details(
        self,
        path: Path,
        statuses: dict[str, str],
        errors: dict[str, str],
        timings_ms: dict[str, int],
        total_started: float,
    ) -> None:
        try:
            total_ms = timings_ms.get("total", self._elapsed_ms(total_started))
            self._db.update_preparation_details(path, statuses, errors, timings_ms, total_ms)
        except Exception:
            pass

    def _result_value(self, ctx: _PrepContext, task: str):
        result = ctx.results.get(task)
        return result.value if result is not None else None

    def _warnings_for(self, ctx: _PrepContext) -> Optional[str]:
        parts = []
        for task, result in sorted(ctx.results.items()):
            if result.status == TrackStatus.FAILED and result.error:
                parts.append(f"{task}: {result.error}")
        return "\n".join(parts)[:2000] if parts else None

    def _cleanup_temp_paths(self, ctx: _PrepContext) -> None:
        """Remove temporary preparation artifacts created for this track."""
        if os.environ.get("WREKKER_KEEP_PREP_TEMP", "0") == "1":
            return
        for path in ctx.temp_paths:
            try:
                p = Path(path)
                if p.exists():
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
                    print(f"[prepare] temp cleanup: {p}", flush=True)
            except Exception as exc:
                print(f"[prepare] temp cleanup skipped ({type(exc).__name__}: {exc})", flush=True)

    def _run_parallel_analysis(self, ctx: _PrepContext) -> None:
        max_workers = int(os.environ.get("WREKKER_PREPARE_CPU_WORKERS", "4") or "4")
        max_workers = max(2, min(max_workers, 6))

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wrk-prepare") as pool:
            futures = {}

            def submit(name: str, fn: Callable[[], object], phase: str, optional: bool = True):
                self._checkpoint()
                ui_name = self._task_ui_name(name)
                self._task_update(ctx.idx, ctx.statuses, ui_name, "running", "")
                fut = pool.submit(self._run_task, ctx, name, fn, optional)
                futures[fut] = (name, phase, optional)
                return fut

            submit("metadata", lambda: self._task_metadata(ctx), "analysis")
            submit("packing.mix_flac_encode", lambda: self._task_mix_flac(ctx), "packing", optional=False)
            submit("waveform", lambda: self._task_waveform(ctx), "analysis", optional=False)
            submit("lufs", lambda: self._task_lufs(ctx), "analysis")
            submit("key", lambda: self._task_key(ctx), "analysis")
            submit("beatgrid", lambda: self._task_beatgrid(ctx), "analysis")
            submit("stems", lambda: self._task_stems(ctx), "stems")

            stem_energy_submitted = False
            stem_flacs_submitted = False
            stem_horizon_submitted = False
            markers_submitted = False

            while futures:
                self._checkpoint()
                done, _pending = wait(futures.keys(), timeout=0.1, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for fut in done:
                    name, phase, optional = futures.pop(fut)
                    result = fut.result()
                    ctx.results[name] = result
                    if result.status == TrackStatus.READY:
                        status = "skipped" if name == "stems" and result.value is None else "done"
                        ui_name = self._task_ui_name(name)
                        self._task_update(ctx.idx, ctx.statuses, ui_name, status, "")
                        if name == "waveform":
                            zoom_status = "done" if getattr(result.value, "zoom_peaks", None) is not None else "skipped"
                            self._task_update(ctx.idx, ctx.statuses, "zoom_waveform", zoom_status, "")
                    else:
                        ctx.errors[name] = result.error or "failed"
                        self._task_update(ctx.idx, ctx.statuses, self._task_ui_name(name), TrackStatus.FAILED, result.error or "")
                        if not optional:
                            for pending in futures:
                                pending.cancel()
                            raise RuntimeError(f"{name} failed: {result.error}")

                    if phase == "analysis":
                        done_count = sum(
                            1 for task in ("metadata", "waveform", "lufs", "key", "beatgrid")
                            if ctx.statuses.get(task) in ("done", TrackStatus.FAILED)
                        )
                        self.track_progress.emit(ctx.idx, "analysis", done_count / 5.0)

                stems_done = ctx.statuses.get("stems") in ("done", "skipped", TrackStatus.FAILED)
                beat_done = ctx.statuses.get("beatgrid") in ("done", TrackStatus.FAILED)
                wave_done = ctx.statuses.get("waveform") == "done"

                if stems_done and not stem_flacs_submitted:
                    stem_flacs_submitted = True
                    if self._result_value(ctx, "stems") is not None:
                        submit("packing.stems_flac_encode", lambda: self._task_stem_flacs(ctx), "packing")
                    else:
                        ctx.results["packing.stems_flac_encode"] = _PrepResult(None, "skipped", "stems unavailable")
                        self._task_update(ctx.idx, ctx.statuses, "stem_flacs", "skipped", "stems unavailable")

                if stems_done and not stem_energy_submitted:
                    stem_energy_submitted = True
                    if self._result_value(ctx, "stems") is not None:
                        submit("stem_energy", lambda: self._task_stem_energy(ctx), "analysis")
                    else:
                        ctx.results["stem_energy"] = _PrepResult(None, "skipped", "stems unavailable")
                        self._task_update(ctx.idx, ctx.statuses, "stem_energy", "skipped", "stems unavailable")

                stem_energy_done = ctx.statuses.get("stem_energy") in ("done", "skipped", TrackStatus.FAILED)
                if beat_done and stem_energy_done and not stem_horizon_submitted:
                    stem_horizon_submitted = True
                    if self._result_value(ctx, "beatgrid") is not None and self._result_value(ctx, "stem_energy") is not None:
                        submit("stem_horizon", lambda: self._task_stem_horizon(ctx), "analysis")
                    else:
                        ctx.results["stem_horizon"] = _PrepResult(None, "skipped", "stem horizon unavailable")
                if beat_done and stem_energy_done and wave_done and not markers_submitted:
                    markers_submitted = True
                    if self._result_value(ctx, "beatgrid") is not None:
                        submit("markers", lambda: self._task_markers(ctx), "analysis")
                    else:
                        ctx.results["markers"] = _PrepResult(None, TrackStatus.FAILED, "beatgrid unavailable")
                        self._task_update(ctx.idx, ctx.statuses, "markers", TrackStatus.FAILED, "beatgrid unavailable")

            self.track_progress.emit(ctx.idx, "analysis", 1.0)

    @staticmethod
    def _task_ui_name(name: str) -> str:
        if name == "packing.mix_flac_encode":
            return "mix_flac"
        if name == "packing.stems_flac_encode":
            return "stem_flacs"
        return name

    def _run_task(
        self,
        ctx: _PrepContext,
        name: str,
        fn: Callable[[], object],
        optional: bool,
    ) -> _PrepResult:
        started = time.perf_counter()
        try:
            if self._cancel_event.is_set():
                raise InterruptedError("cancelled")
            value = fn()
            if self._cancel_event.is_set():
                raise InterruptedError("cancelled")
            elapsed = self._elapsed_ms(started)
            ctx.timings_ms[name] = elapsed
            print(f"[prepare] {name}: {elapsed}ms", flush=True)
            return _PrepResult(value=value, status=TrackStatus.READY)
        except InterruptedError:
            raise
        except Exception as exc:
            elapsed = self._elapsed_ms(started)
            ctx.timings_ms[name] = elapsed
            print(f"[prepare] {name}: {elapsed}ms FAILED ({exc})", flush=True)
            if optional:
                return _PrepResult(value=None, status=TrackStatus.FAILED, error=str(exc))
            return _PrepResult(value=None, status=TrackStatus.FAILED, error=str(exc))

    def _task_mix_flac(self, ctx: _PrepContext) -> bytes:
        return encode_flac_bytes(ctx.audio, ctx.sr, mode=os.environ.get("WREKKER_PREPARE_MODE", "fast"))

    def _task_waveform(self, ctx: _PrepContext):
        from wrekker.core.transport import _compute_waveform
        return _compute_waveform(ctx.audio, ctx.sr)

    def _task_metadata(self, ctx: _PrepContext) -> dict:
        from wrekker.core.transport import _read_track_meta
        return _read_track_meta(ctx.path)

    def _task_lufs(self, ctx: _PrepContext) -> float:
        audio = ctx.audio.astype(np.float32, copy=False)
        mono = np.mean(audio, axis=1) if audio.ndim > 1 else audio
        ms = float(np.mean(np.square(mono, dtype=np.float64)))
        if ms <= 1e-12:
            return -140.0
        import math
        return float(-0.691 + 10.0 * math.log10(ms))

    def _task_key(self, ctx: _PrepContext) -> Optional[str]:
        from wrekker.core.transport import _detect_key
        key_obj = _detect_key(ctx.audio, ctx.sr)
        return str(key_obj) if key_obj else None

    def _task_beatgrid(self, ctx: _PrepContext):
        from wrekker.analysis.beat_tracker import BeatTracker
        policy = os.environ.get("WREKKER_PREPARE_GPU_POLICY", "beat_cpu").lower()
        device = "cpu" if policy == "beat_cpu" else os.environ.get("WREKKER_BEAT_DEVICE", "cpu")
        checkpoint = os.environ.get("WREKKER_BEAT_CHECKPOINT", "final0")
        use_dbn = os.environ.get("WREKKER_BEAT_USE_DBN", "0") == "1"
        started = time.perf_counter()
        tracker = BeatTracker(checkpoint=checkpoint, device=device, use_dbn=use_dbn)
        ctx.timings_ms["beatgrid.model_load"] = self._elapsed_ms(started)
        print(f"[prepare] beatgrid.model_load: {ctx.timings_ms['beatgrid.model_load']}ms", flush=True)
        started = time.perf_counter()
        if device.startswith("cuda") and policy != "parallel_gpu":
            with _GPU_SEMAPHORE:
                grid = tracker.analyze_audio(ctx.audio, ctx.sr, source=str(ctx.path))
        else:
            grid = tracker.analyze_audio(ctx.audio, ctx.sr, source=str(ctx.path))
        ctx.timings_ms["beatgrid.inference"] = self._elapsed_ms(started)
        print(f"[prepare] beatgrid.inference: {ctx.timings_ms['beatgrid.inference']}ms", flush=True)
        return grid

    def _task_stems(self, ctx: _PrepContext) -> Optional[dict]:
        from wrekker.stems.cache import StemCache
        stem_cache = None
        try:
            stem_cache = StemCache()
        except Exception as exc:
            print(f"[prepare] stem cache unavailable ({type(exc).__name__}: {exc})", flush=True)

        if stem_cache is not None:
            cached = stem_cache.load(ctx.path)
            if cached is not None:
                stem_result, _ = cached
                self.track_progress.emit(ctx.idx, "stems", 1.0)
                return {name: stem_result[name] for name in STEM_NAMES}

        if self._stem_model is None:
            self.track_progress.emit(ctx.idx, "stems", 1.0)
            return None

        self._stem_cancel.clear()

        def _on_progress(frac: float) -> None:
            self.track_progress.emit(ctx.idx, "stems", frac)

        policy = os.environ.get("WREKKER_PREPARE_GPU_POLICY", "beat_cpu").lower()
        lock_gpu = policy != "parallel_gpu"
        if lock_gpu:
            _GPU_SEMAPHORE.acquire()
        try:
            stem_result = self._stem_model.separate(
                ctx.audio.T.astype(np.float32),
                ctx.sr,
                cancel_event=self._stem_cancel,
                on_progress=_on_progress,
            )
        finally:
            if lock_gpu:
                _GPU_SEMAPHORE.release()
        if stem_result is None:
            raise InterruptedError("stem separation cancelled")

        if stem_cache is not None:
            try:
                stem_cache.save(stem_result, ctx.path, ctx.sr)
                ctx.temp_paths.append(stem_cache.root / stem_cache._track_hash(ctx.path))
            except Exception as _e:
                print(f"[prepare] stem cache save skipped ({type(_e).__name__}: {_e})", flush=True)
        return {name: stem_result[name] for name in STEM_NAMES}

    def _task_stem_flacs(self, ctx: _PrepContext) -> Optional[dict[str, bytes]]:
        stems = self._result_value(ctx, "stems")
        if not stems:
            return None
        return encode_stem_flacs(
            stems,
            ctx.sr,
            mode=os.environ.get("WREKKER_PREPARE_MODE", "fast"),
        )

    def _task_stem_energy(self, ctx: _PrepContext) -> Optional[np.ndarray]:
        stems = self._result_value(ctx, "stems")
        if not stems:
            return None
        from wrekker.core.transport import _compute_stem_energy
        from wrekker.stems.models import StemResult as SR
        dummy = SR(
            vocals=stems["vocals"], drums=stems["drums"],
            bass=stems["bass"], other=stems["other"],
            model="prepared", duration_s=ctx.audio.shape[0] / ctx.sr,
        )
        return _compute_stem_energy(dummy)

    def _task_markers(self, ctx: _PrepContext) -> Optional[list]:
        beatgrid = self._result_value(ctx, "beatgrid")
        waveform = self._result_value(ctx, "waveform")
        stem_energy = self._result_value(ctx, "stem_energy")
        if beatgrid is None or waveform is None:
            return None
        from wrekker.analysis.auto_markers import AutoMarkerDetector
        from wrekker.core.deck import MARKER_MIN_CONFIDENCE
        raw_marks = AutoMarkerDetector().analyze(
            beatgrid=beatgrid.to_dict(),
            stem_energy=stem_energy,
            waveform_peaks=waveform.peaks,
            duration_s=ctx.audio.shape[0] / ctx.sr,
        )
        markers = [m.to_dict() for m in raw_marks if m.confidence >= MARKER_MIN_CONFIDENCE]
        print(f"[prepare] auto markers: {len(markers)} detected", flush=True)
        return markers

    def _task_stem_horizon(self, ctx: _PrepContext) -> Optional[dict]:
        beatgrid = self._result_value(ctx, "beatgrid")
        stem_energy = self._result_value(ctx, "stem_energy")
        if beatgrid is None or stem_energy is None:
            return None
        from wrekker.analysis.stem_horizon import generate_stem_horizon
        horizon = generate_stem_horizon(
            beatgrid=beatgrid.to_dict(),
            stem_energy=stem_energy,
            duration_s=ctx.audio.shape[0] / ctx.sr,
        )
        if horizon:
            print(f"[prepare] stem horizon: {len(horizon.get('bars') or [])} bars", flush=True)
        return horizon
