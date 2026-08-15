"""Beatgrid continuity across sections where beat detection drops out.

A solo or breakdown with no percussion leaves a hole in the detected beat
array. Every phase/tempo/phrase query reads consecutive entries as one beat
apart, so that hole used to be interpreted as a single very long beat and the
synced deck drifted for the length of the section.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from wrekker.core.deck import BeatGrid, DeckID, DeckState, PhraseMark, fill_beat_gaps

BPM = 120.0
PERIOD = 60.0 / BPM          # 0.5 s per beat
IDEAL = tuple(round(i * PERIOD, 6) for i in range(401))   # 0 s … 200 s


def _grid_with_solo(
    drop_from: int = 201,
    drop_to: int = 211,
    **kwargs,
) -> BeatGrid:
    """Grid whose beats between two indices were never detected (a solo)."""
    beats = IDEAL[:drop_from] + IDEAL[drop_to:]
    return BeatGrid(bpm=BPM, first_beat_s=0.0, beats=beats, **kwargs)


def test_bridge_reconstructs_the_missing_beats_exactly() -> None:
    grid = _grid_with_solo()

    assert len(grid.beats) == len(IDEAL) - 10      # detection lost 10 beats
    assert grid.has_beat_gaps
    assert grid.grid_beats == pytest.approx(IDEAL, abs=1e-6)
    # The raw analysis record stays untouched — LAB edits work from it.
    assert grid.beats == IDEAL[:201] + IDEAL[211:]


def test_phase_stays_continuous_through_the_solo() -> None:
    grid = _grid_with_solo()

    for pos in (100.25, 101.0, 102.3, 104.75, 105.5):
        expected = ((pos - 0.0) / PERIOD) % 1.0
        assert grid.phase_at(pos) == pytest.approx(expected, abs=1e-6)


def test_local_tempo_holds_through_the_solo() -> None:
    grid = _grid_with_solo()

    # Without bridging this read 60 / 5.5 s ≈ 11 BPM.
    assert grid.local_bpm_at(102.5) == pytest.approx(BPM, abs=0.5)


def test_snap_to_phase_lands_on_the_grid_inside_the_solo() -> None:
    grid = _grid_with_solo()

    snapped = grid.snap_to_phase(0.0, 102.4)

    assert snapped == pytest.approx(102.5, abs=1e-6)
    assert grid.phase_at(snapped) == pytest.approx(0.0, abs=1e-6)


def test_sync_phase_error_between_decks_stays_small_during_a_solo() -> None:
    from wrekker.core.transport import _sync_phase_at

    master = replace(DeckState.empty(DeckID.A), beatgrid=BeatGrid(
        bpm=BPM, first_beat_s=0.0, beats=IDEAL))
    follower = replace(DeckState.empty(DeckID.B), beatgrid=_grid_with_solo())

    # Both decks sit on the same musical position; the follower is mid-solo.
    for pos in (100.5, 102.0, 103.75, 105.0):
        err = _sync_phase_at(master, pos) - _sync_phase_at(follower, pos)
        err -= round(err)
        assert abs(err) < 0.01, f"phase error {err:.3f} beats at {pos}s"


def test_phrase_progress_survives_a_solo() -> None:
    from wrekker.sync.phrase_sync import PhraseLockSync

    phrases = tuple(
        PhraseMark(position_sec=i * 16.0, phrase_length=8) for i in range(13)
    )
    intact = replace(DeckState.empty(DeckID.A), beatgrid=BeatGrid(
        bpm=BPM, first_beat_s=0.0, beats=IDEAL, phrase_markers=phrases))
    gapped = replace(DeckState.empty(DeckID.B), beatgrid=_grid_with_solo(
        phrase_markers=phrases))

    sync = PhraseLockSync()
    for pos in (106.0, 112.0, 128.0):
        assert sync.phrase_progress_beats(gapped, pos) == sync.phrase_progress_beats(intact, pos)


def test_bridge_rejoins_the_real_grid_when_tempo_drifted() -> None:
    # 120 BPM before the gap, slightly slower after it: the bridge must still
    # land exactly on the next detected beat instead of accumulating error.
    before = tuple(round(i * 0.5, 6) for i in range(101))          # … 50.0 s
    after = tuple(round(70.0 + i * 0.52, 6) for i in range(100))   # 70.0 s …
    grid = BeatGrid(bpm=BPM, first_beat_s=0.0, beats=before + after)

    bridged = grid.grid_beats

    assert 50.0 in bridged and 70.0 in bridged
    inside = [b for b in bridged if 50.0 < b < 70.0]
    assert inside, "the gap was not bridged"
    steps = [b - a for a, b in zip(bridged, bridged[1:]) if 50.0 <= a < 70.0]
    assert max(steps) < 0.6, "bridge left an interval that still reads as a long beat"


@pytest.mark.parametrize("beats", [(), (12.5,), (1.0, 1.5, 2.0, 2.5)])
def test_grids_without_gaps_are_left_alone(beats) -> None:
    assert fill_beat_gaps(beats, BPM) == pytest.approx(beats)
    grid = BeatGrid(bpm=BPM, first_beat_s=0.0, beats=beats)
    assert not grid.has_beat_gaps
    assert grid.grid_beats == pytest.approx(beats)


def test_pathological_grid_does_not_explode() -> None:
    # Two beats spanning an entire track must not synthesise unbounded beats.
    grid = BeatGrid(bpm=BPM, first_beat_s=0.0, beats=(0.0, 3600.0))

    assert len(grid.grid_beats) <= 4097
    assert grid.phase_at(1800.0) == pytest.approx(grid.phase_at(1800.0))


def test_downbeats_are_bridged_for_bar_alignment() -> None:
    bars = tuple(round(i * 2.0, 6) for i in range(50))        # a bar every 2 s
    gapped = bars[:20] + bars[28:]                            # 8 bars undetected
    grid = BeatGrid(bpm=BPM, first_beat_s=0.0, beats=IDEAL, downbeats=gapped)

    assert grid.grid_downbeats == pytest.approx(bars, abs=1e-6)
