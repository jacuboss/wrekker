from wrekker.ui.widgets.marker_style import (
    MarkerDisplayMode,
    marker_paint_sort_key,
    marker_draw_style,
    marker_label,
    marker_tier,
    should_draw_marker,
)


def test_marker_hierarchy_labels_are_exact() -> None:
    primary = {
        "drop": "DROP",
        "mix_in": "MIX IN",
        "mix_out": "MIX OUT",
        "switch_point": "SWITCH",
    }
    wrekk = {
        "vocal_in": "W:VOC+",
        "vocal_out": "W:VOC-",
        "bass_in": "W:BSS+",
        "bass_out": "W:BSS-",
        "kick_in": "W:KICK+",
        "kick_out": "W:KICK-",
        "top_in": "W:TOP+",
        "top_out": "W:TOP-",
        "vocal_ghost": "W:GHOST",
        "deconstruct": "W:DECON",
        "rebuild": "W:REBUILD",
    }

    for value, label in primary.items():
        assert marker_tier(value) == "primary"
        assert marker_label(value) == label

    for value, label in wrekk.items():
        assert marker_tier(value) == "wrekk"
        assert marker_label(value) == label

    assert marker_tier("phrase") == "guide"
    assert marker_label("phrase") == "PHRASE"
    assert marker_tier("wrekk_top") == "legacy"


def test_essential_mode_preserves_visual_hierarchy() -> None:
    primary = marker_draw_style("drop", 0.9, MarkerDisplayMode.ESSENTIAL, view="overview")
    wrekk = marker_draw_style("vocal_ghost", 0.9, MarkerDisplayMode.ESSENTIAL, view="overview")
    guide = marker_draw_style("phrase", 0.9, MarkerDisplayMode.ESSENTIAL, view="overview")

    assert primary["line_width"] > wrekk["line_width"]
    assert primary["alpha"] > wrekk["alpha"] > guide["alpha"]
    assert primary["label"] is False
    assert wrekk["label"] is False
    assert guide["label"] is False
    assert should_draw_marker("phrase", 0.9, MarkerDisplayMode.ESSENTIAL, view="overview")
    assert should_draw_marker("vocal_ghost", 0.9, MarkerDisplayMode.ESSENTIAL, view="overview")
    assert not should_draw_marker("vocal_in", 0.9, MarkerDisplayMode.ESSENTIAL, view="overview")
    assert not should_draw_marker("first_beat", 0.9, MarkerDisplayMode.ESSENTIAL, view="overview")
    assert not should_draw_marker("drop", 0.69, MarkerDisplayMode.ESSENTIAL, view="overview")
    assert should_draw_marker("drop", 0.70, MarkerDisplayMode.ESSENTIAL, view="overview")
    assert not should_draw_marker("wrekk_top", 0.95, MarkerDisplayMode.ESSENTIAL, view="overview")


def test_paint_order_keeps_primary_on_top() -> None:
    class Marker:
        def __init__(self, value: str) -> None:
            self.type = type("MarkerTypeStub", (), {"value": value})()
            self.position_s = 1.0

    ordered = sorted(
        [Marker("drop"), Marker("phrase"), Marker("vocal_in")],
        key=marker_paint_sort_key,
    )

    assert [m.type.value for m in ordered] == ["phrase", "vocal_in", "drop"]


def test_deck_hud_ignores_legacy_wrekk_markers() -> None:
    from PyQt6.QtWidgets import QApplication

    from wrekker.core.deck import AutoMarker, MarkerType
    from wrekker.ui.widgets.deck import DeckWidget

    _app = QApplication.instance() or QApplication([])
    deck = DeckWidget("A")
    markers = (
        AutoMarker("legacy", MarkerType.WREKK_TOP, "WREKK", 4.0, 0.95),
        AutoMarker("ghost", MarkerType.VOCAL_GHOST, "GHOST", 8.0, 0.92, category="wrekk", family="opportunity"),
    )

    deck._refresh_marker_cache(markers)

    assert deck._markers_by_tier["primary"] == ()
    assert [m.id for m in deck._markers_by_tier["wrekk"]] == ["ghost"]
