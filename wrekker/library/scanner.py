"""
Incremental library scanner.

Walks a directory tree, reads audio metadata via mutagen, and writes tracks to
LibraryDB. Only files whose hash (path + mtime) is not already in the DB are
processed, making re-scans fast even for large libraries.

Usage (programmatic):
    scanner = LibraryScanner(db)
    for progress in scanner.scan(Path("/music"), on_track=print):
        pass   # progress is ScanProgress

Usage (one-shot):
    LibraryScanner(db).scan_blocking(Path("/music"))
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generator, Iterator, Optional

from wrekker.library.database import LibraryDB
from wrekker.library.models import LibraryTrack, SearchQuery, is_audio, track_id

__all__ = ["LibraryScanner", "ScanProgress"]

try:
    import mutagen
    from mutagen import File as MutagenFile
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3NoHeaderError
    _HAS_MUTAGEN = True
except ImportError:
    _HAS_MUTAGEN = False


@dataclass
class ScanProgress:
    total_found:   int
    processed:     int
    new_tracks:    int
    skipped:       int
    errors:        int
    current_path:  str
    done:          bool = False

    @property
    def pct(self) -> float:
        return (self.processed / self.total_found * 100) if self.total_found else 0.0


def _read_mutagen(path: Path) -> dict:
    """
    Extract metadata from an audio file using mutagen.
    Returns a plain dict with string/float/int fields (never raises).
    """
    meta: dict = {
        "title": "", "artist": "", "album": "", "genre": "",
        "duration": 0.0, "bpm": None,
        "year": None, "bitrate": None, "sr": None, "channels": None,
    }
    if not _HAS_MUTAGEN:
        return meta
    try:
        f = MutagenFile(str(path), easy=True)
        if f is None:
            return meta

        def _tag(key: str, default: str = "") -> str:
            val = f.tags.get(key) if f.tags else None
            if val is None:
                return default
            return str(val[0]) if isinstance(val, list) else str(val)

        meta["title"] = _tag("title", path.stem)
        meta["album"] = _tag("album")
        meta["genre"] = _tag("genre")

        # Artist: try a priority list of fields
        for field in ("artist", "albumartist", "album_artist", "performer", "composer"):
            v = _tag(field)
            if v:
                meta["artist"] = v
                break

        year_raw = _tag("date") or _tag("year")
        if year_raw:
            try:
                meta["year"] = int(year_raw[:4])
            except (ValueError, IndexError):
                pass

        bpm_raw = _tag("bpm")
        if bpm_raw:
            try:
                meta["bpm"] = float(bpm_raw)
            except ValueError:
                pass

        # Duration / bitrate / sample rate from audio stream info
        if hasattr(f, "info") and f.info:
            info = f.info
            if hasattr(info, "length"):
                meta["duration"] = float(info.length)
            if hasattr(info, "bitrate"):
                meta["bitrate"] = int(info.bitrate // 1000) if info.bitrate else None
            if hasattr(info, "sample_rate"):
                meta["sr"] = int(info.sample_rate)
            if hasattr(info, "channels"):
                meta["channels"] = int(info.channels)

    except Exception:
        pass
    return meta


class LibraryScanner:
    """
    Incremental directory scanner that writes tracks to LibraryDB.

    Parameters
    ----------
    db:
        Open LibraryDB instance.
    batch_size:
        Number of tracks to upsert in a single DB transaction.
    """

    def __init__(self, db: LibraryDB, batch_size: int = 50) -> None:
        self._db         = db
        self._batch_size = batch_size

    def scan(
        self,
        root:     Path,
        on_track: Optional[Callable[[LibraryTrack], None]] = None,
    ) -> Generator[ScanProgress, None, None]:
        """
        Generator that yields ScanProgress after each processed file.

        Yields the final ScanProgress with done=True when finished.
        """
        root = root.resolve()

        # Collect all audio files first (fast walk)
        audio_files: list[Path] = []
        for p in root.rglob("*"):
            if p.is_file() and is_audio(p):
                audio_files.append(p)

        total = len(audio_files)
        known = self._db.known_ids(root)

        progress = ScanProgress(
            total_found=total, processed=0, new_tracks=0,
            skipped=0, errors=0, current_path="",
        )

        batch: list[LibraryTrack] = []

        for path in audio_files:
            progress.current_path = str(path)

            try:
                tid = track_id(path)
            except OSError:
                progress.errors += 1
                progress.processed += 1
                yield progress
                continue

            if tid in known:
                progress.skipped  += 1
                progress.processed += 1
                yield progress
                continue

            # New or changed file — read metadata
            try:
                meta  = _read_mutagen(path)
                track = LibraryTrack(
                    id       = tid,
                    path     = path,
                    title    = meta["title"] or path.stem,
                    artist   = meta["artist"],
                    album    = meta["album"],
                    genre    = meta["genre"],
                    duration = meta["duration"],
                    bpm      = meta["bpm"],
                    key_num  = None,
                    key_mode = None,
                    year     = meta["year"],
                    bitrate  = meta["bitrate"],
                    sr       = meta["sr"],
                    channels = meta["channels"],
                    added_at = time.time(),
                )
                batch.append(track)
                known.add(tid)
                progress.new_tracks += 1

                if on_track:
                    on_track(track)

                # Flush batch
                if len(batch) >= self._batch_size:
                    self._db.upsert_many(batch)
                    batch.clear()

            except Exception:
                progress.errors += 1

            progress.processed += 1
            yield progress

        # Flush remaining
        if batch:
            self._db.upsert_many(batch)

        # Remove tracks whose files disappeared.  Skip when the walk found
        # nothing but the DB knows tracks under this root — that pattern means
        # the root is an unmounted share, not a genuinely emptied library.
        if audio_files or not known:
            self._db.remove_missing(root)

        progress.done = True
        yield progress

    def scan_blocking(
        self,
        root:     Path,
        on_progress: Optional[Callable[[ScanProgress], None]] = None,
    ) -> ScanProgress:
        """Synchronous scan — blocks until complete. Returns final ScanProgress."""
        last = None
        for prog in self.scan(root):
            last = prog
            if on_progress:
                on_progress(prog)
        return last  # type: ignore[return-value]

    def scan_async(
        self,
        root:        Path,
        on_progress: Optional[Callable[[ScanProgress], None]] = None,
        on_done:     Optional[Callable[[ScanProgress], None]] = None,
    ) -> threading.Thread:
        """
        Start scan in a daemon thread.
        Returns the Thread (already started).
        """
        def _worker():
            result = self.scan_blocking(root, on_progress)
            if on_done:
                on_done(result)

        t = threading.Thread(target=_worker, daemon=True, name=f"scan-{root.name}")
        t.start()
        return t
