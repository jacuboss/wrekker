#!/usr/bin/env python3
"""
Wrekker stem engine — proof of concept CLI.

Usage:  python tools/test_stems.py <audio_file>

Flow:
  1. Starts htdemucs analysis in background (rich progress bar)
  2. Plays original audio immediately via sounddevice
  3. When stems are ready, switches to stem-based mixing transparently
  4. Keys (no Enter): v=vocals  d=drums  b=bass  o=other  q=quit
  5. Final report: avg callback latency, RAM usage
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
import tty
import termios
import select
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Project root on sys.path so we can run from repo root without install
sys.path.insert(0, str(Path(__file__).parent.parent))

from wrekker.audio import load_audio
from wrekker.stems.models import HTDemucsModel, Priority, StemResult, STEM_NAMES
from wrekker.stems.analyzer import StemAnalyzer

# ── palette ──────────────────────────────────────────────────────────────────

_C = {
    "vocals": "red",
    "drums":  "cyan",
    "bass":   "yellow",
    "other":  "magenta",
}
_KEY_MAP = {"v": "vocals", "d": "drums", "b": "bass", "o": "other"}

console = Console()

# ── crossfade / playback engine ───────────────────────────────────────────────

_SAMPLE_RATE_DEFAULT = 44100
_FADE_MS             = 500.0   # crossfade duration in ms


class CrossfadeState:
    """Per-stem gain with equal-power crossfade. Accessed from audio thread."""

    def __init__(self, sr: int, fade_ms: float) -> None:
        self._fade_n  = int(sr * fade_ms / 1000.0)
        self._current = np.ones(4, dtype=np.float64)
        self._target  = np.ones(4, dtype=np.float64)
        self._faded   = np.zeros(4, dtype=np.int64)   # samples already faded

    def set_target(self, idx: int, value: float) -> None:
        """Call from main thread. Not lock-protected — see note below."""
        # Writes to individual float64 are effectively atomic on x86/ARM.
        # The audio thread only reads _current and ramps toward _target;
        # a torn write here at worst causes a one-frame click, acceptable.
        self._target[idx] = value
        self._faded[idx]  = 0

    def get_ramp(self, idx: int, frames: int) -> np.ndarray:
        """Return gain ramp of length `frames`. Called from audio thread only."""
        cur = self._current[idx]
        tgt = self._target[idx]

        if cur == tgt:
            return np.full(frames, cur, dtype=np.float32)

        fade_rem = self._fade_n - self._faded[idx]
        fade_now = min(frames, fade_rem)

        # Equal-power curve: sin for activate, cos for deactivate
        t = np.linspace(0.0, float(fade_now) / self._fade_n, fade_now, dtype=np.float32)
        if tgt > cur:
            curve = np.sin(t * (math.pi / 2)) * (tgt - cur) + cur
        else:
            curve = (np.cos(t * math.pi) * 0.5 + 0.5) * (cur - tgt) + tgt

        ramp = np.empty(frames, dtype=np.float32)
        ramp[:fade_now] = curve
        if fade_now < frames:
            ramp[fade_now:] = tgt

        self._faded[idx]   += fade_now
        self._current[idx]  = float(ramp[-1])
        if self._faded[idx] >= self._fade_n:
            self._current[idx] = tgt

        return ramp


class PlaybackEngine:
    """
    Sounddevice callback-based playback with toggleable stems.

    Before stems are ready: plays original audio.
    After stems arrive: mixes the 4 stems in real time with crossfade.
    """

    def __init__(self, original: np.ndarray, sr: int) -> None:
        self._original  = original         # (samples, 2) float32
        self._sr        = sr
        self._stems:    Optional[StemResult] = None
        self._read_pos  = 0
        self._active    = [True, True, True, True]  # one per STEM_NAMES
        self._fade      = CrossfadeState(sr, _FADE_MS)
        self._latencies: list[float] = []

    def set_stems(self, result: StemResult) -> None:
        """Called from analysis callback (not audio thread)."""
        self._stems = result

    def toggle(self, name: str) -> bool:
        """Toggle stem by name. Returns new active state."""
        idx    = STEM_NAMES.index(name)
        active = not self._active[idx]
        self._active[idx] = active
        self._fade.set_target(idx, 1.0 if active else 0.0)
        return active

    def callback(
        self,
        outdata: np.ndarray,
        frames:  int,
        time_info,
        status,
    ) -> None:
        t0  = time.perf_counter()
        pos = self._read_pos

        stems = self._stems  # single reference read — safe without lock

        if stems is not None:
            mix = np.zeros((frames, 2), dtype=np.float32)
            for i, name in enumerate(STEM_NAMES):
                stem  = stems[name]                    # (channels, samples)
                end   = min(pos + frames, stem.shape[-1])
                chunk_len = end - pos
                if chunk_len <= 0:
                    continue
                chunk = stem[:, pos:end].T             # (chunk_len, channels)
                if chunk.shape[1] == 1:
                    chunk = np.repeat(chunk, 2, axis=1)

                gain = self._fade.get_ramp(i, chunk_len)
                mix[:chunk_len] += chunk * gain[:, np.newaxis]
            outdata[:] = mix
        else:
            end       = min(pos + frames, self._original.shape[0])
            chunk_len = end - pos
            outdata[:chunk_len] = self._original[pos:end]
            if chunk_len < frames:
                outdata[chunk_len:] = 0.0

        self._read_pos = pos + frames
        self._latencies.append((time.perf_counter() - t0) * 1000.0)

    @property
    def avg_callback_ms(self) -> float:
        tail = self._latencies[-200:]
        return sum(tail) / len(tail) if tail else 0.0

    @property
    def peak_callback_ms(self) -> float:
        tail = self._latencies[-200:]
        return max(tail) if tail else 0.0


# ── terminal keyboard ─────────────────────────────────────────────────────────

def _read_char(timeout_s: float = 0.1) -> Optional[str]:
    """Read one char from stdin without Enter, with a timeout."""
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
        if ready:
            return os.read(fd, 1).decode("utf-8", errors="replace")
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── display ───────────────────────────────────────────────────────────────────

def _stem_badge(name: str, active: bool) -> Text:
    color  = _C[name]
    label  = name.upper()
    marker = "●" if active else "○"
    t = Text()
    t.append(marker, style=f"bold {color}")
    t.append(f" {label}", style=color if active else "dim")
    return t


def _build_display(
    track_name:   str,
    engine:       PlaybackEngine,
    stem_active:  list[bool],
    progress:     float,
    has_stems:    bool,
    status_msg:   str,
    elapsed_s:    float,
) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    grid.add_column()

    # Stem toggles
    stem_row = Text()
    for i, name in enumerate(STEM_NAMES):
        stem_row.append_text(_stem_badge(name, stem_active[i]))
        if i < 3:
            stem_row.append("  ")

    # Analysis status line
    if has_stems:
        bar_filled = 20
        status = Text("  ████████████████████ ", style="green")
        status.append("stems HQ ready", style="bold green")
    else:
        filled    = int(progress * 20)
        bar       = "█" * filled + "░" * (20 - filled)
        pct       = int(progress * 100)
        eta_s     = (elapsed_s / progress * (1.0 - progress)) if progress > 0.01 else 0
        status = Text(f"  {bar} {pct:3d}%", style="yellow")
        status.append(f"  {status_msg}", style="dim")
        if eta_s > 0:
            status.append(f"  ETA ~{eta_s:.0f}s", style="dim yellow")

    # Perf stats
    perf = Text(
        f"  cb {engine.avg_callback_ms:.2f}ms avg  "
        f"peak {engine.peak_callback_ms:.2f}ms  "
        f"ram {_rss_mb():.0f}MB",
        style="dim",
    )

    # Controls
    ctrl = Text(
        "  [v] vocals  [d] drums  [b] bass  [o] other  [q] quit",
        style="dim cyan",
    )

    grid.add_row(stem_row, Text())
    grid.add_row(status, Text())
    grid.add_row(perf, Text())
    grid.add_row(ctrl, Text())

    mode = "[cyan]HQ stems[/cyan]" if has_stems else "[yellow]analyzing[/yellow]"
    title = (
        f"[bold cyan]WREKKER[/bold cyan]  "
        f"[dim]{track_name}[/dim]  "
        f"{mode}  "
        f"[dim]{elapsed_s:.0f}s[/dim]"
    )
    return Panel(grid, title=title, border_style="cyan" if has_stems else "yellow")


def _rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        console.print("[red]Usage: python tools/test_stems.py <audio_file>[/red]")
        sys.exit(1)

    track = Path(sys.argv[1]).expanduser().resolve()
    if not track.exists():
        console.print(f"[red]File not found:[/red] {track}")
        sys.exit(1)

    console.print(
        Panel.fit(
            f"[bold cyan]WREKKER[/bold cyan] stem engine\n"
            f"[dim]{track}[/dim]\n"
            f"[dim yellow]Primera ejecución: htdemucs descarga ~200MB y puede tardar 2-3 min[/dim yellow]",
            border_style="cyan",
        )
    )

    # ── load original audio ────────────────────────────────────────────────
    console.print("[dim]Loading audio...[/dim]")
    original, sr = load_audio(track)               # (samples, channels) float32
    if original.shape[1] == 1:
        original = np.repeat(original, 2, axis=1)  # mono → stereo
    duration_s = original.shape[0] / sr
    console.print(
        f"  [dim]{duration_s:.1f}s  {sr}Hz  {original.shape[1]}ch  "
        f"{original.nbytes / 1_048_576:.1f}MB[/dim]"
    )

    # ── setup engine + analysis ────────────────────────────────────────────
    engine = PlaybackEngine(original, sr)

    progress     = [0.0]
    status_msg   = ["esperando worker..."]
    has_stems    = threading.Event()
    toggle_log:  list[tuple[str, bool, float]] = []
    start_time   = time.monotonic()
    stream_ref:  list[Optional[sd.OutputStream]] = [None]

    def _start_playback() -> None:
        s = sd.OutputStream(
            samplerate=sr,
            channels=2,
            dtype="float32",
            blocksize=256,
            callback=engine.callback,
            latency="low",
        )
        s.start()
        stream_ref[0] = s

    def on_complete(result: StemResult) -> None:
        engine.set_stems(result)
        has_stems.set()
        status_msg[0] = "ready"
        # Spec: start playback only after stems are ready
        threading.Thread(target=_start_playback, daemon=True).start()

    def on_progress(frac: float) -> None:
        progress[0] = frac
        elapsed = time.monotonic() - start_time
        if frac == 0.0:
            status_msg[0] = f"cargando modelo htdemucs... {elapsed:.0f}s"
        else:
            eta = (elapsed / frac * (1.0 - frac)) if frac > 0.01 else 0.0
            eta_str = f"  ETA ~{eta:.0f}s" if eta > 2 else ""
            status_msg[0] = f"htdemucs  {frac*100:.0f}%{eta_str}"

    model    = HTDemucsModel()
    analyzer = StemAnalyzer(model)
    job_id   = analyzer.analyze(track, Priority.HIGH, on_complete, on_progress)

    # ── keyboard input thread ──────────────────────────────────────────────
    stop_event    = threading.Event()
    stem_active   = [True, True, True, True]

    def input_loop() -> None:
        while not stop_event.is_set():
            ch = _read_char(0.1)
            if ch is None:
                continue
            if ch == "q":
                stop_event.set()
                return
            if ch in _KEY_MAP:
                name  = _KEY_MAP[ch]
                idx   = STEM_NAMES.index(name)
                state = engine.toggle(name)
                stem_active[idx] = state
                elapsed = time.monotonic() - start_time
                toggle_log.append((name, state, elapsed))

    input_thread = threading.Thread(target=input_loop, daemon=True)
    input_thread.start()

    # ── main display loop ──────────────────────────────────────────────────
    with Live(console=console, refresh_per_second=10, screen=False) as live:
        while not stop_event.is_set():
            elapsed = time.monotonic() - start_time
            live.update(
                _build_display(
                    track_name  = track.name,
                    engine      = engine,
                    stem_active = stem_active,
                    progress    = progress[0],
                    has_stems   = has_stems.is_set(),
                    status_msg  = status_msg[0],
                    elapsed_s   = elapsed,
                )
            )
            time.sleep(0.1)

    # ── cleanup ────────────────────────────────────────────────────────────
    if stream_ref[0] is not None:
        stream_ref[0].stop()
        stream_ref[0].close()
    analyzer.shutdown()

    # ── final report ──────────────────────────────────────────────────────
    console.print()
    played = stream_ref[0] is not None
    lat_line = (
        f"  Latencia callback avg:  [cyan]{engine.avg_callback_ms:.3f}ms[/cyan]\n"
        f"  Latencia callback peak: [cyan]{engine.peak_callback_ms:.3f}ms[/cyan]\n"
        if played else
        "  Latencia callback:      [dim]sin reproducción[/dim]\n"
    )
    console.print(Panel.fit(
        f"[bold]Reporte final[/bold]\n"
        + lat_line +
        f"  RAM proceso:            [cyan]{_rss_mb():.1f}MB[/cyan]\n"
        f"  Stems HQ disponibles:   "
        + ("[green]sí[/green]" if has_stems.is_set() else "[red]no[/red]"),
        border_style="cyan",
        title="[cyan]WREKKER[/cyan]",
    ))

    if toggle_log:
        console.print("\n[dim]Historial de toggles:[/dim]")
        for name, active, t in toggle_log:
            state = "[green]ON[/green]" if active else "[red]OFF[/red]"
            console.print(f"  t={t:.1f}s  [{_C[name]}]{name}[/{_C[name]}] → {state}")


if __name__ == "__main__":
    main()
