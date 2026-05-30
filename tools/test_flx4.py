#!/usr/bin/env python3
"""
DDJ-FLX4 driver smoke test.

Connects the FLX4Driver to a stub Transport/Engine and prints every translated
action. No audio output — confirms the MIDI mapping is alive.

Usage:
    python tools/test_flx4.py
"""
from __future__ import annotations

import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.live import Live
from rich.table import Table

console = Console()


# ── stub Transport / Engine that just logs calls ──────────────────────────────

class _StubEngine:
    pass


class _StubTransport:
    """Minimal stub: logs every call with rich, returns plausible state."""

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._log   = []   # list of (timestamp, action)
        self._state = _make_empty_states()

    def _record(self, action: str) -> None:
        with self._lock:
            self._log.append((time.monotonic(), action))
            if len(self._log) > 40:
                self._log.pop(0)

    def get_state(self, deck_id: str):
        return self._state[deck_id]

    def get_all_states(self):
        return dict(self._state)

    def play(self, deck_id: str):                self._record(f"[green]PLAY[/green] {deck_id}")
    def pause(self, deck_id: str):               self._record(f"[yellow]PAUSE[/yellow] {deck_id}")
    def cue(self, deck_id: str):                 self._record(f"CUE {deck_id}")
    def seek(self, deck_id: str, pos: float):    self._record(f"SEEK {deck_id} → {pos:.3f}s")
    def set_pitch(self, deck_id: str, pct: float): self._record(f"PITCH {deck_id} {pct:+.2f}st")
    def loop_in(self, deck_id: str):             self._record(f"LOOP_IN {deck_id}")
    def loop_out(self, deck_id: str):            self._record(f"LOOP_OUT {deck_id}")
    def loop_toggle(self, deck_id: str):         self._record(f"LOOP_TOGGLE {deck_id}")
    def add_cue(self, deck_id: str, **kw):       self._record(f"ADD_CUE {deck_id} {kw}")
    def jump_to_cue(self, deck_id: str, idx: int): self._record(f"JUMP_CUE {deck_id} #{idx}")
    def set_crossfader(self, val: float):        self._record(f"XFADER → {val:.3f}")
    def set_master_gain(self, gain: float):      self._record(f"MASTER_GAIN → {gain:.3f}")
    def set_stem_gain(self, deck_id: str, stem: str, gain: float):
        self._record(f"STEM_GAIN {deck_id}:{stem} → {gain:.2f}")
    def mute_stem(self, deck_id: str, stem: str, muted: bool):
        self._record(f"MUTE {deck_id}:{stem} {'on' if muted else 'off'}")
    def solo_stem(self, deck_id: str, stem: str, solo: bool):
        self._record(f"SOLO {deck_id}:{stem} {'on' if solo else 'off'}")

    # Internal plumbing accessed by FLX4Driver for loop manipulation
    @property
    def _decks(self):
        return self._state   # both share the same _lock attr pattern via StubDeck

    def _set_state(self, dl, *args, **kwargs):
        self._record(f"_SET_STATE {dl.id}")


def _make_empty_states():
    from wrekker.core.deck import (
        DeckID, DeckStatus, DeckState, StemState, LoudnessMeasure,
        STEM_NAMES,
    )
    def _empty_deck(deck_id, did):
        stems = {
            n: StemState(gain=1.0, muted=False, solo=False, lufs=None, spectral=None)
            for n in STEM_NAMES
        }
        return DeckState(
            id=deck_id, status=DeckStatus.EMPTY,
            track=None, position_s=0.0,
            pitch_pct=0.0, bpm_live=0.0,
            stems=stems, stems_status="none",
            loop=None, cue_points=(),
            sync_enabled=False, sync_master=False,
            metrics=None,
        )

    class _FakeDL:
        def __init__(self, did, state):
            self.id    = did
            self.lock  = threading.Lock()
            self.state = state

    return {
        "A": _FakeDL("A", _empty_deck(DeckID.A, "A")),
        "B": _FakeDL("B", _empty_deck(DeckID.B, "B")),
    }


# ── live display ──────────────────────────────────────────────────────────────

def _build_table(transport: _StubTransport) -> Table:
    t = Table(title="FLX4 driver — translated actions", box=None, expand=True)
    t.add_column("t (s)", style="dim", width=8)
    t.add_column("Action")
    t0 = transport._log[0][0] if transport._log else time.monotonic()
    with transport._lock:
        rows = list(transport._log)
    for ts, action in rows:
        t.add_row(f"{ts - t0:.2f}", action)
    return t


def main() -> None:
    from wrekker.hardware.flx4 import FLX4Driver

    engine    = _StubEngine()
    transport = _StubTransport()

    console.print("[cyan]FLX4 driver smoke test[/cyan]")
    console.print("[dim]Move controls on the DDJ-FLX4. Ctrl-C to quit.[/dim]\n")

    driver = FLX4Driver(transport=transport, engine=engine)  # type: ignore[arg-type]
    try:
        driver.start()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    try:
        with Live(console=console, refresh_per_second=8) as live:
            while True:
                live.update(_build_table(transport))
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        driver.stop()
        console.print("[dim]Stopped.[/dim]")


if __name__ == "__main__":
    main()
