"""
DDJ-FLX4 MIDI driver for Wrekker.

Channel structure (0-indexed):
  0 / 1        → Deck A / B  (transport, mixer, jog)
  4 / 5        → Deck A / B  (BeatFX section)
  6            → Global      (browser, crossfader, filter, headphones)
  7 / 8        → Deck A pads normal / Deck A pads + SHIFT
  9 / 10       → Deck B pads normal / Deck B pads + SHIFT

14-bit controls: crossfader, channel faders, EQ, trim, filter, headphone mix.
MSB arrives first; LSB combines and fires the handler.
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from enum import Enum, auto
from typing import Optional, Callable

import mido

from wrekker.core.engine_v2 import AudioEngine
from wrekker.core.transport import (
    FX_TARGET_A, FX_TARGET_B, FX_TARGET_BOTH, Transport,
)
from wrekker.hardware.midi_protocol import (
    PORT_NAME_PATTERN,
    CH_DECK, CH_BEATFX, CH_GLOBAL, CH_PADS,
    # transport
    NOTE_PLAY_PAUSE, NOTE_PLAY_SHIFT,
    NOTE_CUE, NOTE_CUE_SHIFT,
    NOTE_SYNC, NOTE_SYNC_LONG, NOTE_SYNC_SHIFT,
    NOTE_SHIFT,
    # jog
    NOTE_JOG_TOUCH, NOTE_JOG_TOUCH_SHIFT,
    # loop
    NOTE_LOOP_IN, NOTE_LOOP_OUT,
    NOTE_LOOP_IN_SHIFT, NOTE_LOOP_OUT_SHIFT,
    NOTE_RELOOP_EXIT, NOTE_RELOOP_EXIT_SHIFT,
    NOTE_LOOP_CALL_LEFT, NOTE_LOOP_CALL_RIGHT,
    NOTE_LOOP_JUMP_BACK, NOTE_LOOP_JUMP_FWD,
    # headphones
    NOTE_HEADPHONES_CUE, NOTE_MASTER_CUE,
    # pad modes
    NOTE_PAD_MODE_HOT_CUE, NOTE_PAD_MODE_KEYBOARD,
    NOTE_PAD_MODE_BEAT_JUMP, NOTE_PAD_MODE_BEAT_LOOP,
    NOTE_PAD_MODE_PAD_FX1, NOTE_PAD_MODE_PAD_FX2,
    NOTE_PAD_MODE_SAMPLER, NOTE_PAD_MODE_KEY_SHIFT,
    # browser
    NOTE_SMART_CFX, NOTE_BROWSE_PRESS, NOTE_BROWSE_SHIFT_PRESS,
    CC_BROWSE_KNOB,
    NOTE_LOAD_DECK_A, NOTE_LOAD_DECK_B,
    # BeatFX
    NOTE_BEATFX_ON_OFF, NOTE_BEATFX_ON_OFF_SHIFT,
    NOTE_BEATFX_SELECT_NEXT, NOTE_BEATFX_SELECT_PREV,
    NOTE_BEATFX_BEAT_LEFT, NOTE_BEATFX_BEAT_RIGHT,
    NOTE_BEATFX_CH_A, NOTE_BEATFX_CH_B,
    # pads
    NOTE_PADS_HOT_CUE, NOTE_PADS_BEAT_LOOP,
    NOTE_PADS_BEAT_JUMP, NOTE_PADS_STEMS,
    # CC
    CC_PITCH_MSB, CC_PITCH_LSB,
    CC_CHANNEL_FADER_MSB, CC_CHANNEL_FADER_LSB,
    CC_TRIM_MSB, CC_TRIM_LSB,
    CC_EQ_HIGH_MSB, CC_EQ_HIGH_LSB,
    CC_EQ_MID_MSB,  CC_EQ_MID_LSB,
    CC_EQ_LOW_MSB,  CC_EQ_LOW_LSB,
    CC_FILTER_A_MSB, CC_FILTER_A_LSB,
    CC_FILTER_B_MSB, CC_FILTER_B_LSB,
    CC_CROSSFADER_MSB, CC_CROSSFADER_LSB,
    CC_HEADPHONES_MIX_MSB, CC_HEADPHONES_MIX_LSB,
    CC_HEADPHONES_LEVEL,
    CC_MASTER_VOL_MSB, CC_MASTER_VOL_LSB,
    CC_JOG_PLATTER, CC_JOG_PLATTER_OFF, CC_JOG_RIM, CC_JOG_SEARCH,
    CC_BEATFX_LEVEL, CC_VU_METER,
    # LED
    LED_OFF, LED_ON, LED_DIM,
    # jog constants
    JOG_CENTER, JOG_TICKS_PER_REV, JOG_NUDGE_MAX_PCT,
    # SysEx
    SYSEX_KEEPALIVE,
    PAD_COLORS, STEM_GAIN_MAX,
    build_pad_color_sysex,
)

__all__ = ["FLX4Driver", "PadMode"]

_CC_MAX  = 127.0
_14B_MAX = 16383.0
_14B_CTR = 8192       # center of 14-bit range

_PITCH_BPM_RANGE  = 30.0   # ±30 BPM full throw (dynamic, based on track BPM)
_SCRATCH_SAMPLES  = 44100 / JOG_TICKS_PER_REV  # samples per tick
_NUDGE_SCALE      = 0.04   # ±4% rate change at full jog-side throw (±63 ticks)
_STOP = object()           # sentinel for LED queue


class PadMode(Enum):
    HOT_CUE   = auto()
    STEMS     = auto()   # labelled "KEYBOARD" on hardware
    BEAT_JUMP = auto()
    BEAT_LOOP = auto()


class _JogState:
    __slots__ = ("touch",)
    def __init__(self) -> None:
        self.touch = False


class FLX4Driver:
    """DDJ-FLX4 MIDI driver."""

    def __init__(
        self,
        transport:   Transport,
        engine:      AudioEngine,
        sample_rate: int = 44100,
    ) -> None:
        self._transport   = transport
        self._engine      = engine
        self._sample_rate = sample_rate

        self._lock = threading.Lock()

        # Per-deck state
        self._pad_mode:  dict[str, PadMode] = {"A": PadMode.HOT_CUE, "B": PadMode.HOT_CUE}
        self._jog:       dict[str, _JogState] = {"A": _JogState(), "B": _JogState()}
        self._shift:     dict[str, bool]      = {"A": False, "B": False}

        # 14-bit accumulator: (channel, cc_msb) → stored MSB value
        self._msb: dict[tuple[int, int], int] = {}
        self._lsb: dict[tuple[int, int], int] = {}

        # Beat-jump sizes (beats); scalable by SHIFT+PAD7/8
        self._beatjump_sizes: dict[str, list[float]] = {
            "A": [1.0, 2.0, 4.0, 8.0],
            "B": [1.0, 2.0, 4.0, 8.0],
        }

        # Load callbacks — set by MainWindow after construction
        self._load_a_cb: Optional[Callable] = None
        self._load_b_cb: Optional[Callable] = None

        # Browse callbacks
        self._browse_scroll_cb: Optional[Callable[[int], None]] = None
        self._browse_click_cb:  Optional[Callable[[], None]]    = None

        # Scratch: Rust engine handles rate, release ramp, and backward playback
        self._scratch_impulse_timers: dict[str, Optional[threading.Timer]] = {"A": None, "B": None}

        # Nudge inactivity timers — cleared to 0 after 200 ms of no jog movement
        self._nudge_timers: dict[str, Optional[threading.Timer]] = {"A": None, "B": None}

        # LED output queue
        self._led_queue: "threading.Queue[object]" = __import__("queue").Queue()

        self._port_in:  Optional[mido.ports.BaseInput]  = None
        self._port_out: Optional[mido.ports.BaseOutput] = None

        self._running = False
        self._reader_thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._keepalive_timer: Optional[threading.Timer] = None

    # ── Public: set load callbacks ─────────────────────────────────────────────

    def set_load_callbacks(
        self,
        load_a: Callable,
        load_b: Callable,
    ) -> None:
        """Provide callables that load the currently selected library track."""
        self._load_a_cb = load_a
        self._load_b_cb = load_b

    def set_browse_callbacks(
        self,
        scroll: Callable[[int], None],
        click:  Callable[[], None],
    ) -> None:
        """Wire the browse knob scroll (+1/-1) and press callbacks."""
        self._browse_scroll_cb = scroll
        self._browse_click_cb  = click

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        port_in  = self._find_port("input")
        port_out = self._find_port("output")
        self._port_in  = mido.open_input(port_in)
        self._port_out = mido.open_output(port_out)
        self._running  = True

        self._reader_thread = threading.Thread(
            target=self._midi_reader, daemon=True, name="flx4-reader"
        )
        self._writer_thread = threading.Thread(
            target=self._led_writer, daemon=True, name="flx4-writer"
        )
        self._reader_thread.start()
        self._writer_thread.start()
        self._init_leds()
        self._schedule_keepalive()

    def stop(self) -> None:
        self._running = False
        if self._keepalive_timer:
            self._keepalive_timer.cancel()
        self._led_queue.put(_STOP)
        if self._writer_thread:
            self._writer_thread.join(timeout=2.0)
        if self._port_in:
            self._port_in.close()
        if self._port_out:
            self._port_out.close()

    def __enter__(self) -> "FLX4Driver":
        self.start(); return self
    def __exit__(self, *_) -> None:
        self.stop()

    @staticmethod
    def _find_port(direction: str) -> str:
        fn = mido.get_input_names if direction == "input" else mido.get_output_names
        matches = [p for p in fn() if PORT_NAME_PATTERN in p]
        if not matches:
            raise RuntimeError(
                f"DDJ-FLX4 {direction} port not found. Available: {fn()}"
            )
        return matches[0]

    # ── Keep-alive SysEx ──────────────────────────────────────────────────────

    def _schedule_keepalive(self) -> None:
        if not self._running:
            return
        try:
            msg = mido.Message.from_bytes(SYSEX_KEEPALIVE)
            self._port_out.send(msg)
        except Exception:
            pass
        self._keepalive_timer = threading.Timer(0.2, self._schedule_keepalive)
        self._keepalive_timer.daemon = True
        self._keepalive_timer.start()

    # ── MIDI reader thread ────────────────────────────────────────────────────

    def _midi_reader(self) -> None:
        for msg in self._port_in:
            if not self._running:
                break
            if msg.type == "clock":
                continue
            try:
                self._dispatch(msg)
            except Exception:
                pass

    def _dispatch(self, msg: mido.Message) -> None:
        ch = getattr(msg, "channel", -1)

        # Resolve deck and context from channel
        deck: Optional[str] = None
        pad_shift = False
        is_pad_ch = False
        is_beatfx = False

        if ch == 0:
            deck = "A"
        elif ch == 1:
            deck = "B"
        elif ch == 4:
            deck = "A"; is_beatfx = True
        elif ch == 5:
            deck = "B"; is_beatfx = True
        elif ch == 6:
            deck = None   # global
        elif ch == 7:
            deck = "A"; is_pad_ch = True; pad_shift = False
        elif ch == 8:
            deck = "A"; is_pad_ch = True; pad_shift = True
        elif ch == 9:
            deck = "B"; is_pad_ch = True; pad_shift = False
        elif ch == 10:
            deck = "B"; is_pad_ch = True; pad_shift = True

        if msg.type in ("note_on", "note_off"):
            pressed = (msg.type == "note_on" and msg.velocity > 0)
            if is_pad_ch:
                self._handle_pad(deck, msg.note, pressed, pad_shift)
            elif is_beatfx:
                self._handle_beatfx_note(deck, msg.note, pressed)
            else:
                with self._lock:
                    shift = self._shift[deck] if deck else False
                self._handle_note(deck, ch, msg.note, pressed, shift)

        elif msg.type == "control_change":
            with self._lock:
                shift = self._shift[deck] if deck else False
            self._handle_cc(deck, ch, msg.control, msg.value, shift)

    # ── 14-bit helpers ────────────────────────────────────────────────────────

    def _store_msb(self, ch: int, msb_cc: int, value: int) -> None:
        self._msb[(ch, msb_cc)] = value

    def _combine(self, ch: int, msb_cc: int, lsb_value: int) -> Optional[int]:
        """Return combined 14-bit value if MSB was seen, else None."""
        key = (ch, msb_cc)
        if key not in self._msb:
            return None
        raw = (self._msb.pop(key) << 7) | lsb_value
        return raw  # 0–16383

    def _store_lsb(self, ch: int, msb_cc: int, value: int) -> None:
        self._lsb[(ch, msb_cc)] = value

    def _combine_any_order(self, ch: int, msb_cc: int, is_msb: bool, value: int) -> Optional[int]:
        key = (ch, msb_cc)
        if is_msb:
            self._msb[key] = value
        else:
            self._lsb[key] = value
        if key not in self._msb or key not in self._lsb:
            return None
        raw = (self._msb[key] << 7) | self._lsb[key]
        if not is_msb:
            self._lsb.pop(key, None)
        return raw  # 0–16383

    @staticmethod
    def _14b_to_01(raw: int) -> float:
        return raw / _14B_MAX

    @staticmethod
    def _14b_to_eq_db(raw: int) -> float:
        """0–16383 → ±12 dB, center (8192) = 0 dB."""
        return ((raw / _14B_CTR) - 1.0) * 12.0

    @staticmethod
    def _14b_to_pregain(raw: int) -> float:
        """0–16383 → 0.0–2.0 linear gain; center (8192) ≈ 1.0 (unity)."""
        return raw / _14B_CTR

    @staticmethod
    def _14b_to_stem_gain(raw: int) -> float:
        gain = raw / _14B_CTR
        return 1.0 if abs(gain - 1.0) < 0.025 else max(0.0, min(2.0, gain))

    @staticmethod
    def _14b_to_bipolar(raw: int) -> float:
        v = (raw / _14B_CTR) - 1.0
        return 0.0 if abs(v) < 0.025 else max(-1.0, min(1.0, v))

    # ── Note dispatcher (transport / mixer / global channels) ─────────────────

    def _handle_note(
        self,
        deck: Optional[str],
        ch:   int,
        note: int,
        pressed: bool,
        shift:   bool,
    ) -> None:

        # ── GLOBAL channel ─────────────────────────────────────────────────────
        if deck is None:
            if not pressed:
                return
            if ch == CH_GLOBAL and note == NOTE_MASTER_CUE:
                self._transport.toggle_monitor_cue("master")
                mon = self._transport.get_monitor_state()
                self.light_global_button(NOTE_MASTER_CUE, LED_ON if mon.cue_master else LED_OFF)
                return
            if ch == CH_GLOBAL and note == NOTE_SMART_CFX:
                enabled = self._transport.toggle_smart_cfx()
                self.light_global_button(NOTE_SMART_CFX, LED_ON if enabled else LED_OFF)
                return
            if note in (NOTE_BROWSE_PRESS, NOTE_BROWSE_SHIFT_PRESS):
                if self._browse_click_cb:
                    self._browse_click_cb()
            elif note == NOTE_LOAD_DECK_A and self._load_a_cb:
                self._load_a_cb()
            elif note == NOTE_LOAD_DECK_B and self._load_b_cb:
                self._load_b_cb()
            elif ch == CH_GLOBAL:
                print(f"[flx4] unhandled global note note=0x{note:02X}")
            return

        # ── SHIFT button ───────────────────────────────────────────────────────
        if note == NOTE_SHIFT:
            with self._lock:
                self._shift[deck] = pressed
            return

        if not pressed:
            if note in (NOTE_JOG_TOUCH, NOTE_JOG_TOUCH_SHIFT):
                with self._lock:
                    self._jog[deck].touch = False
                self._engine.scratch_disable(deck)
            return

        # ── Jog touch ──────────────────────────────────────────────────────────
        if note in (NOTE_JOG_TOUCH, NOTE_JOG_TOUCH_SHIFT):
            current_rate = self._engine.get_playback_rate(deck)
            with self._lock:
                self._jog[deck].touch = True
            self._engine.scratch_enable(deck, current_rate)
            return

        # ── Transport ──────────────────────────────────────────────────────────
        if note == NOTE_PLAY_PAUSE:
            from wrekker.core.deck import DeckStatus
            state = self._transport.get_state(deck)
            if state.status == DeckStatus.PLAYING:
                self._transport.pause(deck)
                self.light_button(deck, NOTE_PLAY_PAUSE, LED_OFF)
            else:
                self._transport.play(deck)
                self.light_button(deck, NOTE_PLAY_PAUSE, LED_ON)
            return

        if note == NOTE_PLAY_SHIFT:
            # Reverse roll / censor — not yet implemented, no-op
            return

        if note == NOTE_CUE:
            self._transport.cue(deck)
            self.light_button(deck, NOTE_CUE, LED_ON)
            return

        if note == NOTE_CUE_SHIFT:
            # Jump to start
            self._transport.seek(deck, 0.0)
            return

        if note == NOTE_SYNC:
            state = self._transport.get_state(deck)
            if state.sync_enabled:
                self._transport.unsync(deck)
                self.light_button(deck, NOTE_SYNC, LED_OFF)
            else:
                self._transport.sync(deck)
                self.light_button(deck, NOTE_SYNC, LED_ON)
            return

        if note == NOTE_SYNC_LONG:
            self._transport.set_sync_master(deck)
            self.light_button(deck, NOTE_SYNC, LED_ON)
            return

        if note == NOTE_SYNC_SHIFT:
            # Cycle tempo range — not directly exposed in Transport; no-op for now
            return

        # ── Loop ───────────────────────────────────────────────────────────────
        if note == NOTE_LOOP_IN:
            self._transport.loop_in(deck)
            self.light_button(deck, NOTE_LOOP_IN, LED_ON)
            return

        if note == NOTE_LOOP_OUT:
            self._transport.loop_out(deck)
            self.light_button(deck, NOTE_LOOP_OUT, LED_ON)
            return

        if note == NOTE_LOOP_IN_SHIFT:
            # Loop adjust-in (fine-tune start with jog) — not yet implemented
            return

        if note == NOTE_LOOP_OUT_SHIFT:
            # Loop adjust-out — not yet implemented
            return

        if note == NOTE_RELOOP_EXIT:
            self._transport.loop_toggle(deck)
            state = self._transport.get_state(deck)
            vel   = LED_ON if (state.loop and state.loop.active) else LED_OFF
            self.light_button(deck, NOTE_RELOOP_EXIT, vel)
            return

        if note == NOTE_RELOOP_EXIT_SHIFT:
            # Reloop and stop — toggle loop off and pause
            self._transport.loop_toggle(deck)
            self._transport.pause(deck)
            return

        if note == NOTE_LOOP_CALL_LEFT:
            self._loop_scale(deck, 0.5)
            return

        if note == NOTE_LOOP_CALL_RIGHT:
            self._loop_scale(deck, 2.0)
            return

        if note == NOTE_LOOP_JUMP_BACK:
            self._beat_jump(deck, -32)
            return

        if note == NOTE_LOOP_JUMP_FWD:
            self._beat_jump(deck, +32)
            return

        # ── Pad mode selectors ─────────────────────────────────────────────────
        if note == NOTE_PAD_MODE_HOT_CUE:
            self._set_pad_mode(deck, PadMode.HOT_CUE)
            return
        if note == NOTE_PAD_MODE_KEYBOARD:
            self._set_pad_mode(deck, PadMode.STEMS)
            return
        if note == NOTE_PAD_MODE_BEAT_JUMP:
            self._set_pad_mode(deck, PadMode.BEAT_JUMP)
            return
        if note == NOTE_PAD_MODE_BEAT_LOOP:
            self._set_pad_mode(deck, PadMode.BEAT_LOOP)
            return
        if note in (NOTE_PAD_MODE_PAD_FX1, NOTE_PAD_MODE_PAD_FX2,
                    NOTE_PAD_MODE_SAMPLER, NOTE_PAD_MODE_KEY_SHIFT):
            # Modes not implemented in Wrekker — just acknowledge the LED
            self.light_button(deck, note, LED_DIM)
            return

        # ── Headphone CUE (PFL) ───────────────────────────────────────────
        if note == NOTE_HEADPHONES_CUE:
            self._transport.toggle_monitor_cue(deck)
            # LED is driven by sync_leds() at 10 Hz — no immediate write needed
            return

    # ── CC dispatcher ─────────────────────────────────────────────────────────

    def _handle_cc(
        self,
        deck:    Optional[str],
        ch:      int,
        control: int,
        value:   int,
        shift:   bool,
    ) -> None:

        # ── Global channel (crossfader, filter, headphones) ───────────────────
        if ch == CH_GLOBAL:
            # Browse knob (relative encoder: 1=CW/down, 127=CCW/up)
            if control == CC_BROWSE_KNOB:
                delta = value if value < 64 else value - 128
                if self._browse_scroll_cb:
                    self._browse_scroll_cb(delta)
                return

            # Crossfader 14-bit
            if control == CC_CROSSFADER_MSB:
                self._store_msb(ch, CC_CROSSFADER_MSB, value)
                return
            if control == CC_CROSSFADER_LSB:
                raw = self._combine(ch, CC_CROSSFADER_MSB, value)
                if raw is not None:
                    self._transport.set_crossfader(self._14b_to_01(raw))
                return

            # Master volume 14-bit (FLX4 may send LSB before MSB)
            if control == CC_MASTER_VOL_MSB:
                raw = self._combine_any_order(ch, CC_MASTER_VOL_MSB, True, value)
                if raw is not None:
                    self._transport.set_master_gain(self._14b_to_pregain(raw))
                return
            if control == CC_MASTER_VOL_LSB:
                raw = self._combine_any_order(ch, CC_MASTER_VOL_MSB, False, value)
                if raw is not None:
                    self._transport.set_master_gain(self._14b_to_pregain(raw))
                return

            # Filter A 14-bit
            if control == CC_FILTER_A_MSB:
                self._store_msb(ch, CC_FILTER_A_MSB, value)
                return
            if control == CC_FILTER_A_LSB:
                raw = self._combine(ch, CC_FILTER_A_MSB, value)
                if raw is not None:
                    v = self._14b_to_bipolar(raw)
                    if self._transport.get_smart_cfx_enabled():
                        self._transport.set_wrekk_macro("A", v)
                    else:
                        self._transport.set_channel_filter("A", v)
                return

            # Filter B 14-bit
            if control == CC_FILTER_B_MSB:
                self._store_msb(ch, CC_FILTER_B_MSB, value)
                return
            if control == CC_FILTER_B_LSB:
                raw = self._combine(ch, CC_FILTER_B_MSB, value)
                if raw is not None:
                    v = self._14b_to_bipolar(raw)
                    if self._transport.get_smart_cfx_enabled():
                        self._transport.set_wrekk_macro("B", v)
                    else:
                        self._transport.set_channel_filter("B", v)
                return

            # Headphone mix 14-bit
            if control == CC_HEADPHONES_MIX_MSB:
                self._store_msb(ch, CC_HEADPHONES_MIX_MSB, value)
                return
            if control == CC_HEADPHONES_MIX_LSB:
                raw = self._combine(ch, CC_HEADPHONES_MIX_MSB, value)
                if raw is not None:
                    self._transport.set_headphone_mix(self._14b_to_01(raw))
                return


            # Headphone level 7-bit (0–127 → 0.0–2.0)
            if control == CC_HEADPHONES_LEVEL:
                self._transport.set_headphone_level(value / _CC_MAX * 2.0)
                return
            # BeatFX level on global? (normally on beatfx ch — ignore here)
            print(f"[flx4] unhandled global cc control=0x{control:02X} value={value}")
            return

        # ── BeatFX channel ────────────────────────────────────────────────────
        if ch in (CH_BEATFX["A"], CH_BEATFX["B"]):
            if control == CC_BEATFX_LEVEL:
                if shift:
                    self._transport.set_fx_depth(value / _CC_MAX)
                else:
                    self._transport.set_fx_wet(value / _CC_MAX)
            return

        # ── Deck channels (0 / 1) ─────────────────────────────────────────────
        if deck is None:
            return

        # Tempo fader 14-bit
        if control == CC_PITCH_MSB:
            self._store_msb(ch, CC_PITCH_MSB, value)
            return
        if control == CC_PITCH_LSB:
            raw = self._combine(ch, CC_PITCH_MSB, value)
            if raw is not None:
                # Pioneer convention: fader UP = slower (−), fader DOWN = faster (+)
                # Hardware: TOP → raw≈0, CENTER → raw=8192, BOTTOM → raw≈16383
                # (raw/8192 − 1) maps: UP→−1, CENTER→0, DOWN→+1
                state   = self._transport.get_state(deck)
                native_bpm = (
                    (state.beatgrid.bpm if state.beatgrid else None)
                    or (state.track.bpm if state.track else None)
                    or 120.0
                )
                max_st  = 12.0 * math.log2(1.0 + _PITCH_BPM_RANGE / max(native_bpm, 60.0))
                pct     = (raw / _14B_CTR - 1.0) * max_st
                self._transport.set_pitch(deck, pct)
            return

        # Channel fader 14-bit
        if control == CC_CHANNEL_FADER_MSB:
            self._store_msb(ch, CC_CHANNEL_FADER_MSB, value)
            return
        if control == CC_CHANNEL_FADER_LSB:
            raw = self._combine(ch, CC_CHANNEL_FADER_MSB, value)
            if raw is not None:
                self._transport.set_channel_volume(deck, self._14b_to_01(raw))
            return

        # Trim / pregain 14-bit
        if control == CC_TRIM_MSB:
            self._store_msb(ch, CC_TRIM_MSB, value)
            return
        if control == CC_TRIM_LSB:
            raw = self._combine(ch, CC_TRIM_MSB, value)
            if raw is not None:
                if self._transport.get_smart_cfx_enabled():
                    self._transport.set_stem_gain_from_hardware(
                        deck, "other", self._14b_to_stem_gain(raw)
                    )
                else:
                    self._transport.set_pregain(deck, self._14b_to_pregain(raw))
            return

        # EQ 14-bit
        for msb_cc, lsb_cc, band in (
            (CC_EQ_HIGH_MSB, CC_EQ_HIGH_LSB, "high"),
            (CC_EQ_MID_MSB,  CC_EQ_MID_LSB,  "mid"),
            (CC_EQ_LOW_MSB,  CC_EQ_LOW_LSB,  "low"),
        ):
            if control == msb_cc:
                self._store_msb(ch, msb_cc, value)
                return
            if control == lsb_cc:
                raw = self._combine(ch, msb_cc, value)
                if raw is not None:
                    if self._transport.get_smart_cfx_enabled():
                        stem = {"high": "vocals", "mid": "drums", "low": "bass"}[band]
                        self._transport.set_stem_gain_from_hardware(
                            deck, stem, self._14b_to_stem_gain(raw)
                        )
                    else:
                        self._transport.set_eq(deck, band, self._14b_to_eq_db(raw))
                return

        # Jog wheel
        if control == CC_JOG_PLATTER:
            self._handle_jog(deck, value, mode="scratch_or_bend")
            return
        if control == CC_JOG_PLATTER_OFF:
            self._handle_jog(deck, value, mode="bend")
            return
        if control == CC_JOG_RIM:
            self._handle_jog(deck, value, mode="nudge")
            return
        if control == CC_JOG_SEARCH:
            self._handle_jog_search(deck, value)
            return

    # ── BeatFX note handler ───────────────────────────────────────────────────

    def _handle_beatfx_note(self, deck: str, note: int, pressed: bool) -> None:
        if not pressed:
            return

        with self._lock:
            shift = self._shift[deck]

        if note == NOTE_BEATFX_ON_OFF:
            fx = self._transport.get_fx_state()
            self._transport.set_fx_enabled(not fx.enabled)
            self._refresh_beatfx_on_leds()
            return

        if note == NOTE_BEATFX_ON_OFF_SHIFT:
            self._transport.set_fx_enabled(False)
            self._refresh_beatfx_on_leds()
            return

        if note == NOTE_BEATFX_SELECT_NEXT:
            fx = self._transport.get_fx_state()
            from wrekker.core.transport import FX_NAMES
            next_type = (fx.fx_type + 1) % len(FX_NAMES)
            self._transport.set_fx_type(next_type)
            return

        if note == NOTE_BEATFX_SELECT_PREV:
            fx = self._transport.get_fx_state()
            from wrekker.core.transport import FX_NAMES
            prev_type = (fx.fx_type - 1) % len(FX_NAMES)
            self._transport.set_fx_type(prev_type)
            return

        if note == NOTE_BEATFX_BEAT_LEFT:
            fx = self._transport.get_fx_state()
            from wrekker.core.transport import FX_NAMES
            self._transport.set_fx_type((fx.fx_type - 1) % len(FX_NAMES))
            return

        if note == NOTE_BEATFX_BEAT_RIGHT:
            fx = self._transport.get_fx_state()
            from wrekker.core.transport import FX_NAMES
            self._transport.set_fx_type((fx.fx_type + 1) % len(FX_NAMES))
            return

        if note == NOTE_BEATFX_CH_A:
            self._transport.set_fx_target(FX_TARGET_A)
            self._refresh_beatfx_on_leds()
            return

        if note == NOTE_BEATFX_CH_B:
            self._transport.set_fx_target(FX_TARGET_B)
            self._refresh_beatfx_on_leds()
            return

    # ── Pad handler ───────────────────────────────────────────────────────────

    def _handle_pad(self, deck: str, note: int, pressed: bool, shift: bool) -> None:
        with self._lock:
            mode = self._pad_mode[deck]

        # ── HOT CUE ────────────────────────────────────────────────────────────
        if mode == PadMode.HOT_CUE and note in NOTE_PADS_HOT_CUE:
            idx = NOTE_PADS_HOT_CUE.index(note)
            if not pressed:
                return
            if shift:
                # Delete cue — not yet implemented, no-op
                pass
            else:
                state = self._transport.get_state(deck)
                if idx < len(state.cue_points):
                    self._transport.jump_to_cue(deck, idx)
                else:
                    self._transport.add_cue(deck, label=f"Cue {idx + 1}")
                    self.light_button_on_pad_ch(deck, note, LED_ON, shift=False)
            return

        # ── STEMS / KEYBOARD ──────────────────────────────────────────────────
        if mode == PadMode.STEMS and note in NOTE_PADS_STEMS:
            if not pressed:
                return
            pad_idx = NOTE_PADS_STEMS.index(note)
            self._handle_stem_pad(deck, pad_idx, shift)
            return

        # ── BEAT JUMP ─────────────────────────────────────────────────────────
        if mode == PadMode.BEAT_JUMP and note in NOTE_PADS_BEAT_JUMP:
            if not pressed:
                return
            idx = NOTE_PADS_BEAT_JUMP.index(note)
            # Layout: PAD1=−sz0, PAD2=+sz0, PAD3=−sz1, PAD4=+sz1, etc.
            # PAD7(idx=6)+SHIFT → halve sizes; PAD8(idx=7)+SHIFT → double sizes
            if shift and idx == 6:
                self._scale_beatjump_sizes(deck, 0.5)
                return
            if shift and idx == 7:
                self._scale_beatjump_sizes(deck, 2.0)
                return
            size_idx = idx // 2
            direction = +1 if (idx % 2 == 1) else -1
            sizes = self._beatjump_sizes[deck]
            beats = sizes[min(size_idx, len(sizes) - 1)] * direction
            self._beat_jump(deck, beats)
            return

        # ── BEAT LOOP ─────────────────────────────────────────────────────────
        if mode == PadMode.BEAT_LOOP and note in NOTE_PADS_BEAT_LOOP:
            if not pressed:
                return
            idx = NOTE_PADS_BEAT_LOOP.index(note)
            self._set_beat_loop(deck, idx)
            return

    # ── Jog wheel ─────────────────────────────────────────────────────────────

    def _handle_jog(self, deck: str, value: int, mode: str) -> None:
        """
        mode: 'scratch_or_bend' | 'bend' | 'nudge'
        CC value: 64=stopped, <64=backward, >64=forward.
        """
        ticks = value - JOG_CENTER
        with self._lock:
            touch = self._jog[deck].touch

        if mode == "scratch_or_bend" and touch:
            # Scratch: send raw ticks to the Rust engine.
            self._engine.scratch_tick(deck, ticks)
        elif mode == "scratch_or_bend" and abs(ticks) >= 18:
            # Hard platter flick without capacitive touch: treat it as a short
            # vinyl impulse so fast jog-wheel moves can scratch like a turntable.
            current_rate = self._engine.get_playback_rate(deck)
            self._engine.scratch_enable(deck, current_rate)
            self._engine.scratch_tick(deck, ticks)

            with self._lock:
                t = self._scratch_impulse_timers[deck]
                if t is not None:
                    t.cancel()

            def _release(d=deck):
                with self._lock:
                    self._scratch_impulse_timers[d] = None
                    still_touched = self._jog[d].touch
                if not still_touched:
                    self._engine.scratch_disable(d)

            timer = threading.Timer(0.055, _release)
            with self._lock:
                self._scratch_impulse_timers[deck] = timer
            timer.start()
        else:
            # Nudge / pitch-bend via Rust engine.
            # Rust applies this as a temporary additive offset on playback_rate and
            # decays it smoothly to 0 — does not touch the pitch fader or sync PLL.
            nudge_rate = (ticks / 64.0) * _NUDGE_SCALE
            self._engine.set_nudge(deck, nudge_rate)

            # Reset inactivity timer: after 200 ms of no movement, decay to 0
            with self._lock:
                t = self._nudge_timers[deck]
                if t is not None:
                    t.cancel()

            def _decay(d=deck):
                with self._lock:
                    self._nudge_timers[d] = None
                self._engine.set_nudge(d, 0.0)

            timer = threading.Timer(0.20, _decay)
            with self._lock:
                self._nudge_timers[deck] = timer
            timer.start()

    def _handle_jog_search(self, deck: str, value: int) -> None:
        """SHIFT+jog: fast seek."""
        ticks   = value - JOG_CENTER
        delta_s = ticks * 150 / self._sample_rate   # ~150× normal speed
        proxy   = self._engine.deck_a if deck == "A" else self._engine.deck_b
        new_pos = max(0.0, proxy.position_s + delta_s)
        self._engine.seek(deck, new_pos)

    # ── Loop helpers ──────────────────────────────────────────────────────────

    def _loop_scale(self, deck: str, factor: float) -> None:
        state = self._transport.get_state(deck)
        if state.loop is None:
            return
        old = state.loop
        new_dur = (old.end_s - old.start_s) * factor
        from wrekker.core.deck import LoopState
        loop = LoopState(
            active=old.active, start_s=old.start_s,
            end_s=old.start_s + new_dur, bars=old.bars * factor,
        )
        dl = self._transport._decks[deck]
        with dl.lock:
            self._transport._set_state(dl, dl.state.id, dl.state.status, loop=loop)
        self._engine.set_loop(deck, loop.active, loop.start_s, loop.end_s)

    # ── Beat jump ─────────────────────────────────────────────────────────────

    def _beat_jump(self, deck: str, beats: float) -> None:
        state = self._transport.get_state(deck)
        pos   = self._engine._get_deck(deck).position_s
        bg    = state.beatgrid

        if bg and len(bg.beats) >= 2:
            import bisect
            b = bg.beats
            i = bisect.bisect_right(b, pos) - 1
            target_i = max(0, min(len(b) - 1, round(i + beats)))
            target   = b[target_i]
        else:
            bpm    = (state.bpm_live or (state.track.bpm if state.track else None) or 120.0)
            target = max(0.0, pos + beats * (60.0 / bpm))

        self._transport.seek(deck, max(0.0, target))

    def _scale_beatjump_sizes(self, deck: str, factor: float) -> None:
        sizes = self._beatjump_sizes[deck]
        if factor < 1.0:
            new = [max(1.0 / 16, s * factor) for s in sizes]
        else:
            new = [min(128.0, s * factor) for s in sizes]
        self._beatjump_sizes[deck] = new

    # ── Beat loop ─────────────────────────────────────────────────────────────

    _BEAT_LOOP_SIZES = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0]  # beats

    def _set_beat_loop(self, deck: str, idx: int) -> None:
        beats  = self._BEAT_LOOP_SIZES[min(idx, len(self._BEAT_LOOP_SIZES) - 1)]
        state  = self._transport.get_state(deck)
        pos    = self._engine._get_deck(deck).position_s
        bpm    = (state.bpm_live or (state.track.bpm if state.track else None) or 120.0)
        dur_s  = beats * (60.0 / bpm)
        from wrekker.core.deck import LoopState
        loop   = LoopState(active=True, start_s=pos, end_s=pos + dur_s, bars=beats / 4.0)
        dl     = self._transport._decks[deck]
        with dl.lock:
            self._transport._set_state(dl, dl.state.id, dl.state.status, loop=loop)
        self._engine.set_loop(deck, True, pos, pos + dur_s)

    # ── Stem pads ─────────────────────────────────────────────────────────────

    _STEM_NAMES = ["vocals", "drums", "bass", "other"]

    def _handle_stem_pad(self, deck: str, pad_idx: int, shift: bool) -> None:
        if pad_idx >= 4:
            # Pads 5-8: solo the corresponding stem (0-3)
            stem_idx = pad_idx - 4
            if stem_idx >= len(self._STEM_NAMES):
                return
            stem  = self._STEM_NAMES[stem_idx]
            state = self._transport.get_state(deck)
            self._transport.solo_stem(deck, stem, not state.stems[stem].solo)
            self._update_stem_pad_led(deck, pad_idx, stem)
        else:
            stem = self._STEM_NAMES[pad_idx]
            if shift:
                # Solo: mute all others, unmute this
                state = self._transport.get_state(deck)
                for i, s in enumerate(self._STEM_NAMES):
                    self._transport.mute_stem(deck, s, s != stem)
                    self._update_stem_pad_led(deck, i, s)
            else:
                state = self._transport.get_state(deck)
                self._transport.mute_stem(deck, stem, not state.stems[stem].muted)
                self._update_stem_pad_led(deck, pad_idx, stem)

    def _update_stem_pad_led(self, deck: str, pad_idx: int, stem: str) -> None:
        state = self._transport.get_state(deck)
        s     = state.stems[stem]
        if s.muted:
            self.set_pad_color(deck, pad_idx % 4, 0x00, 0x00, 0x00)
        else:
            r, g, b = PAD_COLORS[pad_idx % 4]
            scale   = min(s.gain / STEM_GAIN_MAX, 1.0)
            self.set_pad_color(deck, pad_idx % 4,
                               int(r * scale), int(g * scale), int(b * scale))

    # ── Pad mode management ───────────────────────────────────────────────────

    def _set_pad_mode(self, deck: str, mode: PadMode) -> None:
        with self._lock:
            self._pad_mode[deck] = mode
        self.set_pad_mode_leds(deck, mode)
        if mode == PadMode.STEMS:
            self._refresh_stem_pad_leds(deck)
        elif mode == PadMode.HOT_CUE:
            self._refresh_hot_cue_leds(deck)
        elif mode == PadMode.BEAT_JUMP:
            self._refresh_beat_jump_leds(deck)
        elif mode == PadMode.BEAT_LOOP:
            self._refresh_beat_loop_leds(deck)

    def _refresh_stem_pad_leds(self, deck: str) -> None:
        state = self._transport.get_state(deck)
        for i, stem in enumerate(self._STEM_NAMES):
            s       = state.stems[stem]
            r, g, b = PAD_COLORS[i]
            if s.muted:
                self.set_pad_color(deck, i, 0x00, 0x00, 0x00)
            else:
                scale = min(s.gain, 1.0)
                self.set_pad_color(deck, i,
                                   int(r * scale), int(g * scale), int(b * scale))

    def _refresh_hot_cue_leds(self, deck: str) -> None:
        state = self._transport.get_state(deck)
        for i, note in enumerate(NOTE_PADS_HOT_CUE):
            vel = LED_ON if i < len(state.cue_points) else LED_OFF
            self.light_button_on_pad_ch(deck, note, vel, shift=False)

    def _refresh_beat_jump_leds(self, deck: str) -> None:
        sizes = self._beatjump_sizes[deck]
        for i, note in enumerate(NOTE_PADS_BEAT_JUMP):
            size_idx = min(i // 2, len(sizes) - 1)
            vel = LED_DIM if sizes[size_idx] < 1.0 else LED_ON
            self.light_button_on_pad_ch(deck, note, vel, shift=False)

    def _refresh_beat_loop_leds(self, deck: str) -> None:
        for i, note in enumerate(NOTE_PADS_BEAT_LOOP):
            self.light_button_on_pad_ch(deck, note, LED_ON, shift=False)

    # ── LED output ────────────────────────────────────────────────────────────

    def light_button(self, deck: str, note: int, velocity: int) -> None:
        ch  = CH_DECK[deck]
        msg = mido.Message("note_on", channel=ch, note=note, velocity=velocity)
        self._led_queue.put(msg)

    def light_global_button(self, note: int, velocity: int) -> None:
        msg = mido.Message("note_on", channel=CH_GLOBAL, note=note, velocity=velocity)
        self._led_queue.put(msg)

    def light_beatfx_button(self, deck: str, note: int, velocity: int) -> None:
        ch = CH_BEATFX[deck]
        msg = mido.Message("note_on", channel=ch, note=note, velocity=velocity)
        self._led_queue.put(msg)

    def light_button_on_pad_ch(
        self, deck: str, note: int, velocity: int, *, shift: bool
    ) -> None:
        ch  = CH_PADS[deck][1 if shift else 0]
        msg = mido.Message("note_on", channel=ch, note=note, velocity=velocity)
        self._led_queue.put(msg)

    def set_pad_color(self, deck: str, pad_idx: int, r: int, g: int, b: int) -> None:
        data = build_pad_color_sysex(deck, pad_idx, r, g, b)
        self._led_queue.put(data)

    def set_vu_meter(self, deck: str, value: float) -> None:
        """Set FLX4 channel VU LED level from linear peak 0.0..1.0+."""
        ch = CH_DECK[deck]
        v = max(0.0, min(1.0, value))
        # Slight gamma lift so medium levels are visible on the controller.
        vel = int(round((v ** 0.55) * 127))
        self._led_queue.put(mido.Message("control_change", channel=ch, control=CC_VU_METER, value=vel))

    def update_level_meters(
        self,
        peak_a: tuple[float, float],
        peak_b: tuple[float, float],
    ) -> None:
        self.set_vu_meter("A", max(peak_a))
        self.set_vu_meter("B", max(peak_b))

    def set_pad_mode_leds(self, deck: str, mode: PadMode) -> None:
        mapping = {
            PadMode.HOT_CUE:   NOTE_PAD_MODE_HOT_CUE,
            PadMode.STEMS:     NOTE_PAD_MODE_KEYBOARD,
            PadMode.BEAT_JUMP: NOTE_PAD_MODE_BEAT_JUMP,
            PadMode.BEAT_LOOP: NOTE_PAD_MODE_BEAT_LOOP,
        }
        for m, note in mapping.items():
            vel = LED_ON if m == mode else LED_OFF
            self.light_button(deck, note, vel)

    def _init_leds(self) -> None:
        self.light_global_button(
            NOTE_SMART_CFX,
            LED_ON if self._transport.get_smart_cfx_enabled() else LED_OFF,
        )
        for deck in ("A", "B"):
            self.set_pad_mode_leds(deck, PadMode.HOT_CUE)
            for note in (NOTE_PLAY_PAUSE, NOTE_CUE, NOTE_SYNC,
                         NOTE_LOOP_IN, NOTE_LOOP_OUT, NOTE_RELOOP_EXIT):
                self.light_button(deck, note, LED_OFF)
            self.light_beatfx_button(deck, NOTE_BEATFX_ON_OFF, LED_OFF)
            for note in NOTE_PADS_HOT_CUE:
                self.light_button_on_pad_ch(deck, note, LED_OFF, shift=False)
            self.light_button(deck, NOTE_HEADPHONES_CUE, LED_OFF)

    # ── LED writer thread ─────────────────────────────────────────────────────

    def _led_writer(self) -> None:
        while True:
            item = self._led_queue.get()
            if item is _STOP:
                break
            try:
                if isinstance(item, mido.Message):
                    self._port_out.send(item)
                elif isinstance(item, (bytes, bytearray)):
                    self._port_out.send(mido.Message.from_bytes(item))
            except Exception:
                pass
            time.sleep(0.001)

    # ── State-driven LED sync ─────────────────────────────────────────────────

    def sync_leds(self) -> None:
        """Re-sync all LEDs from Transport state. Call at ~10 Hz."""
        from wrekker.core.deck import DeckStatus
        self.light_global_button(
            NOTE_SMART_CFX,
            LED_ON if self._transport.get_smart_cfx_enabled() else LED_OFF,
        )
        self._refresh_beatfx_on_leds()
        for deck in ("A", "B"):
            state = self._transport.get_state(deck)

            play_vel = LED_ON if state.status == DeckStatus.PLAYING else LED_OFF
            self.light_button(deck, NOTE_PLAY_PAUSE, play_vel)

            sync_vel = LED_ON if state.sync_enabled else LED_OFF
            self.light_button(deck, NOTE_SYNC, sync_vel)

            if state.loop:
                loop_vel = LED_ON if state.loop.active else LED_DIM
            else:
                loop_vel = LED_OFF
            self.light_button(deck, NOTE_RELOOP_EXIT, loop_vel)

            with self._lock:
                mode = self._pad_mode[deck]
            if mode == PadMode.STEMS:
                self._refresh_stem_pad_leds(deck)
            elif mode == PadMode.HOT_CUE:
                self._refresh_hot_cue_leds(deck)

        # Monitor CUE (PFL) LEDs
        mon = self._transport.get_monitor_state()
        for deck, active in (("A", mon.cue_deck_a), ("B", mon.cue_deck_b)):
            self.light_button(deck, NOTE_HEADPHONES_CUE, LED_ON if active else LED_OFF)
        self.light_global_button(NOTE_MASTER_CUE, LED_ON if mon.cue_master else LED_OFF)

    def _refresh_beatfx_on_leds(self) -> None:
        fx = self._transport.get_fx_state()
        for deck, target in (("A", FX_TARGET_A), ("B", FX_TARGET_B)):
            active = fx.enabled and fx.target in (target, FX_TARGET_BOTH)
            self.light_beatfx_button(deck, NOTE_BEATFX_ON_OFF, LED_ON if active else LED_OFF)
