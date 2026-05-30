"""
DJ play queue.

Maintains an ordered list of LibraryTrack entries and tracks which deck each
track is assigned to. Deck assignments are advisory — the Transport still needs
to be told to load a track; the queue just keeps state and history.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from wrekker.library.models import LibraryTrack

__all__ = ["PlayQueue", "QueueEntry"]


@dataclass
class QueueEntry:
    track:   LibraryTrack
    deck:    Optional[str] = None   # "A" | "B" | None (unassigned)
    played:  bool          = False

    def assign(self, deck: str) -> "QueueEntry":
        return QueueEntry(track=self.track, deck=deck, played=self.played)

    def mark_played(self) -> "QueueEntry":
        return QueueEntry(track=self.track, deck=self.deck, played=True)


class PlayQueue:
    """
    Thread-safe ordered play queue.

    Supports:
    - Append / insert / remove
    - Deck assignment per entry
    - Cursor: current position (the track being loaded/played)
    - next() / prev() navigation
    - History (played entries stay visible, marked played=True)
    """

    def __init__(self) -> None:
        self._lock:    threading.Lock      = threading.Lock()
        self._entries: list[QueueEntry]    = []
        self._cursor:  int                 = -1   # index of current track

    # ── read ─────────────────────────────────────────────────────────────────

    def entries(self) -> list[QueueEntry]:
        with self._lock:
            return list(self._entries)

    def current(self) -> Optional[QueueEntry]:
        with self._lock:
            if 0 <= self._cursor < len(self._entries):
                return self._entries[self._cursor]
        return None

    def peek_next(self) -> Optional[QueueEntry]:
        with self._lock:
            idx = self._cursor + 1
            if 0 <= idx < len(self._entries):
                return self._entries[idx]
        return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ── write ─────────────────────────────────────────────────────────────────

    def append(self, track: LibraryTrack, deck: Optional[str] = None) -> QueueEntry:
        entry = QueueEntry(track=track, deck=deck)
        with self._lock:
            self._entries.append(entry)
        return entry

    def insert_next(self, track: LibraryTrack, deck: Optional[str] = None) -> QueueEntry:
        """Insert immediately after the current cursor position."""
        entry = QueueEntry(track=track, deck=deck)
        with self._lock:
            insert_at = self._cursor + 1
            self._entries.insert(insert_at, entry)
        return entry

    def remove(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self._entries):
                self._entries.pop(index)
                if index <= self._cursor:
                    self._cursor = max(-1, self._cursor - 1)

    def move(self, from_index: int, to_index: int) -> None:
        with self._lock:
            n = len(self._entries)
            if not (0 <= from_index < n and 0 <= to_index < n):
                return
            entry = self._entries.pop(from_index)
            self._entries.insert(to_index, entry)
            # Adjust cursor
            if from_index == self._cursor:
                self._cursor = to_index
            elif from_index < self._cursor <= to_index:
                self._cursor -= 1
            elif to_index <= self._cursor < from_index:
                self._cursor += 1

    def assign_deck(self, index: int, deck: str) -> None:
        with self._lock:
            if 0 <= index < len(self._entries):
                self._entries[index] = self._entries[index].assign(deck)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._cursor = -1

    # ── cursor navigation ─────────────────────────────────────────────────────

    def next(self) -> Optional[QueueEntry]:
        """
        Advance cursor to next entry, mark current as played.
        Returns the new current entry, or None if at end.
        """
        with self._lock:
            if 0 <= self._cursor < len(self._entries):
                self._entries[self._cursor] = self._entries[self._cursor].mark_played()
            next_idx = self._cursor + 1
            if next_idx < len(self._entries):
                self._cursor = next_idx
                return self._entries[self._cursor]
        return None

    def prev(self) -> Optional[QueueEntry]:
        """Move cursor back. Does NOT unmark played."""
        with self._lock:
            prev_idx = self._cursor - 1
            if prev_idx >= 0:
                self._cursor = prev_idx
                return self._entries[self._cursor]
        return None

    def jump_to(self, index: int) -> Optional[QueueEntry]:
        with self._lock:
            if 0 <= index < len(self._entries):
                # Mark everything before as played
                for i in range(index):
                    if not self._entries[i].played:
                        self._entries[i] = self._entries[i].mark_played()
                self._cursor = index
                return self._entries[self._cursor]
        return None

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._cursor
