import numpy as np

from wrekker.analysis.auto_markers import AutoMarkerDetector
from wrekker.ui.widgets.marker_style import MarkerDisplayMode, marker_tier, should_draw_marker


def _techno_grid(duration_s: float = 64.0) -> dict:
    beat = 0.5
    beats = [i * beat for i in range(int(duration_s / beat))]
    downbeats = beats[::4]
    phrases = [
        {"position_sec": 0.0, "phrase_length": 8, "energy_level": 0.3},
        {"position_sec": 16.0, "phrase_length": 8, "energy_level": 0.8},
        {"position_sec": 32.0, "phrase_length": 8, "energy_level": 0.35},
        {"position_sec": 48.0, "phrase_length": 8, "energy_level": 0.9},
    ]
    return {"bpm": 120.0, "confidence": 0.95, "beats": beats, "downbeats": downbeats, "phrase_markers": phrases}


def _stem_energy() -> np.ndarray:
    n = 640
    e = np.zeros((n, 4), dtype=np.float32)

    def fill(start_s, end_s, voc, drm, bass, other):
        s = int(start_s / 64.0 * n)
        q = int(end_s / 64.0 * n)
        e[s:q, :] = (voc, drm, bass, other)

    fill(0, 16, 0.02, 0.05, 0.02, 0.25)
    fill(16, 32, 0.30, 0.42, 0.30, 0.05)
    fill(32, 48, 0.02, 0.05, 0.02, 0.30)
    fill(48, 64, 0.02, 0.45, 0.35, 0.18)
    return e


def test_wrekk_detector_generates_sparse_stem_aware_events_and_opportunities() -> None:
    markers = AutoMarkerDetector().analyze(_techno_grid(), _stem_energy(), np.ones(640), 64.0)
    by_type = {m.type.value: m for m in markers}

    for expected in {
        "vocal_in",
        "vocal_out",
        "bass_in",
        "bass_out",
        "kick_in",
        "kick_out",
        "top_out",
        "vocal_ghost",
        "deconstruct",
        "rebuild",
    }:
        assert expected in by_type
        assert marker_tier(expected) == "wrekk"

    assert by_type["vocal_in"].family == "structural"
    assert by_type["vocal_ghost"].family == "opportunity"
    assert by_type["vocal_ghost"].evidence
    assert by_type["deconstruct"].confidence >= 0.88
    assert by_type["rebuild"].confidence >= 0.88
    assert "wrekk_top" not in by_type
    assert "wrekk_rhythm" not in by_type


def test_wrekk_live_visibility_prefers_opportunities_over_structural_clutter() -> None:
    assert should_draw_marker("vocal_ghost", 0.90, MarkerDisplayMode.ESSENTIAL, view="overview")
    assert should_draw_marker("deconstruct", 0.90, MarkerDisplayMode.ESSENTIAL, view="overview")
    assert not should_draw_marker("vocal_in", 0.90, MarkerDisplayMode.ESSENTIAL, view="overview")
    assert should_draw_marker("vocal_in", 0.90, MarkerDisplayMode.PRIMARY_WREKK, view="overview")
    assert not should_draw_marker("wrekk_top", 0.99, MarkerDisplayMode.ESSENTIAL, view="overview")
