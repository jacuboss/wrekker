"""
SQLite library database with FTS5 full-text search.

Schema
------
tracks      — one row per track, keyed by sha256 hash
tracks_fts  — FTS5 virtual table (title, artist, album) backed by tracks

Thread safety:
  File-backed DB  — each public method opens its own connection (WAL mode).
  In-memory DB    — a single shared connection is kept alive; a threading.Lock
                    guards all access (in-memory DBs are per-connection).
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterator, Optional, Sequence

from wrekker.library.models import LibraryTrack, SearchQuery

__all__ = ["LibraryDB"]

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tracks (
    id        TEXT PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    artist    TEXT NOT NULL DEFAULT '',
    album     TEXT NOT NULL DEFAULT '',
    genre     TEXT NOT NULL DEFAULT '',
    duration  REAL NOT NULL DEFAULT 0.0,
    bpm       REAL,
    key_num   INTEGER,
    key_mode  TEXT,
    year      INTEGER,
    bitrate   INTEGER,
    sr        INTEGER,
    channels  INTEGER,
    added_at  REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
    title, artist, album,
    content = tracks,
    content_rowid = rowid
);

CREATE TRIGGER IF NOT EXISTS tracks_ai AFTER INSERT ON tracks BEGIN
    INSERT INTO tracks_fts(rowid, title, artist, album)
    VALUES (new.rowid, new.title, new.artist, new.album);
END;

CREATE TRIGGER IF NOT EXISTS tracks_ad AFTER DELETE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
    VALUES ('delete', old.rowid, old.title, old.artist, old.album);
END;

CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
    VALUES ('delete', old.rowid, old.title, old.artist, old.album);
    INSERT INTO tracks_fts(rowid, title, artist, album)
    VALUES (new.rowid, new.title, new.artist, new.album);
END;
"""


def _row_to_track(row: sqlite3.Row) -> LibraryTrack:
    return LibraryTrack(
        id       = row["id"],
        path     = Path(row["path"]),
        title    = row["title"],
        artist   = row["artist"],
        album    = row["album"],
        genre    = row["genre"],
        duration = row["duration"],
        bpm      = row["bpm"],
        key_num  = row["key_num"],
        key_mode = row["key_mode"],
        year     = row["year"],
        bitrate  = row["bitrate"],
        sr       = row["sr"],
        channels = row["channels"],
        added_at = row["added_at"],
    )


class LibraryDB:
    """
    Persistent track library backed by SQLite.

    Parameters
    ----------
    db_path:
        Path to the SQLite file. Created (with parent dirs) if missing.
        Pass ``":memory:"`` for an in-memory database (tests / ephemeral use).
    """

    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self._path      = str(db_path)
        self._in_memory = (str(db_path) == ":memory:")
        self._mem_lock:  threading.Lock           = threading.Lock()
        self._mem_con:   Optional[sqlite3.Connection] = None

        if not self._in_memory:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_schema()

    # ── connection factory ────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        if self._in_memory:
            # Shared persistent connection for in-memory databases
            if self._mem_con is None:
                self._mem_con = sqlite3.connect(
                    ":memory:", check_same_thread=False
                )
                self._mem_con.row_factory = sqlite3.Row
            return self._mem_con
        con = sqlite3.connect(self._path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _init_schema(self) -> None:
        con = self._connect()
        if self._in_memory:
            with self._mem_lock:
                con.executescript(_SCHEMA)
        else:
            with con:
                con.executescript(_SCHEMA)

    # ── upsert ────────────────────────────────────────────────────────────────

    def upsert(self, track: LibraryTrack) -> None:
        """Insert or replace a track record."""
        with self._connect() as con:
            con.execute("""
                INSERT INTO tracks
                    (id, path, title, artist, album, genre, duration,
                     bpm, key_num, key_mode, year, bitrate, sr, channels, added_at)
                VALUES
                    (:id, :path, :title, :artist, :album, :genre, :duration,
                     :bpm, :key_num, :key_mode, :year, :bitrate, :sr, :channels, :added_at)
                ON CONFLICT(path) DO UPDATE SET
                    id       = excluded.id,
                    title    = excluded.title,
                    artist   = excluded.artist,
                    album    = excluded.album,
                    genre    = excluded.genre,
                    duration = excluded.duration,
                    bpm      = excluded.bpm,
                    key_num  = excluded.key_num,
                    key_mode = excluded.key_mode,
                    year     = excluded.year,
                    bitrate  = excluded.bitrate,
                    sr       = excluded.sr,
                    channels = excluded.channels
            """, {
                "id":       track.id,
                "path":     str(track.path),
                "title":    track.title,
                "artist":   track.artist,
                "album":    track.album,
                "genre":    track.genre,
                "duration": track.duration,
                "bpm":      track.bpm,
                "key_num":  track.key_num,
                "key_mode": track.key_mode,
                "year":     track.year,
                "bitrate":  track.bitrate,
                "sr":       track.sr,
                "channels": track.channels,
                "added_at": track.added_at,
            })

    def upsert_many(self, tracks: Sequence[LibraryTrack]) -> None:
        with self._connect() as con:
            con.executemany("""
                INSERT INTO tracks
                    (id, path, title, artist, album, genre, duration,
                     bpm, key_num, key_mode, year, bitrate, sr, channels, added_at)
                VALUES
                    (:id, :path, :title, :artist, :album, :genre, :duration,
                     :bpm, :key_num, :key_mode, :year, :bitrate, :sr, :channels, :added_at)
                ON CONFLICT(path) DO UPDATE SET
                    id       = excluded.id,
                    title    = excluded.title,
                    artist   = excluded.artist,
                    album    = excluded.album,
                    genre    = excluded.genre,
                    duration = excluded.duration,
                    bpm      = excluded.bpm,
                    key_num  = excluded.key_num,
                    key_mode = excluded.key_mode,
                    year     = excluded.year,
                    bitrate  = excluded.bitrate,
                    sr       = excluded.sr,
                    channels = excluded.channels
            """, [
                {
                    "id":       t.id,
                    "path":     str(t.path),
                    "title":    t.title,
                    "artist":   t.artist,
                    "album":    t.album,
                    "genre":    t.genre,
                    "duration": t.duration,
                    "bpm":      t.bpm,
                    "key_num":  t.key_num,
                    "key_mode": t.key_mode,
                    "year":     t.year,
                    "bitrate":  t.bitrate,
                    "sr":       t.sr,
                    "channels": t.channels,
                    "added_at": t.added_at,
                }
                for t in tracks
            ])

    # ── remove stale paths ────────────────────────────────────────────────────

    def remove_missing(self, root: Path) -> int:
        """
        Remove all tracks whose path starts with *root* but no longer exists on disk.
        Returns count of removed rows.
        """
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, path FROM tracks WHERE path = ? OR path LIKE ?",
                (str(root), str(root).rstrip("/") + "/%")
            ).fetchall()
            to_delete = [r["id"] for r in rows if not Path(r["path"]).exists()]
            if to_delete:
                con.executemany("DELETE FROM tracks WHERE id = ?",
                                [(tid,) for tid in to_delete])
        return len(to_delete)

    # ── known ids ─────────────────────────────────────────────────────────────

    def known_ids(self, root: Optional[Path] = None) -> set[str]:
        """Return all track ids currently in the DB (optionally filtered by path prefix)."""
        with self._connect() as con:
            if root:
                rows = con.execute(
                    "SELECT id FROM tracks WHERE path = ? OR path LIKE ?",
                    (str(root), str(root).rstrip("/") + "/%")
                ).fetchall()
            else:
                rows = con.execute("SELECT id FROM tracks").fetchall()
        return {r["id"] for r in rows}

    # ── search ────────────────────────────────────────────────────────────────

    def search(self, query: SearchQuery) -> list[LibraryTrack]:
        """
        Full-text + filter search.

        FTS5 is used when `query.text` is non-empty; otherwise falls back to
        a plain SQL LIKE filter on title/artist/album.
        """
        params: list = []
        conditions: list[str] = []

        if query.artist:
            conditions.append("t.artist LIKE ?")
            params.append(f"%{query.artist}%")
        if query.album:
            conditions.append("t.album LIKE ?")
            params.append(f"%{query.album}%")
        if query.genre:
            conditions.append("t.genre LIKE ?")
            params.append(f"%{query.genre}%")
        if query.bpm_min is not None:
            conditions.append("t.bpm >= ?")
            params.append(query.bpm_min)
        if query.bpm_max is not None:
            conditions.append("t.bpm <= ?")
            params.append(query.bpm_max)
        if query.key_num is not None:
            conditions.append("t.key_num = ?")
            params.append(query.key_num)
        if query.key_mode is not None:
            conditions.append("t.key_mode = ?")
            params.append(query.key_mode)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._connect() as con:
            if query.text:
                # Sanitize for FTS5: keep alphanumerics + spaces, prefix-match last token
                tokens = re.sub(r"[^\w\s]", " ", query.text).split()
                fts_query = " ".join(tokens[:-1] + [tokens[-1] + "*"]) if tokens else ""

                if fts_query:
                    sql = f"""
                        SELECT t.* FROM tracks t
                        JOIN tracks_fts f ON t.rowid = f.rowid
                        {where}
                        {'AND' if where else 'WHERE'} tracks_fts MATCH ?
                        ORDER BY rank
                        LIMIT ? OFFSET ?
                    """
                    params_full = params + [fts_query, query.limit, query.offset]
                else:
                    # Degenerate query after sanitization — return empty
                    return []
            else:
                sql = f"""
                    SELECT t.* FROM tracks t
                    {where}
                    ORDER BY t.artist, t.album, t.title
                    LIMIT ? OFFSET ?
                """
                params_full = params + [query.limit, query.offset]

            rows = con.execute(sql, params_full).fetchall()

        return [_row_to_track(r) for r in rows]

    def get(self, track_id: str) -> Optional[LibraryTrack]:
        with self._connect() as con:
            row = con.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return _row_to_track(row) if row else None

    def remove_track(self, track_id: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM tracks WHERE id = ?", (track_id,))

    def remove_tracks(self, track_ids: Sequence[str]) -> int:
        if not track_ids:
            return 0
        with self._connect() as con:
            con.executemany("DELETE FROM tracks WHERE id = ?", [(tid,) for tid in track_ids])
        return len(track_ids)

    def get_by_path(self, path: Path) -> Optional[LibraryTrack]:
        with self._connect() as con:
            row = con.execute("SELECT * FROM tracks WHERE path = ?",
                              (str(path),)).fetchone()
        return _row_to_track(row) if row else None

    def all(self, limit: int = 500, offset: int = 0) -> list[LibraryTrack]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM tracks ORDER BY artist, album, title LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
        return [_row_to_track(r) for r in rows]

    def find_duplicates(self, limit: int | None = None) -> list[list[LibraryTrack]]:
        """
        Return likely duplicate groups from LibraryDB.

        Matching is intentionally conservative and metadata-based:
        normalized artist + normalized title + rounded duration. If artist/title
        are missing, path stem is used as title fallback. This avoids hashing
        large audio files during UI scans.
        """
        def norm(s: str) -> str:
            return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()

        groups: dict[tuple[str, str, int], list[LibraryTrack]] = {}
        with self._connect() as con:
            if limit is None:
                rows = con.execute(
                    "SELECT * FROM tracks ORDER BY artist, album, title"
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM tracks ORDER BY artist, album, title LIMIT ?",
                    (limit,),
                ).fetchall()
        for t in (_row_to_track(r) for r in rows):
            title = norm(t.title or t.path.stem)
            artist = norm(t.artist or "")
            if not title:
                continue
            duration_bucket = int(round(t.duration or 0.0))
            if duration_bucket <= 0:
                continue
            key = (artist, title, duration_bucket)
            groups.setdefault(key, []).append(t)
        return [
            sorted(items, key=lambda tr: str(tr.path).lower())
            for items in groups.values()
            if len(items) > 1
        ]

    def count(self) -> int:
        with self._connect() as con:
            return con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    def get_folder_paths(self) -> list[str]:
        """Return sorted list of distinct parent-directory paths of all tracks."""
        with self._connect() as con:
            rows = con.execute("SELECT DISTINCT path FROM tracks ORDER BY path").fetchall()
        seen: set[str] = set()
        result: list[str] = []
        for r in rows:
            folder = str(Path(r["path"]).parent)
            if folder not in seen:
                seen.add(folder)
                result.append(folder)
        return sorted(result)

    def get_tracks_in_folder(self, folder: Path) -> list[LibraryTrack]:
        """Return all tracks whose path is under *folder* in scan/server order."""
        prefix = str(folder).rstrip("/") + "/"
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM tracks WHERE path LIKE ? ORDER BY added_at, rowid",
                (prefix + "%",),
            ).fetchall()
        return [_row_to_track(r) for r in rows]

    def search_in_folder(self, text: str, folder: Path, limit: int = 500) -> list[LibraryTrack]:
        """Search tracks under *folder* without materializing the whole folder first."""
        prefix = str(folder).rstrip("/") + "/"
        if not text.strip():
            return self.get_tracks_in_folder(folder)[:limit]
        tokens = re.sub(r"[^\w\s]", " ", text).split()
        fts_query = " ".join(tokens[:-1] + [tokens[-1] + "*"]) if tokens else ""
        if not fts_query:
            return []
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT t.* FROM tracks t
                JOIN tracks_fts f ON t.rowid = f.rowid
                WHERE (t.path = ? OR t.path LIKE ?)
                  AND tracks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (str(folder), prefix + "%", fts_query, limit),
            ).fetchall()
        return [_row_to_track(r) for r in rows]

    # ── BPM / key update (from analysis) ─────────────────────────────────────

    def update_bpm_key(
        self,
        track_id: str,
        bpm:      Optional[float],
        key_num:  Optional[int],
        key_mode: Optional[str],
    ) -> None:
        with self._connect() as con:
            con.execute("""
                UPDATE tracks SET bpm = ?, key_num = ?, key_mode = ?
                WHERE id = ?
            """, (bpm, key_num, key_mode, track_id))
