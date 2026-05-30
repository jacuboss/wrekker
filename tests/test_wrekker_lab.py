from __future__ import annotations

import json
import zipfile
from pathlib import Path

from wrekker.formats.fastload import FastloadCache, FASTLOAD_VERSION
from wrekker.lab.session import (
    begin_lab_session,
    human_change_sentence,
    human_revision_title,
    load_lab_state,
    marker_type_from_ui,
    marker_ui_parts,
)


def _make_wrk(path: Path, markers_override: list[dict] | None = None) -> None:
    manifest = {
        "wrk_version": 1,
        "analysis_revision": 0,
        "source": {"path": str(path.with_suffix(".wav")), "hash": "track-hash"},
        "metadata": {
            "title": "Lab Track",
            "artist": "Tester",
            "duration_s": 64.0,
            "sample_rate": 44100,
            "channels": 2,
            "bpm": 120.0,
            "key": "8A",
        },
        "contents": {"has_stems": True, "n_waveform_cols": 0, "n_stems": 4},
    }
    beatgrid = {
        "schema_version": 2,
        "bpm": 120.0,
        "confidence": 0.92,
        "beats": [0.5 + i * 0.5 for i in range(128)],
        "downbeats": [0.5 + i * 2.0 for i in range(32)],
        "phrase_markers": [{"position_sec": 0.5, "phrase_length": 16, "energy_level": 0.5}],
    }
    markers = markers_override or [
        {"id": "m1", "type": "mix_in", "position_s": 4.0, "confidence": 0.9, "reason": "auto"},
        {"id": "m2", "type": "drop", "position_s": 16.0, "confidence": 0.95, "reason": "auto"},
    ]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("audio/full.flac", b"not-used-by-lab-tests")
        z.writestr("analysis/beatgrid.json", json.dumps(beatgrid))
        z.writestr("analysis/markers.json", json.dumps(markers))
        z.writestr("dj/cues.json", "[]")
        z.writestr("dj/loops.json", "[]")


def _read_json(path: Path, name: str):
    with zipfile.ZipFile(path, "r") as z:
        return json.loads(z.read(name))


def test_lab_migrates_legacy_wrk_and_preserves_auto(tmp_path: Path) -> None:
    wrk = tmp_path / "track.wrk"
    _make_wrk(wrk)

    state = load_lab_state(wrk)

    assert state.auto_beatgrid == state.active_beatgrid
    assert state.auto_markers == state.active_markers
    assert _read_json(wrk, "analysis/beatgrid_auto.json") == _read_json(wrk, "analysis/beatgrid.json")
    assert _read_json(wrk, "analysis/markers_auto.json") == _read_json(wrk, "analysis/markers.json")
    assert _read_json(wrk, "analysis/changelog.json")["revisions"]


def test_lab_shift_grid_changes_active_not_auto_and_appends_changelog(tmp_path: Path) -> None:
    wrk = tmp_path / "track.wrk"
    _make_wrk(wrk)
    session = begin_lab_session(wrk)

    session.shift_grid(-0.012, "aligned to kick")
    session.add_hot_cue(16.0, "DROP")
    session.mark_verified()
    revision = session.save("Corrected grid and prepared drop cue")

    auto = _read_json(wrk, "analysis/beatgrid_auto.json")
    active = _read_json(wrk, "analysis/beatgrid.json")
    cues = _read_json(wrk, "dj/cues.json")
    changelog = _read_json(wrk, "analysis/changelog.json")
    manifest = _read_json(wrk, "manifest.json")

    assert auto["beats"][0] == 0.5
    assert active["beats"][0] == 0.488
    assert cues[0]["label"] == "DROP"
    assert changelog["revisions"][-1]["revision_id"] == revision.revision_id
    assert changelog["revisions"][-1]["manual_verified"] is True
    assert manifest["lab"]["manual_verified"] is True


def test_lab_preserves_locked_manual_markers_when_clearing_auto(tmp_path: Path) -> None:
    wrk = tmp_path / "track.wrk"
    _make_wrk(wrk)
    session = begin_lab_session(wrk)

    session.add_marker(8.0, "wrekk_top", "WREKK")
    session.lock_marker("m1", True)
    removed = session.clear_unlocked_auto_markers()
    session.save("Preserved locked/manual markers")

    markers = _read_json(wrk, "analysis/markers.json")
    ids = {m["id"] for m in markers}
    assert removed == 1
    assert "m1" in ids
    assert any(m.get("source") == "manual" for m in markers)
    assert "m2" not in ids


def test_lab_save_failure_leaves_original_wrk_untouched(tmp_path: Path, monkeypatch) -> None:
    wrk = tmp_path / "track.wrk"
    _make_wrk(wrk)
    session = begin_lab_session(wrk)
    before = wrk.read_bytes()
    session.shift_grid(0.025)

    import wrekker.lab.session as lab_session

    def _fail_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(lab_session.os, "replace", _fail_replace)
    try:
        session.save("should fail")
    except OSError:
        pass

    assert wrk.read_bytes() == before


def test_lab_fastload_metadata_updates_without_rebuilding_pcm(tmp_path: Path, monkeypatch) -> None:
    wrk = tmp_path / "track.wrk"
    _make_wrk(wrk)
    cache_root = tmp_path / "fastload"
    monkeypatch.setenv("WREKKER_FASTLOAD_CACHE", str(cache_root))
    cache = FastloadCache()
    d = cache.cache_dir(wrk)
    d.mkdir(parents=True)
    (d / "mix.pcm16").write_bytes(b"pcm-stays")
    (d / "mix.meta.json").write_text(json.dumps({"sr": 44100, "n_frames": 1, "n_channels": 2, "audio_format": "pcm16"}))
    stat = wrk.stat()
    (d / "ready.flag").write_text(json.dumps({
        "fastload_version": FASTLOAD_VERSION,
        "wrk_mtime_ns": stat.st_mtime_ns,
        "wrk_size": stat.st_size,
    }))

    session = begin_lab_session(wrk)
    session.set_bpm(124.0)
    session.save("BPM correction")

    assert (d / "mix.pcm16").read_bytes() == b"pcm-stays"
    assert json.loads((d / "beatgrid.json").read_text())["bpm"] == 124.0
    flag = json.loads((d / "ready.flag").read_text())
    assert flag["fastload_analysis_revision"] >= 1
    assert cache.is_valid(wrk)


def test_lab_filters_low_confidence_auto_markers_but_retains_locked_manual(tmp_path: Path) -> None:
    wrk = tmp_path / "track.wrk"
    markers = [
        {"id": "low-auto", "type": "drum_swap", "position_s": 2.0, "confidence": 0.59, "source": "auto"},
        {"id": "locked-low", "type": "vocal_ghost", "position_s": 3.0, "confidence": 0.63, "source": "auto", "locked": True},
        {"id": "manual-low", "type": "bass_lock", "position_s": 4.0, "confidence": 0.2, "source": "manual"},
        {"id": "good", "type": "drop", "position_s": 5.0, "confidence": 0.86, "source": "auto"},
    ]
    _make_wrk(wrk, markers_override=markers)

    state = load_lab_state(wrk)
    ids = {m["id"] for m in state.active_markers}

    assert "low-auto" not in ids
    assert {"locked-low", "manual-low", "good"}.issubset(ids)
    assert state.auto_markers == markers
    assert state.corrections["filtered_low_confidence_auto_markers"] == 1


def test_lab_marker_taxonomy_maps_friendly_labels_to_internal_types() -> None:
    assert marker_type_from_ui("WREKK", "VOCAL", "OUT") == "vocal_out"
    assert marker_type_from_ui("WREKK", "BASS", "OUT") == "bass_out"
    assert marker_type_from_ui("WREKK", "KICK", "IN") == "kick_in"
    assert marker_type_from_ui("WREKK", "GHOST", "") == "vocal_ghost"
    assert marker_ui_parts("drum_swap") == ("WREKK", "LEGACY", "DRUM SWAP")


def test_lab_history_formatter_uses_human_language() -> None:
    rev = {
        "summary": "Updated beatgrid:shift_grid",
        "changes": [
            {
                "entity": "beatgrid",
                "operation": "shift_grid",
                "before": {"first_beat_s": 0.431},
                "after": {"first_beat_s": 0.397, "delta_s": -0.034},
            },
            {
                "entity": "auto_marker",
                "operation": "manual_create",
                "after": {"type": "deconstruct", "position_s": 32.04},
            },
        ],
    }

    assert human_revision_title(rev) == "Corrected beatgrid alignment"
    assert human_change_sentence(rev["changes"][0]) == "Shifted the beatgrid 34.0 ms earlier."
    assert human_change_sentence(rev["changes"][1]) == "Added DECONSTRUCT marker at 0:32.04."


def test_lab_metronome_click_renderer_uses_draft_grid() -> None:
    import numpy as np
    from wrekker.ui.widgets.lab import _LabPreviewController

    chunk = np.zeros((1024, 2), dtype=np.float32)
    _LabPreviewController._add_clicks(chunk, 0, 44100, (0.0, 0.5), (0.0,), 0.65)

    assert float(np.abs(chunk).max()) > 0.0


def test_lab_timeline_model_allows_source_selection_even_without_stems() -> None:
    from wrekker.ui.qml_models import LabTimelineModel

    model = LabTimelineModel()

    assert model.sourceAvailable("VOCALS") is True
    assert model.sourceAvailable("DRUMS") is True
    assert model.sourceAvailable("BASS") is True
    assert model.sourceAvailable("OTHER") is True


def test_lab_timeline_model_filters_markers_by_selected_stem() -> None:
    import numpy as np
    from types import SimpleNamespace
    from wrekker.ui.qml_models import LabTimelineModel

    meta = SimpleNamespace(duration_s=60.0, waveform_peaks=np.ones(16), stem_energy=np.ones((16, 4)))
    draft = SimpleNamespace(
        duration_s=60.0,
        has_stems=True,
        active_beatgrid={"beats": [], "downbeats": [], "phrase_markers": []},
        auto_beatgrid={"beats": []},
        active_markers=[
            {"id": "v1", "type": "vocal_in", "position_s": 1.0, "confidence": 0.91},
            {"id": "b1", "type": "bass_lock", "position_s": 2.0, "confidence": 0.91},
            {"id": "p1", "type": "phrase", "position_s": 3.0, "confidence": 0.91},
        ],
        cues=[],
        loops=[],
    )
    session = SimpleNamespace(draft=draft)
    model = LabTimelineModel()

    model.sync_from_lab(meta, session, "VOCALS", False)

    marker_ids = {m["id"] for m in model.markers}
    assert marker_ids == {"v1", "p1"}
    assert {m["label"] for m in model.markers} == {"W:VOC", "PHRASE"}

    model.sync_from_lab(meta, session, "FULL MIX", False)
    assert {m["id"] for m in model.markers} == {"v1", "b1", "p1"}

    model.sync_from_lab(meta, session, "ANATOMY", False)
    assert {m["id"] for m in model.markers} == {"v1", "b1", "p1"}


def test_lab_preview_stem_monitor_sums_selected_stems() -> None:
    import numpy as np
    from types import SimpleNamespace
    from wrekker.ui.widgets.lab import _LabPreviewController

    session = SimpleNamespace(draft=SimpleNamespace(duration_s=1.0, active_beatgrid={}))
    ctrl = _LabPreviewController("dummy.wrk", session)
    ctrl._audio = np.ones((4, 2), dtype=np.float32) * 0.9
    ctrl._stems_audio = {
        "vocals": np.ones((4, 2), dtype=np.float32) * 0.1,
        "drums": np.ones((4, 2), dtype=np.float32) * 0.2,
        "bass": np.ones((4, 2), dtype=np.float32) * 0.3,
        "other": np.ones((4, 2), dtype=np.float32) * 0.4,
    }
    ctrl.set_stem_monitor(set(), "VOCALS")

    monitored = ctrl._build_stem_monitor_audio()

    assert monitored is not None
    assert np.allclose(monitored, 0.1)
