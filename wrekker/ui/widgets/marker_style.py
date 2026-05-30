"""Shared visual hierarchy for Auto Markers."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from PyQt6.QtCore import Qt

from wrekker.core.deck import MARKER_MIN_CONFIDENCE
from wrekker.ui import theme


class MarkerDisplayMode(str, Enum):
    OFF = "off"
    PRIMARY = "primary"
    ESSENTIAL = "essential"
    PRIMARY_WREKK = "primary_wrekk"
    FULL = "full"
    DEBUG = "debug"


def coerce_marker_display_mode(value: MarkerDisplayMode | str) -> MarkerDisplayMode:
    if isinstance(value, MarkerDisplayMode):
        return value
    s = str(value or "").strip().lower().replace("-", "_")
    s = s.replace("+", "_").replace(" ", "_")
    aliases = {
        "off": MarkerDisplayMode.OFF,
        "primary": MarkerDisplayMode.PRIMARY,
        "essential": MarkerDisplayMode.ESSENTIAL,
        "primary_wrekk": MarkerDisplayMode.PRIMARY_WREKK,
        "primary__wrekk": MarkerDisplayMode.PRIMARY_WREKK,
        "full": MarkerDisplayMode.FULL,
        "debug": MarkerDisplayMode.DEBUG,
    }
    return aliases.get(s, MarkerDisplayMode.ESSENTIAL)


MarkerTier = Literal["primary", "wrekk", "guide", "legacy"]

PRIMARY_ORDER = (
    "drop",
    "mix_in",
    "mix_out",
    "switch_point",
)
WREKK_STRUCTURAL_ORDER = (
    "vocal_in",
    "vocal_out",
    "bass_in",
    "bass_out",
    "kick_in",
    "kick_out",
    "top_in",
    "top_out",
    "bass_shift",
    "drum_build",
    "drum_strip",
    "rhythm_shift",
    "texture_break",
)
WREKK_OPPORTUNITY_ORDER = (
    "vocal_ghost",
    "deconstruct",
    "rebuild",
    "bass_lock",
    "wash",
)
GUIDE_ORDER = (
    "phrase",
)
LEGACY_ORDER = (
    "wrekk_top",
    "wrekk_rhythm",
    "rhythm_in",
    "drum_swap",
)

PRIMARY_MARKERS = set(PRIMARY_ORDER)
WREKK_STRUCTURAL_MARKERS = set(WREKK_STRUCTURAL_ORDER)
WREKK_OPPORTUNITY_MARKERS = set(WREKK_OPPORTUNITY_ORDER)
WREKK_MARKERS = WREKK_STRUCTURAL_MARKERS | WREKK_OPPORTUNITY_MARKERS
GUIDE_MARKERS = set(GUIDE_ORDER)
LEGACY_MARKERS = set(LEGACY_ORDER)

MARKER_TIER_ORDER = {
    **{value: idx for idx, value in enumerate(PRIMARY_ORDER)},
    **{value: 100 + idx for idx, value in enumerate(WREKK_OPPORTUNITY_ORDER)},
    **{value: 130 + idx for idx, value in enumerate(WREKK_STRUCTURAL_ORDER)},
    **{value: 200 + idx for idx, value in enumerate(GUIDE_ORDER)},
    **{value: 900 + idx for idx, value in enumerate(LEGACY_ORDER)},
}

PRIMARY_UI_LABELS = {
    "drop": "DROP",
    "mix_in": "MIX IN",
    "mix_out": "MIX OUT",
    "switch_point": "SWITCH",
}
WREKK_UI_LABELS = {
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
    "bass_lock": "W:BSS LOCK",
    "wash": "W:WASH",
    "wrekk_top": "W:LEGACY",
    "wrekk_rhythm": "W:LEGACY",
    "rhythm_in": "W:LEGACY",
    "drum_swap": "W:LEGACY",
}
GUIDE_UI_LABELS = {
    "phrase": "PHRASE",
}

UI_LABELS: dict[str, str] = {
    **PRIMARY_UI_LABELS,
    **WREKK_UI_LABELS,
    **GUIDE_UI_LABELS,
}

SPECIFIC_LABELS: dict[str, str] = {
    "first_beat":     "FIRST BEAT",
    "first_downbeat": "FIRST DOWNBEAT",
    "phrase":         "PHRASE",
    "mix_in":         "MIX IN",
    "mix_out":        "MIX OUT",
    "drop":           "DROP",
    "breakdown":      "BREAKDOWN",
    "vocal_in":       "VOCAL IN",
    "vocal_out":      "VOCAL OUT",
    "bass_in":        "BASS IN",
    "bass_out":       "BASS OUT",
    "kick_in":        "KICK IN",
    "kick_out":       "KICK OUT",
    "top_in":         "TOP IN",
    "top_out":        "TOP OUT",
    "bass_shift":     "BASS SHIFT",
    "drum_build":     "DRUM BUILD",
    "drum_strip":     "DRUM STRIP",
    "rhythm_shift":   "RHYTHM SHIFT",
    "texture_break":  "TEXTURE BREAK",
    "vocal_ghost":    "GHOST",
    "deconstruct":    "DECONSTRUCT",
    "rebuild":        "REBUILD",
    "bass_lock":      "BASS LOCK",
    "wash":           "WASH",
    "wrekk_top":      "LEGACY WREKK",
    "wrekk_rhythm":   "LEGACY WREKK",
    "rhythm_in":      "LEGACY RHYTHM",
    "drum_swap":      "DRUM SWAP",
    "switch_point":   "SWITCH POINT",
}

MARKER_COLORS: dict[str, str] = {
    "first_beat":     "#7f8c8d",
    "first_downbeat": "#9aa4a6",
    "phrase":         "#8a8462",
    "mix_in":         "#35e6b5",
    "mix_out":        "#ff9f43",
    "drop":           "#ffcc33",
    "breakdown":      "#9b59ff",
    "vocal_in":       "#ff6baf",
    "vocal_out":      "#d85a9b",
    "bass_in":        "#ffd23f",
    "bass_out":       "#d9a600",
    "kick_in":        "#18d8ff",
    "kick_out":       "#1192b0",
    "top_in":         "#9b7cff",
    "top_out":        "#7252c7",
    "vocal_ghost":    theme.STATUS_WARN,
    "deconstruct":    theme.STATUS_WARN,
    "rebuild":        theme.STATUS_WARN,
    "bass_lock":      "#ffb02e",
    "wash":           "#b78cff",
    "wrekk_top":      "#6d737a",
    "wrekk_rhythm":   "#6d737a",
    "rhythm_in":      "#6d737a",
    "drum_swap":      "#ff7043",
    "switch_point":   "#fff7e0",
}


def marker_value(marker) -> str:
    mtype = getattr(marker, "type", None)
    return mtype.value if mtype is not None else ""


def marker_tier(value: str) -> MarkerTier:
    if value in PRIMARY_MARKERS:
        return "primary"
    if value in WREKK_MARKERS:
        return "wrekk"
    if value in GUIDE_MARKERS:
        return "guide"
    return "legacy"


def marker_sort_key(marker) -> tuple[int, float]:
    value = marker_value(marker)
    return (MARKER_TIER_ORDER.get(value, 999), float(getattr(marker, "position_s", 0.0) or 0.0))


def marker_paint_sort_key(marker) -> tuple[int, int, float]:
    value = marker_value(marker)
    tier = marker_tier(value)
    layer = {"guide": 0, "wrekk": 1, "primary": 2, "legacy": -1}.get(tier, 0)
    priority = -MARKER_TIER_ORDER.get(value, 999)
    return (layer, priority, float(getattr(marker, "position_s", 0.0) or 0.0))


def marker_label(value: str, *, compact: bool = False) -> str:
    return UI_LABELS.get(value, value.upper().replace("_", " "))


def marker_specific_label(value: str) -> str:
    return SPECIFIC_LABELS.get(value, value.upper().replace("_", " "))


def marker_color(value: str, confidence: float = 1.0) -> str:
    if confidence < MARKER_MIN_CONFIDENCE:
        return "#8a8f98"
    return MARKER_COLORS.get(value, "#8a8f98")


def should_draw_marker(
    value: str,
    confidence: float,
    mode: MarkerDisplayMode,
    *,
    view: Literal["overview", "zoom"],
    window_s: float | None = None,
) -> bool:
    if mode == MarkerDisplayMode.OFF:
        return False
    tier = marker_tier(value)
    if mode == MarkerDisplayMode.DEBUG:
        return True
    if value in LEGACY_MARKERS:
        return False
    threshold = MARKER_MIN_CONFIDENCE
    if value in WREKK_STRUCTURAL_MARKERS:
        threshold = 0.80
    elif value in WREKK_OPPORTUNITY_MARKERS:
        threshold = 0.88
    if confidence < threshold:
        return False
    if value not in PRIMARY_MARKERS and value not in WREKK_MARKERS and value not in GUIDE_MARKERS:
        return mode == MarkerDisplayMode.FULL
    if mode == MarkerDisplayMode.PRIMARY:
        return value in PRIMARY_MARKERS or value in GUIDE_MARKERS
    if mode == MarkerDisplayMode.FULL:
        return True
    if mode == MarkerDisplayMode.PRIMARY_WREKK:
        return value in PRIMARY_MARKERS or value in WREKK_MARKERS or value in GUIDE_MARKERS
    if tier == "primary":
        return True
    if tier == "wrekk":
        return value in WREKK_OPPORTUNITY_MARKERS
    if tier == "guide":
        return True
    return False


def marker_draw_style(
    value: str,
    confidence: float,
    mode: MarkerDisplayMode,
    *,
    view: Literal["overview", "zoom"],
    window_s: float | None = None,
) -> dict:
    tier = marker_tier(value)
    low_conf = confidence < MARKER_MIN_CONFIDENCE
    debug = mode == MarkerDisplayMode.DEBUG

    line_width = 1.35 if tier == "primary" else 1.15 if tier == "wrekk" else 1.0
    alpha = 0.84 if tier == "primary" else 0.66 if tier == "wrekk" else 0.16 if tier == "guide" else 0.22
    label = False
    compact = view == "zoom"

    if mode == MarkerDisplayMode.FULL:
        alpha = max(alpha, 0.72 if tier == "wrekk" else alpha)
    elif mode == MarkerDisplayMode.DEBUG:
        alpha = max(alpha, 0.72)
    elif mode == MarkerDisplayMode.ESSENTIAL:
        pass

    if low_conf:
        alpha = 0.36 if debug else 0.26

    return {
        "tier": tier,
        "color": marker_color(value, confidence),
        "line_width": line_width,
        "alpha": alpha,
        "label": label,
        "label_text": marker_specific_label(value) if debug else marker_label(value, compact=compact),
        "tail_height": 11 if tier == "primary" else 9 if tier == "wrekk" else 5,
        "dash": low_conf or tier in {"guide", "legacy"},
        "pen_style": Qt.PenStyle.DashLine if (low_conf or tier in {"guide", "legacy"}) else Qt.PenStyle.SolidLine,
    }


def marker_tooltip(marker) -> str:
    value = marker_value(marker)
    label = marker_specific_label(value)
    pos = float(getattr(marker, "position_s", 0.0) or 0.0)
    mins, secs = divmod(pos, 60)
    conf = int(float(getattr(marker, "confidence", 0.0) or 0.0) * 100)
    reason = getattr(marker, "reason", "") or ""
    tip = f"{label} · {int(mins)}:{secs:05.2f} · {conf}%"
    if reason:
        tip += f" · {reason}"
    return tip
