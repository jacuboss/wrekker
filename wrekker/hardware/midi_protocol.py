"""
DDJ-FLX4 MIDI protocol constants.

Source: Pioneer-DDJ-FLX4.midi.xml + Pioneer-DDJ-FLX4-script.js (Mixxx mapping).
All values verified from the official Mixxx DDJ-FLX4 mapping.
0-indexed channel convention (mido).
"""
from __future__ import annotations

# ── Port identification ────────────────────────────────────────────────────────
PORT_NAME_PATTERN = "DDJ-FLX4"

# ── MIDI channels (0-indexed) ─────────────────────────────────────────────────
CH_DECK   = {"A": 0, "B": 1}   # transport, mixer, jog
CH_BEATFX = {"A": 4, "B": 5}   # BeatFX section
CH_GLOBAL = 6                   # browser, crossfader, filter, headphones
# Pads encode deck AND shift in the channel:
CH_PADS = {
    "A": (7, 8),    # (normal, shift)
    "B": (9, 10),
}

# ── Transport notes (on deck channels 0 / 1) ──────────────────────────────────
NOTE_PLAY_PAUSE      = 0x0B   # 11
NOTE_PLAY_SHIFT      = 0x0E   # 14  PLAY+SHIFT → reverse roll / censor
NOTE_CUE             = 0x0C   # 12
NOTE_CUE_SHIFT       = 0x48   # 72  CUE+SHIFT → jump to start
NOTE_SYNC            = 0x58   # 88  short press: toggle sync
NOTE_SYNC_LONG       = 0x5C   # 92  long press: engage sync master
NOTE_SYNC_SHIFT      = 0x60   # 96  SHIFT+SYNC: cycle tempo range
NOTE_SHIFT           = 0x3F   # 63

# ── Jog wheel notes ───────────────────────────────────────────────────────────
NOTE_JOG_TOUCH       = 0x36   # 54  finger on platter
NOTE_JOG_TOUCH_SHIFT = 0x67   # 103 finger on platter + SHIFT held

# ── Loop notes ────────────────────────────────────────────────────────────────
NOTE_LOOP_IN            = 0x10  # 16
NOTE_LOOP_OUT           = 0x11  # 17
NOTE_LOOP_IN_SHIFT      = 0x4C  # 76  SHIFT+LOOP IN  → loop adjust-in mode
NOTE_LOOP_OUT_SHIFT     = 0x4E  # 78  SHIFT+LOOP OUT → loop adjust-out mode
NOTE_RELOOP_EXIT        = 0x4D  # 77
NOTE_RELOOP_EXIT_SHIFT  = 0x50  # 80  reloop and stop
NOTE_LOOP_CALL_LEFT     = 0x51  # 81  loop ÷2
NOTE_LOOP_CALL_RIGHT    = 0x53  # 83  loop ×2
NOTE_LOOP_JUMP_BACK     = 0x3E  # 62  CALL LEFT+SHIFT  → jump −32 beats
NOTE_LOOP_JUMP_FWD      = 0x3D  # 61  CALL RIGHT+SHIFT → jump +32 beats

# ── Headphone CUE (same note, different deck channels) ────────────────────────
NOTE_HEADPHONES_CUE = 0x54   # 84
# Master CUE — verified from FLX4 hardware: global note 0x63.
NOTE_MASTER_CUE = 0x63   # 99 on CH_GLOBAL

# ── Pad mode selector buttons (on deck channels 0 / 1) ────────────────────────
NOTE_PAD_MODE_HOT_CUE   = 0x1B  # 27
NOTE_PAD_MODE_KEYBOARD   = 0x69  # 105  → Wrekker: STEMS mode
NOTE_PAD_MODE_PAD_FX1    = 0x1E  # 30   → unused
NOTE_PAD_MODE_PAD_FX2    = 0x6B  # 107  → unused
NOTE_PAD_MODE_BEAT_JUMP  = 0x20  # 32
NOTE_PAD_MODE_BEAT_LOOP  = 0x6D  # 109
NOTE_PAD_MODE_SAMPLER    = 0x22  # 34   → unused
NOTE_PAD_MODE_KEY_SHIFT  = 0x6F  # 111  → unused

# ── Browser notes (on CH_GLOBAL = 6) ─────────────────────────────────────────
NOTE_SMART_CFX         = 0x00  # Smart CFX / WREKK toggle
NOTE_BROWSE_PRESS       = 0x41  # 65
NOTE_BROWSE_SHIFT_PRESS = 0x42  # 66
NOTE_LOAD_DECK_A        = 0x46  # 70
NOTE_LOAD_DECK_B        = 0x47  # 71

# ── BeatFX notes (on CH_BEATFX 4 / 5) ────────────────────────────────────────
NOTE_BEATFX_ON_OFF       = 0x47  # 71
NOTE_BEATFX_ON_OFF_SHIFT = 0x43  # 67  turn off + clear all slots
NOTE_BEATFX_SELECT_NEXT  = 0x63  # 99  next FX type
NOTE_BEATFX_SELECT_PREV  = 0x64  # 100 SHIFT + select → prev FX type
NOTE_BEATFX_BEAT_LEFT    = 0x4A  # 74  focus prev slot
NOTE_BEATFX_BEAT_RIGHT   = 0x4B  # 75  focus next slot
NOTE_BEATFX_CH_A         = 0x10  # 16  route FX to deck A (on CH_BEATFX["A"])
NOTE_BEATFX_CH_B         = 0x11  # 17  route FX to deck B (on CH_BEATFX["B"])

# ── Performance pad note ranges (on CH_PADS channels) ────────────────────────
NOTE_PADS_HOT_CUE  = list(range(0x00, 0x08))  # 0–7    hot cues 1-8
NOTE_PADS_BEAT_LOOP= list(range(0x60, 0x68))  # 96–103 beat loops
NOTE_PADS_BEAT_JUMP= list(range(0x20, 0x28))  # 32–39  beat jump
NOTE_PADS_SAMPLER  = list(range(0x30, 0x38))  # 48–55  sampler (unused)
NOTE_PADS_STEMS    = list(range(0x40, 0x48))  # 64–71  keyboard/stems
NOTE_PADS_KEY_SHIFT= list(range(0x70, 0x78))  # 112–119 key shift (unused)

# ── CC mappings ───────────────────────────────────────────────────────────────

# Tempo (pitch) fader — 14-bit, on deck channels 0/1
CC_PITCH_MSB = 0x00   # 0   high 7 bits
CC_PITCH_LSB = 0x20   # 32  low 7 bits  (combined: 0–16383, center=8192)

# Channel fader — 14-bit, on deck channels 0/1
CC_CHANNEL_FADER_MSB = 0x13  # 19
CC_CHANNEL_FADER_LSB = 0x33  # 51

# Pregain / Trim — 14-bit, on deck channels 0/1
CC_TRIM_MSB = 0x04  # 4
CC_TRIM_LSB = 0x24  # 36

# EQ — 14-bit, on deck channels 0/1
CC_EQ_HIGH_MSB = 0x07  # 7
CC_EQ_HIGH_LSB = 0x27  # 39
CC_EQ_MID_MSB  = 0x0B  # 11
CC_EQ_MID_LSB  = 0x2B  # 43
CC_EQ_LOW_MSB  = 0x0F  # 15
CC_EQ_LOW_LSB  = 0x2F  # 47

# Filter / Quick Effect — 14-bit, on CH_GLOBAL (per deck)
CC_FILTER_A_MSB = 0x17  # 23
CC_FILTER_A_LSB = 0x37  # 55
CC_FILTER_B_MSB = 0x18  # 24
CC_FILTER_B_LSB = 0x38  # 56

# Crossfader — 14-bit, on CH_GLOBAL
CC_CROSSFADER_MSB = 0x1F  # 31
CC_CROSSFADER_LSB = 0x3F  # 63

# Headphone mix — 14-bit, on CH_GLOBAL
CC_HEADPHONES_MIX_MSB = 0x0C  # 12  (0=full CUE, 16383=full master)
CC_HEADPHONES_MIX_LSB = 0x2C  # 44

# Headphone level — 7-bit, on CH_GLOBAL (0–127 → 0.0–2.0 gain)
CC_HEADPHONES_LEVEL = 0x09  # 9

# Master volume — 14-bit, on CH_GLOBAL
CC_MASTER_VOL_MSB = 0x08  # 8
CC_MASTER_VOL_LSB = 0x28  # 40

# Jog wheel — on deck channels 0/1
CC_JOG_PLATTER      = 0x22  # 34  platter vinyl-on  → scratch or bend
CC_JOG_PLATTER_OFF  = 0x23  # 35  platter vinyl-off → bend only
CC_JOG_RIM          = 0x21  # 33  rim spin          → nudge
CC_JOG_SEARCH       = 0x29  # 41  SHIFT+jog         → fast seek

# Browser knob — on CH_GLOBAL
CC_BROWSE_KNOB       = 0x40  # 64  rotate
CC_BROWSE_SHIFT_KNOB = 0x64  # 100 SHIFT+rotate (waveform zoom in Mixxx)

# BeatFX level/depth — on CH_BEATFX
CC_BEATFX_LEVEL = 0x02  # 2

# Channel VU LEDs — output CC on deck channels
CC_VU_METER = 0x02

# ── LED velocity values ────────────────────────────────────────────────────────
LED_OFF = 0x00
LED_ON  = 0x7F
LED_DIM = 0x1F

# ── Jog math ──────────────────────────────────────────────────────────────────
JOG_CENTER        = 64      # CC value meaning "no movement"
JOG_TICKS_PER_REV = 720     # Mixxx uses 720 ticks/rev for scratchEnable
JOG_NUDGE_MAX_PCT = 4.0     # ±4% BPM nudge at full spin

# ── SysEx ─────────────────────────────────────────────────────────────────────
SYSEX_PIONEER_PREFIX = bytes([0x00, 0x20, 0x76])
SYSEX_PAD_COLOR_CMD  = 0x01

# Keep-alive: prevents controller from entering power-save (every 200 ms)
SYSEX_KEEPALIVE = bytes([
    0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x05, 0x00, 0x50, 0x02, 0xF7
])

# ── Pad LED colors ─────────────────────────────────────────────────────────────
PAD_COLOR_VOCALS = (0xFF, 0x6B, 0x6B)   # red
PAD_COLOR_DRUMS  = (0x4E, 0xCD, 0xC4)   # cyan
PAD_COLOR_BASS   = (0xFF, 0xE6, 0x6D)   # yellow
PAD_COLOR_OTHER  = (0xA2, 0x9B, 0xFE)   # violet
PAD_COLORS = [PAD_COLOR_VOCALS, PAD_COLOR_DRUMS, PAD_COLOR_BASS, PAD_COLOR_OTHER]

STEM_GAIN_MAX = 2.0


def build_pad_color_sysex(deck: str, pad_idx: int, r: int, g: int, b: int) -> bytes:
    """Build Pioneer SysEx for RGB pad color. deck: 'A'|'B', pad_idx: 0-7."""
    deck_byte = 0x00 if deck == "A" else 0x01
    return bytes([
        0xF0,
        *SYSEX_PIONEER_PREFIX,
        deck_byte,
        SYSEX_PAD_COLOR_CMD,
        pad_idx & 0x07,
        r & 0x7F,
        g & 0x7F,
        b & 0x7F,
        0xF7,
    ])
