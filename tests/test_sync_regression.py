"""Sync regression scenarios driven by the headless harness.

Every threshold is cumulative musical drift measured independently of what the
deck believes, in beats. 0.02 beats is ~9 ms at 128 BPM — around the point a
beatmatching DJ starts to hear a flam.

Thresholds are loose enough for both PLL backends: the Rust controller is
proportional with a 0.02-beat dead zone, the Python fallback is PI and settles
to zero, so a disturbance leaves a slightly different residue in each.
"""
from __future__ import annotations

import pytest

from sync_harness import (
    bump,
    drifting_track,
    gridless_track,
    pitch_move,
    run_sync,
    silence_section,
    steady_track,
)

LOCKED = 0.02          # beats — nothing should move at all
RECOVERED = 0.05       # beats — after a disturbance the PLL settles inside this
TRACKING = 0.15        # beats — following a continuously moving master


def _master() -> "steady_track":
    return steady_track("master", 128.0)


def _follower() -> "steady_track":
    return steady_track("follower", 124.0, first_beat_s=1.13)


def test_identical_tempos_stay_locked() -> None:
    report = run_sync(
        _master(), steady_track("follower", 128.0, first_beat_s=0.41),
        scenario="identical tempos", minutes=3.0,
    )
    assert report.samples > 5_000
    assert report.max_slip_beats < LOCKED, report


def test_tempo_matched_decks_stay_locked() -> None:
    report = run_sync(_master(), _follower(), scenario="124 → 128", minutes=3.0)
    assert report.max_slip_beats < LOCKED, report


def test_sync_holds_through_a_solo_in_the_follower() -> None:
    """The reported regression: a stretch the detector found no beats in."""
    report = run_sync(
        _master(), silence_section(_follower(), 90.0, 140.0),
        scenario="follower solo 90–140 s", minutes=3.0,
    )
    assert report.max_slip_beats < LOCKED, report


def test_sync_holds_through_a_solo_in_the_master() -> None:
    report = run_sync(
        silence_section(_master(), 70.0, 110.0), _follower(),
        scenario="master solo 70–110 s", minutes=3.0,
    )
    assert report.max_slip_beats < LOCKED, report


def test_follower_tracks_a_master_pitch_move() -> None:
    report = run_sync(
        _master(), _follower(), scenario="master pitch +0.5 st", minutes=3.0,
        events=[(60.0, pitch_move("A", 0.5))],
    )
    assert report.max_slip_beats < RECOVERED, report


@pytest.mark.parametrize("offset_s", [0.035, -0.080])
def test_pll_recovers_from_a_platter_bump(offset_s: float) -> None:
    report = run_sync(
        _master(), _follower(), scenario=f"bump {offset_s * 1000:+.0f} ms", minutes=3.0,
        events=[(45.0, bump("B", offset_s))],
    )
    assert report.max_slip_beats < RECOVERED, report
    # It must settle, not keep sliding in the direction of the bump.
    assert abs(report.final_slip_beats) < RECOVERED, report


def test_drifting_master_does_not_accumulate_unbounded_slip() -> None:
    report = run_sync(
        drifting_track("master", 126.0, 130.0),
        steady_track("follower", 128.0, first_beat_s=0.41),
        scenario="master drifting 126 → 130", minutes=3.0,
    )
    assert report.max_slip_beats < TRACKING, report


def test_bpm_only_sync_without_a_beatgrid() -> None:
    report = run_sync(
        _master(), gridless_track("follower", 124.0),
        scenario="no beatgrid", minutes=3.0,
    )
    assert report.max_slip_beats < LOCKED, report


def test_harness_catches_a_broken_beatgrid(monkeypatch) -> None:
    """The harness must fail loudly on the bug it exists to prevent.

    Reverting BeatGrid to reading raw detected beats reproduces the pre-fix
    behaviour: a solo is treated as one long beat. Without this check the
    scenarios above could pass for the wrong reason.
    """
    from wrekker.core.deck import BeatGrid

    monkeypatch.setattr(BeatGrid, "grid_beats", property(lambda self: self.beats))
    report = run_sync(
        _master(), silence_section(_follower(), 90.0, 140.0),
        scenario="solo with gap bridging disabled", minutes=3.0,
    )

    assert report.max_slip_beats > 0.5, report
    # And the deck did not know: it reported far less error than it really had.
    assert report.max_reported_error_beats < report.max_slip_beats
