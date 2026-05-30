"""
Background stem analysis worker.

Single daemon thread consumes from a PriorityQueue.
Jobs are cancelled via threading.Event — cancel() signals the event; the
model's instrumented forward() checks it between chunks and aborts cleanly.

Priority changes are implemented via re-insertion: the old queue entry is
skipped on dequeue when its stored priority no longer matches the job's
current priority.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from queue import PriorityQueue
from typing import Callable

from wrekker.audio import load_audio
from wrekker.stems.cache import StemCache
from wrekker.stems.models import JobID, Priority, StemModel, StemResult

__all__ = ["StemWorker"]


@dataclass
class _Job:
    job_id:       JobID
    track_path:   Path
    priority:     Priority          # mutable — updated by reprioritize()
    cancel_event: threading.Event
    on_complete:  Callable[[JobID, StemResult], None]
    on_progress:  Callable[[JobID, float], None]
    on_error:     Callable[[JobID, Exception], None]


@dataclass(order=True)
class _WorkItem:
    """Queue entry. Ordered by (priority, seq) for FIFO within same priority."""
    priority_val: int
    seq:          int
    job:          _Job = field(compare=False)


class StemWorker:
    def __init__(self, model: StemModel, cache: StemCache) -> None:
        self._model  = model
        self._cache  = cache
        self._queue:  PriorityQueue[_WorkItem] = PriorityQueue()
        self._jobs:   dict[JobID, _Job]  = {}   # live jobs by id
        self._prio:   dict[JobID, int]   = {}   # current priority.value per job
        self._seq     = 0
        self._lock    = threading.Lock()
        self._stop    = threading.Event()
        self._thread  = threading.Thread(
            target=self._run,
            daemon=True,
            name="stem-worker",
        )
        self._thread.start()

    # ── public API ────────────────────────────────────────────────────────────

    def submit(
        self,
        job_id:      JobID,
        track_path:  Path,
        priority:    Priority,
        on_complete: Callable[[JobID, StemResult], None],
        on_progress: Callable[[JobID, float], None],
        on_error:    Callable[[JobID, Exception], None],
    ) -> None:
        job = _Job(
            job_id=job_id,
            track_path=track_path,
            priority=priority,
            cancel_event=threading.Event(),
            on_complete=on_complete,
            on_progress=on_progress,
            on_error=on_error,
        )
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._jobs[job_id] = job
            self._prio[job_id] = priority.value
        self._queue.put(_WorkItem(priority.value, seq, job))

    def cancel(self, job_id: JobID) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job:
            job.cancel_event.set()

    def reprioritize(self, job_id: JobID, new_priority: Priority) -> None:
        """
        Change the priority of a queued job.
        If the job is already being processed, this has no effect on the current
        run but the new priority is stored for any future re-queue.
        """
        with self._lock:
            if job_id not in self._jobs:
                return
            job = self._jobs[job_id]
            job.priority = new_priority
            self._prio[job_id] = new_priority.value
            self._seq += 1
            seq = self._seq
        # Re-insert at new priority; the old item will be skipped on dequeue.
        self._queue.put(_WorkItem(new_priority.value, seq, job))

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    # ── worker loop ───────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except Exception:
                continue

            job = item.job

            with self._lock:
                still_live  = job.job_id in self._jobs
                current_pri = self._prio.get(job.job_id, -1)

            # Skip stale entries: cancelled or superseded by reprioritize()
            if not still_live or job.cancel_event.is_set():
                self._queue.task_done()
                continue
            if current_pri != item.priority_val:
                # A newer entry with the updated priority was inserted; skip this one.
                self._queue.task_done()
                continue

            try:
                self._process(job)
            except Exception as exc:
                job.on_error(job.job_id, exc)
            finally:
                with self._lock:
                    self._jobs.pop(job.job_id, None)
                    self._prio.pop(job.job_id, None)
                self._queue.task_done()

    def _process(self, job: _Job) -> None:
        if job.cancel_event.is_set():
            return

        # Load audio — can be slow for large files; check cancel before separation.
        raw, sr = load_audio(job.track_path)   # (samples, channels) float32
        audio   = raw.T                         # (channels, samples)

        if job.cancel_event.is_set():
            return

        def _progress(frac: float) -> None:
            job.on_progress(job.job_id, frac)

        result = self._model.separate(
            audio,
            sr,
            cancel_event=job.cancel_event,
            on_progress=_progress,
        )

        if result is None or job.cancel_event.is_set():
            return

        self._cache.save(result, job.track_path, sr)
        job.on_complete(job.job_id, result)
