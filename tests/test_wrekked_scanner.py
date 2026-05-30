import json
import zipfile

from wrekker.library.prepared_db import PreparedDB, TrackStatus
from wrekker.library.wrekked_scanner import WrekkedScanner


def _write_wrk(path, source, beatgrid):
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "wrk_version": 1,
        "source": {
            "path": str(source),
            "hash": "fake-hash",
            "mtime_ns": source.stat().st_mtime_ns,
        },
        "metadata": {
            "title": path.stem,
            "artist": "Artist",
            "duration_s": 60.0,
            "bpm": 128.0,
            "key": "8A",
        },
        "contents": {
            "has_mix": True,
            "has_full_audio": True,
            "has_stems": False,
            "has_beatgrid": beatgrid is not None,
        },
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        if beatgrid is not None:
            zf.writestr("analysis/beatgrid.json", json.dumps(beatgrid))


def test_rescan_marks_old_beatgrid_schema_for_upgrade(tmp_path):
    source = tmp_path / "track.flac"
    source.write_bytes(b"source")
    wrk = tmp_path / "prepared" / "Set" / "track.wrk"
    _write_wrk(wrk, source, {"bpm": 128.0, "beats": [0.0, 0.5, 1.0]})

    db = PreparedDB(tmp_path / "prepared.db")
    scanner = WrekkedScanner(db, tmp_path / "prepared")

    assert scanner.scan() == 1
    assert scanner.last_needs_beatgrid_upgrade == 1

    rec = db.find_wrk(source)
    assert rec is not None
    assert rec.analysis_status == TrackStatus.OUTDATED
    assert "beatgrid schema" in (rec.warnings or "")


def test_rescan_keeps_schema_v2_ready(tmp_path):
    source = tmp_path / "track.flac"
    source.write_bytes(b"source")
    wrk = tmp_path / "prepared" / "Set" / "track.wrk"
    _write_wrk(
        wrk,
        source,
        {
            "schema_version": 2,
            "bpm": 128.0,
            "beats": [0.0, 0.5, 1.0],
            "downbeats": [0.0],
            "phrase_markers": [],
        },
    )

    db = PreparedDB(tmp_path / "prepared.db")
    scanner = WrekkedScanner(db, tmp_path / "prepared")

    assert scanner.scan() == 1
    assert scanner.last_needs_beatgrid_upgrade == 0

    rec = db.find_wrk(source)
    assert rec is not None
    assert rec.analysis_status == TrackStatus.READY


def test_rescan_preserves_manual_set_order(tmp_path):
    grid = {
        "schema_version": 2,
        "bpm": 128.0,
        "beats": [0.0, 0.5, 1.0],
        "downbeats": [0.0],
        "phrase_markers": [],
    }
    source_a = tmp_path / "a.flac"
    source_b = tmp_path / "b.flac"
    source_a.write_bytes(b"a")
    source_b.write_bytes(b"b")
    wrk_a = tmp_path / "prepared" / "Set" / "a.wrk"
    wrk_b = tmp_path / "prepared" / "Set" / "b.wrk"
    _write_wrk(wrk_a, source_a, grid)
    _write_wrk(wrk_b, source_b, grid)

    db = PreparedDB(tmp_path / "prepared.db")
    scanner = WrekkedScanner(db, tmp_path / "prepared")

    assert scanner.scan() == 2
    set_id = db.list_wrekked_sets()[0].id
    tracks = db.list_tracks_in_wrekked_set(set_id)
    assert {t.title for t in tracks} == {"a", "b"}

    db.reorder_set_track(set_id, tracks[-1].wrk_id, 0)
    manual_order = [t.wrk_id for t in db.list_tracks_in_wrekked_set(set_id)]

    assert scanner.scan() == 2
    assert [t.wrk_id for t in db.list_tracks_in_wrekked_set(set_id)] == manual_order
