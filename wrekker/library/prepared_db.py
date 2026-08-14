"""
PreparedDB — tracks the preparation status of every track in the library.

Tables:
  prepared_tracks      — one row per source file; .wrk path, status, metadata
  prepared_sets        — named prepared sets (folder = set)
  prepared_set_tracks  — tracks within each set; wrk-path-first access
  library_roots        — user-added scan roots (local folders, mounted paths)

Layout: ~/.local/share/wrekker/prepared.db
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

__all__ = [
    "PreparedDB", "PreparedRecord", "LibraryRoot", "TrackStatus",
    "PreparedSet", "WrekkedTrack",
]


# ── Status constants ───────────────────────────────────────────────────────────

class TrackStatus:
    PENDING    = "pending"
    PROCESSING = "processing"
    READY      = "ready"
    OUTDATED   = "outdated"
    FAILED     = "failed"
    MISSING    = "missing"    # source file gone, .wrk still usable


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class PreparedRecord:
    source_path:    str
    source_hash:    Optional[str]
    source_mtime_ns: Optional[int]
    wrk_path:       str
    wrk_id:         str
    title:          str
    artist:         str
    album:          str
    duration_s:     float
    bpm:            Optional[float]
    key:            Optional[str]
    lufs:           Optional[float]
    stems_ready:    bool
    wrk_ready:      bool
    analysis_status: str
    stem_status:    str
    source_type:    str
    wrk_version:    int
    warnings:       Optional[str]
    last_analyzed_at: Optional[str]
    created_at:     str
    marker_status:  str = "none"
    marker_count:   int = 0
    task_statuses:  Optional[dict] = None
    task_errors:    Optional[dict] = None
    task_timings_ms: Optional[dict] = None
    total_preparation_ms: Optional[int] = None
    analysis_revision: Optional[int] = None
    lab_status: Optional[str] = None
    lab_edited_at: Optional[str] = None
    hot_cue_count: int = 0
    saved_loop_count: int = 0

    def is_current(self, path: Path) -> bool:
        """True if source exists and hash matches."""
        try:
            stat = path.stat()
            payload = f"{path.absolute()}:{stat.st_mtime_ns}"
            h = hashlib.sha256(payload.encode()).hexdigest()
            return h == self.source_hash
        except OSError:
            return False


@dataclass
class PreparedSet:
    id:                 int
    name:               str
    source_root_label:  Optional[str]
    source_root_path:   Optional[str]
    track_count:        int
    notes:              Optional[str]
    description:        Optional[str]
    total_duration_s:   float
    created_at:         str
    updated_at:         str


@dataclass
class WrekkedTrack:
    id:               int
    set_id:           int
    wrk_id:           str
    wrk_path:         str
    title:            str
    artist:           str
    duration_s:       float
    bpm:              Optional[float]
    key:              Optional[str]
    stems_ready:      bool
    wrk_ready:        bool
    source_available: bool
    position:         int
    added_at:         str


@dataclass
class LibraryRoot:
    id:             int
    path:           str
    label:          Optional[str]
    enabled:        bool
    last_scanned_at: Optional[str]
    added_at:       str


# ── Schema ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS prepared_tracks (
    id              INTEGER PRIMARY KEY,
    source_path     TEXT    NOT NULL UNIQUE,
    source_hash     TEXT,
    source_mtime_ns INTEGER,
    wrk_path        TEXT    NOT NULL,
    wrk_id          TEXT    NOT NULL UNIQUE,
    title           TEXT    NOT NULL DEFAULT '',
    artist          TEXT    NOT NULL DEFAULT '',
    album           TEXT    NOT NULL DEFAULT '',
    duration_s      REAL    NOT NULL DEFAULT 0.0,
    bpm             REAL,
    key             TEXT,
    lufs            REAL,
    stems_ready     INTEGER NOT NULL DEFAULT 0,
    wrk_ready       INTEGER NOT NULL DEFAULT 0,
    analysis_status TEXT    NOT NULL DEFAULT 'pending',
    stem_status     TEXT    NOT NULL DEFAULT 'pending',
    source_type     TEXT    NOT NULL DEFAULT 'local',
    wrk_version     INTEGER NOT NULL DEFAULT 0,
    warnings        TEXT,
    last_analyzed_at TEXT,
    marker_status   TEXT    NOT NULL DEFAULT 'none',
    marker_count    INTEGER NOT NULL DEFAULT 0,
    analysis_revision INTEGER,
    lab_status      TEXT,
    lab_edited_at   TEXT,
    hot_cue_count   INTEGER NOT NULL DEFAULT 0,
    saved_loop_count INTEGER NOT NULL DEFAULT 0,
    task_statuses_json TEXT,
    task_errors_json TEXT,
    task_timings_ms_json TEXT,
    total_preparation_ms INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pt_source ON prepared_tracks(source_path);
CREATE INDEX IF NOT EXISTS idx_pt_wrk_id ON prepared_tracks(wrk_id);

CREATE TABLE IF NOT EXISTS prepared_sets (
    id                INTEGER PRIMARY KEY,
    name              TEXT    NOT NULL UNIQUE,
    source_root_label TEXT,
    source_root_path  TEXT,
    track_count       INTEGER NOT NULL DEFAULT 0,
    notes             TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prepared_set_tracks (
    id               INTEGER PRIMARY KEY,
    set_id           INTEGER NOT NULL REFERENCES prepared_sets(id) ON DELETE CASCADE,
    wrk_id           TEXT    NOT NULL,
    wrk_path         TEXT    NOT NULL,
    title            TEXT    NOT NULL DEFAULT '',
    artist           TEXT    NOT NULL DEFAULT '',
    duration_s       REAL    NOT NULL DEFAULT 0.0,
    bpm              REAL,
    key              TEXT,
    stems_ready      INTEGER NOT NULL DEFAULT 0,
    wrk_ready        INTEGER NOT NULL DEFAULT 0,
    source_available INTEGER NOT NULL DEFAULT 0,
    position         INTEGER NOT NULL DEFAULT 0,
    added_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(set_id, wrk_id)
);

CREATE INDEX IF NOT EXISTS idx_pst_set_id   ON prepared_set_tracks(set_id);
CREATE INDEX IF NOT EXISTS idx_pst_wrk_id   ON prepared_set_tracks(wrk_id);
CREATE INDEX IF NOT EXISTS idx_pst_wrk_path ON prepared_set_tracks(wrk_path);

CREATE TABLE IF NOT EXISTS library_roots (
    id              INTEGER PRIMARY KEY,
    path            TEXT    NOT NULL UNIQUE,
    label           TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_scanned_at TEXT,
    added_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _row_to_record(row: sqlite3.Row) -> PreparedRecord:
    def _json_col(name: str) -> Optional[dict]:
        try:
            raw = row[name]
        except Exception:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    return PreparedRecord(
        source_path     = row["source_path"],
        source_hash     = row["source_hash"],
        source_mtime_ns = row["source_mtime_ns"],
        wrk_path        = row["wrk_path"],
        wrk_id          = row["wrk_id"],
        title           = row["title"],
        artist          = row["artist"],
        album           = row["album"],
        duration_s      = row["duration_s"],
        bpm             = row["bpm"],
        key             = row["key"],
        lufs            = row["lufs"],
        stems_ready     = bool(row["stems_ready"]),
        wrk_ready       = bool(row["wrk_ready"]),
        analysis_status = row["analysis_status"],
        stem_status     = row["stem_status"],
        source_type     = row["source_type"],
        wrk_version     = row["wrk_version"],
        warnings        = row["warnings"],
        last_analyzed_at= row["last_analyzed_at"],
        created_at      = row["created_at"],
        marker_status   = row["marker_status"],
        marker_count    = row["marker_count"],
        task_statuses   = _json_col("task_statuses_json"),
        task_errors     = _json_col("task_errors_json"),
        task_timings_ms = _json_col("task_timings_ms_json"),
        total_preparation_ms = row["total_preparation_ms"] if "total_preparation_ms" in row.keys() else None,
        analysis_revision = row["analysis_revision"] if "analysis_revision" in row.keys() else None,
        lab_status      = row["lab_status"] if "lab_status" in row.keys() else None,
        lab_edited_at   = row["lab_edited_at"] if "lab_edited_at" in row.keys() else None,
        hot_cue_count   = row["hot_cue_count"] if "hot_cue_count" in row.keys() else 0,
        saved_loop_count = row["saved_loop_count"] if "saved_loop_count" in row.keys() else 0,
    )


def _row_to_root(row: sqlite3.Row) -> LibraryRoot:
    return LibraryRoot(
        id              = row["id"],
        path            = row["path"],
        label           = row["label"],
        enabled         = bool(row["enabled"]),
        last_scanned_at = row["last_scanned_at"],
        added_at        = row["added_at"],
    )


def _row_to_set(row: sqlite3.Row) -> PreparedSet:
    keys = row.keys()
    return PreparedSet(
        id                = row["id"],
        name              = row["name"],
        source_root_label = row["source_root_label"],
        source_root_path  = row["source_root_path"],
        track_count       = row["track_count"],
        notes             = row["notes"],
        description       = row["description"] if "description" in keys else None,
        total_duration_s  = row["total_duration_s"] if "total_duration_s" in keys else 0.0,
        created_at        = row["created_at"],
        updated_at        = row["updated_at"],
    )


def _row_to_wrekked_track(row: sqlite3.Row) -> WrekkedTrack:
    return WrekkedTrack(
        id               = row["id"],
        set_id           = row["set_id"],
        wrk_id           = row["wrk_id"],
        wrk_path         = row["wrk_path"],
        title            = row["title"],
        artist           = row["artist"],
        duration_s       = row["duration_s"],
        bpm              = row["bpm"],
        key              = row["key"],
        stems_ready      = bool(row["stems_ready"]),
        wrk_ready        = bool(row["wrk_ready"]),
        source_available = bool(row["source_available"]),
        position         = row["position"],
        added_at         = row["added_at"],
    )


# ── Database class ─────────────────────────────────────────────────────────────

class PreparedDB:
    """
    SQLite store for .wrk preparation status and library roots.

    Thread-safe: WAL mode + per-call connections for file-backed DBs.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path, timeout=10)
        con.row_factory = sqlite3.Row
        # foreign_keys is per-connection in SQLite; without it the
        # ON DELETE CASCADE on prepared_set_tracks never fires.
        con.execute("PRAGMA foreign_keys = ON")
        return con

    _MIGRATIONS = [
        "ALTER TABLE prepared_tracks ADD COLUMN bpm_metadata REAL",
        "ALTER TABLE prepared_tracks ADD COLUMN bpm_confidence REAL",
        "ALTER TABLE prepared_tracks ADD COLUMN key_confidence REAL",
        "ALTER TABLE prepared_sets ADD COLUMN description TEXT",
        "ALTER TABLE prepared_sets ADD COLUMN total_duration_s REAL NOT NULL DEFAULT 0.0",
        "ALTER TABLE prepared_tracks ADD COLUMN marker_status TEXT NOT NULL DEFAULT 'none'",
        "ALTER TABLE prepared_tracks ADD COLUMN marker_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE prepared_tracks ADD COLUMN task_statuses_json TEXT",
        "ALTER TABLE prepared_tracks ADD COLUMN task_errors_json TEXT",
        "ALTER TABLE prepared_tracks ADD COLUMN task_timings_ms_json TEXT",
        "ALTER TABLE prepared_tracks ADD COLUMN total_preparation_ms INTEGER",
        "ALTER TABLE prepared_tracks ADD COLUMN analysis_revision INTEGER",
        "ALTER TABLE prepared_tracks ADD COLUMN lab_status TEXT",
        "ALTER TABLE prepared_tracks ADD COLUMN lab_edited_at TEXT",
        "ALTER TABLE prepared_tracks ADD COLUMN hot_cue_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE prepared_tracks ADD COLUMN saved_loop_count INTEGER NOT NULL DEFAULT 0",
    ]

    def _init_schema(self) -> None:
        with self._connect() as con:
            con.executescript(_SCHEMA)
            for stmt in self._MIGRATIONS:
                try:
                    con.execute(stmt)
                except Exception:
                    pass   # column already exists
            # Repair orphans left behind by connections that ran without
            # foreign_keys=ON (set deletions did not cascade).
            try:
                con.execute("""
                    DELETE FROM prepared_set_tracks
                    WHERE set_id NOT IN (SELECT id FROM prepared_sets)
                """)
            except Exception:
                pass

    # ── Track management (source-path-first) ───────────────────────────────────

    def find_wrk(self, source_path: Path) -> Optional[PreparedRecord]:
        """Return the record for source_path, or None if not prepared."""
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM prepared_tracks WHERE source_path = ?",
                (str(source_path.absolute()),),
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_batch_status(self, source_paths: Sequence[Path]) -> dict[str, PreparedRecord]:
        """Return {abs_path_str: PreparedRecord} for all paths that exist in DB."""
        if not source_paths:
            return {}
        placeholders = ",".join("?" * len(source_paths))
        abs_paths = [str(p.absolute()) for p in source_paths]
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM prepared_tracks WHERE source_path IN ({placeholders})",
                abs_paths,
            ).fetchall()
        return {row["source_path"]: _row_to_record(row) for row in rows}

    def upsert_record(
        self,
        source_path:     Path,
        wrk_path:        Path,
        wrk_id:          str,
        title:           str  = "",
        artist:          str  = "",
        album:           str  = "",
        duration_s:      float = 0.0,
        bpm:             Optional[float] = None,
        key:             Optional[str]   = None,
        lufs:            Optional[float] = None,
        stems_ready:     bool = False,
        wrk_ready:       bool = False,
        analysis_status: str  = TrackStatus.PENDING,
        stem_status:     str  = TrackStatus.PENDING,
        source_type:     str  = "local",
        wrk_version:     int  = 0,
        warnings:        Optional[str] = None,
        source_hash:     Optional[str] = None,
        source_mtime_ns: Optional[int] = None,
        marker_status:   str  = "none",
        marker_count:    int  = 0,
        task_statuses:   Optional[dict] = None,
        task_errors:     Optional[dict] = None,
        task_timings_ms: Optional[dict] = None,
        total_preparation_ms: Optional[int] = None,
    ) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        task_statuses_json = json.dumps(task_statuses, sort_keys=True) if task_statuses is not None else None
        task_errors_json = json.dumps(task_errors, sort_keys=True) if task_errors is not None else None
        task_timings_ms_json = json.dumps(task_timings_ms, sort_keys=True) if task_timings_ms is not None else None
        with self._connect() as con:
            con.execute("""
                INSERT INTO prepared_tracks
                    (source_path, source_hash, source_mtime_ns, wrk_path, wrk_id,
                     title, artist, album, duration_s, bpm, key, lufs,
                     stems_ready, wrk_ready, analysis_status, stem_status,
                     source_type, wrk_version, warnings, last_analyzed_at,
                     marker_status, marker_count, task_statuses_json,
                     task_errors_json, task_timings_ms_json, total_preparation_ms)
                VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_path) DO UPDATE SET
                    source_hash      = excluded.source_hash,
                    source_mtime_ns  = excluded.source_mtime_ns,
                    wrk_path         = excluded.wrk_path,
                    title            = excluded.title,
                    artist           = excluded.artist,
                    album            = excluded.album,
                    duration_s       = excluded.duration_s,
                    bpm              = excluded.bpm,
                    key              = excluded.key,
                    lufs             = excluded.lufs,
                    stems_ready      = excluded.stems_ready,
                    wrk_ready        = excluded.wrk_ready,
                    analysis_status  = excluded.analysis_status,
                    stem_status      = excluded.stem_status,
                    wrk_version      = excluded.wrk_version,
                    warnings         = excluded.warnings,
                    last_analyzed_at = excluded.last_analyzed_at,
                    marker_status    = excluded.marker_status,
                    marker_count     = excluded.marker_count,
                    task_statuses_json = excluded.task_statuses_json,
                    task_errors_json = excluded.task_errors_json,
                    task_timings_ms_json = excluded.task_timings_ms_json,
                    total_preparation_ms = excluded.total_preparation_ms
            """, (
                str(source_path.absolute()), source_hash, source_mtime_ns,
                str(wrk_path.absolute()), wrk_id,
                title, artist, album, duration_s, bpm, key, lufs,
                int(stems_ready), int(wrk_ready),
                analysis_status, stem_status,
                source_type, wrk_version, warnings, now,
                marker_status, marker_count, task_statuses_json,
                task_errors_json, task_timings_ms_json, total_preparation_ms,
            ))

    def update_preparation_details(
        self,
        source_path: Path,
        task_statuses: Optional[dict] = None,
        task_errors: Optional[dict] = None,
        task_timings_ms: Optional[dict] = None,
        total_preparation_ms: Optional[int] = None,
    ) -> None:
        """Persist detailed per-task preparation state for progress/history UI."""
        with self._connect() as con:
            con.execute("""
                UPDATE prepared_tracks
                SET task_statuses_json = ?,
                    task_errors_json = ?,
                    task_timings_ms_json = ?,
                    total_preparation_ms = ?
                WHERE source_path = ?
            """, (
                json.dumps(task_statuses or {}, sort_keys=True),
                json.dumps(task_errors or {}, sort_keys=True),
                json.dumps(task_timings_ms or {}, sort_keys=True),
                total_preparation_ms,
                str(source_path.absolute()),
            ))

    def mark_processing(self, source_path: Path) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE prepared_tracks SET analysis_status = ? WHERE source_path = ?",
                (TrackStatus.PROCESSING, str(source_path.absolute())),
            )

    def mark_failed(self, source_path: Path, error: str = "") -> None:
        with self._connect() as con:
            con.execute("""
                UPDATE prepared_tracks
                SET analysis_status = ?, stem_status = ?, wrk_ready = 0, warnings = ?
                WHERE source_path = ?
            """, (TrackStatus.FAILED, TrackStatus.FAILED, error[:2000],
                  str(source_path.absolute())))

    def mark_outdated(self, source_path: Path) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE prepared_tracks SET analysis_status = ? WHERE source_path = ?",
                (TrackStatus.OUTDATED, str(source_path.absolute())),
            )

    def check_outdated(self, source_path: Path) -> bool:
        """Return True if the .wrk is stale relative to the source file."""
        rec = self.find_wrk(source_path)
        if rec is None:
            return False
        return not rec.is_current(source_path)

    def mark_stems_ready(self, source_path: Path) -> None:
        with self._connect() as con:
            con.execute("""
                UPDATE prepared_tracks
                SET stems_ready = 1, stem_status = 'ready'
                WHERE source_path = ?
            """, (str(source_path.absolute()),))

    def update_marker_status(
        self,
        source_path:  Path,
        marker_status: str,
        marker_count:  int = 0,
    ) -> None:
        """Update marker_status and marker_count for a prepared track."""
        with self._connect() as con:
            con.execute("""
                UPDATE prepared_tracks
                SET marker_status = ?, marker_count = ?
                WHERE source_path = ?
            """, (marker_status, marker_count, str(source_path.absolute())))

    def update_lab_status_by_wrk_path(
        self,
        wrk_path: str | Path,
        *,
        analysis_revision: Optional[int] = None,
        lab_status: Optional[str] = None,
        lab_edited_at: Optional[str] = None,
        hot_cue_count: int = 0,
        saved_loop_count: int = 0,
        marker_status: Optional[str] = None,
        marker_count: Optional[int] = None,
    ) -> None:
        """Persist WREKKER LAB status for any prepared track row matching a .wrk."""
        p = str(Path(wrk_path).absolute())
        fields = [
            "analysis_revision = ?",
            "lab_status = ?",
            "lab_edited_at = ?",
            "hot_cue_count = ?",
            "saved_loop_count = ?",
        ]
        values: list = [
            analysis_revision,
            lab_status,
            lab_edited_at,
            int(hot_cue_count),
            int(saved_loop_count),
        ]
        if marker_status is not None:
            fields.append("marker_status = ?")
            values.append(marker_status)
        if marker_count is not None:
            fields.append("marker_count = ?")
            values.append(int(marker_count))
        values.append(p)
        with self._connect() as con:
            con.execute(
                f"UPDATE prepared_tracks SET {', '.join(fields)} WHERE wrk_path = ?",
                values,
            )

    def all_records(self, limit: int = 10_000) -> list[PreparedRecord]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM prepared_tracks LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def _set_status(self, source_path: Path, status: str) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE prepared_tracks SET analysis_status = ? WHERE source_path = ?",
                (status, str(source_path.absolute())),
            )

    def _reset_to_pending(self, source_path: Path) -> None:
        with self._connect() as con:
            con.execute("""
                UPDATE prepared_tracks
                SET wrk_ready = 0, stems_ready = 0,
                    analysis_status = ?, stem_status = ?
                WHERE source_path = ?
            """, (TrackStatus.PENDING, TrackStatus.PENDING,
                  str(source_path.absolute())))

    def scan_for_changes(self) -> tuple[int, int]:
        """
        Check all records against the filesystem at startup.

        Actions taken:
          - PROCESSING records → PENDING  (stuck from a crashed session)
          - Source file gone   → MISSING
          - Source file back   → PENDING  (remounted drive etc.)
          - Source file changed → OUTDATED
          - .wrk file deleted  → wrk_ready=0, PENDING

        Returns (n_outdated, n_missing).
        """
        records = self.all_records()
        n_outdated = 0
        n_missing = 0

        for rec in records:
            source = Path(rec.source_path)

            # Stuck PROCESSING from a previous crash → reset
            if rec.analysis_status == TrackStatus.PROCESSING:
                self._reset_to_pending(source)
                continue

            if not source.exists():
                if rec.analysis_status != TrackStatus.MISSING:
                    self._set_status(source, TrackStatus.MISSING)
                    n_missing += 1
                continue

            # File reappeared after being marked MISSING
            if rec.analysis_status == TrackStatus.MISSING:
                self._reset_to_pending(source)
                continue

            # Source changed since last preparation
            if rec.source_hash and not rec.is_current(source):
                if rec.analysis_status not in (TrackStatus.OUTDATED,
                                               TrackStatus.PENDING):
                    self.mark_outdated(source)
                    n_outdated += 1
                continue

            # Source is current — check if the .wrk was deleted from disk
            if (rec.wrk_ready and rec.wrk_path
                    and not Path(rec.wrk_path).exists()):
                self._reset_to_pending(source)

        return n_outdated, n_missing

    # ── Library roots ──────────────────────────────────────────────────────────

    def get_roots(self) -> list[LibraryRoot]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM library_roots ORDER BY path"
            ).fetchall()
        return [_row_to_root(r) for r in rows]

    def add_root(self, path: Path, label: Optional[str] = None) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO library_roots (path, label) VALUES (?, ?)",
                (str(path.absolute()), label or path.name),
            )

    def remove_root(self, path: Path) -> None:
        with self._connect() as con:
            con.execute(
                "DELETE FROM library_roots WHERE path = ?",
                (str(path.absolute()),),
            )

    def update_root_scanned(self, path: Path) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with self._connect() as con:
            con.execute(
                "UPDATE library_roots SET last_scanned_at = ? WHERE path = ?",
                (now, str(path.absolute())),
            )

    # ── WREKKED sets ───────────────────────────────────────────────────────────

    def upsert_wrekked_set(
        self,
        name:               str,
        source_root_label:  Optional[str] = None,
        source_root_path:   Optional[str] = None,
        notes:              Optional[str] = None,
    ) -> int:
        """
        Insert or update a prepared set by name.  Returns the set's id.
        On conflict the source root and notes are updated; track_count is preserved.
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with self._connect() as con:
            con.execute("""
                INSERT INTO prepared_sets
                    (name, source_root_label, source_root_path, notes, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    source_root_label = excluded.source_root_label,
                    source_root_path  = excluded.source_root_path,
                    notes             = COALESCE(excluded.notes, prepared_sets.notes),
                    updated_at        = excluded.updated_at
            """, (name, source_root_label, source_root_path, notes, now))
            row = con.execute(
                "SELECT id FROM prepared_sets WHERE name = ?", (name,)
            ).fetchone()
        return row["id"]

    def upsert_set_track(
        self,
        set_id:           int,
        wrk_id:           str,
        wrk_path:         str,
        title:            str   = "",
        artist:           str   = "",
        duration_s:       float = 0.0,
        bpm:              Optional[float] = None,
        key:              Optional[str]   = None,
        stems_ready:      bool  = False,
        wrk_ready:        bool  = False,
        source_available: bool  = False,
        position:         int   = 0,
        update_position:  bool  = True,
    ) -> None:
        """Insert or update a track within a set; updates metadata on conflict."""
        position_update = "excluded.position" if update_position else "prepared_set_tracks.position"
        with self._connect() as con:
            con.execute(f"""
                INSERT INTO prepared_set_tracks
                    (set_id, wrk_id, wrk_path, title, artist,
                     duration_s, bpm, key, stems_ready, wrk_ready,
                     source_available, position)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(set_id, wrk_id) DO UPDATE SET
                    wrk_path         = excluded.wrk_path,
                    title            = excluded.title,
                    artist           = excluded.artist,
                    duration_s       = excluded.duration_s,
                    bpm              = excluded.bpm,
                    key              = excluded.key,
                    stems_ready      = excluded.stems_ready,
                    wrk_ready        = excluded.wrk_ready,
                    source_available = excluded.source_available,
                    position         = {position_update}
            """, (
                set_id, wrk_id, wrk_path, title, artist,
                duration_s, bpm, key,
                int(stems_ready), int(wrk_ready), int(source_available), position,
            ))

    def update_set_track_count(self, set_id: int) -> None:
        """Recompute track_count for a set from the actual row count."""
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with self._connect() as con:
            con.execute("""
                UPDATE prepared_sets
                SET track_count = (
                    SELECT COUNT(*) FROM prepared_set_tracks WHERE set_id = ?
                ),
                updated_at = ?
                WHERE id = ?
            """, (set_id, now, set_id))

    def list_wrekked_sets(self) -> list[PreparedSet]:
        """Return all prepared sets ordered alphabetically by name."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM prepared_sets ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [_row_to_set(r) for r in rows]

    def list_tracks_in_wrekked_set(self, set_id: int) -> list[WrekkedTrack]:
        """Return all tracks in a set ordered by position then title."""
        with self._connect() as con:
            rows = con.execute(
                """SELECT * FROM prepared_set_tracks
                   WHERE set_id = ?
                   ORDER BY position, title COLLATE NOCASE""",
                (set_id,),
            ).fetchall()
        return [_row_to_wrekked_track(r) for r in rows]

    def get_set_wrk_ids(self, set_id: int) -> set[str]:
        """Return wrk_ids already present in a WREKKED set."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT wrk_id FROM prepared_set_tracks WHERE set_id = ?",
                (set_id,),
            ).fetchall()
        return {r["wrk_id"] for r in rows}

    def next_set_position(self, set_id: int) -> int:
        """Return the next append position for a WREKKED set."""
        with self._connect() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(position), -1) AS max_position FROM prepared_set_tracks WHERE set_id = ?",
                (set_id,),
            ).fetchone()
        return int(row["max_position"] if row else -1) + 1

    def list_all_wrekked_tracks(self, limit: int = 5_000) -> list[WrekkedTrack]:
        """Return all tracks across all sets ordered by set name then position."""
        with self._connect() as con:
            rows = con.execute(
                """SELECT t.* FROM prepared_set_tracks t
                   JOIN prepared_sets s ON s.id = t.set_id
                   ORDER BY s.name COLLATE NOCASE, t.position, t.title COLLATE NOCASE
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [_row_to_wrekked_track(r) for r in rows]

    def list_recently_wrekked(self, limit: int = 50) -> list[WrekkedTrack]:
        """Return the most recently added tracks across all sets."""
        with self._connect() as con:
            rows = con.execute(
                """SELECT * FROM prepared_set_tracks
                   ORDER BY added_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [_row_to_wrekked_track(r) for r in rows]

    def find_by_wrk_id(self, wrk_id: str) -> Optional[WrekkedTrack]:
        """Return any set-track row matching wrk_id, preferring wrk_ready ones."""
        with self._connect() as con:
            row = con.execute(
                """SELECT * FROM prepared_set_tracks
                   WHERE wrk_id = ?
                   ORDER BY wrk_ready DESC LIMIT 1""",
                (wrk_id,),
            ).fetchone()
        return _row_to_wrekked_track(row) if row else None

    def find_by_wrk_path(self, wrk_path: str) -> Optional[WrekkedTrack]:
        """Return any set-track row matching wrk_path."""
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM prepared_set_tracks WHERE wrk_path = ? LIMIT 1",
                (wrk_path,),
            ).fetchone()
        return _row_to_wrekked_track(row) if row else None

    def search_wrekked(
        self,
        query: str,
        set_id: Optional[int] = None,
        limit:  int = 200,
    ) -> list[WrekkedTrack]:
        """
        Full-text search over title and artist.
        Optionally scoped to a single set.  Returns up to `limit` rows.
        """
        pattern = f"%{query}%"
        with self._connect() as con:
            if set_id is not None:
                rows = con.execute(
                    """SELECT * FROM prepared_set_tracks
                       WHERE set_id = ?
                         AND (title LIKE ? OR artist LIKE ?)
                       ORDER BY title COLLATE NOCASE LIMIT ?""",
                    (set_id, pattern, pattern, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    """SELECT * FROM prepared_set_tracks
                       WHERE title LIKE ? OR artist LIKE ?
                       ORDER BY title COLLATE NOCASE LIMIT ?""",
                    (pattern, pattern, limit),
                ).fetchall()
        return [_row_to_wrekked_track(r) for r in rows]

    def remove_set_track(self, set_id: int, wrk_id: str) -> None:
        """Remove a single track from a set and recompute track_count."""
        with self._connect() as con:
            con.execute(
                "DELETE FROM prepared_set_tracks WHERE set_id = ? AND wrk_id = ?",
                (set_id, wrk_id),
            )
        self.update_set_track_count(set_id)

    def remove_wrekked_set(self, set_id: int) -> None:
        """Remove a set and all its tracks (CASCADE handles the child rows)."""
        with self._connect() as con:
            con.execute("DELETE FROM prepared_sets WHERE id = ?", (set_id,))

    def create_wrekked_set(self, name: str, description: str = "") -> int:
        """
        Create a brand-new set.  Returns the new id.

        Set names are UNIQUE; if the requested name is taken, a numeric
        suffix is appended ("Name (2)", "Name (3)", …) instead of raising.
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with self._connect() as con:
            final_name = name
            for n in range(2, 1000):
                exists = con.execute(
                    "SELECT 1 FROM prepared_sets WHERE name = ?", (final_name,)
                ).fetchone()
                if not exists:
                    break
                final_name = f"{name} ({n})"
            con.execute(
                "INSERT INTO prepared_sets (name, description, updated_at) VALUES (?,?,?)",
                (final_name, description or None, now),
            )
            row = con.execute(
                "SELECT id FROM prepared_sets WHERE name = ?", (final_name,)
            ).fetchone()
        return row["id"]

    def rename_wrekked_set(self, set_id: int, new_name: str) -> bool:
        """Rename a set.  Returns False if the name is already taken."""
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            with self._connect() as con:
                con.execute(
                    "UPDATE prepared_sets SET name = ?, updated_at = ? WHERE id = ?",
                    (new_name, now, set_id),
                )
            return True
        except Exception:
            return False

    def update_set_description(self, set_id: int, description: str) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with self._connect() as con:
            con.execute(
                "UPDATE prepared_sets SET description = ?, updated_at = ? WHERE id = ?",
                (description or None, now, set_id),
            )

    def reorder_set_track(self, set_id: int, wrk_id: str, new_position: int) -> None:
        """Move a track to new_position, shifting others as needed."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT wrk_id FROM prepared_set_tracks WHERE set_id = ? ORDER BY position, title COLLATE NOCASE",
                (set_id,),
            ).fetchall()
            ids = [r["wrk_id"] for r in rows]
            if wrk_id in ids:
                ids.remove(wrk_id)
            new_position = max(0, min(new_position, len(ids)))
            ids.insert(new_position, wrk_id)
            for pos, wid in enumerate(ids):
                con.execute(
                    "UPDATE prepared_set_tracks SET position = ? WHERE set_id = ? AND wrk_id = ?",
                    (pos, set_id, wid),
                )

    def update_set_total_duration(self, set_id: int) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with self._connect() as con:
            con.execute("""
                UPDATE prepared_sets
                SET total_duration_s = (
                    SELECT COALESCE(SUM(duration_s), 0.0)
                    FROM prepared_set_tracks WHERE set_id = ?
                ),
                updated_at = ?
                WHERE id = ?
            """, (set_id, now, set_id))

    def get_set_summary(self, set_id: int) -> dict:
        """Return counts of track readiness in a set."""
        with self._connect() as con:
            row = con.execute("""
                SELECT
                    COUNT(*)                         AS total,
                    SUM(wrk_ready)                   AS wrk_count,
                    SUM(stems_ready)                 AS stems_count,
                    SUM(CASE WHEN NOT source_available THEN 1 ELSE 0 END) AS offline_count,
                    SUM(CASE WHEN NOT wrk_ready THEN 1 ELSE 0 END)        AS broken_count,
                    COALESCE(SUM(duration_s), 0.0)   AS total_duration_s
                FROM prepared_set_tracks WHERE set_id = ?
            """, (set_id,)).fetchone()
        return dict(row) if row else {
            "total": 0, "wrk_count": 0, "stems_count": 0,
            "offline_count": 0, "broken_count": 0, "total_duration_s": 0.0,
        }

    def update_track_metadata(
        self,
        wrk_id: str,
        *,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        duration_s: Optional[float] = None,
        bpm: Optional[float] = None,
        key: Optional[str] = None,
        source_path: Optional[str] = None,
    ) -> None:
        """Update PreparedDB display metadata without requiring the source file."""
        fields: list[str] = []
        values: list[object] = []
        for col, val in (
            ("title", title), ("artist", artist), ("album", album),
            ("duration_s", duration_s), ("bpm", bpm), ("key", key),
        ):
            if val is not None:
                fields.append(f"{col} = ?")
                values.append(val)
        if source_path is not None:
            fields.append("source_path = ?")
            values.append(source_path)
        if not fields:
            return
        with self._connect() as con:
            con.execute(
                f"UPDATE prepared_tracks SET {', '.join(fields)} WHERE wrk_id = ?",
                (*values, wrk_id),
            )
            set_fields = [f for f in fields if not f.startswith("album") and not f.startswith("source_path")]
            set_values = [
                v for f, v in zip(fields, values)
                if not f.startswith("album") and not f.startswith("source_path")
            ]
            if set_fields:
                con.execute(
                    f"UPDATE prepared_set_tracks SET {', '.join(set_fields)} WHERE wrk_id = ?",
                    (*set_values, wrk_id),
                )

    def remove_prepared_track(self, wrk_id: str) -> None:
        """Remove a prepared track from DB indexes and all WREKKED sets."""
        with self._connect() as con:
            con.execute("DELETE FROM prepared_set_tracks WHERE wrk_id = ?", (wrk_id,))
            con.execute("DELETE FROM prepared_tracks WHERE wrk_id = ?", (wrk_id,))

    def remove_by_wrk_path(self, wrk_path: str | Path) -> None:
        """Remove DB rows linked to a .wrk path."""
        p = str(Path(wrk_path).absolute())
        with self._connect() as con:
            con.execute("DELETE FROM prepared_set_tracks WHERE wrk_path = ?", (p,))
            con.execute("DELETE FROM prepared_tracks WHERE wrk_path = ?", (p,))

    def copy_tracks_to_set(self, tracks: Sequence[WrekkedTrack], set_id: int) -> None:
        if not tracks:
            return
        existing = self.get_set_wrk_ids(set_id)
        with self._connect() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(position), -1) AS max_position FROM prepared_set_tracks WHERE set_id = ?",
                (set_id,),
            ).fetchone()
        next_position = int(row["max_position"] if row else -1) + 1
        for t in tracks:
            if t.wrk_id in existing:
                continue
            self.upsert_set_track(
                set_id=set_id, wrk_id=t.wrk_id, wrk_path=t.wrk_path,
                title=t.title, artist=t.artist, duration_s=t.duration_s,
                bpm=t.bpm, key=t.key, stems_ready=t.stems_ready,
                wrk_ready=t.wrk_ready, source_available=t.source_available,
                position=next_position,
            )
            existing.add(t.wrk_id)
            next_position += 1
        self.update_set_track_count(set_id)
        self.update_set_total_duration(set_id)

    def move_tracks_to_set(self, tracks: Sequence[WrekkedTrack], src_set_id: int, dst_set_id: int) -> None:
        if src_set_id == dst_set_id:
            return
        self.copy_tracks_to_set(tracks, dst_set_id)
        for t in tracks:
            self.remove_set_track(src_set_id, t.wrk_id)
        self.update_set_total_duration(src_set_id)

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as con:
            row = con.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
