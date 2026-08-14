# Wrekker — Brand & Design Brief

*Prepared for the designer taking over Wrekker's visual identity. Everything you need to know about what Wrekker is, who it's for, and what makes it worth designing for. No technical knowledge required.*

---

## 1. What is Wrekker?

Wrekker is **DJ performance software** — a program DJs run on their computer to mix music live. Two virtual turntables ("decks"), waveforms scrolling across the screen, faders, effects, and a real hardware DJ controller plugged in. Think of the category occupied by Serato, Rekordbox, or Traktor.

But Wrekker is built on a different premise: it is **stem-native**. Every song can be split into its four musical layers — **vocals, drums, bass, and everything else** — and the DJ can grab, mute, boost, or swap each layer independently, live. A DJ using Wrekker isn't just blending two songs; they're *dismantling* them and rebuilding something new in front of the crowd.

The name says it. **Wrekker = the one who wrecks.** You take finished tracks apart — deliberately, skillfully — and the wreckage becomes the performance.

It is also **free and open source**, made first for Linux (with Windows support), built by and for people who don't want their creative tools locked behind subscriptions and closed ecosystems.

---

## 2. The world it lives in

- **Music:** electronic dance music, with techno at the center of gravity. Steady tempos, long builds, drops, 8/16/32-bar phrases. The software's whole intelligence is tuned to this structure.
- **Place:** the DJ booth. Dark rooms, loud systems, sweaty hands, one glance at the screen between beat-matches. Everything on screen must be readable in the dark at a distance, in seconds.
- **Culture:** two subcultures overlap here — underground club culture and open-source/Linux culture. Both are anti-corporate, craft-obsessed, and suspicious of polish that hides emptiness. Both respect tools that are *serious* about what they do.
- **Ritual:** DJs "prepare" their music before a gig — analyzing tracks, setting cue points, organizing sets. Wrekker turns this ritual into a first-class workflow (see WREKKED, below). Preparation at home; execution in the booth.

---

## 3. Who is it for?

**Primary:** performing DJs who play electronic music and want to do more than blend — they want live remixing, stem manipulation, controlled chaos. Comfortable with technology, likely already frustrated with the limits or the pricing of mainstream DJ software.

**Secondary:** the Linux audio community — musicians and tinkerers who have long lacked a serious, modern DJ tool on their platform. For them, Wrekker existing *at all* is the headline.

These users don't respond to corporate gloss. They respond to evidence of obsession: precision, density of capability, and a tool that clearly comes from inside the culture.

---

## 4. What makes Wrekker special (the selling points)

These are the product truths the identity should radiate:

1. **Wreck the track.** Four-stem live separation on both decks. Kill the vocal, keep the drums; steal the bassline from one song and lay it under another's vocal. This is the core act, and the brand's namesake.

2. **The software reads music the way DJs do.** Wrekker doesn't just detect beats — it understands **phrases** (the 8/16/32-bar blocks dance music is built from) and syncs two songs at the *musical* level, not just the metronome level. Mixes land where a trained DJ's instinct would put them.

3. **It tells you when to strike.** Wrekker analyzes each track ahead of time and plants markers: *here's the drop, here's your mix-out point, here's the moment the vocal disappears and you can do something bold.* Three languages of markers — big structure, stem opportunities, phrase navigation — give the DJ a map of the future.

4. **Stem Horizon — seeing what's coming.** A compact display shows the *upcoming* activity of each stem, bars ahead: the vocals are about to come back, the bass drops out in four bars. The DJ plays the future, not just the present.

5. **Prepared and instant.** The **WREKKED** workflow converts a song into a `.wrk` file — a fully pre-analyzed performance package (audio, stems, waveform, beat grid, markers, artwork). Prepared tracks load nearly instantly. The metaphor: ammunition, prepped and racked before the show.

6. **Verified by hand.** In **WREKKER LAB**, the DJ can inspect and correct everything the automatic analysis produced, and stamp a track *manually verified*. Trust is a feature: nothing between the DJ and a clean mix at 2 a.m.

7. **Works offline, works anywhere.** The prepared library is self-contained — even if the network drive with the original music is gone, the show goes on.

8. **Real hardware.** Native support for the Pioneer DDJ-FLX4 controller (the most popular entry-pro controller) on Linux, where such support is otherwise rare to nonexistent.

9. **Free, open, yours.** GPL-licensed, no subscription, no account, no cloud lock-in. The audio engine is engineered for professional-grade low latency — open source with no compromise on performance.

**One-line essence:** *Wrekker gives DJs X-ray vision and a scalpel — see inside the music, cut where it counts.*

---

## 5. The naming system

The vocabulary is already established in the product and should be treated as brand language:

| Term | What it means | Register |
|---|---|---|
| **Wrekker** | The application. Always capitalized "Wrekker". | The name. Aggressive wordplay on "wrecker", tech-flavored double-K. |
| **WREKKED** | The preparation workflow and the prepared-music browser. A track that's been processed is "wrekked". | Always ALL-CAPS in the UI. Past tense of the brand verb. |
| **`.wrk`** | The file format for a prepared track. | Lowercase, monospace feel. The "ammunition" artifact. |
| **WREKKER LAB** | The workshop where DJs verify and correct a track's analysis. | ALL-CAPS. Clinical, precise space — the workbench, not the stage. |
| **WREKK** | The family of live-performance moves and effects on stems (e.g. "WREKK FX", "WREKK markers"). | ALL-CAPS. The act itself, weaponized. |
| **Stem Horizon** | The looking-into-the-future stem display. | Title case. The one poetic name in the system — keep it. |
| **Deck A / Deck B** | The two turntables. | Industry-standard. |
| **P / W / G** | The three marker languages: Primary (structure), WREKK (stem opportunities), Guide (phrases). | Single-letter badges in the UI. |

Note the pattern: the brand family is built on one root (**WREKK-**) declined like a verb — *Wrekker* (the actor), *WREKKED* (the state), *WREKK* (the act), *.wrk* (the artifact). This is a genuinely strong naming asset; the visual identity can lean on it.

---

## 6. What exists visually today

The current look is developer-built but internally consistent — treat it as an honest starting point, not a finished identity.

**The UI is very dark.** Near-black layered surfaces (`#080808` → `#161616`), thin dark borders, restrained light-grey text. Small, bold, letter-spaced ALL-CAPS labels everywhere. Utilitarian, dense, cockpit-like.

**There is an existing logo:** an amber (`#ffb000`) angular glyph, drawn in Inkscape, used as the app icon. Functional, but it has never had professional attention — **redesigning or refining the mark is in scope.**

**Functional color already carries meaning** (users have learned these; changing them has real cost):

| Role | Color | Meaning |
|---|---|---|
| Accent / brand | `#ffb000` amber | Highlights, active states, the logo, "attention here" |
| Deck A | `#00d4ff` electric cyan | Everything belonging to the left deck |
| Deck B | `#ff1f5a` hot pink | Everything belonging to the right deck |
| Vocals stem | `#ff6b6b` coral | The voice layer |
| Drums stem | `#4ecdc4` teal | The percussion layer |
| Bass stem | `#ffe66d` yellow | The low end |
| Other stem | `#a29bfe` violet | Synths, melodies, everything else |
| OK / warn / error | `#00e87a` / `#ffa726` / `#ff4444` | Status language |

**Typography today** is Inter/Roboto system fallback — i.e., unchosen. A deliberate typographic voice (especially for the wordmark and ALL-CAPS feature names) is one of the biggest opportunities.

**Waveforms are the landscape.** The most-looked-at pixels in the app are spectral waveforms: amplitude mountains tinted by frequency (warm amber lows, green mids, violet highs) with beat ticks and colored markers. Any brand art that echoes waveform/stem-layer imagery will feel native.

---

## 7. Personality

If Wrekker were a character: **a demolition engineer with perfect pitch.** Destruction, but surgical. It should feel:

- **Precise, not sterile** — the precision of a good tool, with grease on it
- **Aggressive, not edgy-for-edgy's-sake** — the aggression is in what the DJ *does*, the interface stays calm
- **Underground, not amateur** — at home on a techno flyer, engineered like an instrument
- **Open, not hobbyist** — free software that never apologizes for being free

Anti-references: consumer-DJ neon party gloss; corporate SaaS friendliness (rounded pastel blobs, mascots); retro-vinyl nostalgia (Wrekker is forward-looking); generic "AI product" purple gradients.

---

## 8. What we need from you

In rough priority order:

1. **Logo & mark** — refine or redesign the glyph; it must survive at 16 px (system tray) up to 1080 px, on near-black, in monochrome, and as a favicon.
2. **Wordmark & typography** — a distinctive treatment for "Wrekker" and rules for the ALL-CAPS sub-brands (WREKKED, WREKKER LAB, WREKK); a recommended UI/branding typeface pairing (must be freely licensable — see constraints).
3. **App icon set** — Linux: 256×256 PNG + scalable SVG; Windows: `.ico`. Dark-dock and light-dock friendly.
4. **Brand palette guidance** — how the brand accent lives alongside the *functional* colors above (which are effectively fixed); guidance for marketing surfaces (website, social) vs. the in-app UI.
5. **Launch collateral** — GitHub social-preview card, README header image, first-run wizard artwork, and a screenshot-framing style for release announcements.
6. **Optional but welcome** — a simple brand sheet (logo rules, spacing, misuse) so future contributors keep it consistent.

---

## 9. Constraints & practical notes

- **Dark stays.** The product is used in dark rooms; the UI will remain dark-first. Marketing surfaces can explore, but the app itself won't go light.
- **Functional colors are load-bearing.** Deck, stem, and status colors are a learned language inside the app. The brand accent (currently amber) is the flexible piece; propose changes there first.
- **Licensing:** Wrekker is GPL open source. All identity assets and fonts must be under licenses compatible with free redistribution (e.g. OFL fonts). No stock with restrictive terms.
- **Naming is settled.** "Wrekker" (capital W, double K), the WREKK- family, and `.wrk` are fixed. The identity expresses them; it doesn't rename them.
- **Audience reads authenticity.** Screenshots in collateral should show *real* use — real waveforms, real track metadata — never obviously fake content.
- **Current version:** 0.1.0-beta. First public release is imminent; the identity will debut with it.

---

## 10. Open questions for kickoff

1. Should the amber accent survive as *the* brand color, or is the brand color yours to propose (with amber as incumbent)?
2. One mark for everything, or a small system (Wrekker mark + derived badges for WREKKED / LAB)?
3. How literal should the identity be about the "wreck" metaphor vs. the "surgical/precision" side?
4. Wordmark only, glyph only, or lockup — what leads on the app icon?

*Contact the maintainer for screenshots, the current SVG logo, and a live demo of the app.*
