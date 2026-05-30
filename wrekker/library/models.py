"""
Library data models.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = ["LibraryTrack", "SearchQuery", "track_id"]

_AUDIO_EXTS = {
    ".mp3", ".flac", ".ogg", ".opus", ".wav", ".aiff", ".aif",
    ".m4a", ".mp4", ".wma", ".alac",
}


def track_id(path: Path) -> str:
    """Stable hash: absolute path + mtime_ns."""
    stat    = path.stat()
    payload = f"{path.absolute()}:{stat.st_mtime_ns}"
    return hashlib.sha256(payload.encode()).hexdigest()


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in _AUDIO_EXTS


@dataclass(frozen=True)
class LibraryTrack:
    id:        str           # sha256(path+mtime)
    path:      Path
    title:     str
    artist:    str
    album:     str
    genre:     str
    duration:  float         # seconds
    bpm:       Optional[float]
    key_num:   Optional[int]  # Camelot number 1-12
    key_mode:  Optional[str]  # "A" | "B"
    year:      Optional[int]
    bitrate:   Optional[int]  # kbps
    sr:        Optional[int]  # sample rate Hz
    channels:  Optional[int]
    added_at:  float          # unix timestamp

    @property
    def display_title(self) -> str:
        return self.title or self.path.stem

    @property
    def display_artist(self) -> str:
        return self.artist or "Unknown"

    @property
    def duration_str(self) -> str:
        m, s = divmod(int(self.duration), 60)
        return f"{m}:{s:02d}"

    @property
    def bpm_str(self) -> str:
        return f"{self.bpm:.1f}" if self.bpm else "—"

    @property
    def key_str(self) -> str:
        if self.key_num and self.key_mode:
            return f"{self.key_num}{self.key_mode}"
        return "—"


@dataclass(frozen=True)
class SearchQuery:
    text:       str   = ""
    artist:     str   = ""
    album:      str   = ""
    genre:      str   = ""
    bpm_min:    Optional[float] = None
    bpm_max:    Optional[float] = None
    key_num:    Optional[int]   = None
    key_mode:   Optional[str]   = None
    limit:      int   = 200
    offset:     int   = 0
