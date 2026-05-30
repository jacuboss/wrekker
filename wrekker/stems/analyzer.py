"""
Public API for stem analysis.

StemAnalyzer orchestrates the cache, worker, and per-track status tracking.
All public methods are thread-safe.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Callable

from wrekker.stems.cache import StemCache
from wrekker.stems.models import JobID, Priority, StemModel, StemResult, StemStatus
from wrekker.stems.worker import StemWorker

__all__ = ["StemAnalyzer"]


class StemAnalyzer:
    def __init__(
        self,
        model: StemModel,
        cache_root: Path | None = None,
    ) -> None:
        self._cache  = StemCache(cache_root)
        self._worker = StemWorker(model, self._cache)
        self._status: dict[Path, StemStatus] = {}
        self._jobs:   dict[Path, JobID]      = {}   # track_path → active job_id
        self._lock   = threading.Lock()

        # Purge stale entries without blocking startup
        threading.Thread(
            target=self._cache.purge_old,
            daemon=True,
            name="cache-purge",
        ).start()

    # ── public API ────────────────────────────────────────────────────────────

    def analyze(
        self,
        track_path: Path,
        priority:   Priority,
        on_complete: Callable[[StemResult], None],
        on_progress: Callable[[float], None],
    ) -> JobID:
        """
        Start analysis of track_path at the given priority.

        If stems are already cached, on_complete is called synchronously and
        an empty string is returned (no worker job needed).

        Otherwise returns a JobID that can be passed to cancel() or used with
        reprioritize() on the underlying worker.
        """
        # Serve from cache immediately if available
        cached = self._cache.load(track_path)
        if cached is not None:
            result, _sr = cached
            with self._lock:
                self._status[track_path] = StemStatus.READY
            on_complete(result)
            return ""

        job_id = str(uuid.uuid4())

        with self._lock:
            # Cancel any existing job for this track
            old_job = self._jobs.get(track_path)
            if old_job:
                self._worker.cancel(old_job)

            self._status[track_path] = StemStatus.QUEUED
            self._jobs[track_path]   = job_id

        def _on_complete(jid: JobID, result: StemResult) -> None:
            with self._lock:
                self._status[track_path] = StemStatus.READY
                self._jobs.pop(track_path, None)
            on_complete(result)

        def _on_progress(jid: JobID, frac: float) -> None:
            with self._lock:
                # First progress call marks transition from QUEUED → ANALYZING
                if self._status.get(track_path) == StemStatus.QUEUED:
                    self._status[track_path] = StemStatus.ANALYZING
            on_progress(frac)

        def _on_error(jid: JobID, exc: Exception) -> None:
            with self._lock:
                self._status[track_path] = StemStatus.FAILED
                self._jobs.pop(track_path, None)

        self._worker.submit(
            job_id,
            track_path,
            priority,
            _on_complete,
            _on_progress,
            _on_error,
        )
        return job_id

    def cancel(self, job_id: JobID) -> None:
        self._worker.cancel(job_id)

    def get_status(self, track_path: Path) -> StemStatus:
        if self._cache.is_cached(track_path):
            return StemStatus.READY
        with self._lock:
            return self._status.get(track_path, StemStatus.NONE)

    def get_stems(self, track_path: Path) -> StemResult | None:
        cached = self._cache.load(track_path)
        if cached is None:
            return None
        result, _sr = cached
        return result

    def shutdown(self, timeout: float = 5.0) -> None:
        self._worker.shutdown(timeout=timeout)
