"""Phrase-locked sync helpers.

This module never processes audio. It only reads DeckState/BeatGrid metadata
and returns timing offsets that Transport/UI can apply through the Rust engine.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Optional

from wrekker.core.deck import BeatGrid, DeckState


@dataclass(frozen=True)
class _PhrasePoint:
    position_s: float
    length_bars: int

    @property
    def length_beats(self) -> int:
        return max(1, self.length_bars * 4)


class PhraseLockSync:
    """
    Aligns decks on 8/16-bar musical phrase boundaries.

    Positive offsets mean the slave should wait before entering. Negative
    offsets mean the slave phrase boundary is later than the master's target
    and transport may choose to seek or keep beat-only sync.
    """

    def compute_phrase_offset(
        self,
        master_deck: DeckState,
        slave_deck: DeckState,
    ) -> float:
        master_boundary = self.next_phrase_boundary(master_deck)
        slave_boundary = self.next_phrase_boundary(slave_deck)
        if master_boundary < 0.0 or slave_boundary < 0.0:
            return 0.0
        master_wait = max(0.0, master_boundary - master_deck.position_s)
        slave_wait = max(0.0, slave_boundary - slave_deck.position_s)
        return master_wait - slave_wait

    def snap_slave_to_phrase(
        self,
        master_deck: DeckState,
        slave_deck: DeckState,
    ) -> float:
        """
        Return a slave position whose beat index inside the phrase matches the
        master's current beat index. Chooses the nearest matching phrase point.
        """
        master_bg = master_deck.beatgrid
        slave_bg = slave_deck.beatgrid
        if master_bg is None or slave_bg is None or not slave_bg.beats:
            return -1.0

        target_progress = self.phrase_progress_beats(master_deck)
        points = self._phrase_points(slave_bg)
        if not points:
            return -1.0

        best_pos = None
        best_dist = float("inf")
        near_pos = max(0.0, slave_deck.position_s)
        for point in points:
            progress = target_progress % point.length_beats
            marker_idx = max(0, bisect.bisect_right(slave_bg.beats, point.position_s) - 1)
            beat_idx = marker_idx + progress
            if 0 <= beat_idx < len(slave_bg.beats):
                candidate = float(slave_bg.beats[beat_idx])
            else:
                candidate = point.position_s + progress * slave_bg.beat_period_s
            dist = abs(candidate - near_pos)
            if dist < best_dist:
                best_dist = dist
                best_pos = candidate

        return max(0.0, best_pos) if best_pos is not None else -1.0

    def next_phrase_boundary(self, deck: DeckState) -> float:
        bg = deck.beatgrid
        if bg is None:
            return -1.0

        points = self._phrase_points(bg)
        pos = max(0.0, deck.position_s)
        if points:
            positions = tuple(p.position_s for p in points)
            i = bisect.bisect_right(positions, pos + 1e-6)
            if i < len(points):
                return points[i].position_s
            return self._extrapolate_next_phrase(bg, points[-1], pos)

        return self._regular_phrase_boundary(bg, pos, 8)

    def phrase_progress_beats(
        self,
        deck: DeckState,
        position_s: Optional[float] = None,
    ) -> int:
        """Return current beat index inside the active phrase."""
        bg = deck.beatgrid
        if bg is None or not bg.beats:
            return 0
        pos = max(0.0, deck.position_s if position_s is None else position_s)
        beat_idx = max(0, bisect.bisect_right(bg.beats, pos) - 1)
        point = self._active_phrase_point(bg, pos)
        if point is None:
            phrase_len = 32
            return beat_idx % phrase_len
        marker_idx = max(0, bisect.bisect_right(bg.beats, point.position_s) - 1)
        return (beat_idx - marker_idx) % point.length_beats

    def phrase_length_beats(
        self,
        deck: DeckState,
        position_s: Optional[float] = None,
    ) -> int:
        """Return active phrase length in beats, defaulting to 8 bars."""
        bg = deck.beatgrid
        if bg is None:
            return 32
        pos = max(0.0, deck.position_s if position_s is None else position_s)
        point = self._active_phrase_point(bg, pos)
        return point.length_beats if point is not None else 32

    def phrase_progress_fraction(
        self,
        deck: DeckState,
        position_s: Optional[float] = None,
    ) -> float:
        length = self.phrase_length_beats(deck, position_s)
        if length <= 0:
            return 0.0
        return self.phrase_progress_beats(deck, position_s) / float(length)

    def is_phrase_locked(self, master_deck: DeckState, slave_deck: DeckState) -> bool:
        master_progress = self.phrase_progress_beats(master_deck)
        slave_progress = self.phrase_progress_beats(slave_deck)
        return master_progress == slave_progress

    def is_phrase_locked_at(
        self,
        master_deck: DeckState,
        master_position_s: float,
        slave_deck: DeckState,
        slave_position_s: float,
    ) -> bool:
        master_progress = self.phrase_progress_beats(master_deck, master_position_s)
        slave_progress = self.phrase_progress_beats(slave_deck, slave_position_s)
        return master_progress == slave_progress

    def _phrase_points(self, bg: BeatGrid) -> tuple[_PhrasePoint, ...]:
        if bg.phrase_markers:
            return tuple(
                _PhrasePoint(
                    position_s=float(mark.position_sec),
                    length_bars=16 if int(mark.phrase_length) >= 16 else 8,
                )
                for mark in bg.phrase_markers
            )

        if bg.downbeats:
            return tuple(
                _PhrasePoint(position_s=float(pos), length_bars=8)
                for idx, pos in enumerate(bg.downbeats)
                if idx % 8 == 0
            )

        if bg.beats:
            return tuple(
                _PhrasePoint(position_s=float(pos), length_bars=8)
                for idx, pos in enumerate(bg.beats)
                if idx % 32 == 0
            )

        return ()

    def _active_phrase_point(self, bg: BeatGrid, pos: float) -> _PhrasePoint | None:
        points = self._phrase_points(bg)
        if not points:
            return None
        positions = tuple(p.position_s for p in points)
        idx = bisect.bisect_right(positions, pos) - 1
        if idx < 0:
            return points[0]
        return points[idx]

    def _extrapolate_next_phrase(
        self,
        bg: BeatGrid,
        last: _PhrasePoint,
        pos: float,
    ) -> float:
        if bg.beats:
            beat_idx = max(0, bisect.bisect_right(bg.beats, last.position_s) - 1)
            step = last.length_beats
            next_idx = beat_idx + step
            while next_idx < len(bg.beats) and bg.beats[next_idx] <= pos + 1e-6:
                next_idx += step
            if next_idx < len(bg.beats):
                return float(bg.beats[next_idx])

        phrase_s = last.length_beats * bg.beat_period_s
        n = max(1, int((pos - last.position_s) // phrase_s) + 1)
        return last.position_s + n * phrase_s

    def _regular_phrase_boundary(self, bg: BeatGrid, pos: float, bars: int) -> float:
        length_beats = bars * 4
        if bg.beats:
            i = max(0, bisect.bisect_right(bg.beats, pos))
            next_i = ((i + length_beats - 1) // length_beats) * length_beats
            if next_i < len(bg.beats):
                return float(bg.beats[next_i])

        phrase_s = length_beats * bg.beat_period_s
        first = bg.first_beat_s
        n = max(0, int((pos - first) // phrase_s) + 1)
        return first + n * phrase_s
