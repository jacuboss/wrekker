#!/usr/bin/env python3
"""
DDJ-FLX4 MIDI protocol monitor.

Logs every raw MIDI message with hex values and detects 14-bit CC pairs.

Usage:
    python tools/flx4_monitor.py              # live log, all messages
    python tools/flx4_monitor.py --map        # deduplicated map only
    python tools/flx4_monitor.py --led NOTE   # send LED on/off to test feedback

Notes:
    Mido channels are 0-based:
        mido_ch=0 means MIDI channel 1
        mido_ch=4 means MIDI channel 5
"""

from __future__ import annotations

import sys
import time
import argparse
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import mido
from rich.console import Console
from rich.table import Table
from rich.live import Live

console = Console()

FLX4_PATTERN = "DDJ-FLX4"


def find_port(pattern: str, direction: str = "input") -> str:
    fn = mido.get_input_names if direction == "input" else mido.get_output_names
    ports = fn()
    matches = [p for p in ports if pattern in p]

    if not matches:
        console.print(f"[red]No MIDI port matching '{pattern}' found.[/red]")
        console.print(f"Available: {ports}")
        sys.exit(1)

    return matches[0]


def _midi_channel_display(mido_channel: int | str) -> str:
    """
    Mido channels are 0-based.
    Human MIDI channels are 1-based.
    """
    if isinstance(mido_channel, int):
        return f"mido_ch={mido_channel} midi_ch={mido_channel + 1}"
    return "mido_ch=- midi_ch=-"


def _msg_label(msg: mido.Message) -> str:
    ch = getattr(msg, "channel", "-")
    ch_label = _midi_channel_display(ch)

    if msg.type == "control_change":
        return (
            f"CC   {ch_label} "
            f"cc=0x{msg.control:02X}({msg.control:3d})"
        )

    if msg.type in ("note_on", "note_off"):
        return (
            f"{msg.type[:4]} {ch_label} "
            f"note=0x{msg.note:02X}({msg.note:3d})"
        )

    if msg.type == "sysex":
        return f"SYSX data={msg.data[:8]}..."

    if msg.type == "pitchwheel":
        return f"PTCH {ch_label}"

    return f"{msg.type:<8} {ch_label}"


def _msg_value(msg: mido.Message) -> str:
    if msg.type == "control_change":
        return f"val={msg.value:3d} (0x{msg.value:02X})"

    if msg.type in ("note_on", "note_off"):
        return f"vel={msg.velocity:3d} (0x{msg.velocity:02X})"

    if msg.type == "pitchwheel":
        return f"pitch={msg.pitch}"

    return ""


class CC14Tracker:
    """
    Tracks 14-bit MIDI CC pairs.

    MIDI 14-bit CC uses:
        MSB: CC 0–31
        LSB: CC 32–63

    Example:
        CC 2  = coarse / MSB
        CC 34 = fine / LSB because 34 = 2 + 32

    Combined value:
        value14 = (MSB << 7) | LSB
        normalized = value14 / 16383.0
    """

    def __init__(self) -> None:
        self._state: dict[tuple[int, int], dict[str, int]] = {}

    def update(self, msg: mido.Message) -> tuple[int, int, int, int, int, float] | None:
        if msg.type != "control_change":
            return None

        ch = msg.channel
        cc = msg.control
        val = msg.value

        # MSB / coarse CC
        if 0 <= cc <= 31:
            base_cc = cc
            state = self._state.setdefault((ch, base_cc), {"msb": 0, "lsb": 0})
            state["msb"] = val

        # LSB / fine CC
        elif 32 <= cc <= 63:
            base_cc = cc - 32
            state = self._state.setdefault((ch, base_cc), {"msb": 0, "lsb": 0})
            state["lsb"] = val

        else:
            return None

        msb = state["msb"]
        lsb = state["lsb"]
        value14 = (msb << 7) | lsb
        normalized = value14 / 16383.0

        return ch, base_cc, msb, lsb, value14, normalized


def monitor(show_map: bool = False, decode_14bit: bool = True) -> None:
    port_in = find_port(FLX4_PATTERN, "input")

    console.print(f"[cyan]FLX4 monitor[/cyan] → [dim]{port_in}[/dim]")
    console.print(
        "[dim]Move controls to map the protocol. Ctrl-C to exit.[/dim]\n"
    )
    console.print(
        "[dim]Mido channels are 0-based: mido_ch=4 means MIDI channel 5.[/dim]\n"
    )

    seen: dict[str, list[str]] = defaultdict(list)
    cc14 = CC14Tracker()

    def build_map_table() -> Table:
        table = Table(title="Accumulated map / unique controls", box=None)
        table.add_column("Control", style="cyan", min_width=52)
        table.add_column("Recent values")
        table.add_column("Count", justify="right")

        for label, vals in sorted(seen.items()):
            sample = " ".join(vals[-5:])
            table.add_row(label, sample, str(len(vals)))

        return table

    with mido.open_input(port_in) as port:
        if show_map:
            with Live(build_map_table(), refresh_per_second=4, console=console) as live:
                for msg in port:
                    if msg.type == "clock":
                        continue

                    if decode_14bit and msg.type == "control_change":
                        combined = cc14.update(msg)
                        if combined is not None:
                            ch, base_cc, msb, lsb, value14, norm = combined
                            label = (
                                f"CC14 mido_ch={ch} midi_ch={ch + 1} "
                                f"msb_cc=0x{base_cc:02X}({base_cc}) "
                                f"lsb_cc=0x{base_cc + 32:02X}({base_cc + 32})"
                            )
                            seen[label].append(f"{value14}/{norm:.4f}")
                            live.update(build_map_table())
                            continue

                    label = _msg_label(msg)
                    value = _msg_value(msg)
                    seen[label].append(value)
                    live.update(build_map_table())

        else:
            for msg in port:
                if msg.type == "clock":
                    continue

                ts = f"[dim]{time.monotonic():.3f}[/dim]"

                if decode_14bit and msg.type == "control_change":
                    combined = cc14.update(msg)
                    if combined is not None:
                        ch, base_cc, msb, lsb, value14, norm = combined
                        console.print(
                            f"{ts}  "
                            f"CC14 mido_ch={ch} midi_ch={ch + 1} "
                            f"msb_cc=0x{base_cc:02X}({base_cc:3d}) "
                            f"lsb_cc=0x{base_cc + 32:02X}({base_cc + 32:3d}) "
                            f"msb={msb:3d} lsb={lsb:3d} "
                            f"value14={value14:5d} norm={norm:.4f}"
                        )
                        continue

                label = _msg_label(msg)
                value = _msg_value(msg)
                console.print(f"{ts}  {label:<64} {value}")


def send_led(note: int) -> None:
    port_out = find_port(FLX4_PATTERN, "output")

    console.print(
        f"[cyan]LED test[/cyan] note=0x{note:02X} ({note}) → [dim]{port_out}[/dim]"
    )

    with mido.open_output(port_out) as out:
        for mido_ch in (0, 1):
            for vel in (0x7F, 0x00):
                msg = mido.Message(
                    "note_on",
                    channel=mido_ch,
                    note=note,
                    velocity=vel,
                )
                out.send(msg)
                console.print(
                    f"  sent: {msg!r} "
                    f"({ _midi_channel_display(mido_ch) })"
                )
                time.sleep(0.3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DDJ-FLX4 MIDI monitor")

    parser.add_argument(
        "--map",
        action="store_true",
        help="Deduplicated map view",
    )

    parser.add_argument(
        "--led",
        type=lambda x: int(x, 0),
        metavar="NOTE",
        help="Test LED: note number, hex ok: 0x0B",
    )

    parser.add_argument(
        "--raw-cc",
        action="store_true",
        help="Disable 14-bit CC decoding and print raw CC messages only",
    )

    args = parser.parse_args()

    if args.led is not None:
        send_led(args.led)
    else:
        monitor(show_map=args.map, decode_14bit=not args.raw_cc)