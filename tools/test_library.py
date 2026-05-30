#!/usr/bin/env python3
"""
Library smoke test.

Scans a directory (or SMB share), prints track count + sample rows,
and demos search.

Usage:
    python tools/test_library.py /path/to/music
    python tools/test_library.py --smb myserver          # uses ~/.config/wrekker/smb.conf
    python tools/test_library.py --smb-info              # print example smb.conf
"""
from __future__ import annotations

import sys
import argparse
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

console = Console()


def _track_table(tracks, title: str = "Tracks") -> Table:
    t = Table(title=title, box=None, show_lines=False)
    t.add_column("Title",    style="white",   max_width=35)
    t.add_column("Artist",   style="cyan",    max_width=25)
    t.add_column("Album",    style="dim",     max_width=25)
    t.add_column("Dur",      style="green",   width=6)
    t.add_column("BPM",      style="yellow",  width=7)
    t.add_column("Key",      style="magenta", width=5)
    for track in tracks:
        t.add_row(
            track.display_title[:35],
            track.display_artist[:25],
            track.album[:25],
            track.duration_str,
            track.bpm_str,
            track.key_str,
        )
    return t


def scan_path(music_path: Path) -> None:
    from wrekker.library import LibraryDB, LibraryScanner, SearchQuery

    db      = LibraryDB(":memory:")
    scanner = LibraryScanner(db)

    console.print(f"\n[cyan]Scanning[/cyan] {music_path}\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning...", total=None)
        last_prog = None

        def on_progress(prog):
            nonlocal last_prog
            last_prog = prog
            progress.update(
                task,
                total=prog.total_found or 1,
                completed=prog.processed,
                description=f"[cyan]{Path(prog.current_path).name[:40]}[/cyan]"
                            if prog.current_path else "Scanning...",
            )

        result = scanner.scan_blocking(music_path, on_progress=on_progress)

    console.print(
        f"\n[green]Done.[/green]  "
        f"Found [bold]{result.total_found}[/bold] audio files — "
        f"[green]{result.new_tracks}[/green] new, "
        f"[dim]{result.skipped}[/dim] skipped, "
        f"[red]{result.errors}[/red] errors.\n"
    )

    total = db.count()
    console.print(f"DB contains [bold]{total}[/bold] tracks.\n")

    if total == 0:
        return

    # Show first 20
    sample = db.all(limit=20)
    console.print(_track_table(sample, title=f"First {len(sample)} tracks"))

    # Demo search
    console.print("\n[bold]Search demo[/bold] — try typing a query (empty = skip):")
    try:
        q = input("  Search> ").strip()
    except (EOFError, KeyboardInterrupt):
        q = ""

    if q:
        from wrekker.library.models import SearchQuery
        results = db.search(SearchQuery(text=q, limit=20))
        console.print(f"\n[green]{len(results)}[/green] results for [cyan]{q!r}[/cyan]")
        if results:
            console.print(_track_table(results, title="Search results"))


def smb_info() -> None:
    from wrekker.library.smb import _CONFIG_PATH, write_smb_config_example
    write_smb_config_example()
    console.print(f"[cyan]SMB config example written to:[/cyan] {_CONFIG_PATH}")
    console.print(_CONFIG_PATH.read_text())


def smb_scan(name: str) -> None:
    from wrekker.library.smb import load_smb_config
    shares = load_smb_config()
    if name not in shares:
        console.print(f"[red]Section [{name}] not found in smb.conf.[/red]")
        console.print(f"Available: {list(shares.keys())}")
        sys.exit(1)
    share = shares[name]
    console.print(f"[cyan]Mounting[/cyan] {share.smb_url} ...")
    try:
        local = share.mount()
        console.print(f"[green]Mounted at[/green] {local}")
        scan_path(local)
    except Exception as e:
        console.print(f"[red]Mount failed:[/red] {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wrekker library test")
    parser.add_argument("path", nargs="?", help="Local music directory")
    parser.add_argument("--smb",      metavar="NAME", help="Mount SMB share by config name")
    parser.add_argument("--smb-info", action="store_true", help="Print example smb.conf and exit")
    args = parser.parse_args()

    if args.smb_info:
        smb_info()
    elif args.smb:
        smb_scan(args.smb)
    elif args.path:
        scan_path(Path(args.path))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
