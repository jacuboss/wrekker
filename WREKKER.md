# Wrekker — Referencia técnica completa

> Software de DJ profesional de código abierto para Linux.  
> Stack: Python · PyQt6 · Rust (CPAL/PyO3) · HTDemucs · librosa · SQLite

---

## Índice

1. [Arquitectura general](#1-arquitectura-general)
2. [Capa de audio — Engine Rust](#2-capa-de-audio--engine-rust)
3. [Capa de control — Transport](#3-capa-de-control--transport)
4. [Modelos de datos — Deck](#4-modelos-de-datos--deck)
5. [Ecualización — EQ](#5-ecualización--eq)
6. [Medición en tiempo real — Metering](#6-medición-en-tiempo-real--metering)
7. [Separación de stems](#7-separación-de-stems)
8. [Biblioteca de música](#8-biblioteca-de-música)
9. [Formato .wrk — Pistas preparadas](#9-formato-wrk--pistas-preparadas)
10. [Interfaz gráfica — UI](#10-interfaz-gráfica--ui)
11. [Hardware — Pioneer DDJ-FLX4](#11-hardware--pioneer-ddj-flx4)
12. [Flujo de datos completo](#12-flujo-de-datos-completo)
13. [Seguridad entre hilos](#13-seguridad-entre-hilos)
14. [Rendimiento](#14-rendimiento)
15. [Cambios recientes — WREKK, sync y FLX4](#15-cambios-recientes--wrekk-sync-y-flx4)
16. [Sección WREKKED — browser de pistas preparadas](#16-sección-wrekked--browser-de-pistas-preparadas)
17. [Fastload cache — carga instantánea de .wrk](#17-fastload-cache--carga-instantánea-de-wrk)
18. [Seguridad de audio durante la carga](#18-seguridad-de-audio-durante-la-carga)
19. [Library & WREKKED UX upgrade — metadata rica y sets editables](#19-library--wrekked-ux-upgrade--metadata-rica-y-sets-editables)
20. [WREKKED Management UI — biblioteca local de performance](#20-wrekked-management-ui--biblioteca-local-de-performance)
21. [WREKKED como browser único — Library integrada](#21-wrekked-como-browser-único--library-integrada)
22. [PrepareDialog — pause, cancel y cierre cooperativo](#22-preparedialog--pause-cancel-y-cierre-cooperativo)
23. [Correcciones de integridad de Library y WREKKED](#23-correcciones-de-integridad-de-library-y-wrekked)
24. [Beatmatching state of the art — Beat This, Rubber Band, PhaseSync y PhraseLock](#24-beatmatching-state-of-the-art--beat-this-rubber-band-phasesync-y-phraselock)
25. [Detalles de implementación adicionales](#25-detalles-de-implementación-adicionales)

---

## 1. Arquitectura general

Wrekker separa responsabilidades en cinco capas verticales independientes:

```
┌──────────────────────────────────────────────┐
│  UI  (PyQt6 — hilo principal)                │
│  MainWindow · DeckWidget · LibraryWidget     │
│  PrepareDialog                               │
└─────────────────┬────────────────────────────┘
                  │  señales Qt + llamadas directas
┌─────────────────▼────────────────────────────┐
│  CONTROL  (Transport — Python)               │
│  Estado de decks · Carga · Sync/PLL · Stems  │
│  PreparedDB (carga .wrk prioritaria)         │
└─────────────────┬────────────────────────────┘
                  │  PyO3 bindings
┌─────────────────▼────────────────────────────┐
│  ENGINE  (Rust — wrekker_engine)             │
│  Callback CPAL · EQ · Crossfader · Meters    │
│  AtomicF32/AtomicBool — sin GIL en hot path  │
└─────────────────┬────────────────────────────┘
                  │  hilos daemon
┌─────────────────▼────────────────────────────┐
│  WORKERS  (StemWorker · analysis · library)  │
│  HTDemucs · librosa · scanner · cache        │
│  PrepareWorker (produce archivos .wrk)       │
└──────────────────────────────────────────────┘
                  │
┌─────────────────▼────────────────────────────┐
│  BIBLIOTECA PREPARADA                        │
│  PreparedDB (SQLite) · .wrk files (ZIP)      │
│  ~/.local/share/wrekker/prepared/            │
└──────────────────────────────────────────────┘
```

**Principios de diseño:**

| Principio | Aplicación |
|-----------|-----------|
| Estado inmutable | `DeckState` es un dataclass frozen; se reemplaza atómicamente |
| Sin GIL en el callback | El hilo CPAL es 100% Rust; Python nunca toca el hot path |
| Sin allocaciones en el callback | Buffers pre-asignados en `AudioBuffers`; el callback no llama a `malloc` |
| Estado compartido lock-free | `AtomicF32`, `AtomicBool`, `AtomicU64` entre Python y Rust |
| Prioridad en stems | Cola de prioridad; el deck activo va primero |
| Waveform incremental | Peaks → Beats → Stem energy, cada fase agrega datos |
| .wrk como fuente de verdad | El archivo .wrk es la única store persistente; StemCache es temporal |

---

## 2. Capa de audio — Engine Rust

### `NativeEngine` (`wrekker/engine_rs/src/lib.rs`)

Motor de audio implementado en Rust, compilado como extensión Python con **PyO3** y **maturin**.  
El callback de audio corre en un hilo **CPAL** separado. El GIL de Python nunca se adquiere en el hot path.

#### Compilación

```bash
cd wrekker/engine_rs && maturin develop --release
```

#### Inicialización

```python
from wrekker.core.engine_v2 import AudioEngine
engine = AudioEngine(sr=44100, blocksize=256)
engine.start()
```

- **`blocksize=256`** — 5.8 ms de presupuesto por callback. Seguro porque el callback no toca el GIL.

#### Estado compartido lock-free (`shared.rs`)

Toda la comunicación entre Python y el hilo CPAL usa tipos atómicos:

```rust
pub struct DeckShared {
    // Control (Python escribe, Rust lee)
    playing:       AtomicBool,
    pending_seek:  AtomicI64,      // -1 = sin seek; ≥ 0 = frame destino
    stem_targets:  [AtomicF32; 4], // gains de stems (vocals/drums/bass/other)
    loop_active:   AtomicBool,
    loop_start:    AtomicU64,      // frame de inicio de loop
    loop_end:      AtomicU64,      // frame de fin de loop (exclusivo)
    playback_rate: AtomicF32,      // 1.0 = normal, 1.05 = +5% tempo (vinyl-style)
    eq_low_db / eq_mid_db / eq_high_db: AtomicF32,

    // Salida (Rust escribe; Python lee a 60 Hz, ~45 Hz o ~10 Hz según ruta UI)
    position:        AtomicU64,    // frame actual (entero)
    peak_l / peak_r: AtomicF32,   // nivel de pico L/R con decay
    clip_l / clip_r: AtomicBool,  // latch de clipping
    lufs_momentary:  AtomicF32,
    lufs_shortterm:  AtomicF32,
    spectrum:       [AtomicF32; 16],
    stem_peaks:     [AtomicF32; 4],

    // Buffer de audio en vivo (para osciloscopios)
    live_buf: Mutex<Vec<f32>>,     // 1024 frames, interleaved stereo

    // Carga de buffers
    buffer_epoch: AtomicU64,
    buffer:       RwLock<Option<Arc<AudioBuffers>>>,
}
```

#### Ciclo del callback CPAL (`audio.rs` → `lib.rs`)

```
Por cada bloque de 256 frames:

1. Detectar nuevo buffer (buffer_epoch cambió) → actualizar DeckAudioState
2. Aplicar seek pendiente (pending_seek ≥ 0)
3. Si parado: decaer peaks (×0.85) y retornar

Para cada frame de salida:
    a. Interpolar la muestra con Hermite cúbico (pos_f fraccional → hermite_frame)
    b. Para cada stem activo: acumular en buf × gain (con smoother)
       - Si WREKK FX está activo y el deck tiene stems, aplicar DSP stem-aware
         antes de sumar el stem al deck.
    c. Avanzar pos_f += playback_rate
    d. Si loop activo y pos_f ≥ loop_end: wrap → loop_start + overshoot % span

Post-render:
4. EQ de 3 bandas (Rust, biquad IIR) sobre buf_a y buf_b
4b. FX normal (`FxProcessor`) sobre el deck completo, solo cuando el banco
    activo es NORMAL.
5. update_live_buf() → ring-shift en live_buf (try_lock, no bloquea)
6. Crossfader de igual potencia: cos(x)·A + sin(x)·B
7. Master gain
8. Peak detection + latch de clipping
9. LUFS K-weighted (integrador de 256 frames, ventanas 400ms/3s)
10. Spectrum Goertzel (acumula 1024 frames, dispara al completarse)
11. Publicar posición y métricas via atómicos
```

#### Interpolación Hermite cúbica (`hermite_frame` en `audio.rs`)

Se usa **Catmull-Rom** (4 puntos), con continuidad C¹ en los derivados. Reemplazó a `lerp_frame` para reducir aliasing durante scratch lento (rate < 0.7×).

```rust
#[inline(always)]
fn hermite_frame(data: &[f32], frame_i: usize, frac: f32, n_frames: usize) -> (f32, f32) {
    // f0..f3: vecinos con clamping en los bordes
    let f0 = frame_i.saturating_sub(1);
    let f1 = frame_i;
    let f2 = (frame_i + 1).min(n_frames - 1);
    let f3 = (frame_i + 2).min(n_frames - 1);
    let t = frac; let t2 = t*t; let t3 = t2*t;
    // Bases de Hermite
    let h00 =  2.0*t3 - 3.0*t2 + 1.0;
    let h10 =      t3 - 2.0*t2 + t;
    let h01 = -2.0*t3 + 3.0*t2;
    let h11 =      t3 -      t2;
    // Tangentes Catmull-Rom: m1 = 0.5*(p2-p0), m2 = 0.5*(p3-p1)
    let interp = |ch: usize| -> f32 {
        let p0 = sample(f0,ch); let p1 = sample(f1,ch);
        let p2 = sample(f2,ch); let p3 = sample(f3,ch);
        let m1 = 0.5*(p2-p0);  let m2 = 0.5*(p3-p1);
        h00*p1 + h10*m1 + h01*p2 + h11*m2
    };
    (interp(0), interp(1))
}
```

Costo: ~12 MACs/frame (vs ~4 para interpolación lineal). Neutral a unity rate; mejora perceptible con scratch lento.

#### Scratch y Nudge (`audio.rs` — `ScratchShared`, `ScratchEngine`, `NudgeEngine`)

El jog wheel del FLX4 controla el scratch y el nudge a través de dos motores independientes
en Rust que modifican el `playback_rate` del deck.

**`ScratchShared`** — atómicos de comunicación Python → CPAL:

```rust
pub struct ScratchShared {
    pending_ticks: AtomicI32,  // ticks acumulados del encoder (positivo/negativo)
    bypass_stop:   AtomicBool, // si True, ignorar el estado "parado" durante el scratch
}
```

**`ScratchEngine`** — motor de scratch tipo vinilo:

- **Resolución:** 720 ticks/revolución del encoder; 33.33 RPM equivale a una vuelta completa por beat a 128 BPM
- **Conversión:** cada tick → `delta_rate = ticks / (720 * 33.33/60 * sr / blocksize)`
- **Release exponencial:** cuando se suelta el jog (`bypass_stop = false`), el rate decae suavemente hacia 0 (o hacia `playback_rate` si estaba reproduciendo) con una constante de tiempo de ~80 ms
- **`bypass_stop`:** mientras el jog está tocado (`JOG_TOUCH` pressed), se ignora el flag `playing=false` del deck → el scratch suena aunque el deck esté pausado

**`NudgeEngine`** — nudge suave para tempo matching:

- Desplaza el `playback_rate` proporcionalmente al eje del encoder externo (rim/lateral)
- Filtro paso-bajo de un polo (τ=60 ms) para suavizar los cambios bruscos de velocidad del encoder
- Se acumula en `pending_ticks`; el callback CPAL drena los ticks en cada bloque
- Inactividad de 200 ms → rate vuelve a 1.0 (o al sync rate si sync activo)

**API Python:**

| Método | Acción |
|--------|--------|
| `scratch_enable(deck_id)` | Activa modo scratch (`bypass_stop=True`); el jog controla el rate directamente |
| `scratch_disable(deck_id)` | Desactiva scratch; inicia release exponencial |
| `scratch_tick(deck_id, delta)` | Envía `delta` ticks al `ScratchShared.pending_ticks` (AtomicI32 add) |
| `set_nudge(deck_id, delta)` | Envía ticks de nudge al `NudgeEngine`; usado para el eje lateral del jog |

---

#### `_DeckProxy` — Interfaz Python (`engine_v2.py`)

Python accede al estado del deck a través de `_DeckProxy`, que habla con `NativeEngine` vía PyO3:

```python
proxy.position_s      # lee AtomicU64 desde Rust
proxy.waveform        # Python-side; no toca el hilo de audio
proxy.pitch_factor    # float en Python; fuente de verdad para bpm_live display
```

#### API completa del engine

**Transporte:**

| Método | Acción |
|--------|--------|
| `play(deck_id)` | Inicia reproducción |
| `pause(deck_id)` | Pausa |
| `seek(deck_id, pos_s)` | Seek atómico (pending_seek ← frame) |
| `set_playback_rate(deck_id, rate)` | Cambia tempo+pitch (vinyl-style); range 0.5–2.0 |
| `get_playback_rate(deck_id)` | Lee tasa actual |
| `set_loop(deck_id, active, start_s, end_s)` | Define región de loop |
| `set_cue_point(deck_id, pos_s)` | Marca cue |

**Scratch / Nudge:**

| Método | Acción |
|--------|--------|
| `scratch_enable(deck_id)` | Activa scratch (`bypass_stop=True`); el jog controla el rate |
| `scratch_disable(deck_id)` | Desactiva scratch; inicia release exponencial |
| `scratch_tick(deck_id, delta)` | Envía `delta` ticks de encoder al motor de scratch (AtomicI32) |
| `set_nudge(deck_id, delta)` | Nudge suave; one-pole LP τ=60 ms; timeout 200 ms → rate=1.0 |

**Mezcla:**

| Método | Rango | Efecto |
|--------|-------|--------|
| `set_crossfader(value)` | 0.0–1.0 | 0=solo A, 0.5=igual, 1=solo B |
| `set_master_gain(gain)` | 0.0–2.0 | Ganancia maestra |
| `set_channel_volume(deck_id, vol)` | 0.0–1.0 | Channel fader lineal (1.0 = unity) |
| `set_pregain(deck_id, gain)` | 0.0–4.0 | Trim/pregain (1.0 = unity, 2.0 = +6 dBFS); sigue disponible por API/UI |
| `set_channel_filter(deck_id, value)` | −1.0–+1.0 | Filtro bipolar de canal; desde hardware se omite cuando WREKK activo para preservar/restaurar el filtro normal |
| `set_stem_gain(deck_id, stem, gain)` | 0.0–2.0 | Fader manual de stem; WREKK macro se aplica encima |
| `set_stem_gain_from_hardware(deck_id, stem, gain)` | 0.0–2.0 | Alias de `set_stem_gain`, llamado desde FLX4 |

**EQ (Rust, post-mix):**

| Método | Banda | Tipo |
|--------|-------|------|
| `set_eq_low(deck_id, db)` | Low-shelf 200 Hz | ±12 dB |
| `set_eq_mid(deck_id, db)` | Peaking 1 kHz | ±12 dB |
| `set_eq_high(deck_id, db)` | High-shelf 8 kHz | ±12 dB |

**Medición:**

| Método | Retorna |
|--------|---------|
| `get_lufs_momentary/shortterm(deck_id)` | dBFS (400ms / 3s) |
| `get_spectrum(deck_id)` | 16 valores dBFS (Goertzel) |
| `get_peak_levels(deck_id)` | `(peak_l, peak_r)` lineal 0–1 |
| `get_master_peak()` | `(peak_l, peak_r)` del bus master |
| `get_clip_flags(deck_id)` | `(clip_l, clip_r)` bool latched |
| `get_phase_correlation()` | float –1.0 a +1.0 |
| `get_stem_peak(deck_id, idx)` | Pico por stem (0–4) con decay ×0.90 |
| `get_live_audio(deck_id)` | Vec\<f32\> — 1024 frames stereo interleaved |
| `get_master_live_audio()` | Vec\<f32\> — 1024 frames stereo del bus master |

---

## 3. Capa de control — Transport

### `Transport` (`wrekker/core/transport.py`)

API pública para todas las operaciones de DJ. La UI y el hardware solo hablan con `Transport`.

```python
Transport(engine, analyzer, prepared_db=None)
```

Si se pasa `prepared_db`, `_load_worker` consulta la base de datos de pistas preparadas antes de lanzar el análisis completo.

#### Carga de pista (`load_track`)

La carga es completamente asíncrona. El hilo principal no se bloquea.

```
1. load_track() (hilo principal):
   - Cancela job de stems anterior
   - Limpia beatgrid + sync state del deck
   - Lanza _load_worker

2. _load_worker (hilo daemon):
   0. Si prepared_db disponible:
      rec = prepared_db.find_wrk(path)
      Si rec.wrk_ready y .wrk existe y rec.is_current(path):
          → _load_from_wrk(rec)  ← omite los pasos a–g
          → retornar (sin HTDemucs, sin librosa)
      Si rec existe pero obsoleto:
          → prepared_db.mark_outdated(path)  ← continúa con carga normal

   a. load_audio(path) → ffmpeg o soundfile
   b. _read_track_meta(path) → artist/title/bpm/artwork vía mutagen
      (prioridad: artist → albumartist → album_artist → performer → composer)
   c. engine.load_track(deck_id, audio) → reproducción inmediata del original
   d. _compute_waveform(audio, sr) → WaveformData con peaks + colores espectrales
   e. waveform_seq++ → la UI pinta la waveform
   f. Lanza _analysis_worker (hilo daemon)
   g. Lanza StemAnalyzer.analyze() (StemWorker)

3. _analysis_worker (hilo daemon):
   a. _detect_bpm_beats() → BPM corregido (80-185) + posiciones de beats
      - Analiza hasta 3 secciones (inicio/mitad/final)
      - Corrección half/double-time: ajusta al rango DJ 80-185 BPM
      - Usa metadata BPM como hint para resolver ambigüedades
   b. BeatGrid(bpm, first_beat_s, confidence, source) → guarda en DeckState
   c. _detect_key() → Krumhansl-Schmuckler → HarmonicKey
   d. Actualiza waveform.beats → waveform_seq++

4. StemWorker → on_complete:
   a. engine.update_stems() → Rust activa mezcla por stems
   b. _stem_waveform_worker:
      - _compute_stem_energy(stems) → (N, 4) float32
      - waveform.stem_energy = resultado (preserva beats anteriores)
      - waveform_seq++
```

#### `_load_from_wrk` (carga desde .wrk preparado)

Si la pista tiene un `.wrk` válido y actualizado, se omite todo el análisis:

```
_load_from_wrk(rec: PreparedRecord):
   1. load_wrk(rec.wrk_path) → PreparedTrack
   2. engine.load_track(deck_id, pt.audio, pt.sr) → reproducción inmediata
   3. Construir WaveformData desde pt.waveform_peaks/colors + beatgrid.beats + stem_energy
   4. Construir BeatGrid desde pt.beatgrid dict
   5. Construir TrackInfo con pt.title/artist/artwork_data
   6. Si pt.stems disponibles: engine.load_stems() + StemResult inmediato
   7. waveform_seq++ → UI pinta en < 500 ms (sin HTDemucs, sin librosa)
```

**Nota crítica:** Al parchear `waveform.beats`, siempre se preserva `waveform.stem_energy` existente (y viceversa). Evita la race condition donde el parche de beats borraba el overlay de stems cuando la caché de stems era rápida.

#### Controles DJ

**Pitch / BPM:**

```python
set_pitch(deck_id, pitch_pct)   # ±16 semitonos → set_playback_rate(2^(pct/12))
# Desactiva sync en el follower si estaba activo
```

**Cue (estilo Pioneer):**

```
Mientras PLAYING → pausa + guarda cue point en posición actual
Mientras PAUSED  → salta al cue point + play (modo preview)
cue_release()    → si está en preview, vuelve al cue point + pausa
```

**Loop:**

| Método | Acción |
|--------|--------|
| `loop_in(deck_id)` | Marca inicio en posición actual |
| `loop_out(deck_id)` | Marca fin + activa loop |
| `loop_toggle(deck_id)` | Activa/desactiva con los puntos actuales |
| `loop_set_bars(deck_id, bars)` | Loop de N compases (1/2, 1, 2, 4, 8, 16) |

El loop se cumple **dentro del callback Rust** por frame, incluyendo loops más cortos que el bloque CPAL.

**Stems:**

| Método | Acción |
|--------|--------|
| `set_stem_gain(deck_id, stem, gain)` | Fader 0.0–2.0 |
| `mute_stem(deck_id, stem, muted)` | Silencia preservando nivel del fader |
| `solo_stem(deck_id, stem, solo)` | Solo activo (silencia los otros) |

---

### Beatmatching profesional — BeatGrid, PhaseSync y PhraseLock

#### `BeatGrid` (en `deck.py`)

Ancla la estructura rítmica de una pista cargada. Desde schema v2, el beatgrid
guarda no solo BPM y primer beat, sino beats explícitos, downbeats, frases,
swing y flags de confianza:

```python
BeatGrid(
    bpm             = 128.0,
    first_beat_s    = 0.43,
    confidence      = 0.94,
    beats           = (...),       # timestamps en segundos
    downbeats       = (...),
    phrase_markers  = (...),       # PhraseMark(position_sec, phrase_length, energy)
    swing_factor    = 0.12,
    beat_period_ms  = 468.75,
    schema_version  = 2,
    analysis_model  = "beat_this_v1",
    low_confidence  = False,
)

# Métodos:
grid.phase_at(pos_s)                       # → float [0.0, 1.0)
grid.snap_to_phase(target_phase, near_pos) # → float posición más cercana con esa fase
grid.local_bpm_at(pos_s)                   # → BPM local usando spacing de beats
```

El análisis offline se hace durante WREKKED con `BeatTracker`
(`wrekker/analysis/beat_tracker.py`), wrapper de Beat This! en CPU. Si un track
tiene swing o tempo variable, Wrekker usa los beats reales en vez de asumir una
grilla rígida.

#### Flujo de sync (`sync(deck_id)`)

```
1. Si ya estaba sincronizado → unsync() y salir (toggle)

2. Determinar master:
   - Si no hay master: el otro deck se convierte en master automáticamente
   - El master nunca cambia al presionar SYNC en el follower

3. Calcular sync_rate:
   sync_rate = master_bpm_live / follower_native_bpm   (clamp 0.5–2.0)
   set_playback_rate(follower, sync_rate)

4. Phrase snap (si ambos tienen BeatGrid):
   target = PhraseLockSync.snap_slave_to_phrase(master, follower)
   engine.seek(follower, target)

5. Phase snap fino:
   phase_err = master_phase - follower_phase            (normalizado a ±0.5)
   Si |phase_err| > 0.01 beats:
       target = follower_grid.snap_to_phase(master_phase, follower_pos)
       engine.seek(follower, target)

6. Activar _FollowerSync(master_id, base_rate, pll=_SyncPLL())
7. DeckState: sync_enabled=True, sync_phase_error=phase_err
```

#### PLL continuo (`NativePhaseSync`, Rust)

La clase Python `_SyncPLL` es una fachada. Cuando `wrekker_engine.so` está
disponible, la corrección vive en `NativePhaseSync`, expuesto desde
`wrekker/engine_rs/src/phase_sync.rs`.

```python
# Para cada follower activo:

# 1. Rastrear cambios de BPM del master (master movió pitch fader)
ideal_rate = master_bpm_live / follower_native_bpm
if |ideal_rate - base_rate| > 1e-4:
    base_rate = ideal_rate

# 2. Si ambos están reproduciendo:
phase_err = master_phase - follower_phase   # normalizado a (-0.5, 0.5]
correction = PLL.update(phase_err, dt, master_bpm, follower_native)
new_rate = base_rate × (1 + correction)
set_playback_rate(follower, new_rate)
```

`PhaseSync` usa:

- `kp` proporcional.
- `dead_zone_beats` típico de 0.02 beats.
- `max_correction_rate` en semitonos/segundo.
- `is_locked()` para LED/UI.
- `phase_error_ms()` para diagnóstico.
- `snap_to_grid()` para snap inmediato.
- `pull_in(beats_to_converge)` para entrada suave.

**Estabilidad del display de BPM (`bpm_live`):**

El campo `bpm_live` del follower usa `fs.base_rate` (tasa nominal) en lugar de `new_rate`
(que incluye la corrección oscilatoria del PLL), para que el display de la UI no fluctúe:

```python
bpm_live = follower_grid.bpm * fs.base_rate   # estable en pantalla
# new_rate incluye ±4% de corrección PLL → ±6 BPM de oscilación visible si se usara
```

La corrección se aplica al engine de audio pero no se refleja en el display.

#### Play con alineación de fase (`_phase_align_resume`)

Cuando el deck follower estaba pausado y el usuario presiona Play:

```
master_phase = master_grid.phase_at(master_pos)
target = follower_grid.snap_to_phase(master_phase, follower_pos)
engine.seek(follower, target)   # ≤ half-beat de desplazamiento
pll.reset()
set_playback_rate(follower, base_rate)
```

Resultado: el follower reanuda en el beat correcto, aunque el master haya seguido reproduciendo varios compases.

#### PhraseLockSync

`wrekker/sync/phrase_sync.py` calcula la relación musical entre master y slave.
No procesa audio; solo lee `DeckState` y `BeatGrid`.

Funciones principales:

| Método | Uso |
|--------|-----|
| `compute_phrase_offset(master, slave)` | offset en segundos para alinear próxima frase |
| `next_phrase_boundary(deck)` | próximo inicio de frase |
| `phrase_progress_beats(deck, pos)` | beat actual dentro de la frase |
| `phrase_length_beats(deck, pos)` | 32 beats para 8 compases, 64 para 16 |
| `phrase_progress_fraction(deck, pos)` | progreso 0.0–1.0 para UI |
| `is_phrase_locked_at(master, master_pos, slave, slave_pos)` | estado de lock musical |
| `snap_slave_to_phrase(master, slave)` | posición del slave con el mismo índice de frase |

La UI muestra un phrase meter por deck en el path de 60 Hz:

| Color | Estado |
|-------|--------|
| Verde | phrase-locked |
| Amarillo | beat-locked, frase desalineada |
| Rojo | sin sync |
| Gris | sin beatgrid |

#### Métodos de sync adicionales

| Método | Descripción |
|--------|-------------|
| `sync(deck_id)` | Toggle sync: activa como follower o desactiva |
| `unsync(deck_id)` | Desactiva sync explícitamente; el playback rate queda como está |
| `set_sync_master(deck_id)` | Designa un deck como master; el otro pierde flag `sync_master` |
| `get_sync_master()` | Retorna el deck_id del master actual, o None |
| `get_state(deck_id)` | Retorna `DeckState` actual del deck (thread-safe) |
| `get_all_states()` | Dict `{deck_id: DeckState}` para ambos decks |
| `get_harmonic_compatibility()` | Float 0.0–1.0 o None — compatibilidad armónica entre los dos decks |

#### `set_sync_master(deck_id)`

Designa el deck de referencia. El master no cambia su tempo; el follower es el que se ajusta. La UI muestra el botón **M** iluminado en el master.

#### PLL — ganancia integral y absorción de drift

`_SyncPLL` tiene dos ganancias:

| Constante | Valor | Rol |
|-----------|-------|-----|
| `Kp = 0.180` | proporcional | corrección inmediata de error de fase |
| `Ki = 0.0120` | integral | corrección acumulada de drift BPM entre grids |
| `MAX_CORRECTION = 0.060` | — | ±6 % de tasa instantánea máxima |
| `WINDUP_LIMIT = 1.0` | beat·s | anti-windup del integrador |

Cuando el error de fase acumulado supera `0.002` en un tick, `_FollowerSync.absorb_correction()` traslada una fracción de la corrección PLL al `nominal_rate` del follower. Esto evita que el integrador del PLL absorba indefinidamente errores de análisis BPM, que son constantes.

```python
def absorb_correction(self, correction: float, dt: float) -> None:
    self.rate_bias += correction * dt * 0.06
    self.rate_bias = max(-0.04, min(0.04, self.rate_bias))
    self._refresh_base_rate()  # base_rate = nominal_rate × (1 + rate_bias)
```

Así el display `bpm_live` refleja el `base_rate` (estable) y el engine aplica `new_rate = base_rate × (1 + correction)` (que oscila ±6 % durante el chase, invisible en la UI).

#### FX BPM tracking en `tick_sync`

Al final de cada tick de 60 Hz, `tick_sync()` llama `engine.fx_set_bpm(bpm)` con
el BPM actual del target de FX normal y `engine.wrekk_fx_set_bpm(bpm)` con el
BPM del target de WREKK FX. Esto mantiene los efectos sincronizados a tempo en
tiempo real cuando el master mueve el pitch fader.

```python
fx_target = self._fx_state.target
for did in decks_for_target(fx_target):
    bpm = st.bpm_live or st.track.bpm
    if bpm: engine.fx_set_bpm(bpm); break
```

#### Monitor CUE / PFL — `MonitorCueState`

Dataclass frozen que registra el estado de escucha en auriculares:

```python
MonitorCueState(
    cue_deck_a:      bool  = False,  # PFL deck A activo
    cue_deck_b:      bool  = False,  # PFL deck B activo
    cue_master:      bool  = False,  # fuerza auriculares a bus master
    headphone_mix:   float = 0.0,    # 0.0 = CUE puro, 1.0 = master puro
    headphone_level: float = 1.0,    # ganancia 0.0–2.0 (1.0 = unity)
)
```

**API de Transport para monitor:**

| Método | Descripción |
|--------|-------------|
| `toggle_monitor_cue("A")` | Alterna PFL deck A → llama `engine.set_headphone_cue("A", bool)` |
| `toggle_monitor_cue("B")` | Alterna PFL deck B → llama `engine.set_headphone_cue("B", bool)` |
| `toggle_monitor_cue("master")` | Alterna MST CUE → llama `engine.set_headphone_cue_master(bool)` |
| `set_monitor_cue(target, enabled)` | Versión explícita (no toggle) |
| `get_monitor_state()` | → `MonitorCueState` actual |
| `set_headphone_mix(value)` | 0.0–1.0; llama `engine.set_headphone_mix()` |
| `set_headphone_level(value)` | 0.0–2.0; llama `engine.set_headphone_level()` |

`MonitorCueState` controla routing real cuando el engine Rust detecta una salida
FLX4/multicanal compatible. El engine construye un stream CUE separado, toma los
buses live pre-fader de Deck A/B más el bus master, aplica `headphone_mix` y
`headphone_level`, y escribe la mezcla de auriculares en los canales 2-3. Si no
hay dispositivo multicanal válido, el estado sigue sincronizado entre UI,
Transport y hardware, pero no hay bus físico de auriculares disponible.

#### Comportamiento ante pitch manual en el follower

Si el usuario mueve el pitch fader de un deck que está sincronizado como follower, el sync se **desactiva automáticamente** (igual que Serato).

#### Análisis armónico

```python
get_harmonic_compatibility() → float 0.0–1.0
```

Compara claves en la rueda Camelot:

| Score | Compatibilidad |
|-------|---------------|
| 1.00 | Misma clave y modo |
| 0.85 | ±1 número, mismo modo |
| 0.75 | Mismo número, modo opuesto |
| 0.50 | ±2 números, mismo modo |
| 0.25 | ±3 números, mismo modo |
| 0.00 | Tritono o sin relación |

---

### Waveform — construcción incremental

```python
WaveformData:
    peaks:       (N,) float32          # envolvente de amplitud 0–1 (2000 cols)
    colors:      (N, 3) uint8          # color espectral RGB por columna
    beats:       tuple[float]          # posiciones de beats en segundos
    stem_energy: (N, 4) float32        # energía media por stem por columna (None hasta Demucs)
    stem_horizon: dict | None          # actividad bar-synchronous por stem para Stem Horizon
    zoom_peaks:  (M,) float32 | None   # peaks alta resolución para zoom (256 samples/col)
    zoom_colors: (M, 3) uint8 | None   # colores espectrales para zoom
    zoom_chunk:  int                   # muestras por columna de zoom (256 → ~172 cols/s @ 44100)
```

`zoom_peaks` y `zoom_colors` se computan en `_compute_zoom_peaks()` durante la carga del audio (en el mismo hilo que los peaks generales). Con `zoom_chunk = 256`, la resolución es ~172 columnas/segundo a 44100 Hz, lo que permite ver con exactitud la forma de onda dentro de una ventana de ~4 s en el zoom widget.

Fases:

```
Al cargar la pista    → peaks + colors   (batch FFT, ~200 ms)
Al detectar BPM       → beats + BeatGrid (~5 s, librosa)
Al separar stems      → stem_energy      (~1–40 s, HTDemucs)
Al tener grid+energía → stem_horizon    (bar activity, sin rerun stems)
Desde .wrk preparado  → todo de una vez  (< 500 ms, sin análisis)
```

Cada fase incrementa `waveform_seq`. La UI detecta el cambio en el tick y repinta.

**Colores espectrales:**

```
bass  (< 300 Hz)   → ámbar   (#ffb347)
mid   (300–3 kHz)  → verde   (#2ecc71)
high  (> 3 kHz)    → violeta (#7b68ee)
```

---

## 4. Modelos de datos — Deck

### `deck.py` (`wrekker/core/deck.py`)

Todos los modelos son dataclasses frozen. Cuando algo cambia se crea un nuevo objeto.

#### `HarmonicKey`

```python
HarmonicKey(number=8, mode="A")  # → "8A" → Am
```

#### `TrackInfo`

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `path` | Path | Ruta al archivo |
| `title` | str | Título (de tags o nombre de archivo) |
| `artist` | str | Artista (prioridad: artist → albumartist → performer → composer) |
| `duration_s` | float | Duración en segundos |
| `sample_rate` | int | Sample rate del audio cargado en el engine (Hz) |
| `channels` | int | Número de canales (normalmente 2 = estéreo) |
| `bpm` | float \| None | BPM de tags (hint inicial) |
| `key` | HarmonicKey \| None | Clave detectada por Krumhansl-Schmuckler |
| `file_hash` | str | SHA256(ruta + mtime) — ID para cache de stems |
| `artwork_data` | bytes \| None | Portada del álbum (JPEG/PNG, de tags) |

#### `BeatGrid`

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `bpm` | float | BPM corregido al rango DJ (80–185) — BPM global/primario |
| `first_beat_s` | float | Timestamp del primer beat detectado |
| `confidence` | float | 0.0–1.0 según cantidad de beats consistentes |
| `source` | str | `"analyzed"` \| `"metadata"` \| `"manual"` \| `"imported"` |
| `user_adjusted` | bool | True si el usuario ha corregido la grid manualmente |
| `beats` | tuple[float] | Timestamps explícitos de beats en segundos; `()` = grid constante |
| `downbeats` | tuple[float] | Timestamps de tiempos fuertes (downbeats) |
| `phrase_markers` | tuple[PhraseMark] | Marcadores de inicio de frase 8/16 compases |
| `swing_factor` | float | Desviación media respecto a grid rígido (0.0 = recto, ~1.0 = swing 2:1) |
| `beat_period_ms` | float | Periodo promedio de beat en milisegundos |
| `schema_version` | int | Versión del schema del beatgrid (1 = legado, 2 = Beat This!) |
| `analysis_model` | str | `"beat_this_v1"` \| `""` |
| `low_confidence` | bool | True cuando `confidence < 0.6` |
| `dynamic_tempo` | bool | True cuando el tempo varía más del 5% a lo largo del track |
| `bpm_min` | float \| None | BPM mínimo local (percentil 5); None si tempo constante |
| `bpm_max` | float \| None | BPM máximo local (percentil 95); None si tempo constante |

**Propiedad `bpm_display`:** devuelve `"min–max"` para grids con tempo dinámico, o `"128.0"` para grids constantes.

Métodos:

| Método | Descripción |
|--------|-------------|
| `phase_at(pos_s)` | Fase en [0.0, 1.0) en la posición dada |
| `snap_to_phase(target_phase, near_pos)` | Posición más cercana con esa fase |
| `local_bpm_at(pos_s)` | BPM local usando spacing real entre beats (bisect) |

#### `DeckState`

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `id` | DeckID | "A" o "B" |
| `status` | DeckStatus | EMPTY / LOADING / READY / PLAYING / PAUSED |
| `track` | TrackInfo \| None | Pista cargada |
| `position_s` | float | Posición en segundos |
| `pitch_pct` | float | Pitch en semitonos (±16) |
| `bpm_live` | float | BPM × playback_rate (lo que suena) |
| `stems` | dict[str, StemState] | Estado de los 4 stems |
| `stems_status` | str | "none" / "queued" / "analyzing" / "ready" |
| `loop` | LoopState \| None | Región de loop activa |
| `cue_points` | tuple[CuePoint] | Hot cues guardados |
| `sync_enabled` | bool | True si este deck es follower sincronizado |
| `sync_master` | bool | True si este deck es el master de tempo |
| `beatgrid` | BeatGrid \| None | Grid de beats (disponible tras análisis) |
| `sync_phase_error` | float \| None | Error de fase en beats (±0.5); None si no sincronizado |
| `metrics` | DeckMetrics \| None | Última snapshot de LUFS + espectro |

**Propiedad `dynamic_tempo`:** delega a `beatgrid.dynamic_tempo`; True cuando el track cargado tiene tempo variable. Usado por la UI para mostrar el rango "min–max" en lugar de un BPM fijo.

#### `StemState`

```python
StemState(gain=1.0, muted=False, solo=False, lufs=None, spectral=None)
# effective_gain → 0.0 si muted, de lo contrario gain
```

#### `DeckMetrics`

```python
DeckMetrics(
    lufs       = LoudnessMeasure(momentary=-18.5, short_term=-20.1, true_peak=-1.2),
    spectral   = SpectralBands(sub=-45, bass=-30, mids=-25, highs=-38),
    spectrum   = (-42.0, -38.5, ..., -55.0),   # 16 valores Goertzel en dBFS
    phase_corr = 0.87,
)
```

---

## 5. Ecualización — EQ

El EQ es **Rust puro**, implementado como filtros biquad IIR en `audio.rs`.

| Banda | Tipo | Frecuencia | Rango |
|-------|------|-----------|-------|
| LOW | Low-shelf | 200 Hz | ±12 dB |
| MID | Peaking | 1 kHz (Q=0.9) | ±12 dB |
| HIGH | High-shelf | 8 kHz | ±12 dB |

- Coeficientes recalculados vía `eq_dirty: AtomicBool` cuando Python cambia un valor  
- Procesado en el hilo CPAL directamente sobre el buffer de salida, post-mix de stems  
- Las bandas en 0 dB omiten el filtrado completamente (no-op sin degradación numérica)

```python
engine.set_eq("A", "low",  float("-inf"))  # kill de bajos
engine.set_eq("A", "mid",  +3.0)           # +3 dB en medios
engine.set_eq("A", "high",  0.0)           # agudos sin cambio
```

---

## 6. Medición en tiempo real — Metering

Toda la medición ocurre en el hilo CPAL (Rust), nunca toca el GIL.

### LUFS K-weighted (`lufs.rs` — `KWeightedLUFS`)

Cumple ITU-R BS.1770-4.

**Filtro K-weighting (dos etapas):**
1. High-shelf +4 dB a ~1.7 kHz
2. High-pass RLB a ~38 Hz

**Integración:**
- **Momentánea (400 ms):** ring-buffer de bloques de 256 frames; suma de cuadrados K-weighted
- **Short-term (3 s):** ventana deslizante de 128 bloques de 256 frames
- Publicados cada callback vía `lufs_momentary` y `lufs_shortterm` (AtomicF32)

### Espectro Goertzel (`audio.rs`)

16 bandas log-espaciadas (31 Hz – 18 kHz). Acumula 1024 muestras mono, luego calcula la potencia de Goertzel para cada banda:

```
bandas: 31, 50, 80, 125, 200, 315, 500, 800, 1250, 2000, 3150, 5000, 8000, 10000, 14000, 18000 Hz
```

Más eficiente que FFT para pocas frecuencias fijas. El resultado se publica como `spectrum[16]` (AtomicF32, dBFS).

### Peak tracking y clipping

- `peak_l / peak_r` con decay ×0.85 por callback (se aplica incluso cuando el deck está parado)
- `clip_l / clip_r` latch booleano; se limpia con `reset_clip()`
- `stem_peaks[4]` con decay ×0.90 por callback

### Osciloscopio en vivo (`live_buf`)

Cada deck y el bus master tienen un ring-buffer de 1024 frames estéreo (interleaved f32):

```rust
pub live_buf: Mutex<Vec<f32>>   // 2048 floats = 1024 frames × 2 canales
```

El hilo CPAL escribe con `try_lock()` (skip si contended), nunca bloquea. Python lee con `lock()` en la ruta visual de ~45 Hz. La UI renderiza tres osciloscopios en `MasterWidget`.

### Correlación de fase

Calculada en Rust post-crossfader sobre el bus master. Rango –1.0 (oposición) a +1.0 (en fase). Publicada vía `phase_corr: AtomicF32` en `MixerShared`.

---

## 7. Separación de stems

Wrekker usa [Facebook HTDemucs](https://github.com/facebookresearch/demucs) para separar cada pista en cuatro stems:

| Stem | Frecuencias predominantes | Color en waveform |
|------|--------------------------|-------------------|
| `vocals` | 80 Hz – 8 kHz | Rosa #ff6b81 |
| `drums` | Transientes broadband | Amarillo #ffd32a |
| `bass` | Sub-bajos 20–300 Hz | Rojo-naranja #ff4757 |
| `other` | Instrumentos armónicos | Verde #2ed573 |

### `StemAnalyzer` (`wrekker/stems/analyzer.py`)

```python
analyzer.analyze(
    track_path  = path,
    priority    = Priority.HIGH,
    on_complete = lambda result: engine.update_stems(deck_id, result),
    on_progress = lambda frac:  update_progress_bar(frac),
)
```

1. Si está en caché → `on_complete` síncronamente (< 1 s)
2. Si no → encola en `StemWorker` con la prioridad dada

### `StemWorker`

| Prioridad | Uso |
|-----------|-----|
| URGENT (0) | Deck activo, sin caché |
| HIGH (1) | Deck secundario |
| NORMAL (2) | Siguiente en cola |
| LOW (3) | Resto de la cola |

### `HTDemucsModel`

- Lazy-loading (~80 MB, primera llamada)
- CUDA si disponible; fallback a CPU
- CPU Ryzen 7 4700U: ~0.45× realtime

### `StemCache` — caché temporal

```
/tmp/wrekker/stems/{sha256(ruta+mtime)}/
    ├── vocals.wav
    ├── drums.wav
    ├── bass.wav
    ├── other.wav
    └── meta.json
```

**Importante:** `StemCache` es **temporal y no persistente**. Vive en `/tmp` (tmpfs), puede llenarse (~400 MB/track como WAV FLOAT sin comprimir). Su propósito es acelerar recargas dentro de la misma sesión. El store persistente autorizado son los archivos `.wrk` (ver sección 9). Si `stem_cache.save()` falla (error de disco/espacio), se ignora silenciosamente — el `.wrk` ya tiene los stems comprimidos en FLAC.

Invalidación automática si el archivo cambia (mtime diferente → hash diferente).

---

## 8. Biblioteca de música

### `LibraryDB` (`wrekker/library/database.py`)

SQLite con FTS5 (Full Text Search).

```sql
CREATE TABLE tracks (
    id TEXT PRIMARY KEY,   -- SHA256(ruta + mtime)
    path TEXT UNIQUE,
    title, artist, album, genre TEXT,
    duration REAL, bpm REAL,
    key_num INTEGER, key_mode TEXT,
    year INTEGER, bitrate INTEGER, sr INTEGER, channels INTEGER,
    added_at REAL
);
CREATE VIRTUAL TABLE tracks_fts USING fts5(title, artist, album, content=tracks);
```

**Métodos principales:**

| Método | Descripción |
|--------|-------------|
| `upsert(track)` | Inserta o actualiza |
| `search(query)` | FTS5: texto libre + filtros BPM/clave |
| `tracks_in_folder(path)` | Todos los tracks bajo una carpeta |
| `remove_missing(root)` | Elimina tracks cuyo archivo ya no existe |

### `LibraryScanner`

- Usa **mutagen** para metadata (ID3, Vorbis, MP4, FLAC)
- Formatos: `.mp3 .flac .ogg .opus .wav .aiff .m4a .mp4 .wma .alac`

### `PreparedDB` (`wrekker/library/prepared_db.py`)

SQLite WAL en `~/.local/share/wrekker/prepared.db`. Registra el estado de preparación de cada pista.

```sql
CREATE TABLE prepared_tracks (
    source_path     TEXT PRIMARY KEY,
    wrk_path        TEXT,
    wrk_id          TEXT,       -- SHA256(abs_path) → usado como PK en el DB
    title           TEXT,
    artist          TEXT,
    album           TEXT,
    duration_s      REAL,
    bpm             REAL,
    key             TEXT,
    wrk_ready       INTEGER DEFAULT 0,
    stems_ready     INTEGER DEFAULT 0,
    wrk_version     INTEGER DEFAULT 0,
    analysis_status TEXT DEFAULT 'pending',   -- pending/processing/ready/failed/outdated
    stem_status     TEXT DEFAULT 'pending',
    source_hash     TEXT,       -- SHA256(abs_path + mtime_ns)
    source_mtime_ns INTEGER,
    error_msg       TEXT,
    created_at      REAL,
    updated_at      REAL,
    -- añadidas por migración ALTER TABLE (backward-safe):
    bpm_metadata    REAL,       -- BPM de los tags ID3/Vorbis (fuente original)
    bpm_confidence  REAL,       -- confianza del análisis BPM (0.0–1.0)
    key_confidence  REAL        -- confianza del análisis de clave (0.0–1.0)
);

CREATE TABLE library_roots (
    path        TEXT PRIMARY KEY,
    label       TEXT,
    last_scanned REAL
);
```

**Migraciones** — `_MIGRATIONS` en `_init_schema()`, cada una envuelta en try/except para idempotencia:

```python
_MIGRATIONS = [
    "ALTER TABLE prepared_tracks ADD COLUMN bpm_metadata   REAL",
    "ALTER TABLE prepared_tracks ADD COLUMN bpm_confidence REAL",
    "ALTER TABLE prepared_tracks ADD COLUMN key_confidence REAL",
    "ALTER TABLE prepared_sets   ADD COLUMN description    TEXT",
    "ALTER TABLE prepared_sets   ADD COLUMN total_duration_s REAL NOT NULL DEFAULT 0.0",
]
```

**`PreparedRecord`** — dataclass con:
- `is_current(path)`: devuelve True si el hash SHA256(ruta+mtime) coincide con `source_hash` guardado
- Indica si el `.wrk` sigue siendo válido para la versión actual del archivo fuente

**`PreparedSet`** — dataclass:
```python
PreparedSet:
    id:               int
    name:             str
    source_root_label: Optional[str]
    source_root_path:  Optional[str]
    track_count:      int
    notes:            Optional[str]
    description:      Optional[str]   # campo editable libre
    total_duration_s: float           # suma de duration_s de las pistas
    created_at:       str
    updated_at:       str
```

**Métodos principales de `PreparedRecord`:**

| Método | Descripción |
|--------|-------------|
| `find_wrk(path)` | Busca registro por ruta; retorna `PreparedRecord` o None |
| `get_batch_status(paths)` | Dict `path → PreparedRecord` para un lote |
| `upsert_record(...)` | Inserta o actualiza todos los campos |
| `mark_processing(path)` | Cambia status a PROCESSING |
| `mark_failed(path, error)` | Cambia status a FAILED, guarda traceback |
| `mark_outdated(path)` | Marca como obsoleto (fuente cambió) |
| `mark_stems_ready(path)` | Actualiza stem_status → READY |
| `get_roots()` | Lista de `LibraryRoot` registrados |
| `add_root(path, label)` | Agrega carpeta fuente |
| `remove_root(path)` | Elimina carpeta fuente |
| `update_root_scanned(path)` | Actualiza timestamp de último escaneo |

**Métodos de WREKKED sets (CRUD manual — fuera del scanner):**

| Método | Descripción |
|--------|-------------|
| `create_wrekked_set(name)` | Crea set vacío; retorna `set_id` o `None` si el nombre existe |
| `rename_wrekked_set(set_id, new_name)` | Renombra; retorna `False` si colisión |
| `update_set_description(set_id, description)` | Actualiza campo de descripción libre |
| `reorder_set_track(set_id, wrk_id, new_pos)` | Reordena pista; actualiza `position` de todas |
| `update_set_total_duration(set_id)` | Recalcula y guarda `total_duration_s` |
| `update_set_track_count(set_id)` | Recalcula y guarda `track_count` |
| `get_set_summary(set_id)` | Dict: `{total, wrk_count, stems_count, offline_count, broken_count, total_duration_s}` |

---

## 9. Formato .wrk — Pistas preparadas

### Descripción

Un archivo `.wrk` es un contenedor **ZIP** que encapsula todo lo necesario para cargar una pista instantáneamente: audio completo, stems separados, waveform precalculada, beatgrid, cues, artwork y metadata.

El `.wrk` es el **store persistente autorizado**. La `StemCache` en `/tmp` es efímera.

### Estructura del contenedor ZIP

```
track.wrk  (ZIP)
├── manifest.json          ← metadata + índice de contenidos
├── audio/
│   ├── full.flac          ← mezcla completa, stereo, FLAC lossless
│   ├── vocals.flac        ← stem separado por HTDemucs
│   ├── drums.flac
│   ├── bass.flac
│   └── other.flac
├── analysis/
│   ├── waveform_peaks.f32
│   ├── waveform_colors.bin
│   ├── stem_energy.f32
│   ├── stem_horizon.json ← actividad bar-synchronous ACTIVE para STEM HORIZON
│   ├── beatgrid_auto.json ← snapshot AUTO preservado por WREKKER LAB
│   ├── beatgrid.json      ← análisis ACTIVE usado en performance
│   ├── markers_auto.json  ← Auto Markers originales preservados
│   ├── markers.json       ← markers ACTIVE
│   ├── corrections.json   ← estado LAB, revision, flags de verificación
│   └── changelog.json     ← historial auditable de commits LAB
├── dj/
│   ├── cues.json          ← hot cues preparados
│   └── loops.json         ← loops guardados
└── artwork/
    └── cover.jpg|png|webp
```

Los `.wrk` legacy pueden no tener `*_auto.json`, `corrections.json` ni
`changelog.json`. Al abrirlos en WREKKER LAB se migran de forma segura: el
beatgrid/markers existentes se copian como capa AUTO inmutable y los archivos
`beatgrid.json`/`markers.json` quedan como capa ACTIVE.

### `manifest.json`

```json
{
  "wrk_version": 1,
  "wrk_id": "sha256hex",       // SHA256(abs_source_path) — usado como PK en DB
  "source_path": "/music/...",
  "source_hash": "sha256hex",  // SHA256(abs_path + mtime_ns) — detecta cambios
  "title": "Track Name",
  "artist": "Artist",
  "album": "",
  "duration_s": 245.3,
  "bpm": 128.0,
  "key": "8A",
  "has_stems": true,
  "has_artwork": true,
  "created_at": 1747500000.0
}
```

### Rutas en disco

Los `.wrk` se guardan bajo `~/.local/share/wrekker/prepared/tracks/` con organización legible:

```
prepared/
└── tracks/
    ├── TRVL 9/                    ← nombre de la carpeta fuente (sanitizado)
    │   ├── 01 Artist - Title.wrk  ← nombre del archivo fuente (sin extensión)
    │   ├── 02 Artist - Title.wrk
    │   └── index.json             ← mapa filename → source_path + hash
    ├── House Mix/
    │   └── ...
    └── ...
```

El `wrk_id` (SHA256 de la ruta absoluta) se usa únicamente como clave en la base de datos, no como nombre de archivo.

#### Detección de colisiones

Si dos archivos de carpetas distintas tienen el mismo nombre de stem:

1. `_index_owner(wrk_dir, stem)` lee `index.json` para ver qué `source_path` usa ese nombre
2. Si es diferente al archivo actual → appends ` (2)`, ` (3)`, etc.

#### `index.json` por carpeta

```json
{
  "01 Artist - Title": {
    "source_path": "/music/TRVL 9/01 Artist - Title.mp3",
    "source_hash": "abc123..."
  }
}
```

Se escribe atómicamente: `index.tmp` → `os.replace()`.

### API Python (`wrekker/formats/wrk.py`)

| Función | Descripción |
|---------|-------------|
| `create_wrk(source_path, output_path, audio, sr, stems, ...)` | Escribe un `.wrk` atómicamente (`.tmp` → `os.replace`) |
| `load_wrk(path)` → `PreparedTrack` | Lee y descomprime un `.wrk` |
| `validate_wrk(path)` → `ValidationReport` | Verifica integridad (manifest + archivos esperados) |
| `inspect_wrk(path)` → dict | Retorna manifest sin cargar audio (para la UI) |
| `wrk_id_for(source_path)` → str | SHA256(abs_path) — ID para la DB |
| `wrk_path_for(source_path, root)` → Path | Ruta legible con detección de colisiones |
| `update_folder_index(wrk_path, source_path, src_hash)` | Actualiza `index.json` atómicamente |

#### `PreparedTrack` dataclass

```python
PreparedTrack:
    audio:          np.ndarray   # (samples, 2) float32 — mezcla completa
    sr:             int
    stems:          dict | None  # {"vocals": ndarray, ...} o None
    waveform_peaks: np.ndarray   # (N,) float32
    waveform_colors: np.ndarray  # (N, 3) uint8
    stem_energy:    np.ndarray | None  # (N, 4) float32
    beatgrid:       dict | None  # {"bpm":..., "beats":[...], ...}
    cues:           list
    loops:          list
    artwork_data:   bytes | None
    title:          str
    artist:         str
    album:          str
    duration_s:     float
    bpm:            float | None
    key:            str | None
```

### `PrepareWorker` (`wrekker/ui/workers/prepare_worker.py`)

QThread que procesa un lote de pistas en background:

**Señales:**

| Señal | Parámetros |
|-------|-----------|
| `track_started` | `(index: int, title: str, total: int)` |
| `track_progress` | `(index: int, phase: str, frac: float)` — phase: audio/analysis/stems/packing/fastload |
| `track_done` | `(index: int, title: str)` |
| `track_failed` | `(index: int, title: str, error: str)` |
| `track_skipped` | `(index: int, title: str)` |
| `all_done` | `(n_ok: int, n_failed: int, n_skipped: int)` |

**Control:**

```python
worker.pause()   # congela el loop (threading.Event)
worker.resume()  # despausa
worker.cancel()  # señala cancel_event + stem_cancel; despausa
```

**Fases de procesamiento por pista:**

```
0. Verificar PreparedDB: si rec.wrk_ready y .wrk existe y is_current → skip (emit track_skipped)
   Si obsoleto → mark_outdated y continuar

1. "audio"    → load_audio() via ffmpeg/soundfile
2. "analysis" → _detect_bpm_beats() + _detect_key() + _compute_waveform()
3. "stems"    → si stem_cache.load() → usar caché; si stem_model → HTDemucs.separate()
               → stem_cache.save() (no-fatal: falla silenciosamente si /tmp lleno)
4. "packing"  → create_wrk() atómico → validate_wrk() → update_folder_index()
5. "fastload" → FastloadCache().build() mix-only PCM16 (non-fatal; "Caching fastload" en UI)
   upsert_record() en PreparedDB con status=READY
```

Resultado de Phase 5: el primer load en vivo de cada pista preparada siempre usa la caché fastload — nunca hay decode FLAC durante una actuación.

En caso de error: imprime traceback completo a stdout (`[prepare] FAILED {title}:\n{tb}`), emite `track_failed`, llama `mark_failed()`.

---

## 10. Interfaz gráfica — UI

### `MainWindow` (`wrekker/ui/main_window.py`)

```
┌─────────────────────────────────────────┐
│  WREKKER                      HH:MM:SS  │
├──────────┬──────────────┬───────────────┤
│  DECK A  │    MASTER    │    DECK B     │
├──────────┴──────────────┴───────────────┤
│              BIBLIOTECA                 │
└─────────────────────────────────────────┘
```

```python
MainWindow(transport, engine, prepared_db=None, stem_model=None)
```

Conecta `library_widget.prepare_tracks` → `_on_prepare_tracks()`:

```python
def _on_prepare_tracks(self, tracks):
    root = Path.home() / ".local/share/wrekker/prepared"
    worker = PrepareWorker(tracks, self._prepared_db, root, self._stem_model)
    dialog = PrepareDialog(worker, [t.display_title for t in tracks], self)
    worker.finished.connect(lambda: self._library.refresh_statuses())
    worker.start()
    dialog.show()
```

**Bucle de actualización (16 ms / 60 Hz):**

El tick está dividido en tres cadencias para mantener suave el movimiento sin
meter trabajo pesado en cada frame:

```python
# Cada tick (60 Hz — path rápido):
dt = now - last_tick_t
transport.tick_sync(dt)          # PLL de sync (actualiza rates de followers)

state_a = transport.get_state("A")
state_b = transport.get_state("B")
pos_a   = engine.deck_a.position_s
pos_b   = engine.deck_b.position_s

# Waveform — solo si cambia waveform_seq
if get_waveform_seq("A") != _wf_seq_a:
    deck_a_widget.set_waveform(engine.get_waveform("A"))

# Cross-deck beat overlay (aislado en try/except para no bloquear update_state)
try:
    a_playing = state_a.status == DeckStatus.PLAYING
    b_playing = state_b.status == DeckStatus.PLAYING
    bg_b = state_b.beatgrid
    deck_a.set_other_deck_overlay(
        pos_s        = pos_b,
        beats        = bg_b.beats if bg_b else (),
        bpm          = state_b.bpm_live or (bg_b.bpm if bg_b else 0.0),
        first_beat_s = bg_b.first_beat_s if bg_b else 0.0,
        source_playing = b_playing,        # el overlay depende del deck fuente
    )
    # … idem para deck_b con datos de deck_a
except Exception:
    pass

deck_a_widget.update_state(state_a, pos_a, None, {})
deck_b_widget.update_state(state_b, pos_b, None, {})

# Path visual de medidores (~45 Hz):
if visual_meters:
    spectrum = engine.get_spectrum(...)
    master_widget.set_scopes(...)
    master_widget.set_deck_peaks(...)
    deck_a_widget.set_stem_peak_levels(...)

# Cada 6to tick (~10 Hz — path lento): métricas y LUFS
if _heavy_tick_n == 0:
    metrics_a = engine.get_deck_metrics("A")
    if metrics_a:                          # mantiene último valor si no hay datos
        _last_metrics_a = metrics_a
    deck_a_widget.update_state(state_a, pos_a, _last_metrics_a, stem_lufs_a)
```

**Motivo del split 60 Hz / 45 Hz / 10 Hz:** playheads, zoom waveform, beat overlays,
phrase meter y feedback de FX viven a 60 Hz para sentirse fluidos. Spectrum,
osciloscopios, deck peak meters, mini meters y stem peaks viven en una ruta visual
dedicada de ~45 Hz: siguen respondiendo rápido, pero no compiten con el scroll del
zoom waveform. Las métricas LUFS pesadas siguen a ~10 Hz, porque sus ventanas de
integración cambian mucho más lento.

---

### `LibraryWidget` (`wrekker/ui/widgets/library.py`)

#### Barra de herramientas (top bar)

```
[ + Folder ]  [ SCAN ]  [ PREPARE ]  [  Buscar...  ]
```

- **+ Folder**: `QFileDialog` → `prepared_db.add_root()` → escaneo automático
- **SCAN**: Reescanea la carpeta seleccionada en el árbol
- **PREPARE**: Verde (color `STATUS_OK`); prepara los tracks seleccionados, o todos los visibles si no hay selección; deshabilitado si no hay `prepared_db`

#### Panel izquierdo

```
── SOURCES ──
  ▸ /music/TRVL 9          ← raíces de biblioteca (color STATUS_OK)
  ▸ /music/House Mix

── FOLDERS ──
  ▸ Artists/
  ▸ ...
```

- Las raíces muestran menú contextual: **Prepare Set** / **Rescan** / **Remove**

#### Tabla de pistas — columnas

| Índice | Columna | Ancho | Datos |
|--------|---------|-------|-------|
| 0 | Title | stretch | título de la pista |
| 1 | Artist | stretch | artista |
| 2 | Album | 120 px | álbum |
| 3 | Duration | 60 px | `M:SS` |
| 4 | BPM | 70 px | delegate dual-color (ver abajo) |
| 5 | Key | 60 px | delegate con punto de compatibilidad |
| 6 | STATUS | 60 px | badge de color |

**Columna BPM — `_DualBPMDelegate`:**

Dibuja dos valores en la misma celda separados por `/`:
- BPM de **metadata** (ID3/Vorbis): `#3498db` (azul)
- BPM analizado por **Wrekker**: `#f39c12` (naranja)

```python
# Rol personalizado:
_BPM_DATA_ROLE = Qt.ItemDataRole.UserRole + 1
# model.data(index, _BPM_DATA_ROLE) retorna (meta_bpm, wrk_bpm) tuple
```

Si alguno es None → se muestra solo el disponible. Si ambos coinciden → solo el WRK BPM en naranja. Fondo de selección correcto vía `initStyleOption() + drawPrimitive(PE_PanelItemViewItem)`.

**Columna Key — `_KeyCompatDelegate`:**

Dibuja un punto de color (6px) a la izquierda + texto de clave:

| Color del punto | Compat score | Descripción |
|-----------------|-------------|-------------|
| `#2ecc71` verde | 0.85–1.0 | Misma tónica o ±1 Camelot |
| `#f39c12` amarillo | 0.50–0.85 | Compatible (relativa, ±2) |
| `#e74c3c` rojo | < 0.50 | Choca armónicamente |
| Gris | None | Sin referencia o clave desconocida |

```python
_COMPAT_ROLE = Qt.ItemDataRole.UserRole + 2
# model.data(index, _COMPAT_ROLE) retorna float | None
```

**Propagación de clave de referencia:**

`MainWindow._get_reference_key(state_a, state_b)` en el heavy tick (10 Hz):
1. Si hay sync master → usa su clave
2. Si algún deck está reproduciendo → usa su clave
3. Si algún deck tiene pista cargada → usa su clave

Si cambia respecto a la anterior: `self._library.set_reference_key(ref_key)` → `model.set_reference_key(key)` → emite `dataChanged` para toda la columna Key → los `_COMPAT_ROLE` se recalculan en el siguiente paint.

#### Columna STATUS (índice 6)

| Badge | Color | Significado |
|-------|-------|-------------|
| `WRK` | Verde | `.wrk` listo con stems |
| `STEMS` | Azul/cyan | `.wrk` listo pero sin stems |
| `PREP…` | Naranja | En proceso de preparación |
| `FAIL` | Rojo | Error en preparación |
| `OLD` | Gris | `.wrk` obsoleto (fuente cambió) |
| (vacío) | — | Sin preparar |

`refresh_statuses()` re-consulta `prepared_db.get_batch_status()` para el set visible.

#### Menú contextual de pista (clic derecho)

```
Load to Deck A
Load to Deck B
─────────────────────
Prepare
Rebuild .wrk
─────────────────────
Add to WREKKED Set ▶  [lista de sets existentes]
                      + New Set…
─────────────────────
Build fastload cache   (si no hay caché válida)
Rebuild fastload cache (si ya hay caché)
Delete fastload cache  (si hay caché)
─────────────────────
Reveal source in Files
Reveal .wrk in Files   (si wrk_path existe)
─────────────────────
Delete .wrk            (confirma antes)
```

`FastloadCache.is_valid()` se llama una vez al abrir el menú (no en cada paint).

---

### `PrepareDialog` (`wrekker/ui/widgets/prepare_dialog.py`)

Ventana no-modal que muestra el progreso de preparación del lote:

```
┌─ Preparing 12 tracks… ────────────────────────────┐
│ ████████████████░░░░░░░░  6 / 12                  │
├────────────────────────────────────────────────────┤
│ [WRK ] Artist - Title A         ████████ 100%     │
│ [ … ] Artist - Title B          ████░░░░  Stems   │
│ [SKIP] Artist - Title C         ░░░░░░░░           │
│ [FAIL] Artist - Title D         Error msg...      │
│ ...                                               │
├────────────────────────────────────────────────────┤
│                          [ Pause ]  [ Cancel ]    │
└────────────────────────────────────────────────────┘
```

- Fila `_TrackRow`: badge 80px | título (expandible) | barra de progreso 160px | fase 100px
- `set_failed()`: muestra primera línea del error inline en rojo; traceback completo como tooltip
- Al completar: encabezado cambia a `"Done — N prepared, M skipped, K failed"`; Pause deshabilitado; Cancel → Close

---

### `DeckWidget` (`wrekker/ui/widgets/deck.py`)

**Señales emitidas:**

| Señal | Cuándo |
|-------|--------|
| `play_pause` | Click en ▶/⏸ |
| `cue_pressed / cue_released` | Botón CUE (hold-to-preview) |
| `loop_in / loop_out / loop_toggle` | Botones de loop |
| `sync_pressed` | Click en SYNC (toggle: activa o desactiva sync) |
| `sync_master_pressed` | Click en M (designa master) |
| `seek` | Click/drag en la waveform |
| `stem_gain / stem_mute / stem_solo` | Controles de stem |

**Botón SYNC — color según fase:**

| Color | Condición |
|-------|-----------|
| Verde #2ecc71 | `sync_enabled` + `|phase_error| < 0.05` beats (locked) |
| Naranja #f39c12 | `sync_enabled` + `|phase_error| < 0.20` beats (correcting) |
| Rojo #e74c3c | `sync_enabled` + `|phase_error| ≥ 0.20` beats (large drift) |
| Gris (default) | `sync_enabled = False` |

**Artwork:** Thumbnail 48×48 px (JPEG/PNG de tags ID3/FLAC/MP4) en la fila de info de pista.

**Next Auto Marker:** debajo del overview waveform, cada deck muestra el siguiente
auto marker alcanzable con countdown por cada jerarquía. No hay un único
indicador global; se calculan tres próximos markers independientes:

```text
P  MIX OUT   0:32
W  DECON     0:16
G  PHRASE    5.3s
```

Las letras compactas son:

| Letra | Jerarquía | Función |
|-------|-----------|---------|
| `P` | Primary | Estructura grande de mezcla |
| `W` | WREKK | Anatomía stem-aware y oportunidades |
| `G` | Guide | Timing estructural |

Cada fila tiene su propio LED de confianza:

| Color | Confianza |
|-------|-----------|
| Verde | >=85% |
| Amarillo | 70%–<85% |
| Gris | Sin marker próximo |

El umbral mínimo de Auto Markers es 70%. `AutoMarkerDetector` no emite markers
por debajo de ese valor y los paths de carga/guardado (`.wrk`, fastload,
regeneración) también filtran markers <70%, incluyendo datos antiguos.

El tooltip conserva tipo, posición, confianza y razón del detector. Si hay loop
activo, el helper solo considera markers que el playhead puede alcanzar dentro
del loop; no anuncia un marker fuera del loop como si fuera el próximo evento.

Cada categoría se busca por separado después de la posición actual. Esto evita
que un PHRASE cercano o un evento W oculte el próximo evento primario.

**Jerarquía visual de Auto Markers:**

| Nivel | Tipos | Render |
|-------|-------|--------|
| Primary | DROP, MIX IN, MIX OUT, SWITCH | Colita inferior fina, color fuerte, sin label sobre waveform |
| WREKK Structural | VOCAL IN/OUT, BASS IN/OUT, KICK IN/OUT, TOP IN/OUT | Colita W, visible en LAB y en modo W expandido |
| WREKK Opportunity | GHOST, DECONSTRUCT, REBUILD | Colita W naranja, visible en live si supera umbral alto |
| Guide | PHRASE | Colita inferior muy corta y sutil |

Definición de producto: Primary identifica dónde cambia la estructura de mezcla.
WREKK identifica qué cambia dentro de los stems y cuándo eso produce una
oportunidad de manipulación. Guide conserva el contexto de frase.

`WREKK` genérico y `GHOST` ya no son Primary. Los tipos legacy `wrekk_top`,
`wrekk_rhythm`, `rhythm_in` y `drum_swap` se conservan para revisión/debug, pero
no se muestran como Primary normal. `GHOST` se representa como WREKK Opportunity.

El detector WREKK es rule-based, beat-sincrónico y persistente: compara ventanas
de 4+ compases alrededor de downbeats/frases usando `stem_energy` ya preparado,
sin rerun de stem separation. Cada W marker guarda `category=wrekk`, `family`
(`structural` u `opportunity`), `stem_targets`, confianza, evidencia y razón
legible para LAB. Las oportunidades usan umbral live más estricto que los eventos
estructurales para evitar saturación.

El botón `MKRS` usa `ESSENTIAL` por defecto y su menú contextual expone los modos
`OFF`, `PRIMARY`, `ESSENTIAL`, `PRIMARY + WREKK`, `FULL` y `DEBUG`. La waveform
no dibuja texto de marker por defecto para no tapar la forma de onda; el
significado específico vive en el tooltip y en WREKKER LAB.

### WREKK Stem Horizon

Stem Horizon es el componente compacto dentro del area `STEMS` de cada deck. Su
funcion no es reemplazar las lineas `P` / `W` / `G`, sino mostrar la anatomia
futura de `VOC`, `DRM`, `BSS` y `OTH` directamente encima de sus faders
respectivos.

Producto:

- Primary markers dicen cuando cambia la estructura grande de mezcla.
- Guide markers muestran contexto de frase.
- WREKK markers identifican eventos stem-aware y oportunidades.
- Stem Horizon muestra la forma interna futura del track junto a los controles
  de stems.

Datos:

- `wrekker.analysis.stem_horizon.generate_stem_horizon()` crea una linea de
  tiempo bar-synchronous a partir de `stem_energy`, beatgrid y frases.
- Cada stem guarda estados `0/1/2`: inactivo, presente o dominante.
- Las transiciones `in`, `out` y `shift` se conservan como evidencia ligera para
  el widget y para LAB.
- El resultado se persiste en `.wrk` como `analysis/stem_horizon.json` y en
  fastload como `stem_horizon.json`.
- No se rerun stem separation para Horizon; tracks legacy sin metadata siguen
  cargando y muestran un estado sutil de Horizon no generado.

UI live:

- `StemHorizonWidget` es QWidget. En live decks se instancia por stem dentro de
  cada `_StemRow`, encima del `QSlider` de ese stem, para distribuir el espacio
  vertical sin crear un bloque adicional sobre todas las filas.
- Modo `LED Blocks`: default recomendado; 8 compases futuros en bloques.
- Modo `Future Bars`: banda continua de actividad futura.
- Modo `Stem Waveforms`: mini-overview full-track por stem con playhead.
- Modo `Off`: colapsa la vista y deja el area STEMS como antes.
- El tick realtime solo actualiza posicion/cadencia; la geometria estructural se
  lee de datos cacheados.

SETTINGS expone el control en `Stems & WREKK -> Stem Horizon`: enabled, display
mode, rango 4/8/16/32 bars, countdown, flag W, niveles de dominancia, detalle e
intensidad. El default de perfil es `LED Blocks`, 8 bars, visible siempre para
`.wrk` compatibles.

WREKKER LAB muestra una vista read-only full-track de Stem Horizon en la pestaña
de markers para inspeccionar actividad y markers W relacionados. La correccion
manual de regiones de actividad queda preparada como siguiente paso; por ahora
la regeneracion ocurre en preparacion y en futuros flujos de regeneracion LAB.

---

### `ZoomWaveformWidget` — overlay de beats cruzados

Cada deck muestra una waveform zoomeada (ventana de ~4 s centrada en el playhead) con un
overlay semitransparente de los beats del **otro** deck. Permite ver a simple vista el
alineamiento de fases sin mirar displays numéricos.

**`set_other_deck(pos_s, beats, bpm, first_beat_s, own_playing)`:**

```python
def set_other_deck(self, pos_s, beats, bpm, first_beat_s, own_playing=True):
    # Solo avanza la posición de referencia si el deck VIEWER está reproduciendo.
    # En pausa, _other_pos_s queda congelado → el overlay no se desplaza.
    if own_playing:
        self._other_pos_s = pos_s
    # No borrar datos buenos con actualizaciones vacías (antes del análisis).
    if beats:
        self._other_beats   = beats
        self._other_first_s = first_beat_s
    if bpm > 0:
        self._other_bpm = bpm
    # Auto-habilitar en cuanto hay algo que mostrar.
    if not self._overlay_on and (self._other_beats or self._other_bpm > 0):
        self._overlay_on = True
```

**Renderizado del overlay:**

```python
# Para cada beat del otro deck dentro de la ventana visible:
bx = (beat_s - _other_pos_s + half_win) / window_s * w
# Línea vertical semitransparente (~40% alpha) en color del otro deck
```

Los beats se localizan con búsqueda binaria (`bisect`) sobre la tupla `_other_beats`, lo que
evita iterar todos los beats de la pista.

**Botón OVL:** toggle visible en el `DeckWidget`; habilita/deshabilita `_overlay_on`.

**Regla de congelación (`own_playing`):**
- Si el deck A (viewer) está **pausado** → `_other_pos_s` de su ZoomWaveformWidget no avanza
  → el overlay queda anclado en el instante de la pausa
- Si el deck B (fuente) está pausado pero A está reproduciendo → A sigue avanzando normalmente,
  el overlay en A congelaría la posición de B (beats de B siguen visibles en la última posición)
- El estado del deck fuente **no afecta** al overlay del viewer

---

### `PositionBarWidget` (`wrekker/ui/widgets/waveform.py`)

Clickeable y arrastrable para seek.

**Capas visuales (de abajo a arriba):**

```
┌─────────────────────────────────────────────────────┐
│  Vocal zone  (rosa 3px)   ← vocals > 35% del mix   │ ← top
│  Stem dominance strip (5px) — color del stem        │
├─────────────────────────────────────────────────────┤
│  Waveform con colores espectrales                   │
│  (played = color pleno, unplayed = 30% alpha)       │
├─────────────────────────────────────────────────────┤
│  Beat markers (5px blanco semitransparente)         │ ← bottom
├─────────────────────────────────────────────────────┤
│  Loop region overlay (naranja #ff9f43)              │
│  Marcadores IN/OUT: líneas 2px que SOBRESALEN       │
│  el borde inferior hacia la franja de timestamps.  │
│  Ticks horizontales (8px) arriba y abajo.           │
│  Triángulos en el top de cada marcador.             │
│  Label "LOOP" centrado cuando está activo.         │
│  Cue markers (triángulos de colores)                │
│  Playhead (línea blanca + triángulo)                │
└─────────────────────────────────────────────────────┘
  0:00                                          -3:45
```

**Overlay de stems en tiempo real:**

```python
# Vectorizado con numpy (~0.1 ms):
weighted   = stem_energy × stem_gains     # (N, 4)
total      = weighted.sum(axis=1)         # (N,)
dominant   = argmax(weighted, axis=1)     # (N,) → stem index 0–3
vocal_mask = weighted[:,0] / total > 0.35
```

---

### `MasterWidget` (`wrekker/ui/widgets/master.py`)

| Sección | Controles |
|---------|-----------|
| Osciloscopios | 3 paneles _LiveScopeWidget: Deck A / Master / Deck B |
| Fase | Barra de correlación + valor numérico |
| Compatibilidad armónica | Score 0.0–1.0 + detalle de claves |
| EQ Deck A | 3 faders: HIGH / MID / LOW |
| Medidores de volumen | Deck A \| Master \| Deck B (L+R + LED de clip) |
| EQ Deck B | 3 faders |
| Master volume | Slider 0–200% |
| Crossfader | A ← CENTER → B |

**Osciloscopios:** Los tres `_LiveScopeWidget` convierten el buffer estéreo a mono
y dibujan la forma de onda con QPainter sobre un panel oscuro con borde, centro
sutil y trazo de alto contraste. Deck A usa cyan/azul, Master blanco/gris y Deck B
magenta/rojo. Se actualizan en el path visual de ~45 Hz y normalizan amplitud por
percentil para que señales quietas sigan legibles sin que señales fuertes saturen
visualmente.

---

## 11. Hardware — Pioneer DDJ-FLX4

### `FLX4Driver` (`wrekker/hardware/flx4.py`)

Driver MIDI bidireccional. Dos hilos daemon:
- `_midi_reader_thread` — loop bloqueante mido, clasifica mensajes
- `_led_writer_thread` — drena `_led_queue`, envía SysEx/Note

#### Mapeo de controles

| Control | Tipo MIDI | Acción |
|---------|-----------|--------|
| Pitch fader | CC MSB+LSB | `transport.set_pitch()` ±16st |
| Play/Pause | Note | `transport.play/pause()` |
| CUE | Note press/release | `transport.cue() / cue_release()` |
| SYNC | Note | `transport.sync()` (toggle) |
| Loop IN/OUT/toggle | Note | `transport.loop_in/out/toggle()` |
| Jog wheel (top) | Note JOG_TOUCH + CC JOG_TOP | Scratch (ver abajo) |
| Jog wheel (rim) | CC JOG_RIM | Nudge (ver abajo) |
| EQ HIGH/MID/LOW | CC por deck | Normal: `transport.set_eq()`; WREKK: hardware controla vocals/drums/bass sin cambiar labels visuales |
| TRIM | CC por deck | Normal: `transport.set_pregain()`; WREKK: hardware controla `other` sin mover el slider visual de pregain |
| CFX/FILTER | 14-bit CC global ch. 7 | Normal: `set_channel_filter()`; WREKK: `set_wrekk_macro()` |
| SMART CFX / WREKK | Note 0x00 global ch. 7 | `transport.toggle_smart_cfx()` + LED on/off |
| BeatFX ON/OFF | Note BeatFX ch. A/B | `transport.set_fx_enabled()` + LED por deck target |
| BeatFX LEVEL | CC BeatFX ch. A/B | `set_fx_wet()` o `set_fx_depth()` con SHIFT |
| BeatFX BEAT LEFT/RIGHT | Note BeatFX ch. A/B | Tipo de FX anterior/siguiente |
| Crossfader | CC global | `transport.set_crossfader()` |
| Browse knob/press | CC/Note global | Navegacion/activacion en browser activo |
| LOAD A/B | Note global | Carga la seleccion del browser activo en Deck A/B |
| HEADPHONES CUE A/B + MST CUE | Note | PFL/MST CUE real cuando hay salida multicanal |
| HEADPHONES MIX/LEVEL | CC global | Mezcla CUE/master y ganancia de auriculares |
| VU meter deck | CC 0x02 deck ch. A/B | Peak meter del engine → LED meter del FLX4 |
| Pads STEMS mode | Note | Mute/solo de cada stem |

Controles FLX4 que siguen deliberadamente como no-op o pendientes: `PLAY+SHIFT`
reverse/censor, `SHIFT+SYNC` tempo range, `LOOP IN/OUT+SHIFT` ajuste fino de
loop, `SHIFT+HOT CUE pad` para borrar cues y `SHIFT+BROWSE` para zoom.

#### Jog wheel — scratch y nudge

**Scratch (superficie superior del jog):**

```
JOG_TOUCH Note ON  → transport.scratch_enable(deck_id)
                      (bypass_stop=True: el audio suena aunque el deck esté parado)
JOG_TOP   CC delta → transport.scratch_tick(deck_id, delta)
                      delta positivo = jog hacia adelante, negativo = atrás
JOG_TOUCH Note OFF → transport.scratch_disable(deck_id)
                      (inicia release exponencial ~80 ms hacia rate normal)
```

- Resolución: 720 ticks/vuelta completa del jog
- El callback CPAL drena `pending_ticks` cada bloque (256 frames) y ajusta `pos_f` directamente,
  igual que si el vinilo girara a la velocidad proporcional al número de ticks/s

**Nudge (eje exterior/rim del jog):**

```
JOG_RIM CC delta → transport.set_nudge(deck_id, delta)
                    one-pole LP τ=60 ms suaviza saltos bruscos
                    timeout 200 ms sin nuevos ticks → rate vuelve a 1.0 (o sync rate)
```

- Útil para empujar o frenar ligeramente el tempo sin entrar en modo scratch
- Si el deck está sincronizado (follower), el rate base es el de sync; el nudge lo desplaza temporalmente

---

## 12. Flujo de datos completo

```
Usuario / FLX4
     │
     ▼  señales Qt / MIDI
Transport (Python)  ──────────────────────────────────────┐
     │                                                     │  PyO3 calls
     │  load_track()                                       ▼
     │                                               NativeEngine (Rust)
     ├─ [si .wrk válido y actual]                   CPAL thread:
     │   _load_from_wrk()                             ├─ DeckAudioState.fill()
     │      ├─ load_wrk() → PreparedTrack             │    ├─ hermite_frame × playback_rate
     │      ├─ engine.load_track() → inmediato        │    ├─ stem gains (StemSmoother)
     │      ├─ WaveformData desde .wrk                │    ├─ loop wrap (per-sample)
     │      └─ BeatGrid + StemResult desde .wrk       │    └─ peak decay (always)
     │                                               ├─ EQ biquad (post-mix)
     └─ [si no .wrk] _load_worker (hilo daemon)      ├─ update_live_buf()
        ├─ load_audio()                              ├─ crossfader (equal power)
        ├─ _read_track_meta() → artist/artwork       ├─ master gain
        ├─ engine.load_track() → AudioBuffers        ├─ peak + clip detection
        ├─ _compute_waveform() → WaveformData        ├─ KWeightedLUFS (400ms/3s)
        └─ waveform_seq++                            └─ Goertzel spectrum (1024 frames)

_analysis_worker (daemon)
   ├─ BeatTracker / Beat This! → BeatGrid schema v2
   ├─ _detect_key() → HarmonicKey
   └─ waveform.beats = beatgrid.beats → waveform_seq++

StemWorker (daemon, priority queue)
   ├─ HTDemucs.separate()
   ├─ cache.save() [non-fatal]
   └─ on_complete()
       ├─ engine.load_stems()
       └─ _stem_waveform_worker()
           ├─ _compute_stem_energy(stems)
           └─ waveform.stem_energy → waveform_seq++

PrepareWorker (QThread, batch)
   └─ [por cada track]
       ├─ load_audio()
       ├─ BeatTracker / waveform / metadata
       ├─ HTDemucs.separate()
       ├─ create_wrk() → .wrk en disco
       └─ PreparedDB.upsert_record()

Transport.tick_sync (60 Hz)
   ├─ track master BPM live
   ├─ PhraseLockSync snap al activar SYNC
   ├─ NativePhaseSync phase error → rate correction
   └─ DeckState.sync_phase_error para UI

MainWindow tick 60 Hz (path rápido)
   ├─ transport.tick_sync(dt)
   ├─ lee DeckState + posición atómica
   ├─ lee waveform_seq → repinta waveform si cambió
   ├─ cross-deck overlay
   ├─ phrase meter
   ├─ faders/mixer feedback
   └─ update_realtime_state(state, pos)

MainWindow visual meters ~45 Hz
   ├─ spectrum
   ├─ osciloscopios A/Main/B
   ├─ deck peak meters
   └─ mini meters / stem peaks

MainWindow tick 10 Hz (cada 6 ticks)
   ├─ lee DeckMetrics (LUFS, phase corr)
   ├─ stem LUFS/proxies
   ├─ full update_state()
   └─ FLX4 sync LEDs / library compat dots
```

---

## 13. Seguridad entre hilos

| Componente | Hilo(s) accedentes | Mecanismo |
|------------|-------------------|-----------|
| `DeckAudioState.fill()` | Solo CPAL (Rust) | Sin locks; acceso exclusivo |
| `DeckShared atomics` | CPAL (W) + main vía PyO3 (R/W) | `AtomicF32/Bool/U64` (lock-free) |
| `AudioBuffers` | CPAL (R) + load_worker (W) | `RwLock<Option<Arc<AudioBuffers>>>` |
| `live_buf` | CPAL (W `try_lock`) + Python (R `lock`) | `Mutex<Vec<f32>>` |
| `DeckState` | Main + load/analysis workers | `threading.Lock` por deck en Transport |
| `_DeckProxy.waveform` | Workers (W) + main (R) | Asignación atómica de referencia Python |
| `LibraryDB` | Cualquiera | WAL mode SQLite; conexión nueva por llamada |
| `PreparedDB` | Main + PrepareWorker | WAL mode SQLite; thread-safe |
| `StemWorker._queue` | Cualquiera | `queue.PriorityQueue` (thread-safe) |
| `FLX4Driver._pad_mode` | MIDI reader + LED writer | `threading.Lock` |
| `PrepareWorker._cancel_event` | UI (W) + PrepareWorker (R) | `threading.Event` |

**GIL y el callback de audio:**

El hilo CPAL corre exclusivamente en Rust. El GIL de Python **nunca** se adquiere durante el procesado de audio. Toda la comunicación usa tipos atómicos de Rust (`atomic_float`, `std::sync::atomic`).

El intervalo de switch del GIL se configura a 1 ms para que los hilos workers (análisis, stems, UI) sean más responsivos:

```python
sys.setswitchinterval(0.001)
```

---

## 14. Rendimiento

| Operación | Tiempo típico | Hilo | Notas |
|-----------|--------------|------|-------|
| Callback CPAL (Rust) | < 0.5 ms | CPAL (realtime) | Presupuesto: 5.8 ms @ 256 frames, 44100 Hz |
| Interpolación Hermite | +8 MACs/frame vs lineal | CPAL | ~12 MACs total; imperceptible al presupuesto |
| Carga de MP3 5 MB | ~200 ms | daemon | ffmpeg → buffer en memoria |
| Beat tracking + clave | variable | daemon / PrepareWorker | Beat This! offline para `.wrk`; clave armónica fuera del callback |
| Separación HTDemucs | ~1–5 s (GPU) / ~10–40 s (CPU) | StemWorker | — |
| Carga desde caché | < 500 ms | daemon | WAV float32 desde disco |
| Carga desde .wrk | < 500 ms | daemon | FLAC → sin análisis, sin HTDemucs |
| Waveform (peaks + colores) | ~200 ms | daemon | Batch FFT de 2000 chunks |
| Energía de stems | ~100 ms | daemon | RMS por columna, numpy; no toca audio realtime |
| Preparación .wrk (CPU) | ~10–45 s/pista | PrepareWorker | audio + análisis + HTDemucs + ZIP |
| Preparación .wrk (GPU) | ~2–8 s/pista | PrepareWorker | HTDemucs CUDA |
| LUFS en Rust | ~0 ms extra | CPAL | Integrado en el callback |
| Goertzel spectrum | ~0.05 ms | CPAL | 16 bandas cada 1024 muestras |
| Tick de UI (path rápido) | < 5 ms | Main | 60 Hz: estado, waveform, overlay, phrase meter, FX feedback |
| Tick de UI (medidores visuales) | < 5 ms | Main | ~45 Hz: spectrum, peak meters, mini meters, osciloscopios |
| Tick de UI (path lento) | < 8 ms | Main | 10 Hz (cada 6 ticks): métricas, LUFS, mixer sync, LEDs de estado |
| tick_sync (PLL) | < 0.1 ms | Main | 60 Hz, 2 decks, aritmética simple |
| Recompute stem overlay | < 0.1 ms | Main | numpy argmax (N, 4) |
| Búsqueda FTS5 | < 5 ms | Main | SQLite FTS5 |
| refresh_statuses() | < 10 ms | Main | get_batch_status() SQLite WAL |

**Configuración de producción (aplicada en `app.py`):**

```python
sys.setswitchinterval(0.001)
torch.set_num_threads(2)
threadpool_limits(limits=2, user_api="blas")
AudioEngine(blocksize=256)   # CPAL Rust, sin GIL

prepared_db = PreparedDB(wrekker_data / "prepared.db")
stem_model  = HTDemucsModel()   # compartido entre Transport y PrepareWorker
transport   = Transport(engine, analyzer, prepared_db=prepared_db)
MainWindow(..., prepared_db=prepared_db, stem_model=stem_model)
```

---

## 15. Cambios recientes — WREKK, sync y FLX4

### Smart CFX / WREKK

Smart CFX es un modo global con branding **WREKK**:

| Modo | EQ HIGH/MID/LOW | TRIM | CFX/FILTER |
|------|------------------|------|------------|
| Normal | EQ high/mid/low del deck | Pregain normal | Filtro bipolar de canal |
| WREKK | Hardware controla vocals/drums/bass sin mover los faders visuales de EQ | Pregain normal en UI; el control hardware de TRIM puede controlar `other` sin mover el slider de gain | Macro de separación de stems |

Reglas actuales:

- Los labels visuales de EQ permanecen siempre `HIGH`, `MID`, `LOW`. Al activar
  WREKK no cambian a `VOC/DRM/BSS`.
- Los faders visuales de EQ siempre muestran el EQ real del engine. Al activar WREKK no saltan a valores de stems.
- Los stems se muestran y se editan en los controles dedicados de stems del `DeckWidget`.
- El estado de EQ, pregain y filtro normal se preserva al alternar WREKK.
- El filtro normal de canal se desactiva mientras WREKK está activo y se restaura al salir.
- El macro WREKK se guarda como estado separado de CFX normal.
- El macro trabaja como capa sobre los stem gains manuales:

```python
final_stem_gain = manual_stem_gain * wrekk_macro_multiplier * wrekk_fx_multiplier
```

Dirección del macro:

| CFX | Resultado |
|-----|-----------|
| Centro | Macro neutral; vuelve al balance manual de stems |
| Izquierda | Aísla/enfatiza vocals + other; reduce drums + bass |
| Derecha | Aísla/enfatiza drums + bass; reduce vocals + other |

### Sync / beatmatching

El sync usa beatgrid estable y un PLL de fase:

- El BPM base viene de `beatgrid.bpm` / `first_beat_s`, no del volumen master ni de peaks de audio. Aislar drums, vocals u otros stems no cambia el cálculo de beatmatching.
- `sync()` hace un snap inicial de fase cuando ambos tracks tienen beatgrid.
- `tick_sync(dt)` mantiene fase con un PI controller y muestra BPM nominal estable, no la corrección instantánea.
- Si el pitch fader del master se mueve, el follower escala su `applied_rate` inmediatamente con el nuevo rate nominal. La corrección fina del PLL queda encima, evitando que el follower se quede atrás y se desfase.
- Si el usuario mueve manualmente el pitch del follower, se desactiva sync para ese deck.

### UI en tiempo real

- El tick de UI corre a 60 Hz.
- Playhead, zoom waveform, overlays de beat, waveform stem anatomy y feedback de FX viven en el path rápido.
- Spectrum, osciloscopios, deck peak meters, stem peak meters y mini meters viven en una ruta visual de ~45 Hz.
- Métricas pesadas y LUFS se quedan en el path lento de ~10 Hz.
- El overlay de beats entre decks depende del estado de reproducción del deck fuente, no del deck que lo está mirando.
- El orden de osciloscopios en el master es `A | Main | B`.

### DDJ-FLX4

- `NOTE 0x00` en MIDI channel 7 toggles Smart CFX / WREKK.
- El LED del botón WREKK refleja el estado del modo.
- EQ HIGH/MID/LOW en WREKK controlan stems por hardware sin tocar el EQ del engine,
  pero los labels del UI siguen siendo HIGH/MID/LOW.
- CFX en modo normal controla filtro bipolar de canal.
- CFX en WREKK controla el macro de separación de stems.
- SHIFT + FX para WREKK FX queda preparado a nivel de API, pero no está mapeado
  todavía en esta implementación.
- BeatFX ON/OFF tiene LED por deck, encendido cuando FX está activo y el target incluye ese deck.
- BeatFX BEAT LEFT/RIGHT ya recorren tipos de FX; no cambian slots de hardware.
- Los VU meters del FLX4 reciben peak levels del engine igual que la UI.
- BeatFX wet/depth y el feedback visual de FX se actualizan en el path rápido para evitar lag visible.

### Carga y visualización

- Cargar Deck B no debe vaciar el deck si falla el análisis de stems.
- El menú contextual de biblioteca selecciona la fila bajo el cursor antes de cargar a deck A/B.
- La waveform stem anatomy overlay se actualiza con los stem gains efectivos.
- Los stem gains aplicados por hardware pasan por smoothing del engine para evitar clicks.

---

## Lanzamiento

```bash
# Normal
python -m wrekker.ui.app

# Solo biblioteca (sin audio)
python -m wrekker.ui.app --no-audio

# Sin controlador FLX4
python -m wrekker.ui.app --no-controller

# Con diagnóstico de timing
python -m wrekker.ui.app --debug

# Compilar el engine Rust tras cambios en engine_rs/
cd wrekker/engine_rs && maturin develop --release
```

En modo `--debug`, la consola imprime cada ~5 s:
```
[ui-tick] avg=2.1ms  peak=4.8ms  (50 ticks)
```

### SETTINGS — configuracion profesional persistente

Wrekker ahora tiene un sistema SETTINGS centralizado y versionado en
`wrekker/settings/store.py`. El archivo persistente se guarda en:

```text
~/.config/wrekker/settings.json
```

Conceptos del sistema:

- `SettingsStore`: carga, valida, migra, guarda, importa/exporta y aplica
  defaults efectivos al runtime.
- `SettingsSchemaVersion`: version del documento JSON para migraciones seguras.
- `SettingsProfile`: perfil completo de configuracion para setups distintos.
- `DefaultSettings`: defaults de codigo que preservan el comportamiento actual.
- `RuntimeOverrides`: variables de entorno presentes en el lanzamiento.
- `SettingsValidationResult`: errores/warnings antes de aplicar o guardar.

Precedencia:

1. Defaults de codigo.
2. Perfil persistente activo.
3. Variables de entorno/CLI explicitas.
4. Acciones temporales de runtime.

La UI principal abre SETTINGS desde el boton `SET`, `Wrekker > Settings` o
`Ctrl+,`. La ventana usa navegacion lateral, busqueda, dirty state, acciones de
perfil, reset por seccion, reset total, import/export y metadatos de aplicacion:
live-safe, requiere reinicio de audio o requiere reinicio de app.

Perfiles:

- `Default` existe siempre y no se puede borrar.
- El usuario puede crear, duplicar, renombrar, eliminar perfiles no protegidos,
  marcar startup profile, importar/exportar perfiles y exportar/importar todo el
  documento de settings.
- Cambios de dispositivo, sample rate, buffer o routing se guardan sin cortar
  audio silenciosamente. La UI informa que requieren reinicio de audio/app.

Audio y routing:

- La pagina `Audio & Routing` expone sample rate, buffer, latency preset,
  master 0/1, CUE 2/3, headphone mix/level y politicas de fallback.
- El engine Rust actual sigue abriendo el dispositivo CPAL default y auto-detecta
  FLX4/multicanal para CUE. Selectores de dispositivo explicito y test tones se
  muestran como no disponibles cuando no existe API segura para aplicarlos.

Rutas y caches:

- SETTINGS sincroniza WREKKED root, `.wrk` root, fastload root y temp stem cache
  con `PreparedDB.app_settings`, preservando la compatibilidad existente.
- Limpiar fastload o temp stems esta separado de borrar `.wrk`; la UI explica
  que `.wrk` y audio fuente se preservan.

LAB:

- WREKKER LAB consume defaults reales desde el perfil: source inicial, compare,
  phrase length, metronomo y click level.
- El renderer LAB estable sigue siendo `texture`; el fallback `classic` permanece
  disponible.

Waveforms:

- El renderer estable por defecto de decks es `TextureZoomWaveformWidget`.
- El renderer QWidget clasico queda como fallback.
- QML deck waveforms siguen siendo experimentales y opt-in; nunca se habilitan
  por migracion o default silencioso.

Stem Horizon:

- `Stems & WREKK` expone `Stem Horizon` con modos `Off`, `LED Blocks`,
  `Future Bars` y `Stem Waveforms`.
- El default es `LED Blocks`, 8 bars, visible para `.wrk` compatibles.
- El modo se guarda por perfil y se aplica a ambos decks sin reanalizar el
  track. Cambiarlo solo afecta render/UI.

### Flags de entorno principales

Las variables de entorno siguen siendo overrides validos para desarrollo,
diagnostico y sesiones puntuales. Cuando existen al arrancar, tienen prioridad
sobre el perfil persistente y SETTINGS las reporta como runtime overrides.

| Variable | Uso |
|----------|-----|
| `WREKKER_PREPARE_MODE=fast|balanced|archive` | Perfil de compresión FLAC para `.wrk`; por defecto `fast`. |
| `WREKKER_WRK_FLAC_COMPRESSION_LEVEL=0..8` | Override directo del nivel FLAC. |
| `WREKKER_WRK_AUDIO_ENCODE_THREADS=1..4` | Hilos para codificar audio/stems FLAC. |
| `WREKKER_PREPARE_CPU_WORKERS=2..6` | Workers CPU del pipeline WREKKED. |
| `WREKKER_PREPARE_GPU_POLICY=beat_cpu|parallel_gpu|...` | Política de uso GPU entre Beat This! y HTDemucs. |
| `WREKKER_BEAT_DEVICE=cpu|cuda|cuda:0` | Dispositivo Beat This! cuando la política no fuerza CPU. |
| `WREKKER_BEAT_CHECKPOINT=final0` | Checkpoint Beat This!. |
| `WREKKER_BEAT_USE_DBN=1` | Activa postproceso DBN. |
| `WREKKER_KEEP_PREP_TEMP=1` | Conserva temporales de preparación. |
| `WREKKER_W_MARKER_DEBUG=1` | Log resumido del detector WREKK Markers: estructurales, oportunidades y total emitido. |
| `WREKKER_STEM_HORIZON_DEBUG=1` | Log de diagnostico del widget Stem Horizon y estado de datos/cadencia. |
| `WREKKER_FASTLOAD_CACHE=/ruta` | Raíz del fastload cache. |
| `WREKKER_STEM_CACHE_PATH=/ruta` | Raíz del cache temporal de stems. |
| `WREKKER_UI_TICK_MS=8` | Intervalo solicitado del tick principal de UI. |
| `WREKKER_UI_CLOCK=pipe|thread|qtimer` | Reloj del tick visual principal; por defecto `pipe`. |
| `WREKKER_UI_TARGET_FPS=60` | Cadencia objetivo del reloj visual con `WREKKER_UI_CLOCK=pipe` o `thread`. |
| `WREKKER_UI_TICK_LOG=1` | Log resumido del tick de UI. |
| `WREKKER_UI_TICK_PROFILE=1` | Perfilado de secciones del tick de UI. |
| `WREKKER_DISABLE_CROSS_OVERLAY=1` | Desactiva overlay visual del otro deck. |
| `WREKKER_DISABLE_DECK_REALTIME=1` | Desactiva actualizaciones realtime de decks. |
| `WREKKER_ZOOM_FPS_LOG=1` | FPS/pintado del zoom waveform QWidget. |
| `WREKKER_ZOOM_RENDERER=texture|classic|legacy` | Selecciona renderer del zoom; default `texture`. `classic`/`legacy` mantiene el método anterior como fallback. |
| `WREKKER_ZOOM_CACHE_SCALE=2` | Supersampling horizontal del cache visual del zoom para reducir saltitos de columna. |
| `WREKKER_ZOOM_PEAK_SMOOTH=3` | Suavizado visual de peaks para reducir shimmer/temblor temporal. |
| `WREKKER_TEXTURE_ZOOM_CACHE_SCALE=4` | Supersampling específico del renderer `texture`. |
| `WREKKER_TEXTURE_ZOOM_PEAK_SMOOTH=5` | Suavizado específico del renderer `texture`. |
| `WREKKER_LAB_FORCE_WIDGET_TIMELINE=1` | Fuerza el timeline QWidget de WREKKER LAB aunque Qt Quick esté disponible. |
| `WREKKER_LAB_WAVEFORM_RENDERER=texture|classic|legacy` | Selecciona renderer del timeline QWidget de LAB; default `texture`. `classic`/`legacy` mantiene el método anterior como fallback. |
| `WREKKER_LAB_TEXTURE_CACHE_SCALE=4` | Multiplicador de cache horizontal del renderer `texture` de LAB; si no se define, hereda `WREKKER_TEXTURE_ZOOM_CACHE_SCALE`. |
| `WREKKER_LAB_TEXTURE_PX_PER_SECOND=256` | Resolución horizontal de la textura LAB para zoom tipo SoundCloud sin borrar picos. |
| `WREKKER_LAB_TEXTURE_PEAK_SMOOTH=0` | Suavizado del renderer `texture` de LAB; por defecto preserva transientes y evita apariencia de polígonos. |
| `WREKKER_ZOOM_DISABLE_REPAINT=1` | Diagnóstico: no solicita repaint del zoom para aislar backing-store/compositor. |
| `WREKKER_ZOOM_ANIM_MS=1` | Intervalo de despertar del timer de zoom; el pacing interno limita frames reales. |
| `WREKKER_ZOOM_TARGET_FPS=60` | Cadencia visual objetivo del zoom waveform. |
| `WREKKER_ZOOM_OWN_TIMER=1` | Activa timer propio del zoom para profiling; por defecto el tick principal anima ambos decks. |
| `WREKKER_WAVEFORM_POSITION_DEBUG=1` | Diagnóstico de posición visual QWidget. |
| `WREKKER_UI_PLATFORM_LOG=1` | Imprime plataforma Qt, pantalla, DPR y refresh reportado. |
| `WREKKER_ENABLE_QML_DECK_WAVEFORMS=1` | Solicita timelines de deck en QML. |
| `WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS=1` | Habilita la ruta QML de deck marcada como inestable. |
| `WREKKER_WAVEFORM_RENDER_DEBUG=1` | Log de renderer QML/fallback. |
| `WREKKER_WAVEFORM_FPS_LOG=1` | FPS de modelos QML de waveform/deck. |
| `WREKKER_QML_FPS_LOG=1` | FPS del timeline QML de WREKKER LAB. |

---

---

## 16. Sección WREKKED — browser de pistas preparadas

### Motivación

Las fuentes SMB se desconectan durante una actuación. El browser original de biblioteca requiere que el archivo fuente exista para cargar una pista. WREKKED es un browser independiente que opera sobre los archivos `.wrk` directamente — offline desde el primer momento.

### `WrekkedScanner` (`wrekker/library/wrekked_scanner.py`)

Descubre sets y pistas bajo `~/.local/share/wrekker/prepared/tracks/{nombre_set}/*.wrk`.

```
~/.local/share/wrekker/prepared/tracks/
└── House Mix/          ← nombre del set = nombre de carpeta
    ├── Track A.wrk
    └── Track B.wrk
```

- Lee solo `manifest.json` del ZIP — nunca descomprime FLAC
- `wrk_id` desde `manifest["source_path"]` (mismo pipeline que PrepareWorker); fallback a `sha256(wrk_path)`
- `source_available = True` solo si el `source_path` del manifest existe en disco
- `scan()` → upserta `PreparedSet` + `PreparedSetTrack` en `PreparedDB`; retorna el número total de pistas

### WREKKER LAB

WREKKER LAB es una ventana standalone grande para corregir análisis y preparar
performance metadata de un `.wrk`. Entry points actuales:

- menú contextual de una fila WREKKED → `Open in WREKKER LAB`
- Manage WREKKED Library → `Open Selected Track in WREKKER LAB`
- menú de markers/deck cuando el deck cargó un `.wrk` → `Edit Analysis in WREKKER LAB`

Arquitectura:

- `wrekker.lab.session.LabEditSession` mantiene una copia draft en memoria.
- Undo/redo usa snapshots de la sesión, sin escribir al disco.
- `Save Corrections` escribe una sola revisión con `AnalysisRevision` y lista de
  `AnalysisChange`.
- El guardado es transaccional: crea un ZIP temporal, preserva entradas grandes
  (`audio/*.flac`, waveform binaria, artwork), reemplaza solo JSON, valida y
  hace `os.replace()`.
- Si falla el save, el `.wrk` original queda intacto.
- Fastload se refresca solo en metadata (`metadata.json`, `beatgrid.json`,
  `markers.json`, `cues.json`, `loops.json`, `ready.flag`). No reconstruye PCM.

Capacidades MVP implementadas:

- migración legacy a capas `beatgrid_auto.json`/`markers_auto.json`
- capa ACTIVE en `beatgrid.json`/`markers.json`
- `analysis/corrections.json` con status, revision, hot cue count, loop count
- `analysis/changelog.json` con historial auditable
- compare AUTO vs ACTIVE
- waveform LAB con fuentes `FULL MIX`, `VOCALS`, `DRUMS`, `BASS`, `OTHER`,
  `STEM ANATOMY` usando waveform/stem_energy precomputados
- timeline LAB en QML con fallback QWidget; permite seek, selección de fuente,
  mute/isolate de stems en el preview y compare AUTO
- preview de audio aislado usando `AudioEngine`: play/pause/stop/seek,
  actualización de posición y cierre limpio del stream al cerrar LAB
- metrónomo renderizado sobre el beatgrid draft, con click distinto para beats y
  downbeats, nivel ajustable y headroom para evitar clipping
- monitor de stems en preview: mute/isolate de `VOCALS`, `DRUMS`, `BASS` y
  `OTHER`; carga stems desde fastload si está válido o desde el `.wrk` como
  fallback
- edición constant-tempo: shift grid, set first beat, set BPM, BPM x2//2,
  set downbeat, regenerar frases 8/16/32
- snapping ligero a transiente de drums desde `stem_energy`
- crear/editar/bloquear/borrar markers; limpiar auto markers desbloqueados
- convertir marker a hot cue
- agregar/borrar hot cues y loops guardados
- `MANUAL VERIFIED`
- badges LAB en WREKKED (`VERIFIED`, `LAB EDITED`, `GRID EDITED`,
  `MARKERS EDITED`, `CUES READY`, `DYN TODO`)

Limitaciones explícitas del MVP:

- preview LAB no tiene todavía selector avanzado de dispositivo/salida, routing
  CUE dedicado ni controles finos de latencia; usa el engine local básico
- no hay edición warp-anchor/dynamic-tempo completa
- no hot-swap de beatgrid en decks activos; tras guardar se debe recargar la pista

### Tablas nuevas en `PreparedDB`

```sql
CREATE TABLE IF NOT EXISTS prepared_sets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    source_root_label TEXT, source_root_path TEXT,
    track_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    description TEXT,                -- editable libre (ALTER TABLE migration)
    total_duration_s REAL NOT NULL DEFAULT 0.0,  -- (ALTER TABLE migration)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prepared_set_tracks (
    id INTEGER PRIMARY KEY,
    set_id INTEGER NOT NULL REFERENCES prepared_sets(id) ON DELETE CASCADE,
    wrk_id TEXT NOT NULL, wrk_path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '', artist TEXT NOT NULL DEFAULT '',
    duration_s REAL NOT NULL DEFAULT 0.0, bpm REAL, key TEXT,
    stems_ready INTEGER NOT NULL DEFAULT 0,
    wrk_ready   INTEGER NOT NULL DEFAULT 0,
    source_available INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(set_id, wrk_id)
);
```

Ver §8 para los métodos completos de CRUD sobre estos sets.

El orden de un set es siempre `prepared_set_tracks.position`; no se deriva del
titulo, artista ni nombre de archivo. Al importar desde playlist o carpeta SMB,
Wrekker asigna posiciones en el orden recibido por la fuente. Los rescans de
`WrekkedScanner` actualizan metadata/estado, pero no pisan posiciones ya
existentes; esto evita que un set armado manualmente vuelva a ordenarse
alfanumericamente.

### `WrekkedWidget` (`wrekker/ui/widgets/wrekked.py`)

Browser de pistas preparadas. Mismo estilo visual que `LibraryWidget` — top bar, splitter, tabla, colores de theme.

**Panel izquierdo — lista de sets:**

```
[ + New ]                  ← crea set vacío (QInputDialog)
────────────────────
All Prepared    ← virtual __all__
Recently Added  ← virtual __recent__
── SETS ──
  House Mix  (12)  ← nombre + track_count; verde STATUS_OK
  TRVL 9     (8)
```

Menú contextual de set (clic derecho):
- **Edit Set…** → abre `WrekkedSetDialog`
- **Rename…** → `QInputDialog` inline
- **Duplicate** → copia el set con " (copy)" en el nombre
- **Delete Set…** → confirma + `remove_wrekked_set()`

Menú contextual de pista dentro de un set real:
- **Move Up in Set**
- **Move Down in Set**
- **Remove from Set**

Estas acciones escriben `position` inmediatamente y preservan el orden aunque el
track esté cargable por `.wrk` o por fastload.

**Panel derecho — tabla de pistas:**

| Columna | Ancho | Datos |
|---------|-------|-------|
| Title | stretch | `title` o nombre de archivo |
| Artist | stretch | `artist` |
| Duration | 60 px | `M:SS` |
| BPM | 60 px | `128.0` |
| Key | 50 px | `8A` |
| STATUS | 60 px | badge de color |

**Badges de STATUS (prioridad en orden):**

| Badge | Color | Condición |
|-------|-------|-----------|
| `FASTLOAD` | `#00e87a` verde brillante | fastload cache válida (`FastloadCache.is_valid()`) |
| `WRK` | `#2ecc71` verde | `wrk_ready` + `source_available` |
| `STEMS` | `#3498db` azul | `stems_ready` |
| `SRC OFF` | `#e67e22` naranja | fuente SMB no disponible (pista sigue cargable) |
| `BROKEN` | `#e74c3c` rojo | `wrk_ready = False` |

FASTLOAD es un estado adicional (no exclusivo): una pista puede tener WRK + FASTLOAD → muestra FASTLOAD. El badge verde brillante distingue las pistas que cargarán sin delay.

**Check de FASTLOAD en background:**

Al seleccionar un set, se lanza un hilo daemon:
```python
def _check_fastload_bg(self, tracks):
    cache = FastloadCache()
    result = {t.wrk_path: cache.is_valid(Path(t.wrk_path)) for t in tracks}
    self._fastload_ready.emit(result)   # pyqtSignal(object)
```
La señal `_fastload_ready` → `_on_fastload_ready(d)` → `model.set_fastload(d)` → `dataChanged` en columna STATUS.

**Barra de stats (bajo la lista de sets):**

```
12 tracks  ·  1:23:45  ·  8 WRK  ·  5 FASTLOAD  ·  2 SRC OFF
```
Actualizada tras cada carga de set y tras completar el check de fastload.

**Señales:**

```python
load_wrk_track = pyqtSignal(str, str)   # (deck_id, wrk_path)
rescan_done    = pyqtSignal(int)        # n_tracks
```

**Menú contextual de pista (clic derecho):**

```
Load to Deck A
Load to Deck B
─────────────────────
Build fastload cache   (si no hay caché)
Rebuild fastload cache (si ya hay caché)
Delete fastload cache
─────────────────────
Add to Set ▶  [lista de otros sets]
Remove from Set        (si hay set activo seleccionado)
─────────────────────
Reveal .wrk in Files
```

### Tab switcher en `MainWindow`

Tab strip plano sobre un `QStackedWidget`:

```python
# Tab strip — BG_PANEL; visualmente fusionado con top bars de los widgets
tab_strip = QWidget()
tab_strip.setFixedHeight(30)
self._tab_lib_btn = QPushButton("LIBRARY")   # activo: TEXT_BRIGHT
self._tab_wrk_btn = QPushButton("WREKKED")   # inactivo: TEXT_DIM
```

- `_switch_browser(index)`: alterna QStackedWidget y actualiza estilos de botones
- `_on_load_wrk_track(deck_id, wrk_path)` → `transport.load_wrk_track(deck_id, wrk_path)`
- `WrekkedScanner.scan()` lanzado 1 s tras startup; resultado conectado a `wrekked_widget.on_rescan_done`

---

## 17. Fastload cache — carga instantánea de .wrk

### Problema

El decode FLAC de la mezcla completa tardaba ~21 s (medido como `mix decode: 21888ms`). Toda primera carga de un `.wrk` bloqueaba el deck por decenas de segundos.

### Solución: `FastloadCache` (`wrekker/formats/fastload.py`)

Caché completa en `~/.cache/wrekker/fastload/{sha256(wrk_path)}/`. Almacena **todo** lo que se necesita para la Fase 1 y Fase 2 de carga, sin abrir el ZIP `.wrk`:

```
~/.cache/wrekker/fastload/{key}/
    ├── metadata.json         ← copia del manifest del .wrk
    ├── mix.pcm16             ← int16 interleaved, ~10.6 MB/min @ 44.1 kHz estéreo
    ├── mix.meta.json         ← {"n_frames": …, "n_channels": …, "sr": …, "audio_format": "pcm16"}
    ├── waveform_peaks.f32    ← (N,) float32
    ├── waveform_colors.bin   ← (N, 3) uint8
    ├── stem_energy.f32       ← (N, 4) float32
    ├── stem_horizon.json     ← actividad bar-synchronous de VOC/DRM/BSS/OTH
    ├── beatgrid.json         ← beatgrid schema v2
    ├── cues.json             ← lista de hot cues
    ├── loops.json            ← loops guardados
    ├── artwork.jpg | .png    ← portada (si existe)
    ├── [vocals.pcm16]        ← opcional; solo si se cachearon stems
    ├── [drums.pcm16]
    ├── [bass.pcm16]
    ├── [other.pcm16]
    ├── [stems.meta.json]     ← presencia indica stems en caché
    └── ready.flag            ← {"fastload_version": 2, "wrk_mtime_ns": …, "wrk_size": …, …}
```

La presencia de `metadata.json`, `waveform_peaks.f32`, `beatgrid.json` y demás archivos de análisis permite que un **cache HIT** sirva toda la Fase 1 sin tocar el ZIP `.wrk`.

**`FastloadSettings` — configuración de caché:**

```python
@dataclass
class FastloadSettings:
    enabled:       bool = True
    audio_format:  str  = FORMAT_PCM16   # "pcm16" | "f32"
    cache_stems:   bool = False          # stems opcionales; ocupan más espacio
    cache_root:    Path | None = None    # None → usa WREKKER_FASTLOAD_CACHE o ~/.cache/…

    def effective_root(self) -> Path:
        # Prioridad: self.cache_root → WREKKER_FASTLOAD_CACHE env → default
```

**Constantes:**

```python
FASTLOAD_VERSION = 2   # invalida caches v1 (f32); solo PCM16 en producción
FORMAT_PCM16 = "pcm16" # int16 interleaved, ~10.6 MB/min
FORMAT_F32   = "f32"   # float32, ~21 MB/min (modo legado)
```

**Codificación PCM16:**

```python
_PCM16_SCALE = 32767.0

def _to_pcm16(audio: np.ndarray) -> np.ndarray:  # → int16
    return (np.clip(audio, -1.0, 1.0) * _PCM16_SCALE).astype(np.int16)

def _from_pcm16(raw: np.ndarray, n_frames: int, n_channels: int) -> np.ndarray:  # → float32
    return raw.reshape(n_frames, n_channels).astype(np.float32) / _PCM16_SCALE
```

**API completa:**

| Método | Descripción |
|--------|-------------|
| `is_valid(wrk_path)` | `ready.flag` existe y `fastload_version + wrk_mtime_ns + wrk_size` coinciden |
| `has_stems(wrk_path)` | True si `stems.meta.json` existe (stems en caché) |
| `load_metadata(wrk_path)` | Carga `WrkMetadata` completa desde caché (sin abrir ZIP) |
| `load_mix(wrk_path)` | Lee PCM16/f32 → `(audio: ndarray, sr: int)` |
| `load_all_stems(wrk_path)` | Lee todos los stems → `dict[str, (channels, n_frames) float32]` o None |
| `build(wrk_path, meta, mix_audio, mix_sr, stems_raw=None, audio_format=FORMAT_PCM16)` | Escribe caché atómicamente (`tmp → rename`) |
| `entry_size_bytes(wrk_path)` | Bytes del directorio de caché de esta pista |
| `total_size_bytes()` | Bytes totales de toda la caché fastload |
| `invalidate(wrk_path)` | Elimina directorio de caché de esta pista |
| `clean_orphans()` | Elimina entradas incompletas (sin `ready.flag`); retorna count |
| `clean_older_than(max_age_days)` | Elimina entradas cuyo `created_at` supera la edad; retorna count |

### Staged loading en `Transport._load_from_wrk`

```
Fase 0 — Lookup de caché (~1 ms)
   cache_hit  = cache.is_valid(wrk_path)   ← check ready.flag
   stems_hit  = cache_hit and cache.has_stems(wrk_path)

Fase 1 — Metadata (~3–50 ms)
   Si cache_hit → cache.load_metadata()    ← lee todo desde disco plano, sin abrir ZIP
   Si MISS      → load_wrk_metadata()      ← abre ZIP, lee manifest + waveform + beatgrid
   → DeckStatus = WRK_LOADING
   → UI muestra waveform, info de pista, beatgrid — inmediatamente

Fase 2 — Mix audio (fastload HIT: 10–80 ms; MISS: FLAC decode 300–2000 ms)
   Si cache_hit → cache.load_mix()         → engine.load_track()   [fast path]
   Si MISS      → load_wrk_mix()           → engine.load_track()
                  Thread(_build_mix_cache) ← construye caché completa en background
   → DeckStatus = READY
   → Reproducción disponible

Fase 3 — Stems (background daemon)
   Si stems_hit  → cache.load_all_stems()  → engine.update_stems()  [fast path]
   Si cache_hit pero sin stems
                 → load_wrk_stems()        → engine.update_stems()
                   Thread(_build_stems_cache) ← añade stems a la caché existente
   Si MISS       → load_wrk_stems()        → engine.update_stems()
   → DeckStatus READY con stems activos
```

**Guard de track hash:** antes de aplicar stems en Phase 3, se comprueba que `dl.state.track.file_hash` siga coincidiendo con el hash original. Si el usuario cargó otro track mientras los stems cargaban, los stems se descartan silenciosamente.

**Log de referencia con caché fría:**

```
[wrk-load] fastload lookup: 1ms  (MISS)
[wrk-load] flac mix decode: 480ms  (building fastload cache in background)
[wrk-load] playable (flac): 485ms
...
[wrk-load] fastload mix read: 18ms
[wrk-load] playable (fastload): 21ms
```

### Phase 5 en `PrepareWorker`

Después de `validate_wrk()` y antes de `upsert_record()`, el PrepareWorker construye la caché de fastload mix:

```python
# ── Phase 5: fastload mix cache ───────────────────────────────────────
self.track_progress.emit(idx, "fastload", 0.0)
try:
    from wrekker.formats.fastload import FastloadCache, FORMAT_PCM16
    from wrekker.formats.wrk import load_wrk_metadata
    wrk_meta = load_wrk_metadata(wrk_path)
    FastloadCache().build(
        wrk_path=wrk_path, meta=wrk_meta,
        mix_audio=audio, mix_sr=sr,
        stems_raw=None, audio_format=FORMAT_PCM16,
    )
except Exception as _e:
    print(f"[prepare] fastload cache skipped ({_e})", flush=True)
self.track_progress.emit(idx, "fastload", 1.0)
```

Resultado: el primer load en vivo de cualquier pista preparada siempre usa la caché → no hay decode FLAC en actuación.

`PrepareDialog` muestra `"Caching fastload"` durante esta fase (entrada añadida a `_PHASE_LABEL`).

**Fases actualizadas de PrepareWorker:**

```
1. "audio"    → load_audio()
2. "analysis" → BPM + key + waveform
3. "stems"    → StemCache / HTDemucs
4. "packing"  → create_wrk() → validate_wrk()
5. "fastload" → FastloadCache().build() (mix-only PCM16, non-fatal)
   upsert_record() en PreparedDB
```

---

## 18. Seguridad de audio durante la carga

### Problema

Mientras una pista cargaba (waveform y metadata visibles en la UI), era posible presionar Play y escuchar el audio de la **pista anterior**. El engine seguía reproduciendo el buffer viejo hasta que el nuevo reemplazara el `AudioBuffers` en Rust.

### Correcciones en `Transport` (`wrekker/core/transport.py`)

**1. Silenciar el engine al inicio de la carga** — en `load_track()` y `load_wrk_track()`:

```python
# Inmediatamente tras push del estado LOADING:
try:
    self._engine.pause(deck_id)
except Exception:
    pass
```

El hilo CPAL Rust lee `playing=False` atómicamente y deja de mezclar el buffer viejo. Sin latencia perceptible.

**2. Guardia en `play()`:**

```python
def play(self, deck_id: str) -> None:
    dl = self._decks[deck_id]
    with dl.lock:
        current_status = dl.state.status
    if current_status == DeckStatus.LOADING:
        return   # no reproducir mientras carga
    if current_status == DeckStatus.EMPTY:
        return   # no reproducir deck vacío
    # ... resto de la lógica de play
```

Presionar Play durante la carga (ya sea desde la UI, hardware FLX4 o API) es ahora un no-op silencioso.

**3. `load_wrk_track()` — nueva API pública:**

```python
transport.load_wrk_track(deck_id: str, wrk_path: str) -> None
```

Punto de entrada para el WREKKED browser. Ejecuta el mismo pipeline staged de 3 fases que `_load_from_wrk`, pero con `wrk_path` como fuente de verdad (sin `source_path` en la DB). Incluye la misma guardia de silenciado.

---

---

## 19. Library & WREKKED UX upgrade — metadata rica y sets editables

### Motivación

La biblioteca mostraba un solo BPM (el analizado) sin forma de detectar discrepancias con los tags originales, y la clave no tenía contexto armónico respecto a lo que sonaba. Los sets WREKKED eran de solo lectura tras el escaneo.

### Roles de modelo personalizados en `_TrackTableModel`

```python
_BPM_DATA_ROLE  = Qt.ItemDataRole.UserRole + 1   # → (meta_bpm, wrk_bpm) | None
_COMPAT_ROLE    = Qt.ItemDataRole.UserRole + 2   # → float 0.0–1.0 | None
```

`data()` evalúa estos roles para el índice solicitado:
- `_BPM_DATA_ROLE`: retorna la tupla `(rec.bpm_metadata, rec.bpm)` donde `rec` es el `PreparedRecord` cacheado
- `_COMPAT_ROLE`: llama `HarmonicKey.compatibility(track_key, ref_key)` si ambas claves son válidas; `None` si no hay referencia

### `_DualBPMDelegate` (columna 4)

`QStyledItemDelegate` que pide `_BPM_DATA_ROLE` y dibuja:
```
  [128.0] / [127.5]
    azul      naranja
```
Fondo de selección dibujado con `style.drawPrimitive(PE_PanelItemViewItem, opt)` antes del texto para respetar el tema.

### `_KeyCompatDelegate` (columna 5)

Dibuja un punto (6 px de diámetro) a la izquierda + texto de clave:

```python
COMPAT_COLORS = {
    (0.85, 1.01): "#2ecc71",   # verde — perfecta / ±1 Camelot
    (0.50, 0.85): "#f39c12",   # amarillo — compatible
    (0.00, 0.50): "#e74c3c",   # rojo — conflicto
}
# None → gris (sin referencia)
```

`HarmonicKey.compatibility()` — tabla de scores Camelot:

| Relación | Score |
|----------|-------|
| Misma clave (0 pasos) | 1.00 |
| ±1 paso Camelot | 0.85 |
| Relativa mayor/menor | 0.75 |
| ±2 pasos | 0.50 |
| ±3 pasos | 0.25 |
| Todo lo demás | 0.00 |

### Propagación de clave de referencia

`MainWindow._last_ref_key` se compara en cada heavy tick (10 Hz). Solo se propaga cuando cambia:

```python
ref_key = self._get_reference_key(state_a, state_b)
if ref_key != self._last_ref_key:
    self._last_ref_key = ref_key
    self._library.set_reference_key(ref_key)
```

`_get_reference_key()` prioriza: sync master → reproduciendo → cualquier cargado → `None`.

`LibraryWidget.set_reference_key(key)` → `model.set_reference_key(key)`:
```python
def set_reference_key(self, key):
    self._ref_key = key
    top = self.index(0, 5)
    bot = self.index(self.rowCount() - 1, 5)
    self.dataChanged.emit(top, bot, [_COMPAT_ROLE])
```

### `WrekkedSetDialog` (`wrekker/ui/widgets/wrekked_set_dialog.py`)

Editor completo de un set WREKKED. Se abre desde el menú contextual del panel de sets ("Edit Set…").

**Layout:**
```
┌─ Edit WREKKED Set ────────────────────────────┐
│  Name: [ House Mix                          ] │
│ ─────────────────────────────────────────── │
│  [FASTLOAD] Artist - Title A                  │
│  [WRK     ] Artist - Title B                  │
│  [SRC OFF ] Artist - Title C  ← naranja       │
│  ─────────────────────────────────────────  │
│  [ ↑ Up ]  [ ↓ Down ]            [ Remove ]   │
│  5 tracks  ·  35:12  ·  3 WRK  ·  2 FASTLOAD │
│ ─────────────────────────────────────────── │
│  [ Delete Set… ]        [ Cancel ] [ Save ]   │
└───────────────────────────────────────────────┘
```

- **Drag-and-drop interno** (`InternalMove`) en `QListWidget`
- El orden en la lista al hacer Save se usa como nuevo `position` en DB
- `_save()` lee la lista widget (respeta drag-drop), llama `reorder_set_track()` por cada item, luego `remove_set_track()` para los que fueron eliminados, luego `update_set_track_count()` + `update_set_total_duration()`
- `_delete_set()` → `remove_wrekked_set()` → `self.done(2)` (código especial → el caller sabe que el set fue eliminado y refresca la lista)
- Fastload check en background thread con `QTimer.singleShot(0, self._populate_list)` para actualizar badges en el hilo principal

### Resumen de archivos modificados

| Archivo | Cambio principal |
|---------|-----------------|
| `wrekker/library/prepared_db.py` | `_MIGRATIONS`, `PreparedSet` fields, métodos CRUD de sets |
| `wrekker/ui/widgets/library.py` | `_DualBPMDelegate`, `_KeyCompatDelegate`, `_BPM_DATA_ROLE`, `_COMPAT_ROLE`, menú contextual completo, `set_reference_key()` |
| `wrekker/ui/widgets/wrekked.py` | FASTLOAD badge, background check, stats bar, `+ New`, menús set/track |
| `wrekker/ui/widgets/wrekked_set_dialog.py` | NUEVO: `WrekkedSetDialog` |
| `wrekker/ui/main_window.py` | `_last_ref_key`, `_get_reference_key()`, propagación a 10 Hz |
| `wrekker/ui/workers/prepare_worker.py` | Phase 5 "fastload" |
| `wrekker/ui/widgets/prepare_dialog.py` | `_PHASE_LABEL["fastload"]` |

---

## 20. WREKKED Management UI — biblioteca local de performance

### Objetivo

WREKKED deja de comportarse como una lista de archivos preparados y pasa a ser la
biblioteca local de performance de Wrekker. La UI permite revisar compatibilidad
armónica, estado de `.wrk`, estado de fastload, sets preparados, metadata y rutas
de almacenamiento desde el propio producto, manteniendo el tema oscuro/neón de
Wrekker.

### Track action menu por fila

`LibraryWidget` y `WrekkedWidget` ahora incluyen una columna final `"..."`.
Al hacer click se abre el mismo menú contextual de la fila, sin depender del
click derecho.

Acciones disponibles según contexto:

| Acción | Library | WREKKED |
|--------|---------|---------|
| Load to Deck A/B | Sí | Sí |
| Prepare `.wrk` / Rebuild `.wrk` | Sí | N/A |
| Build/Rebuild/Delete Fastload Cache | Sí | Sí |
| Reveal Fastload Cache Folder | Sí | Sí |
| Validate Cache | Sí | Sí |
| Add to WREKKED Set | Sí | Sí |
| Move to WREKKED Set | N/A | Sí |
| Remove from Current WREKKED Set | N/A | Sí |
| Edit Metadata | Sí, vía PreparedDB | Sí |
| Reveal Source File | Sí | N/A |
| Reveal `.wrk` File | Sí | Sí |
| Delete `.wrk` | Sí, con confirmación | Sí, con confirmación |
| Remove from Library | Sí, con confirmación | vía manager |

Las acciones destructivas piden confirmación explícita. `SOURCE OFFLINE` no se
marca como `BROKEN` cuando el `.wrk` existe y está listo; WREKKED sigue siendo
usable localmente aunque el origen SMB no esté montado.

### Compatibilidad armónica en Library y WREKKED

La columna `Key` mantiene el indicador circular de compatibilidad:

| Score | Color | Significado |
|-------|-------|-------------|
| `>= 0.75` | verde | compatible |
| `>= 0.50` | amarillo | usable / tensión moderada |
| `< 0.50` | rojo | pobre compatibilidad |
| `None` | gris | sin referencia o sin key |

`MainWindow` propaga la clave de referencia a Library y WREKKED en el heavy tick
cuando cambia:

```python
ref_key = self._get_reference_key(state_a, state_b)
if ref_key != self._last_ref_key:
    self._last_ref_key = ref_key
    self._library.set_reference_key(ref_key)
    self._wrekked.set_reference_key(ref_key)
```

La prioridad de referencia sigue siendo: sync/main master → deck reproduciendo →
cualquier deck cargado → `None`.

### Fastload por set

El menú contextual de sets WREKKED incluye:

- Build Fastload for Set
- Rebuild Fastload for Set
- Delete Fastload for Set
- Validate Fastload for Set
- Show Fastload Status

Las operaciones de build/rebuild corren en background threads y actualizan el
contador superior con progreso: track actual, total, success, skipped y failed.
Delete conserva los `.wrk` y elimina solo los directorios de fastload.

El resumen del set distingue estados:

```
27 tracks · 27 WRK · 24 FASTLOAD · 3 NO CACHE · 8 SOURCE OFFLINE
```

`WRK READY` y `FASTLOAD READY` son estados separados:

| Estado | Implicación |
|--------|-------------|
| `WRK READY + NO CACHE` | reproducible desde `.wrk`, carga más lenta |
| `WRK READY + FASTLOAD` | optimizado para carga rápida |
| `WRK READY + SOURCE OFFLINE + FASTLOAD` | completamente usable localmente |
| `WRK READY + SOURCE OFFLINE + NO CACHE` | usable desde `.wrk`, pero sin optimización |
| `BROKEN` | `.wrk` ausente o no usable |

### `ManageWrekkedLibraryDialog`

Nuevo diálogo: `wrekker/ui/widgets/wrekked_manage_dialog.py`.

Accesos:

- botón `MANAGE` en la barra superior de WREKKED
- botón `"..."` del header de sets WREKKED
- menú contextual de set
- botón `WREKKED` en la barra de Library

Layout:

```
┌─ Manage WREKKED Library ───────────────────────────────────────┐
│ Search                         [New Set] [Rename] [Settings]   │
├───────────────┬────────────────────────────────────────────────┤
│ All Prepared  │ Title Artist BPM Key WRK STEMS FASTLOAD Source │
│ Recently      │ ...                                            │
│ Source Offline│                                                │
│ No Fastload   │                                                │
│ Broken Items  │                                                │
│ ─ SETS ─      │                                                │
│ TRVL 9        │                                                │
├───────────────┴────────────────────────────────────────────────┤
│ .wrk path · cache path · size · mode · expected load path       │
└────────────────────────────────────────────────────────────────┘
```

Funciones principales:

- ver todos los sets y vistas virtuales: All Prepared, Recently Prepared,
  Source Offline, No Fastload Cache, Broken Items
- crear, renombrar, duplicar y borrar sets
- seleccionar una o varias pistas
- mover/copiar pistas entre sets
- quitar pistas del set actual
- editar metadata de PreparedDB sin requerir el archivo fuente
- construir/reconstruir/borrar/validar fastload por pista o por set
- borrar `.wrk` con confirmación
- revelar `.wrk` y carpeta de fastload
- ver detalle de `.wrk`, cache, tamaño, modo, formato y load path esperado

### Metadata editable

`MetadataEditDialog` edita metadata de visualización en PreparedDB:

- title
- artist
- BPM override
- key override

El diseño deja preparada la UI para album, genre y comments, pero la persistencia
actual se limita a los campos soportados por las tablas existentes. La metadata
de PreparedDB actúa como fuente de verdad de la UI y no depende del archivo fuente
original.

API añadida:

```python
PreparedDB.update_track_metadata(wrk_id, title=..., artist=..., bpm=..., key=...)
```

Esta actualización sincroniza `prepared_tracks` y `prepared_set_tracks`.

### Settings de rutas

`WrekkedPathSettingsDialog` permite ver, editar, validar y revelar:

- WREKKED Library Path
- `.wrk` Storage Path
- Fastload Cache Path
- Temporary Stem Cache Path
- Backup/Export Location

También expone:

- Fastload Mode: Disabled, Mix Only, WREKKED Sets, Selected Tracks, Auto/LRU
- Fastload Format: PCM16, F32

Los valores se guardan en `PreparedDB.app_settings`.

El `FastloadCache` ahora respeta `WREKKER_FASTLOAD_CACHE` cuando no se pasa
`cache_root` explícito. Al guardar `Fastload Cache Path`, el diálogo actualiza
ese env var para la sesión actual.

`MainWindow` usa `wrekked_library_path` como raíz preparada al crear
`WrekkedScanner` y al lanzar `PrepareWorker`.

### Cambios de base de datos

Nueva tabla:

```sql
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

Nuevas APIs:

| Método | Uso |
|--------|-----|
| `update_track_metadata()` | overrides de metadata de UI |
| `remove_prepared_track()` | elimina índice PreparedDB y membresías WREKKED |
| `copy_tracks_to_set()` | copia tracks preparados a otro set |
| `move_tracks_to_set()` | mueve tracks entre sets |
| `get_setting()` / `set_setting()` | persistencia de rutas y opciones |

### Archivos principales modificados

| Archivo | Cambio |
|---------|--------|
| `wrekker/ui/widgets/library.py` | columna `"..."`, menú ampliado, acceso a manager, metadata edit, cache reveal/validate |
| `wrekker/ui/widgets/wrekked.py` | columna `"..."`, compat light, set fastload actions, manager button, confirmaciones destructivas |
| `wrekker/ui/widgets/wrekked_manage_dialog.py` | nuevo manager completo de biblioteca WREKKED |
| `wrekker/library/prepared_db.py` | settings, metadata overrides, copy/move/remove helpers |
| `wrekker/formats/fastload.py` | raíz configurable por `WREKKER_FASTLOAD_CACHE` |
| `wrekker/ui/main_window.py` | rutas configurables y propagación de key reference a WREKKED |

---

## 21. WREKKED como browser único — Library integrada

### Cambio de navegación

La UI deja de presentar `LIBRARY` y `WREKKED` como pestañas separadas cuando
`PreparedDB` está disponible. `MainWindow` ahora monta WREKKED como browser único
y le inyecta también `LibraryDB`.

Library pasa a ser una subcategoría dentro del panel izquierdo de WREKKED:

```
▾ SETS
  All Prepared
  Recently Added
  TRVL 9
  Warmup
  Peak Time

▾ LIBRARY
  All Library
    Carpeta A
    Carpeta B
    ...
  Playlists
    TRVL 9
```

Esto reduce el cambio mental entre “buscar música” y “gestionar performance
sets”: todo ocurre dentro de la misma superficie WREKKED.

`SETS` y `LIBRARY` son encabezados colapsables. Al hacer click alternan entre
`▾` expandido y `▸` colapsado. `SETS` siempre aparece primero porque representa
la biblioteca local de performance; `LIBRARY` queda debajo como fuente general
para buscar y preparar material.

### Playlists como sets WREKKED

WREKKED detecta playlists `.m3u` y `.m3u8` dentro de las raíces de Library y las
muestra bajo `LIBRARY → Playlists`.

El menú contextual de una playlist ofrece:

- `Import Playlist as WREKKED Set`
- `Reveal Playlist File`

Al importar:

1. se crea un set WREKKED con el nombre de la playlist
2. las pistas ya preparadas se agregan inmediatamente
3. las pistas sin `.wrk` pero con source disponible se envían a `PrepareWorker`
4. al terminar la preparación, `MainWindow` inserta los `.wrk` resultantes en el set
5. las entradas duplicadas se omiten y se reportan en el estado de la UI

Las rutas relativas dentro de `.m3u/.m3u8` se resuelven contra la carpeta de la
playlist.

### Duplicados

`prepared_set_tracks` mantiene una restricción `UNIQUE(set_id, wrk_id)`, pero la
UI también evita duplicados antes de escribir:

- se deduplican rutas repetidas dentro de playlists
- se saltan tracks cuyo `wrk_id` ya está en el set destino
- para tracks aún no preparados, se calcula el futuro `wrk_id` con
  `wrk_id_for(source_path)` y se evita mandarlos a procesar si ya existen en el set
- al terminar `PrepareWorker`, `MainWindow` vuelve a consultar los `wrk_id`
  existentes antes de insertar resultados

Los flujos `Add to Set`, `Import Playlist as WREKKED Set` e `Import Scope`
reportan cuántos duplicados fueron omitidos.

### Rescan Library y revisión de duplicados

El botón `RESCAN` es contextual:

- si el usuario está en `SETS`, ejecuta `WrekkedScanner`
- si el usuario está en `LIBRARY`, ejecuta `LibraryScanner` sobre la carpeta
  seleccionada o sobre las raíces de Library

Al terminar un rescan de Library, WREKKED llama:

```python
LibraryDB.find_duplicates()
```

La detección es conservadora y metadata-based:

```
normalized_artist + normalized_title + rounded_duration_s
```

No se hashean archivos de audio completos durante el scan para mantener la UI
rápida.

Si hay duplicados, se abre `DuplicateLibraryDialog`:

- muestra grupos de duplicados con todas las rutas
- permite eliminar filas seleccionadas de `LibraryDB` sin borrar audio del disco
- permite `Keep First in Each Group`
- permite mandar duplicados seleccionados a un set WREKKED, usando el mismo flujo
  prepare-and-add para los que no tengan `.wrk`

Esto deja la decisión en el DJ: conservar varias copias, limpiar solo el índice
o convertir una selección en material preparado.

### Filas virtuales de Library dentro de WREKKED

Cuando el usuario selecciona `All Library` o una carpeta de Library, WREKKED
consulta `LibraryDB` y convierte cada `LibraryTrack` en una fila virtual
compatible con la tabla WREKKED.

Estados:

| Estado | Significado |
|--------|-------------|
| `WRK` / `FASTLOAD` | el track de Library ya tiene `.wrk` listo |
| `NO WRK` | existe en Library, pero todavía no fue preparado |
| `BROKEN` | el registro preparado existe pero el `.wrk` no está usable |

Las filas `NO WRK` pueden cargarse desde el source original o prepararse desde
el menú contextual.

### Carga desde Library integrada

`WrekkedWidget` emite dos señales distintas:

```python
load_wrk_track(deck_id, wrk_path)      # pista preparada/local
load_source_track(library_track, deck) # pista normal de Library
```

`MainWindow` conecta `load_source_track` al mismo path que usaba `LibraryWidget`,
es decir `Transport.load_track(deck_id, track.path)`.

### Preparar y añadir a set

`WrekkedWidget` puede emitir:

```python
prepare_library_tracks(list[LibraryTrack], set_id)
```

Si `set_id` es `0` o `None`, solo prepara los tracks. Si `set_id` apunta a un
set WREKKED, `MainWindow._on_prepare_tracks()` ejecuta `PrepareWorker` y, al
recibir `all_done`, busca cada `.wrk` en `PreparedDB` y lo inserta en
`prepared_set_tracks`.

Esto habilita el flujo:

```
Library track sin .wrk
→ Prepare .wrk
→ construir fastload
→ upsert PreparedDB
→ añadir automáticamente al set WREKKED seleccionado
```

### Búsqueda de biblioteca general en Manage WREKKED Library

`ManageWrekkedLibraryDialog` ahora recibe opcionalmente `LibraryDB` y muestra una
banda superior `GENERAL LIBRARY`:

```
GENERAL LIBRARY [ Search full library...                 ] [ Add to Set ] [ Import Scope ]
Scope: All Library / Folder / Playlist
Title · Artist · BPM · Key · Prepared · Source
```

La búsqueda consulta `LibraryDB.search(SearchQuery(...))`, filtra por el alcance
seleccionado cuando corresponde, y cruza resultados con
`PreparedDB.get_batch_status()`.

El selector de alcance permite explorar:

- All Library
- carpetas de Library
- playlists `.m3u/.m3u8`

Al pulsar `Add to Set`:

- los tracks con `.wrk` listo se agregan inmediatamente al set seleccionado
- los tracks sin `.wrk` pero con source disponible emiten `prepare_and_add_tracks`
- los tracks sin source disponible se omiten

El manager no crea entradas rotas: si un track no preparado no puede procesarse
porque el source no existe, no se añade al set.

Si el alcance seleccionado es una playlist, `Import Scope` importa la playlist
completa como un nuevo set WREKKED y procesa automáticamente las pistas que aún
no tengan `.wrk`.

### Limpieza de `.wrk` corruptos

`WrekkedScanner` valida que cada `.wrk` pueda abrirse como ZIP y que tenga
`manifest.json`. Si un `.wrk` no puede inspeccionarse:

- se mueve el archivo a `_corrupt_wrks/` dentro de la raíz WREKKED
- se eliminan sus referencias en `prepared_tracks` y `prepared_set_tracks`
- `WrekkedWidget.on_rescan_done()` informa cuántos `.wrk` corruptos fueron puestos en cuarentena

Esto evita que la biblioteca quede poblada por entradas `BROKEN` imposibles de
cargar sin destruir el archivo original de forma irreversible. Si aún existe un `.wrk` roto enlazado desde la UI, `Delete .wrk` queda
habilitado mientras el archivo exista, aunque `wrk_ready` sea falso.

`Reveal .wrk in Files` abre la carpeta contenedora del `.wrk`. La salida de
`xdg-open` se redirige para evitar ruido de consola como warnings de icon theme
(`kf.iconthemes: Icon theme "hicolor" not found`), que no indican un fallo de
Wrekker.

### Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `wrekker/ui/main_window.py` | WREKKED como browser único, Library integrada, prepare-and-add al terminar worker |
| `wrekker/ui/widgets/wrekked.py` | sección `LIBRARY`, playlists importables, filas virtuales de Library, carga source, prepare-and-add |
| `wrekker/ui/widgets/wrekked_manage_dialog.py` | búsqueda/exploración de Library por alcance, import de playlist y `Add to Set` con preparación automática |
| `wrekker/library/database.py` | detección de duplicados y eliminación de filas duplicadas del índice |
| `wrekker/library/wrekked_scanner.py` | cuarentena automática de `.wrk` corruptos durante rescan |

---

## 22. PrepareDialog — pause, cancel y cierre cooperativo

`PrepareDialog` corre sobre `PrepareWorker` en un `QThread`. El diálogo ahora
trata explícitamente los tres controles de usuario:

| Control | Comportamiento |
|---------|----------------|
| Pause | llama `worker.pause()` y cambia a `Resume` |
| Resume | llama `worker.resume()` |
| Cancel | llama `worker.cancel()`, desactiva pause y cambia el botón a `Close` |
| X / Close window | solicita cancelación y acepta el cierre de la ventana |

`PrepareWorker` mantiene cancelación cooperativa: no mata una operación nativa o
CPU-bound a mitad de llamada, pero hace checkpoints entre fases:

```python
self._checkpoint()
```

Los checkpoints respetan pausa y cancelación antes/después de:

- carga de audio
- análisis BPM/key/waveform
- separación de stems
- cálculo de stem energy
- empaquetado `.wrk`
- fastload cache

Si se cancela durante una preparación, el track actual se marca como `SKIP` y el
worker sale del loop. En stems, el `cancel_event` se sigue pasando al modelo para
interrumpir cuando el backend lo soporte.

---

## 23. Correcciones de integridad de Library y WREKKED

### Rescan de Library con archivos modificados

`LibraryTrack.id` sigue representando `path + mtime_ns`, pero `LibraryDB` ahora
hace `UPSERT` por `path`. Si un archivo ya indexado cambia en disco, el rescan
actualiza la fila existente con el nuevo `id` y la metadata nueva en lugar de
fallar por `UNIQUE(path)`.

Los filtros por raíz en `known_ids()` y `remove_missing()` ahora usan:

```sql
path = root OR path LIKE root || '/%'
```

Así se evita mezclar raíces hermanas como `/Music/A` y `/Music/AB`.

### Búsqueda scoped

`LibraryDB.search_in_folder()` permite buscar dentro de una carpeta sin cargar
toda la carpeta en memoria. WREKKED y Manage WREKKED usan esta ruta cuando el
scope activo es una carpeta. En playlists, la búsqueda se limita a los tracks de
la playlist.

### Duplicados y prepare

`MainWindow._on_prepare_tracks()` deduplica el batch antes de lanzar
`PrepareWorker` usando el futuro `wrk_id`. Si el prepare apunta a un set WREKKED,
también omite tracks que ya existen en ese set.

`PreparedDB.copy_tracks_to_set()` ahora agrega tracks al final del set y omite
`wrk_id` existentes. `move_tracks_to_set()` es no-op si origen y destino son el
mismo set, evitando borrados accidentales.

### Rescan results

Después de un rescan de Library, WREKKED muestra un resumen con:

- tracks encontrados
- procesados
- nuevos/actualizados
- skipped
- errores
- grupos duplicados

Si hay duplicados, se abre después el diálogo de resolución.

### Thread-safety UI

El `LibraryWidget` legacy ya no modifica widgets desde callbacks del thread de
scanner. Los callbacks emiten señales Qt y la UI se actualiza en el hilo
principal.

En `ManageWrekkedLibraryDialog`, los refrescos post-fastload y el filtro
`No Fastload Cache` también vuelven al hilo de UI por señales, no por
`QTimer.singleShot()` lanzado desde threads de fondo.

---

## 24. Beatmatching state of the art — Beat This, Rubber Band, PhaseSync y PhraseLock

Esta sección documenta el sistema moderno de beatmatching integrado en Wrekker.
El objetivo es que el DJ tenga sync usable en mezcla real, no solo BPM matching:

- Beat tracking offline con Beat This!.
- Beatgrid schema v2 dentro del `.wrk`.
- Time stretching preparado en Rust con Rubber Band.
- Phase-locked loop en Rust para mantener fase beat-a-beat.
- Phrase-locked sync para alinear puntos musicales de 8/16 compases.
- Phrase meter visual a 60 Hz.

### 24.1 Beat Tracking Offline

Archivo principal:

```text
wrekker/analysis/beat_tracker.py
```

`BeatTracker` usa Beat This! como modelo offline. Corre durante WREKKED, no en
tiempo real.

```python
grid = BeatTracker().analyze(audio_path)
```

Retorna:

| Campo | Significado |
|-------|-------------|
| `bpm` | BPM global estimado |
| `bpm_variable` | `True` si el tempo varía más de 2 % |
| `beats` | timestamps de beats en segundos |
| `downbeats` | timestamps de downbeats |
| `confidence` | confianza global 0.0–1.0 |
| `phrase_markers` | inicios de frase, longitud 8/16 compases y energía |
| `beat_period_ms` | periodo promedio de beat |
| `swing_factor` | desviación media respecto a grid rígido |
| `analysis_model` | `beat_this_v1` |
| `low_confidence` | flag cuando `confidence < 0.6` |

Si el `.wrk` ya contiene `analysis/beatgrid.json` con `schema_version >= 2`,
`PrepareWorker` respeta el fastload cache y no reanaliza. Si detecta un schema
viejo, marca el track para reanálisis.

`WrekkedScanner` aplica el mismo criterio durante RESCAN. Al registrar un `.wrk`
existente, lee `analysis/beatgrid.json`; si falta o tiene schema anterior a v2,
la fila de `PreparedDB` queda con `analysis_status='outdated'` y un warning de
upgrade. En la UI WREKKED, el menú contextual permite lanzar la actualización a
schema v2 para los `.wrk` seleccionados cuyo source todavía existe.

### 24.2 Beatgrid Schema V2

Dentro del ZIP `.wrk`:

```text
analysis/beatgrid.json
```

Ejemplo:

```json
{
  "schema_version": 2,
  "model": "beat_this_v1",
  "bpm": 128.0,
  "bpm_variable": false,
  "confidence": 0.94,
  "swing_factor": 0.12,
  "beat_period_ms": 468.75,
  "beats": [0.234, 0.703, 1.171],
  "downbeats": [0.234, 2.109],
  "phrase_markers": [
    {"position_sec": 0.234, "phrase_length": 8, "energy_level": 0.65}
  ],
  "analyzed_at": "2026-05-20T07:54:33Z"
}
```

`wrekker/core/transport.py` convierte ese dict a `wrekker.core.deck.BeatGrid`.
La UI y el sync usan esa representación estable.

### 24.3 Rubber Band Time Stretch Wrapper

Archivo principal:

```text
wrekker/engine_rs/src/time_stretch.rs
```

Wrekker expone `NativeTimeStretch` por PyO3:

```python
from wrekker_engine import NativeTimeStretch

ts = NativeTimeStretch(44100, 2, "faster", 10.0)
ts.set_time_ratio(1.01)
ts.set_pitch_semitones(0.0)
out = ts.process(input_interleaved)
```

Modos:

| Modo | Uso | Latencia |
|------|-----|----------|
| `Faster` | playback, key shift, sync en vivo | objetivo 10 ms |
| `Finer` | render offline, stems, export | sin límite estricto |

Características:

- Ratio 0.5x–2.0x.
- Pitch ±12 semitonos.
- Preservación de formantes para vocals.
- Backend Rubber Band vía FFI cuando `librubberband` está disponible.
- Fallback passthrough explícito si no se encuentra la librería.

El procesamiento sigue estando en Rust. Python solo configura parámetros.

### 24.4 PhaseSync en Rust

Archivo principal:

```text
wrekker/engine_rs/src/phase_sync.rs
```

`PhaseSync` implementa un PLL musical:

```rust
pub struct PhaseSync {
    master_bpm: f64,
    slave_bpm: f64,
    phase_error: f64,
    kp: f64,
    dead_zone_beats: f64,
    max_correction_rate: f64,
}
```

API expuesta a Python:

```python
from wrekker_engine import NativePhaseSync

pll = NativePhaseSync(kp=0.35, dead_zone_beats=0.02, max_correction_rate=2.0)
ratio = pll.update_phase_error(slave_minus_master, master_bpm, slave_bpm, dt)
```

Semántica:

- Error positivo: el slave está adelantado; el PLL lo desacelera.
- Error negativo: el slave está atrasado; el PLL lo acelera.
- Errores dentro de `dead_zone_beats` no producen corrección.
- `max_correction_rate` limita la agresividad en semitonos/segundo.

Tests:

```text
phase_sync::tests::test_pll_convergence
phase_sync::tests::test_dead_zone
phase_sync::tests::test_snap_to_grid
```

### 24.5 Integración de `Transport`

`Transport.sync(deck_id)` hace:

1. Elegir master.
2. Calcular `sync_rate = master_bpm / follower_native_bpm`.
3. Aplicar playback rate.
4. Si hay phrase markers, llamar `PhraseLockSync.snap_slave_to_phrase()`.
5. Hacer snap fino de fase.
6. Activar `_FollowerSync` con `_SyncPLL`.

`_SyncPLL` conserva fallback Python, pero cuando `NativePhaseSync` está
disponible delega la corrección a Rust.

`tick_sync(dt)` se llama en el loop visual de 60 Hz. No procesa audio: solo lee
posiciones atómicas y actualiza `playback_rate` del follower. La aplicación real
del rate ocurre dentro del callback Rust.

### 24.6 Phrase-Locked Sync

Archivo:

```text
wrekker/sync/phrase_sync.py
```

`PhraseLockSync` no toca audio. Solo calcula posiciones musicales:

```python
sync = PhraseLockSync()
target = sync.snap_slave_to_phrase(master_state, slave_state)
progress = sync.phrase_progress_fraction(deck_state, position_s)
locked = sync.is_phrase_locked_at(master, master_pos, slave, slave_pos)
```

Fuentes de frase, por orden:

1. `BeatGrid.phrase_markers`.
2. `BeatGrid.downbeats`, agrupados cada 8 compases.
3. `BeatGrid.beats`, fallback cada 32 beats.
4. Grid regular por BPM si no hay beats explícitos.

Esto permite alinear:

- Beat 3 de frase master con beat 3 de frase slave.
- Drops con drops.
- Builds con builds.
- Breakdowns con entradas correctas.

### 24.7 Phrase Meter Visual

Archivos:

```text
wrekker/ui/widgets/deck.py
wrekker/ui/main_window.py
```

Cada deck tiene un `PhraseMeterWidget` compacto. Se actualiza en el path de
60 Hz con posiciones reales leídas desde el engine Rust.

Estados:

| Color | Estado |
|-------|--------|
| Verde | phrase-locked |
| Amarillo | beat-locked, frase desalineada |
| Rojo | sin sync |
| Gris | sin beatgrid / idle |

El cálculo del estado usa:

- `sync_enabled`.
- `sync_phase_error`.
- `PhraseLockSync.is_phrase_locked_at(...)`.

No se guarda en `DeckState`; es un estado visual derivado para evitar reconstruir
todos los snapshots a 60 Hz.

### 24.8 Scratch, Crossfader y Hot Path

Cambios relacionados en Rust:

- Scratch usa Hermite cúbico y suavizado de rate.
- Hard flick de jog wheel funciona incluso si el deck está pausado.
- Crossfader, master gain, channel gain y pregain tienen rampas sample-a-sample.
- Hay limiter final para evitar clipping cuando dos tracks full-scale coinciden.
- El overlay de beats no se borra al pausar, cambiar stems o mover EQ.

Regla de arquitectura:

> Toda modificación de audio en tiempo real debe estar en Rust. Python solo
> configura estado, prepara datos o actualiza UI.

### 24.9 Tests y Verificación

Python:

```bash
python -m pytest -q tests/test_beat_tracker.py tests/test_phrase_sync.py
```

Rust:

```bash
cd wrekker/engine_rs
cargo fmt --check
cargo check
cargo test
cargo build --release
```

Smoke de PyO3:

```bash
python -c "from wrekker_engine import NativeTimeStretch, NativePhaseSync; print(NativeTimeStretch(44100,2,'faster',10).rubberband_active); print(NativePhaseSync().is_locked)"
```

---

---

## 25. Detalles de implementación adicionales

### 25.1 Audio resampling — `_resample_audio` y `_resample_stem_result`

El engine Rust opera a su propio sample rate (configurado en `AudioEngine`). Si el archivo fuente tiene un SR diferente, `transport.py` lo convierte antes de enviarlo:

```python
def _resample_audio(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    # Usa scipy.signal.resample_poly con GCD para ratio exacto
    # Fallback: interpolación lineal por canal si scipy no está disponible
```

```python
def _resample_stem_result(result: StemResult, dst_sr: int) -> StemResult:
    # Detecta src_sr desde result.duration_s y shape del stem
    # Resamplea cada stem al engine SR antes de cargar en Rust
```

El resampling ocurre en el hilo daemon de carga, nunca en el callback CPAL.

### 25.2 Zoom waveform — `_compute_zoom_peaks`

```python
_ZOOM_CHUNK = 256   # samples por columna de zoom (~172 cols/s @ 44100 Hz)

def _compute_zoom_peaks(audio, sr) -> (zoom_peaks, zoom_colors):
    # Mismo pipeline que los peaks generales pero con chunk=256
    # zoom_peaks:  (M,) float32 — max |amplitude| por bloque de 256
    # zoom_colors: (M, 3) uint8 — color espectral batch-FFT sobre chunk=256
```

Se llama una vez al cargar el audio original y una vez tras el decode FLAC en carga `.wrk`. El `ZoomWaveformWidget` usa estos arrays para dibujar la ventana de ~4 s centrada en el playhead a 60 Hz.

### 25.3 FX completo — `FXState` y API de Transport

**`FXState` (frozen dataclass):**

```python
FXState(
    enabled:       bool   = False,
    fx_type:       int    = 0,      # índice en FX_NAMES
    target:        int    = 0,      # FX_TARGET_A | FX_TARGET_B | FX_TARGET_BOTH
    wet:           float  = 0.8,
    depth:         float  = 0.5,
    feedback:      float  = 0.5,
    time_division: float  = 0.5,    # fracción en FX_TIME_DIVISIONS
    color:         float  = 0.0,
    fx_bank:       str    = "normal",

    # Estado separado del banco WREKK FX
    wrekk_enabled: bool   = False,
    wrekk_fx_type: int    = 0,
    wrekk_target:  int    = FX_TARGET_A,
    wrekk_stem_target: int = 1,
    wrekk_wet:     float  = 0.8,
    wrekk_depth:   float  = 0.5,
    wrekk_feedback: float = 0.5,
    wrekk_time_division: float = 0.5,
    wrekk_color:   float  = 0.0,
    wrekk_stems_ready: bool = True,
    wrekk_stems_status: str = "",
)
```

**Constantes:**

```python
FX_TARGET_A    = 0
FX_TARGET_B    = 1
FX_TARGET_BOTH = 2

FX_NAMES = ["Filter", "Echo", "Delay", "Reverb", "Flanger",
            "Phaser", "Bitcrusher", "Roll", "Trans", "Noise"]

FX_BANK_NORMAL = "normal"
FX_BANK_WREKK  = "wrekk"

WREKK_FX_NAMES = [
    "VOCAL GHOST", "TOP WASH", "DRUM CRUSH", "RHYTHM GATE",
    "STEM ROLL", "BASS LOCK", "DECONSTRUCT", "REBUILD",
]

WREKK_STEM_TARGETS = [
    ("VOC", 0), ("DRM", 1), ("BSS", 2), ("OTH", 3),
    ("TOP", 4), ("RHYTHM", 5),
]

FX_TIME_DIVISIONS = [
    ("1/16", 0.0625), ("1/8", 0.125), ("1/4", 0.25),
    ("1/2", 0.5),     ("1",   1.0),   ("2",   2.0), ("4", 4.0),
]
```

**API de Transport:**

| Método | Descripción |
|--------|-------------|
| `get_fx_state()` | → `FXState` actual |
| `set_fx_enabled(enabled)` | On/off del rack de FX |
| `set_fx_bank(bank)` | Cambia banco activo (`normal` / `wrekk`) sin perder estado del otro banco |
| `set_fx_type(fx_type)` | Cambia efecto por índice |
| `set_fx_target(target)` | Deck A, B o ambos |
| `set_fx_wet(wet)` | Mix seco/húmedo |
| `set_fx_depth(depth)` | Profundidad del efecto |
| `set_fx_feedback(feedback)` | Retroalimentación (para Echo/Delay/Flanger) |
| `set_fx_time_division(td)` | Fracción de beat para efectos rítmicos |
| `set_fx_color(color)` | Parámetro de color/tono del efecto |
| `set_wrekk_fx_type(fx_type)` | Selección de WREKK FX |
| `set_wrekk_fx_target(target)` | Target A/B/Both para WREKK FX |
| `set_wrekk_fx_stem_target(target)` | Target VOC/DRM/BSS/OTH/TOP/RHYTHM para `STEM ROLL` |
| `set_wrekk_fx_wet/depth/feedback/time_division/color(...)` | Parámetros del banco WREKK FX |

Todos los setters escriben en `self._fx_state` (Python mirror) y llaman al método
correspondiente de `engine`. El banco normal usa `engine.fx_*`; WREKK FX usa
`engine.wrekk_fx_*`.

#### Banco WREKK FX

WREKK FX es un banco separado del banco normal. Cambiar a `wrekk` no renombra,
sobrescribe ni resetea el banco normal: selección, target, wet, depth, feedback,
beat division y color del banco normal quedan preservados. Al volver a
`normal`, se restaura el estado normal previo. Solo el banco activo se aplica al
motor.

WREKK FX manipula la anatomía del track y requiere stems. Si un target no tiene
stems ready, Transport desactiva la aplicación del efecto para ese target y el
UI muestra `STEMS REQUIRED`. Con target `Both`, si solo un deck tiene stems,
puede aplicarse al deck listo y mostrar advertencia parcial.

Capas de stem:

| Capa | Stems |
|------|-------|
| `VOC` | vocals |
| `DRM` | drums |
| `BSS` | bass |
| `OTH` | other |
| `TOP` | vocals + other |
| `RHYTHM` | drums + bass |

Efectos iniciales:

| WREKK FX | Procesa | Resumen |
|----------|---------|---------|
| `VOCAL GHOST` | vocals | Reduce vocal dry y deja cola delay/ghost beat-synced |
| `TOP WASH` | vocals + other | Lava capa top con delay/wash sin tocar rhythm |
| `DRUM CRUSH` | drums | Bitcrush/distorsión controlada solo en drums |
| `RHYTHM GATE` | drums + bass | Gate rítmico BPM-aware sobre rhythm |
| `STEM ROLL` | VOC/DRM/BSS/OTH/TOP/RHYTHM | Loop roll de una capa mientras el resto sigue |
| `BASS LOCK` | bass-centered | Mantiene/enfatiza bass y reduce capas no-bass |
| `DECONSTRUCT` | all stems | Desarma el track por capas según depth/color |
| `REBUILD` | all stems | Reconstruye el track por capas según depth/color |

El DSP de WREKK FX corre en Rust dentro de `DeckAudioState`, en el render por
stems antes de sumar el deck. El FX normal sigue corriendo en `FxProcessor`
sobre el deck completo después del EQ. Esta separación mantiene intacto el
banco normal y evita DSP Python en el callback.

Modelo de ganancia:

```python
final_stem_gain =
    manual_stem_gain
    * wrekk_macro_multiplier
    * wrekk_fx_multiplier
```

Los efectos que añaden señal húmeda (`VOCAL GHOST`, `TOP WASH`, `STEM ROLL`)
mezclan el wet sobre el stem ya escalado por las capas anteriores. Desactivar
WREKK FX o devolver sus parámetros a neutro no modifica los faders manuales de
stems ni el macro WREKK.

### 25.4 Nuevos valores de `StemStatus`

Durante la carga staged desde `.wrk`, el campo `DeckState.stems_status` usa estos valores adicionales (además de los clásicos `none/queued/analyzing/ready`):

| Valor | Situación |
|-------|-----------|
| `StemStatus.WRK_LOADING` | `.wrk` encontrado, cargando metadata (Phase 1) |
| `StemStatus.MIX_READY` | Mix cargado en engine; stems aún no disponibles |
| `StemStatus.STEMS_LOADING` | Stems leyéndose desde fastload o ZIP (Phase 3 background) |
| `StemStatus.FAILED` | Error irrecuperable leyendo stems |

### 25.5 Soporte 4 decks — `DeckID`

`DeckID` es un enum de cadenas con valores `A`, `B`, `C`, `D`. Los decks C y D están preparados para una expansión futura a 4 decks:

```python
class DeckID(str, Enum):
    A = "A"
    B = "B"
    C = "C"   # reservado — futura expansión a 4 decks
    D = "D"   # reservado — futura expansión a 4 decks
```

La UI y Transport actual solo instancian decks A y B.

### 25.6 Waveform — fase de carga completa actualizada

```
Al cargar audio original    → peaks + colors + zoom_peaks + zoom_colors  (~200 ms)
Al detectar BPM (librosa)   → beats + BeatGrid                           (~5 s)
Al separar stems (HTDemucs) → stem_energy                                (~1–40 s)
Desde .wrk (cache HIT)      → todo en Phase 1 desde disco plano          (< 50 ms)
Desde .wrk (cache MISS)     → Phase 1 desde ZIP + zoom_peaks en Phase 2  (< 500 ms)
```

Cada fase incrementa `pb.waveform_seq`. La UI detecta el cambio a 60 Hz y repinta sin bloquear.

### 25.7 Índice del módulo — nuevos `__all__` en Transport

```python
__all__ = [
    "Transport",
    "FXState",
    "MonitorCueState",
    "FX_BANK_NORMAL",
    "FX_BANK_WREKK",
    "FX_NAMES",
    "WREKK_FX_NAMES",
    "WREKK_STEM_TARGETS",
    "FX_TARGET_A",
    "FX_TARGET_B",
    "FX_TARGET_BOTH",
]
```

`MonitorCueState` y `FXState` son los tipos que la UI y el hardware importan desde `wrekker.core.transport`.

---

*Wrekker — construido para DJs que también son ingenieros.*

---

## 26. Packaging, distribución y primer arranque

Wrekker separa el instalador base de los componentes AI pesados. El binario
distribuido contiene la aplicación, UI, engine Rust y dependencias ligeras; en
el primer arranque `FirstTimeWizard` revisa `models_dir()` y ofrece instalar:

- PyTorch CPU / torchaudio.
- HTDemucs y sus pesos.
- Beat This! y su checkpoint.

Si el usuario cancela, la aplicación entra en modo degradado. En ese modo los
`.wrk` preparados siguen cargando y reproduciendo, pero la separación nueva de
stems y Beat This! quedan deshabilitados hasta completar el setup.

### 26.1 Rutas centralizadas

Todo código nuevo debe usar `wrekker.config.paths`:

- `config_dir()` / `settings_file()` para configuración persistente.
- `data_dir()` para biblioteca, PreparedDB y recursos de usuario.
- `models_dir()` para modelos descargados por el wizard.
- `fastload_dir()` para fastload.
- `smb_config_file()` para credenciales SMB locales, nunca versionadas.

En Linux se respetan `XDG_CONFIG_HOME`, `XDG_DATA_HOME` y `XDG_CACHE_HOME`.
En Windows se usan `%APPDATA%` y `%LOCALAPPDATA%`.

### 26.2 Seguridad de configuración pública

`wrekker/config/default_settings.json` es el único template de configuración
commiteado. Todos los campos sensibles están vacíos.

`wrekker/config/sanitize.py` provee:

- `sanitize_settings()`
- `sanitize_settings_file()`
- `assert_no_sensitive_data()`

`packaging/check-sensitive-data.py` corre como primer job del workflow de
release. Si detecta credenciales SMB, hosts/IPs, usuario local en rutas o
configs no sanitizadas, la release se bloquea antes de construir artefactos.

### 26.3 Canales de distribución

- Flatpak: `packaging/flatpak/io.github.wrekker.Wrekker.yml`.
- AppImage Linux: `packaging/appimage/build-appimage.sh`.
- Windows beta: `packaging/windows/build-windows.ps1` +
  `packaging/windows/installer.nsi`.
- GitHub Releases: `.github/workflows/release.yml`, ejecutado en tags
  `v*.*.*`.

Windows usa CPAL/WASAPI por defecto. `rubberband.dll` y VC Redistributable se
incluyen en el flujo del instalador. ASIO queda documentado como futuro/experimental
hasta que se valide con drivers Pioneer.
