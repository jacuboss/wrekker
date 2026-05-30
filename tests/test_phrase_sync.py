from dataclasses import replace

from wrekker.core.deck import BeatGrid, DeckID, DeckState, PhraseMark
from wrekker.sync import PhraseLockSync


def _grid() -> BeatGrid:
    beats = tuple(i * 0.5 for i in range(256))
    return BeatGrid(
        bpm=120.0,
        first_beat_s=0.0,
        confidence=1.0,
        beats=beats,
        downbeats=tuple(beats[i] for i in range(0, len(beats), 4)),
        phrase_markers=(
            PhraseMark(0.0, 8, 0.5),
            PhraseMark(16.0, 8, 0.6),
            PhraseMark(32.0, 8, 0.7),
        ),
        schema_version=2,
    )


def _state(deck_id: DeckID, pos: float, bg: BeatGrid) -> DeckState:
    return replace(DeckState.empty(deck_id), position_s=pos, beatgrid=bg)


def test_phrase_progress_from_markers():
    sync = PhraseLockSync()
    state = _state(DeckID.A, 7.0, _grid())

    assert sync.phrase_progress_beats(state) == 14
    assert sync.phrase_length_beats(state) == 32
    assert sync.phrase_progress_fraction(state) == 14 / 32


def test_phrase_lock_detects_same_progress():
    sync = PhraseLockSync()
    bg = _grid()
    master = _state(DeckID.A, 7.0, bg)
    slave = _state(DeckID.B, 23.0, bg)

    assert sync.is_phrase_locked(master, slave)


def test_snap_slave_to_phrase_nearest_equivalent_beat():
    sync = PhraseLockSync()
    bg = _grid()
    master = _state(DeckID.A, 7.0, bg)   # beat 14 in phrase
    slave = _state(DeckID.B, 18.0, bg)

    target = sync.snap_slave_to_phrase(master, slave)

    assert target == 23.0
