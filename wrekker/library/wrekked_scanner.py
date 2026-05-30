"""
WrekkedScanner — discovers prepared sets from the local prepared/tracks/ tree.

Folder layout expected:
    ~/.local/share/wrekker/prepared/tracks/{set_name}/*.wrk

Each immediate sub-directory becomes one PreparedSet.  Every .wrk file inside
is registered as a WrekkedTrack.  The scanner reads only the manifest from each
.wrk (fast, no FLAC decode) to populate title/artist/bpm/key.

source_available is set to True only when the source_path from the .wrk manifest
actually exists on disk (e.g. SMB is mounted).  It is informational — the track
is always loadable via load_wrk_track() regardless.
"""

from __future__ import annotations

import hashlib
import zipfile
import json
import logging
import shutil
from pathlib import Path
from typing import Optional, Callable

from wrekker.config.paths import data_dir
from wrekker.library.prepared_db import PreparedDB, TrackStatus

__all__ = ["WrekkedScanner"]

log = logging.getLogger(__name__)

_DEFAULT_PREPARED_ROOT = data_dir() / "prepared" / "tracks"


def _wrk_id_for(source_path: str) -> str:
    """Reproduce the wrk_id as assigned during preparation (sha256 of abs source path)."""
    return hashlib.sha256(source_path.encode()).hexdigest()


def _inspect_wrk(wrk_path: Path) -> Optional[dict]:
    """
    Read only manifest.json from the .wrk ZIP.  Returns the parsed dict or None
    on any error.  Does NOT decode any audio.
    """
    try:
        with zipfile.ZipFile(wrk_path, "r") as zf:
            with zf.open("manifest.json") as f:
                return json.loads(f.read().decode("utf-8"))
    except Exception as exc:
        log.warning("WrekkedScanner: cannot read %s — %s", wrk_path, exc)
        return None


def _source_info(manifest: dict) -> tuple[str, str | None, int | None]:
    """Return (source_path, source_hash, source_mtime_ns) across old/new manifests."""
    source = manifest.get("source", {})
    source_path = (
        source.get("path")
        or manifest.get("source_path")
        or manifest.get("metadata", {}).get("source_path")
        or ""
    )
    source_hash = source.get("hash") or manifest.get("source_hash")
    source_mtime = source.get("mtime_ns") or manifest.get("source_mtime_ns")
    try:
        source_mtime = int(source_mtime) if source_mtime is not None else None
    except (TypeError, ValueError):
        source_mtime = None
    return str(source_path), source_hash, source_mtime


def _beatgrid_needs_schema_v2(wrk_path: Path) -> bool:
    """True when analysis/beatgrid.json is missing or older than schema v2."""
    try:
        from wrekker.analysis.beat_tracker import needs_reanalysis

        with zipfile.ZipFile(wrk_path, "r") as zf:
            names = set(zf.namelist())
            if "analysis/beatgrid.json" not in names:
                return True
            beatgrid = json.loads(zf.read("analysis/beatgrid.json"))
        return needs_reanalysis(beatgrid)
    except Exception:
        return True


class WrekkedScanner:
    """
    Scans the prepared/tracks/ tree and registers sets + tracks into PreparedDB.

    Designed to run in a background thread at startup and on demand (RESCAN button).
    An optional progress callback receives (current, total) integer counts.
    """

    def __init__(
        self,
        db: PreparedDB,
        prepared_root: Optional[Path] = None,
    ) -> None:
        self._db   = db
        self._root = Path(prepared_root) if prepared_root else _DEFAULT_PREPARED_ROOT
        self.last_removed_corrupt = 0
        self.last_quarantined_corrupt = 0
        self.last_needs_beatgrid_upgrade = 0

    def scan(
        self,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """
        Scan the prepared/tracks/ tree.  Returns the total number of .wrk files
        processed.  Safe to call from a background thread.
        """
        if not self._root.exists():
            log.debug("WrekkedScanner: prepared root does not exist: %s", self._root)
            return 0

        # Collect all set dirs (immediate children that are directories)
        set_dirs = [
            d for d in self._root.iterdir() if d.is_dir()
        ]
        if not set_dirs:
            return 0

        # Enumerate .wrk files per set so we can report accurate totals
        set_wrks: list[tuple[Path, list[Path]]] = []
        for set_dir in set_dirs:
            wrks = [p for p in set_dir.iterdir() if p.is_file() and p.suffix.lower() == ".wrk"]
            if wrks:
                set_wrks.append((set_dir, wrks))

        total = sum(len(w) for _, w in set_wrks)
        done  = 0

        removed_corrupt = 0
        self.last_removed_corrupt = 0
        self.last_quarantined_corrupt = 0
        self.last_needs_beatgrid_upgrade = 0

        for set_dir, wrks in set_wrks:
            set_name = set_dir.name
            set_id   = self._db.upsert_wrekked_set(
                name              = set_name,
                source_root_label = None,
                source_root_path  = None,
            )

            for position, wrk_path in enumerate(wrks):
                registered = self._register_track(set_id, wrk_path, position)
                if registered is None:
                    try:
                        self._quarantine_corrupt_wrk(wrk_path)
                        self._db.remove_by_wrk_path(wrk_path)
                        removed_corrupt += 1
                        log.warning("WrekkedScanner: quarantined corrupt .wrk: %s", wrk_path)
                    except Exception as exc:
                        log.warning("WrekkedScanner: failed to quarantine corrupt .wrk %s — %s", wrk_path, exc)
                elif registered:
                    self.last_needs_beatgrid_upgrade += 1
                done += 1
                if on_progress:
                    on_progress(done, total)

            self._db.update_set_track_count(set_id)

        if removed_corrupt:
            log.warning("WrekkedScanner: quarantined %d corrupt .wrk file(s)", removed_corrupt)
        self.last_removed_corrupt = removed_corrupt
        self.last_quarantined_corrupt = removed_corrupt
        return total

    def _quarantine_corrupt_wrk(self, wrk_path: Path) -> Path:
        """Move an unreadable .wrk out of active sets without permanently deleting it."""
        quarantine_dir = self._root / "_corrupt_wrks"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / wrk_path.name
        if target.exists():
            stem = wrk_path.stem
            suffix = wrk_path.suffix
            i = 1
            while target.exists():
                target = quarantine_dir / f"{stem}.{i}{suffix}"
                i += 1
        shutil.move(str(wrk_path), str(target))
        return target

    def _register_track(
        self,
        set_id:   int,
        wrk_path: Path,
        position: int,
    ) -> Optional[bool]:
        """
        Register one .wrk.

        Returns:
          - None if corrupt/unreadable
          - True if the .wrk is readable but needs beatgrid schema-v2 upgrade
          - False if readable and current
        """
        manifest = _inspect_wrk(wrk_path)
        if manifest is None:
            return None

        # Derive wrk_id from the source_path recorded in the manifest when
        # available; fall back to hashing the wrk_path itself so offline tracks
        # always get a stable id.
        source_path_str, source_hash, source_mtime_ns = _source_info(manifest)
        if source_path_str:
            wrk_id = _wrk_id_for(source_path_str)
        else:
            wrk_id = hashlib.sha256(str(wrk_path.absolute()).encode()).hexdigest()

        source_available = bool(source_path_str and Path(source_path_str).exists())
        needs_beatgrid_upgrade = _beatgrid_needs_schema_v2(wrk_path)

        meta = manifest.get("metadata", manifest)  # some wrks nest under "metadata"
        title      = meta.get("title", "") or wrk_path.stem
        artist     = meta.get("artist", "") or ""
        duration_s = float(meta.get("duration_s", 0.0))
        bpm_raw    = meta.get("bpm")
        bpm        = float(bpm_raw) if bpm_raw is not None else None
        key        = meta.get("key") or None

        # Determine stems/wrk readiness from manifest contents list
        contents   = manifest.get("contents", {})
        wrk_ready  = bool(contents.get("has_mix", True))
        stems_ready = bool(contents.get("has_stems", False))

        self._db.upsert_set_track(
            set_id           = set_id,
            wrk_id           = wrk_id,
            wrk_path         = str(wrk_path.absolute()),
            title            = title,
            artist           = artist,
            duration_s       = duration_s,
            bpm              = bpm,
            key              = key,
            stems_ready      = stems_ready,
            wrk_ready        = wrk_ready,
            source_available = source_available,
            position         = position,
            update_position  = False,
        )

        if source_path_str and source_available:
            status = TrackStatus.OUTDATED if needs_beatgrid_upgrade else TrackStatus.READY
            warning = (
                "beatgrid schema < 2; re-run Prepare to upgrade"
                if needs_beatgrid_upgrade else None
            )
            self._db.upsert_record(
                source_path       = Path(source_path_str),
                wrk_path          = wrk_path,
                wrk_id            = wrk_id,
                title             = title,
                artist            = artist,
                duration_s        = duration_s,
                bpm               = bpm,
                key               = key,
                stems_ready       = stems_ready,
                wrk_ready         = wrk_ready,
                analysis_status   = status,
                stem_status       = TrackStatus.READY if stems_ready else TrackStatus.PENDING,
                wrk_version       = int(manifest.get("wrk_version", 0) or 0),
                warnings          = warning,
                source_hash       = source_hash,
                source_mtime_ns   = source_mtime_ns,
            )

        return needs_beatgrid_upgrade
