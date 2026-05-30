# DDJ-FLX4 → Wrekker: Control mapping

> **Source:** Pioneer-DDJ-FLX4.midi.xml + Pioneer-DDJ-FLX4-script.js (official Mixxx mapping).
> All values verified from the Mixxx DDJ-FLX4 mapping — no [VERIFY] placeholders remain.
>
> **Columns:**
> - **CH** = MIDI channel (0-indexed, mido convention)
> - **Type** = `NOTE` (button/pad) or `CC` (knob/fader)
> - **No.** = note or CC number (decimal and hex)
> - **Range** = values sent by hardware
> - **Status** = ✅ implemented | ⚠️ partial | ❌ not implemented

---

## MIDI channels

| Channel (mido) | MIDI ch | Section |
|---|---|---|
| 0 | 1 | Deck A (transport, mixer, jog) |
| 1 | 2 | Deck B (transport, mixer, jog) |
| 4 | 5 | Deck A BeatFX section |
| 5 | 6 | Deck B BeatFX section |
| 6 | 7 | Global (browser, crossfader, filter, headphones) |
| 7 | 8 | Deck A pads — normal |
| 8 | 9 | Deck A pads — SHIFT held |
| 9 | 10 | Deck B pads — normal |
| 10 | 11 | Deck B pads — SHIFT held |

---

## NOTES (note_on / note_off)

### Transport (CH 0 / 1)

| CH | Type | No. | Hex | Vel range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 0 / 1 | NOTE | 11 | 0x0B | 0 / 127 | PLAY/PAUSE | Play or Pause deck | ✅ |
| 0 / 1 | NOTE | 14 | 0x0E | 0 / 127 | PLAY + SHIFT | Reverse roll / censor (no-op) | ❌ |
| 0 / 1 | NOTE | 12 | 0x0C | 0 / 127 | CUE | Pioneer-style CUE (pause/set/preview) | ✅ |
| 0 / 1 | NOTE | 72 | 0x48 | 0 / 127 | CUE + SHIFT | Jump to track start | ✅ |
| 0 / 1 | NOTE | 88 | 0x58 | 0 / 127 | SYNC | Toggle sync on/off | ✅ |
| 0 / 1 | NOTE | 92 | 0x5C | 0 / 127 | SYNC (long press) | Set as sync master | ✅ |
| 0 / 1 | NOTE | 96 | 0x60 | 0 / 127 | SHIFT + SYNC | Cycle tempo range (no-op) | ❌ |
| 0 / 1 | NOTE | 63 | 0x3F | 0 / 127 | SHIFT | Modifier — enables alt functions | ✅ |

### Jog wheel (CH 0 / 1)

| CH | Type | No. | Hex | Vel range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 0 / 1 | NOTE | 54 | 0x36 | 0 / 127 | JOG TOUCH (platter) | Enable scratch when finger on platter | ✅ |
| 0 / 1 | NOTE | 103 | 0x67 | 0 / 127 | JOG TOUCH + SHIFT | Enable scratch (SHIFT held) | ✅ |

### Loop (CH 0 / 1)

| CH | Type | No. | Hex | Vel range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 0 / 1 | NOTE | 16 | 0x10 | 0 / 127 | LOOP IN | Set loop start at current position | ✅ |
| 0 / 1 | NOTE | 17 | 0x11 | 0 / 127 | LOOP OUT | Set loop end, activate loop | ✅ |
| 0 / 1 | NOTE | 76 | 0x4C | 0 / 127 | SHIFT + LOOP IN | Loop adjust-in mode (no-op) | ❌ |
| 0 / 1 | NOTE | 78 | 0x4E | 0 / 127 | SHIFT + LOOP OUT | Loop adjust-out mode (no-op) | ❌ |
| 0 / 1 | NOTE | 77 | 0x4D | 0 / 127 | RELOOP/EXIT | Toggle loop active/inactive | ✅ |
| 0 / 1 | NOTE | 80 | 0x50 | 0 / 127 | RELOOP/EXIT + SHIFT | Reloop and stop | ✅ |
| 0 / 1 | NOTE | 81 | 0x51 | 0 / 127 | LOOP CALL LEFT | Loop ÷2 | ✅ |
| 0 / 1 | NOTE | 83 | 0x53 | 0 / 127 | LOOP CALL RIGHT | Loop ×2 | ✅ |
| 0 / 1 | NOTE | 62 | 0x3E | 0 / 127 | CALL LEFT + SHIFT | Jump −32 beats | ✅ |
| 0 / 1 | NOTE | 61 | 0x3D | 0 / 127 | CALL RIGHT + SHIFT | Jump +32 beats | ✅ |

### Pad mode selectors (CH 0 / 1)

| CH | Type | No. | Hex | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|
| 0 / 1 | NOTE | 27 | 0x1B | HOT CUE (mode button) | Switch pads to Hot Cue mode | ✅ |
| 0 / 1 | NOTE | 105 | 0x69 | KEYBOARD (mode button) | Switch pads to Stems mode | ✅ |
| 0 / 1 | NOTE | 30 | 0x1E | PAD FX 1 (mode button) | Unused | ❌ |
| 0 / 1 | NOTE | 107 | 0x6B | PAD FX 2 (mode button) | Unused | ❌ |
| 0 / 1 | NOTE | 32 | 0x20 | BEAT JUMP (mode button) | Switch pads to Beat Jump mode | ✅ |
| 0 / 1 | NOTE | 109 | 0x6D | BEAT LOOP (mode button) | Switch pads to Beat Loop mode | ✅ |
| 0 / 1 | NOTE | 34 | 0x22 | SAMPLER (mode button) | Unused | ❌ |
| 0 / 1 | NOTE | 111 | 0x6F | KEY SHIFT (mode button) | Unused | ❌ |

### Performance pads — Hot Cue mode (CH 7 / 9 normal, CH 8 / 10 + SHIFT)

| CH | Type | No. | Hex | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|
| 7 / 9 | NOTE | 0–7 | 0x00–0x07 | Pads 1–8 | Jump to cue N / set if none exists | ✅ |
| 8 / 10 | NOTE | 0–7 | 0x00–0x07 | SHIFT + Pads 1–8 | Delete cue (no-op) | ❌ |

### Performance pads — Beat Loop mode (CH 7 / 9)

| CH | Type | No. | Hex | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|
| 7 / 9 | NOTE | 96–103 | 0x60–0x67 | Pads 1–8 | Set beat loop: 1/4, 1/2, 1, 2, 4, 8, 16, 32 beats | ✅ |

### Performance pads — Beat Jump mode (CH 7 / 9)

| CH | Type | No. | Hex | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|
| 7 / 9 | NOTE | 32–39 | 0x20–0x27 | Pads 1–8 | Jump ±beats (sizes: 1, 2, 4, 8) | ✅ |
| 8 / 10 | NOTE | 32 | 0x20 | SHIFT + Pad 7 | Halve all beat-jump sizes | ✅ |
| 8 / 10 | NOTE | 33 | 0x21 | SHIFT + Pad 8 | Double all beat-jump sizes | ✅ |

### Performance pads — Stems / Keyboard mode (CH 7 / 9)

| CH | Type | No. | Hex | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|
| 7 / 9 | NOTE | 64 | 0x40 | Pad 1 | Toggle mute Vocals | ✅ |
| 7 / 9 | NOTE | 65 | 0x41 | Pad 2 | Toggle mute Drums | ✅ |
| 7 / 9 | NOTE | 66 | 0x42 | Pad 3 | Toggle mute Bass | ✅ |
| 7 / 9 | NOTE | 67 | 0x43 | Pad 4 | Toggle mute Other | ✅ |
| 7 / 9 | NOTE | 68 | 0x44 | Pad 5 | Solo Vocals (toggle) | ✅ |
| 7 / 9 | NOTE | 69 | 0x45 | Pad 6 | Solo Drums (toggle) | ✅ |
| 7 / 9 | NOTE | 70 | 0x46 | Pad 7 | Solo Bass (toggle) | ✅ |
| 7 / 9 | NOTE | 71 | 0x47 | Pad 8 | Solo Other (toggle) | ✅ |
| 8 / 10 | NOTE | 64–67 | 0x40–0x43 | SHIFT + Pads 1–4 | Solo this stem, mute others | ✅ |

### Browser (CH 6 — Global)

| CH | Type | No. | Hex | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|
| 6 | NOTE | 0 | 0x00 | SMART CFX / WREKK | Toggle WREKK control mode | ✅ |
| 6 | NOTE | 65 | 0x41 | BROWSE (press) | Activate/open selected browser item | ✅ |
| 6 | NOTE | 66 | 0x42 | BROWSE + SHIFT (press) | Activate/open selected browser item | ✅ |
| 6 | NOTE | 70 | 0x46 | LOAD (Deck A) | Load selected track to Deck A | ✅ |
| 6 | NOTE | 71 | 0x47 | LOAD (Deck B) | Load selected track to Deck B | ✅ |
| 6 | NOTE | 99 | 0x63 | MASTER CUE | Toggle master bus in headphone CUE | ✅ |

### Headphones (CH 0 / 1)

| CH | Type | No. | Hex | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|
| 0 | NOTE | 84 | 0x54 | HEADPHONES CUE A | Toggle PFL routing for Deck A + LED feedback | ✅ |
| 1 | NOTE | 84 | 0x54 | HEADPHONES CUE B | Toggle PFL routing for Deck B + LED feedback | ✅ |

### BeatFX section (CH 4 = Deck A, CH 5 = Deck B)

| CH | Type | No. | Hex | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|
| 4 / 5 | NOTE | 71 | 0x47 | BEAT FX ON/OFF | Toggle FX enabled | ✅ |
| 4 / 5 | NOTE | 67 | 0x43 | BEAT FX ON/OFF + SHIFT | Disable FX | ✅ |
| 4 / 5 | NOTE | 99 | 0x63 | BEAT FX SELECT (next) | Next FX type | ✅ |
| 4 / 5 | NOTE | 100 | 0x64 | BEAT FX SELECT (prev / SHIFT) | Previous FX type | ✅ |
| 4 / 5 | NOTE | 74 | 0x4A | BEAT FX BEAT LEFT | Previous FX type | ✅ |
| 4 / 5 | NOTE | 75 | 0x4B | BEAT FX BEAT RIGHT | Next FX type | ✅ |
| 4 | NOTE | 16 | 0x10 | BEAT FX CH A | Route FX to Deck A | ✅ |
| 5 | NOTE | 17 | 0x11 | BEAT FX CH B | Route FX to Deck B | ✅ |

---

## CC (control_change — knobs and faders)

### Tempo / Pitch fader — 14-bit (CH 0 / 1)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 0 / 1 | CC | 0 | 0x00 | 0–127 | PITCH FADER MSB | High 7 bits (14-bit combined) | ✅ |
| 0 / 1 | CC | 32 | 0x20 | 0–127 | PITCH FADER LSB | Low 7 bits → ±16 semitones | ✅ |

> Combined 14-bit: `(MSB << 7) | LSB`, range 0–16383. Center = 8192 → 0 semitones.
> Formula: `pitch_semitones = (1 − raw/8192) × 16`.

### Channel fader — 14-bit (CH 0 / 1)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 0 / 1 | CC | 19 | 0x13 | 0–127 | CHANNEL FADER MSB | High 7 bits | ✅ |
| 0 / 1 | CC | 51 | 0x33 | 0–127 | CHANNEL FADER LSB | Low 7 bits → channel volume 0.0–1.0 | ✅ |

### Trim / Pregain — 14-bit (CH 0 / 1)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 0 / 1 | CC | 4 | 0x04 | 0–127 | TRIM MSB | High 7 bits | ✅ |
| 0 / 1 | CC | 36 | 0x24 | 0–127 | TRIM LSB | Low 7 bits → pregain 0.0–2.0 (center=1.0) | ✅ |

### EQ 3-band — 14-bit (CH 0 / 1)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 0 / 1 | CC | 7 | 0x07 | 0–127 | EQ HIGH MSB | High 7 bits | ✅ |
| 0 / 1 | CC | 39 | 0x27 | 0–127 | EQ HIGH LSB | Low 7 bits → ±12 dB | ✅ |
| 0 / 1 | CC | 11 | 0x0B | 0–127 | EQ MID MSB | High 7 bits | ✅ |
| 0 / 1 | CC | 43 | 0x2B | 0–127 | EQ MID LSB | Low 7 bits → ±12 dB | ✅ |
| 0 / 1 | CC | 15 | 0x0F | 0–127 | EQ LOW MSB | High 7 bits | ✅ |
| 0 / 1 | CC | 47 | 0x2F | 0–127 | EQ LOW LSB | Low 7 bits → ±12 dB | ✅ |

### Crossfader — 14-bit (CH 6 — Global)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 6 | CC | 31 | 0x1F | 0–127 | CROSSFADER MSB | High 7 bits | ✅ |
| 6 | CC | 63 | 0x3F | 0–127 | CROSSFADER LSB | Low 7 bits → 0.0 (full A) to 1.0 (full B) | ✅ |

### Filter / Quick Effect — 14-bit (CH 6 — Global)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 6 | CC | 23 | 0x17 | 0–127 | FILTER A MSB | High 7 bits | ✅ |
| 6 | CC | 55 | 0x37 | 0–127 | FILTER A LSB | Low 7 bits → FX Color −1.0 to +1.0 | ✅ |
| 6 | CC | 24 | 0x18 | 0–127 | FILTER B MSB | High 7 bits | ✅ |
| 6 | CC | 56 | 0x38 | 0–127 | FILTER B LSB | Low 7 bits → FX Color −1.0 to +1.0 | ✅ |

### Headphone mix — 14-bit (CH 6 — Global)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 6 | CC | 12 | 0x0C | 0–127 | HEADPHONE MIX MSB | High 7 bits | ✅ |
| 6 | CC | 44 | 0x2C | 0–127 | HEADPHONE MIX LSB | Low 7 bits → cue/master blend | ✅ |

### Headphone level — 7-bit (CH 6 — Global)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 6 | CC | 9 | 0x09 | 0–127 | HEADPHONE LEVEL | Headphone output gain 0.0–2.0 | ✅ |

### Master volume — 14-bit (CH 6 — Global)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 6 | CC | 8 | 0x08 | 0–127 | MASTER VOL MSB | High 7 bits | ✅ |
| 6 | CC | 40 | 0x28 | 0–127 | MASTER VOL LSB | Low 7 bits → master gain | ✅ |

### Jog wheel (CH 0 / 1)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 0 / 1 | CC | 34 | 0x22 | 0–127 | JOG PLATTER (vinyl on) | 64=stopped; scratch (touch) or bend | ✅ |
| 0 / 1 | CC | 35 | 0x23 | 0–127 | JOG PLATTER (vinyl off) | 64=stopped; bend only | ✅ |
| 0 / 1 | CC | 33 | 0x21 | 0–127 | JOG RIM spin | 64=stopped; nudge tempo | ✅ |
| 0 / 1 | CC | 41 | 0x29 | 0–127 | JOG (SHIFT held) | 64=stopped; fast seek | ✅ |

> Relative encoding: 64 = no movement, < 64 = backward, > 64 = forward.
> Mixxx uses 720 ticks/revolution for scratch calculations.

### Browser knob (CH 6 — Global)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 6 | CC | 64 | 0x40 | 0–127 | BROWSE knob rotate | Browser navigation in active library/WREKKED panel | ✅ |
| 6 | CC | 100 | 0x64 | 0–127 | BROWSE + SHIFT rotate | Waveform zoom (TODO) | ❌ |

### BeatFX level / depth (CH 4 / 5)

| CH | Type | CC | Hex | Range | Physical control | Wrekker action | Status |
|---|---|---|---|---|---|---|---|
| 4 / 5 | CC | 2 | 0x02 | 0–127 | BEAT FX LEVEL knob | FX wet (normal) / depth (SHIFT) | ✅ |

---

## LED / SysEx

| Item | Value | Notes |
|---|---|---|
| LED OFF | 0x00 | velocity = 0 |
| LED ON | 0x7F | velocity = 127 |
| LED DIM | 0x1F | velocity = 31 |
| Keep-alive SysEx | `F0 00 40 05 00 00 04 05 00 50 02 F7` | Send every 200 ms to prevent controller sleep |
| Pad RGB SysEx | `F0 00 20 76 [deck] 01 [pad] [r] [g] [b] F7` | deck: 0x00=A, 0x01=B; pad: 0–7; bytes < 0x80 |

---

## Pending / TODO

| Control | Pending action |
|---|---|
| SHIFT + HOT CUE pad | Delete cue point |
| PLAY + SHIFT | Reverse roll / censor |
| SHIFT + SYNC | Cycle tempo range |
| LOOP IN/OUT + SHIFT | Fine loop-start / loop-end adjust with jog |
| BROWSE + SHIFT rotate | Waveform zoom |
