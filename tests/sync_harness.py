"""Headless sync harness.

Drives the real Transport, PLL and beatgrid math against a stubbed engine on a
virtual clock, so a multi-minute mix simulates in milliseconds and runs in CI
without an audio device.

The harness never asks the app how well it is doing. It measures musical drift
itself, from the simulated playback positions against each track's true beat
grid, so a bug that leaves the deck *believing* it is locked while the audio
walks away still fails here. That is the failure mode real sync bugs have:
``DeckState.sync_phase_error`` read fine while a track drifted through a solo.

Run a longer soak by hand:

    python tests/sync_harness.py --minutes 20
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

if __name__ == "__main__":      # `python tests/sync_harness.py` from the repo root
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wrekker.core.deck import BeatGrid, DeckID, DeckState, DeckStatus, TrackInfo
from wrekker.core.transport import Transport

__all__ = [
    "FakeEngine",
    "SimTrack",
    "SyncReport",
    "bump",
    "drifting_track",
    "gridless_track",
    "pitch_move",
    "run_sync",
    "silence_section",
    "steady_track",
]


# ── engine stand-in ───────────────────────────────────────────────────────────

class _FakeDeckProxy:
    """Mirror of engine_v2._DeckProxy: what Transport reads and writes."""

    def __init__(self) -> None:
        self.position_s = 0.0
        self.cue_point_s = 0.0
        self.pitch_factor = 1.0
        self.waveform = None
        self.waveform_seq = 0
        self.stems = None


class FakeEngine:
    """Advances deck positions on a virtual clock at whatever rate sync sets.

    This is the honest part of the model: the Rust callback's only job in the
    sync loop is to play each deck at the rate Transport asked for, and that is
    exactly what this does.
    """

    _sr = 44100

    def __init__(self) -> None:
        self._decks = {"A": _FakeDeckProxy(), "B": _FakeDeckProxy()}
        self._rate = {"A": 1.0, "B": 1.0}
        self._playing = {"A": False, "B": False}
        self.published: dict = {}
        self.min_rate = math.inf
        self.max_rate = -math.inf

    def advance(self, dt: float) -> None:
        for deck_id, deck in self._decks.items():
            if self._playing[deck_id]:
                deck.position_s = max(0.0, deck.position_s + self._rate[deck_id] * dt)

    # Surface Transport drives during sync
    def _get_deck(self, deck_id: str) -> _FakeDeckProxy:
        return self._decks[deck_id]

    def play(self, deck_id: str) -> None:
        self._playing[deck_id] = True

    def pause(self, deck_id: str) -> None:
        self._playing[deck_id] = False

    def is_playing(self, deck_id: str) -> bool:
        return self._playing[deck_id]

    def seek(self, deck_id: str, position_s: float) -> None:
        self._decks[deck_id].position_s = max(0.0, float(position_s))

    def set_playback_rate(self, deck_id: str, rate: float) -> None:
        self._rate[deck_id] = float(rate)
        self.min_rate = min(self.min_rate, float(rate))
        self.max_rate = max(self.max_rate, float(rate))

    def get_playback_rate(self, deck_id: str) -> float:
        return self._rate[deck_id]

    def publish_state(self, states: dict) -> None:
        self.published = dict(states)

    def set_cue_point(self, deck_id: str, position_s: float) -> None:
        self._decks[deck_id].cue_point_s = float(position_s)

    def set_loop(self, *_args) -> None:
        pass

    def set_stem_gain(self, *_args) -> None:
        pass

    def fx_set_bpm(self, _bpm: float) -> None:
        pass

    def wrekk_fx_set_bpm(self, _bpm: float) -> None:
        pass


# ── simulated tracks ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SimTrack:
    """A track with two grids: where the beats really are, and what analysis found.

    ``ideal_beats`` is the oracle — the harness measures against it and the
    application never sees it. ``detected_beats`` is what gets loaded into the
    deck, and may be missing beats the detector could not find.
    """

    name: str
    bpm: float
    ideal_beats: tuple[float, ...]
    detected_beats: tuple[float, ...]
    duration_s: float

    def beat_index(self, pos_s: float) -> float:
        """Fractional beat number at pos_s on the true grid."""
        b = self.ideal_beats
        if len(b) < 2:
            return pos_s / max(60.0 / self.bpm, 1e-6)
        i = bisect.bisect_right(b, pos_s) - 1
        if i < 0:
            return (pos_s - b[0]) / (b[1] - b[0])
        if i >= len(b) - 1:
            return (len(b) - 1) + (pos_s - b[-1]) / (b[-1] - b[-2])
        return i + (pos_s - b[i]) / (b[i + 1] - b[i])


def steady_track(
    name: str,
    bpm: float,
    first_beat_s: float = 0.017,
    duration_s: float = 900.0,
) -> SimTrack:
    period = 60.0 / bpm
    n = int((duration_s - first_beat_s) / period)
    beats = tuple(round(first_beat_s + i * period, 9) for i in range(n))
    return SimTrack(name, bpm, beats, beats, duration_s)


def silence_section(track: SimTrack, start_s: float, end_s: float) -> SimTrack:
    """Same music; analysis found no beats between start_s and end_s (a solo)."""
    kept = tuple(b for b in track.detected_beats if not start_s <= b <= end_s)
    return replace(track, detected_beats=kept)


def drifting_track(
    name: str,
    bpm_start: float,
    bpm_end: float,
    first_beat_s: float = 0.017,
    duration_s: float = 900.0,
) -> SimTrack:
    """Tempo ramps across the track, as a human-played recording would."""
    beats: list[float] = []
    t = first_beat_s
    while t < duration_s:
        beats.append(round(t, 9))
        bpm = bpm_start + (bpm_end - bpm_start) * (t / duration_s)
        t += 60.0 / bpm
    grid = tuple(beats)
    return SimTrack(name, (bpm_start + bpm_end) / 2.0, grid, grid, duration_s)


def gridless_track(name: str, bpm: float, duration_s: float = 900.0) -> SimTrack:
    """Analysis produced no beat positions at all — BPM-only sync."""
    return replace(steady_track(name, bpm, duration_s=duration_s), detected_beats=())


# ── events ────────────────────────────────────────────────────────────────────

def bump(deck_id: str, seconds: float) -> Callable[[Transport, FakeEngine], None]:
    """Knock a deck off phase, the way a hand on the platter would."""
    def _apply(_transport: Transport, engine: FakeEngine) -> None:
        engine.seek(deck_id, engine._get_deck(deck_id).position_s + seconds)
    return _apply


def pitch_move(deck_id: str, semitones: float) -> Callable[[Transport, FakeEngine], None]:
    """Move a deck's pitch fader mid-mix."""
    def _apply(transport: Transport, _engine: FakeEngine) -> None:
        transport.set_pitch(deck_id, semitones)
    return _apply


# ── measurement ───────────────────────────────────────────────────────────────

@dataclass
class SyncReport:
    scenario: str
    simulated_s: float
    samples: int
    max_slip_beats: float
    rms_slip_beats: float
    final_slip_beats: float
    max_reported_error_beats: float
    rate_range: tuple[float, float]
    master_bpm: float

    @property
    def max_slip_ms(self) -> float:
        return self.max_slip_beats * 60_000.0 / max(self.master_bpm, 1.0)

    def __str__(self) -> str:
        lo, hi = self.rate_range
        return (
            f"{self.scenario}\n"
            f"  simulated      {self.simulated_s / 60:.1f} min ({self.samples} samples)\n"
            f"  max slip       {self.max_slip_beats:.4f} beats ({self.max_slip_ms:.1f} ms)\n"
            f"  rms slip       {self.rms_slip_beats:.4f} beats\n"
            f"  final slip     {self.final_slip_beats:+.4f} beats\n"
            f"  reported error {self.max_reported_error_beats:.4f} beats (what the deck believed)\n"
            f"  follower rate  {lo:.4f} … {hi:.4f}"
        )


# ── simulation ────────────────────────────────────────────────────────────────

def _load(transport: Transport, deck_id: str, track: SimTrack, position_s: float) -> None:
    """Put a track on a deck without touching audio decoding."""
    detected = track.detected_beats
    grid = BeatGrid(
        bpm=track.bpm,
        first_beat_s=detected[0] if detected else 0.0,
        confidence=0.9,
        beats=detected,
        downbeats=detected[::4],
    )
    info = TrackInfo(
        path=Path(f"/simulated/{track.name}.flac"),
        title=track.name,
        artist="harness",
        duration_s=track.duration_s,
        sample_rate=44100,
        channels=2,
        bpm=track.bpm,
        key=None,
        file_hash=track.name,
    )
    deck_lock = transport._decks[deck_id]
    with deck_lock.lock:
        deck_lock.state = replace(
            DeckState.empty(DeckID(deck_id)),
            status=DeckStatus.READY,
            track=info,
            beatgrid=grid,
            position_s=position_s,
        )
    transport._engine.seek(deck_id, position_s)


def run_sync(
    master: SimTrack,
    follower: SimTrack,
    *,
    scenario: str = "sync",
    minutes: float = 2.0,
    tick_hz: float = 60.0,
    master_start_s: float = 30.0,
    follower_start_s: float = 30.0,
    settle_s: float = 3.0,
    grace_s: float = 3.0,
    events: Sequence[tuple[float, Callable[[Transport, FakeEngine], None]]] = (),
) -> SyncReport:
    """Simulate a synced mix and report how far the decks actually drifted.

    Slip is cumulative musical drift in beats, measured against a single
    baseline taken once sync has settled: tempo-matched decks advance the same
    number of beats per second, whatever their BPMs, so a non-zero reading is
    real drift and not an artefact of the tempo difference.

    ``events`` fire at the given simulated time — a pitch move, a platter bump.
    Samples inside ``grace_s`` after an event are left out of the max/rms so the
    figures describe locked playback rather than the correction transient, but
    the baseline never moves: drift the PLL fails to recover still shows up.
    """
    engine = FakeEngine()
    transport = Transport(engine, analyzer=None)

    _load(transport, "A", master, master_start_s)
    _load(transport, "B", follower, follower_start_s)

    transport.play("A")
    transport.play("B")
    transport.set_sync_master("A")
    transport.sync("B")

    dt = 1.0 / tick_hz
    total_ticks = int(minutes * 60.0 * tick_hz)
    pending = sorted(events, key=lambda e: e[0])
    next_event = 0
    blackout_until = settle_s

    base_master = base_follower = None
    max_slip = 0.0
    sum_sq = 0.0
    samples = 0
    last_slip = 0.0
    max_reported = 0.0

    for tick in range(total_ticks):
        now = tick * dt
        while next_event < len(pending) and pending[next_event][0] <= now:
            pending[next_event][1](transport, engine)
            blackout_until = now + grace_s
            next_event += 1

        engine.advance(dt)
        transport.tick_sync(dt)

        if now < settle_s:
            continue

        m_idx = master.beat_index(engine._get_deck("A").position_s)
        f_idx = follower.beat_index(engine._get_deck("B").position_s)
        if base_master is None:
            base_master, base_follower = m_idx, f_idx
            continue

        slip = (m_idx - base_master) - (f_idx - base_follower)
        last_slip = slip
        if now < blackout_until:
            continue        # correction transient: tracked, not scored

        max_slip = max(max_slip, abs(slip))
        sum_sq += slip * slip
        samples += 1

        reported = transport.get_state("B").sync_phase_error
        if reported is not None:
            max_reported = max(max_reported, abs(float(reported)))

    return SyncReport(
        scenario=scenario,
        simulated_s=total_ticks * dt,
        samples=samples,
        max_slip_beats=max_slip,
        rms_slip_beats=math.sqrt(sum_sq / samples) if samples else 0.0,
        final_slip_beats=last_slip,
        max_reported_error_beats=max_reported,
        rate_range=(engine.min_rate, engine.max_rate),
        master_bpm=master.bpm,
    )


# ── manual soak ───────────────────────────────────────────────────────────────

def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="WREKKER sync soak test")
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--tick-hz", type=float, default=60.0)
    args = parser.parse_args()

    master = steady_track("master", 128.0)
    follower = steady_track("follower", 124.0, first_beat_s=1.13)
    scenarios = [
        ("identical tempo", master,
         steady_track("follower", 128.0, first_beat_s=0.41), 0.02),
        ("124 → 128 match", master, follower, 0.02),
        ("follower solo 90–140 s", master,
         silence_section(follower, 90.0, 140.0), 0.02),
        ("master solo 70–110 s", silence_section(master, 70.0, 110.0), follower, 0.02),
        ("drifting master 126 → 130", drifting_track("master", 126.0, 130.0),
         steady_track("follower", 128.0, first_beat_s=0.41), 0.25),
    ]

    failures = 0
    for name, m, f, limit in scenarios:
        report = run_sync(m, f, scenario=name, minutes=args.minutes, tick_hz=args.tick_hz)
        over = report.max_slip_beats > limit
        failures += over
        print(report)
        print(f"  {'FAIL' if over else 'ok'}  (limit {limit:.2f} beats)\n")

    if failures:
        print(f"{failures} scenario(s) drifted past their limit")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
