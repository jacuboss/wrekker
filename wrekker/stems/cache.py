"""
Disk cache for stem separation results.

Layout: {root}/{sha256(abs_path + mtime_ns)}/
    vocals.wav  drums.wav  bass.wav  other.wav  meta.json

Hash invalidation is automatic: if the source file changes (mtime),
the hash changes and the old cache entry is orphaned (cleaned by purge_old).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from wrekker.stems.models import STEM_NAMES, StemResult

__all__ = ["StemCache"]

_META_FILE = "meta.json"
_DEFAULT_MAX_AGE_DAYS = 7


class StemCache:
    _disabled_roots: set[str] = set()

    def __init__(self, root: Path | None = None) -> None:
        configured = (
            root
            or os.environ.get("WREKKER_STEM_CACHE_PATH")
            or os.environ.get("WREKKER_TEMP_STEM_CACHE_PATH")
        )
        self._root = Path(configured) if configured else Path("/tmp/wrekker/stems")
        self._root.mkdir(parents=True, exist_ok=True)

    # ── public API ────────────────────────────────────────────────────────────

    def is_cached(self, track_path: Path) -> bool:
        d = self._entry_dir(track_path)
        if not d.exists():
            return False
        has_stems = all((d / f"{name}.wav").exists() for name in STEM_NAMES)
        return has_stems and (d / _META_FILE).exists()

    def save(self, result: StemResult, track_path: Path, sr: int) -> None:
        root_key = str(self._root.absolute())
        if root_key in self._disabled_roots:
            raise RuntimeError(f"stem cache disabled for unwritable path: {self._root}")

        d = self._entry_dir(track_path)
        try:
            d.mkdir(parents=True, exist_ok=True)

            for name in STEM_NAMES:
                stem: np.ndarray = result[name]  # (channels, samples)
                sf.write(
                    str(d / f"{name}.wav"),
                    stem.T,          # soundfile expects (samples, channels)
                    sr,
                    subtype="FLOAT",
                )

            meta = {
                "model":      result.model,
                "duration_s": result.duration_s,
                "sample_rate": sr,
                "created_at": time.time(),
            }
            (d / _META_FILE).write_text(json.dumps(meta))
        except Exception as exc:
            shutil.rmtree(d, ignore_errors=True)
            if isinstance(exc, OSError) and exc.errno in (
                errno.ENOSPC, errno.EACCES, errno.EROFS,
            ):
                self._disabled_roots.add(root_key)
            raise RuntimeError(self._diagnostic(exc)) from exc

    def load(self, track_path: Path) -> tuple[StemResult, int] | None:
        """Return (StemResult, sample_rate) or None if not cached / corrupted."""
        d = self._entry_dir(track_path)
        if not self.is_cached(track_path):
            return None

        try:
            meta = json.loads((d / _META_FILE).read_text())
            sr   = meta["sample_rate"]
            stems: dict[str, np.ndarray] = {}
            for name in STEM_NAMES:
                audio, file_sr = sf.read(
                    str(d / f"{name}.wav"),
                    dtype="float32",
                    always_2d=True,
                )
                if file_sr != sr:
                    # Metadata mismatch — treat as corrupted
                    return None
                stems[name] = audio.T  # (channels, samples)

            return (
                StemResult(
                    vocals=stems["vocals"],
                    drums=stems["drums"],
                    bass=stems["bass"],
                    other=stems["other"],
                    model=meta["model"],
                    duration_s=meta["duration_s"],
                ),
                sr,
            )
        except Exception:
            return None

    def purge_old(self, max_age_days: int = _DEFAULT_MAX_AGE_DAYS) -> int:
        """Remove entries older than max_age_days. Returns count removed."""
        cutoff = time.time() - max_age_days * 86_400
        removed = 0
        for entry in self._root.iterdir():
            if not entry.is_dir():
                continue
            meta_path = entry / _META_FILE
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
                if meta.get("created_at", 0) < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except Exception:
                continue
        return removed

    # ── internals ─────────────────────────────────────────────────────────────

    def _track_hash(self, track_path: Path) -> str:
        stat = track_path.stat()
        payload = f"{track_path.absolute()}:{stat.st_mtime_ns}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def _entry_dir(self, track_path: Path) -> Path:
        return self._root / self._track_hash(track_path)

    def remove(self, track_path: Path) -> bool:
        """Remove one cached track entry. Returns True when an entry was removed."""
        d = self._entry_dir(track_path)
        if not d.exists():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True

    @property
    def root(self) -> Path:
        return self._root

    def _diagnostic(self, exc: Exception) -> str:
        probe = self._root if self._root.exists() else self._root.parent
        try:
            usage = shutil.disk_usage(probe)
            free = usage.free
            total = usage.total
        except Exception:
            free = total = -1
        writable = os.access(probe, os.W_OK)
        errno_val = getattr(exc, "errno", None)
        return (
            f"{type(exc).__name__}: {exc}; errno={errno_val}; "
            f"cache_path={self._root}; writable={writable}; "
            f"disk_free={free}; disk_total={total}"
        )
