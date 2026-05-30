# Changelog

Registro de cambios realizados por Codex en este workspace.

## 2026-05-29

### Packaging, distribución y First-Time Wizard

- Añadido `wrekker.config.paths` para resolver config/data/cache/modelos de
  forma cross-platform en Linux y Windows.
- Añadido template público sanitizado `wrekker/config/default_settings.json`.
- Añadido `wrekker/config/sanitize.py` con sanitización y assert de datos
  sensibles antes de publicar.
- Añadido `packaging/check-sensitive-data.py` y workflow de release que lo
  ejecuta como primer gate.
- Añadido First-Time Setup Wizard para instalar PyTorch CPU, HTDemucs y Beat
  This! fuera del instalador base, con modo degradado si se cancela.
- Añadidos artefactos de distribución: Flatpak, AppImage, Windows NSIS y
  GitHub Actions para tags `v*.*.*`.
- Movidas dependencias AI pesadas a extra opcional `wrekker[ai]` para que el
  instalador base no cargue PyTorch/Demucs/Beat This! por defecto.
- Reemplazados hardcodes runtime de rutas Wrekker por helpers centralizados.

## 2026-05-28

### WREKKED sets — orden persistente de performance

- El scanner WREKKED deja de reordenar carpetas `.wrk` alfabeticamente durante
  rescan y ya no pisa `prepared_set_tracks.position` si el track ya existe.
- Importar sets desde playlist/carpeta conserva el orden recibido y agrega
  tracks al final del set con `next_set_position()`.
- Las vistas de carpeta de Library usan orden de escaneo/servidor (`added_at`,
  `rowid`) en vez de ordenar por path antes de crear/importar sets.
- El menú contextual de pistas en WREKKED permite `Move Up in Set` y
  `Move Down in Set` para reordenar sets `.wrk`/fastloadables sin abrir otro
  flujo.
- Manage WREKKED Library agrega `Edit Set Order...` en el menú de sets.
- Añadida regresión para asegurar que un rescan no destruye el orden manual.

### WREKK Stem Horizon

- Añadido `StemActivityTimeline` bar-synchronous generado desde beatgrid,
  frases y `stem_energy` ya preparado, sin rerun de stem separation.
- Persistido Stem Horizon en `.wrk` como `analysis/stem_horizon.json` y en
  fastload como `stem_horizon.json`; tracks legacy sin Horizon siguen cargando.
- Integrado `StemHorizonWidget` en el area `STEMS` de ambos decks: cada stem
  renderiza su Horizon encima de su fader correspondiente, preservando intactas
  las lineas HUD `P` / `W` / `G`.
- Añadidos modos de visualizacion: `LED Blocks` por defecto, `Future Bars`,
  `Stem Waveforms` y `Off`.
- SETTINGS ahora expone `Stems & WREKK -> Stem Horizon`: enabled, display mode,
  rango 4/8/16/32 bars, countdown, flag W, dominancia, detalle e intensidad.
- WREKKER LAB muestra una vista read-only de Stem Horizon para inspeccionar la
  actividad full-track junto a markers W.
- Añadida flag diagnostica `WREKKER_STEM_HORIZON_DEBUG=1` y tests de generacion
  bar-aligned/persistencia `.wrk`.

### WREKKER LAB — waveform alineada con decks

- El renderer texture de WREKKER LAB ahora usa el mismo enfoque texture/pixmap
  estable de decks, con cache scale `4` y peak smoothing LAB `0` para preservar
  transientes.
- FULL MIX en LAB usa `waveform_colors` espectrales cuando estan disponibles,
  en vez de render monocromatico.
- El cache de textura LAB evita interpolacion lineal entre columnas de analisis;
  usa columnas discretas para no convertir picos en rampas/poligonos.

### WREKK Markers — arquitectura stem-aware P/W/G

- Refactorizada la jerarquia visible de Auto Markers a `P` / `W` / `G`.
  Primary queda reservado para `DROP`, `MIX IN`, `MIX OUT` y `SWITCH`.
- `WREKK` generico deja de ser Primary visible. `GHOST` pasa a ser una
  oportunidad WREKK, no un marker primario.
- Añadidos markers W estructurales: `VOCAL IN/OUT`, `BASS IN/OUT`,
  `KICK IN/OUT` y `TOP IN/OUT`.
- Añadidas oportunidades W de fase 1: `GHOST`, `DECONSTRUCT` y `REBUILD`, con
  umbral live mas estricto que los eventos estructurales.
- `AutoMarker` ahora persiste metadata enriquecida: `category`, `family`,
  `stem_targets`, `evidence`, `related_events`, `live_visibility` y `hidden`.
- El detector WREKK ahora compara ventanas beat/phrase-aligned de varios
  compases usando `stem_energy` ya preparado; no rerun stem separation.
- El HUD de deck muestra lineas `P`, `W` y `G`; `ESSENTIAL` muestra
  oportunidades W de alta confianza, mientras `PRIMARY + WREKK` expande eventos
  estructurales.
- WREKKER LAB actualiza el editor de markers a la categoria `WREKK`, muestra
  evidencia/razon en la tabla y conserva tipos legacy como review/debug.
- SETTINGS agrega controles DJ-facing para Primary/WREKK enabled, visibilidad W,
  thresholds W, grouping y politica de cooldown.
- Actualizadas pruebas para taxonomia, visibilidad live, detector W y mapeos LAB.

### Documentacion — flags y estado de ejecucion

- Actualizado `README.md` para usar el entry point real
  `python -m wrekker.ui.app` en vez de `python -m wrekker`.
- Documentadas las flags de la app: `--no-audio`, `--no-controller` y
  `--debug`, con ejemplos de uso combinado.
- Documentadas las variables de entorno implementadas para preparacion `.wrk`,
  fastload/stem cache, diagnostico de UI/waveforms y renderer QML.
- Documentadas las utilidades CLI actuales: `tools/flx4_monitor.py`,
  `tools/test_flx4.py`, `tools/test_library.py` y `tools/test_stems.py`.
- Corregida la documentacion QML para reemplazar flags planeadas por las flags
  reales implementadas: `WREKKER_WAVEFORM_RENDER_DEBUG`,
  `WREKKER_WAVEFORM_POSITION_DEBUG`, `WREKKER_WAVEFORM_FPS_LOG`,
  `WREKKER_ENABLE_QML_DECK_WAVEFORMS` y
  `WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS`.
- Añadida en `WREKKER.md` una referencia tecnica de las flags de entorno
  principales.

### Documentacion — WREKKER LAB preview real

- Corregida la descripcion de WREKKER LAB: el preview de audio y metronomo ya
  estan implementados mediante `_LabPreviewController` y `AudioEngine`.
- Documentado que el preview reproduce/pausa/detiene/hace seek, renderiza click
  sobre el beatgrid draft y soporta monitor de stems con mute/isolate.
- Aclarado que los stems del preview se cargan desde fastload cuando existe o
  desde el `.wrk` como fallback.
- Ajustados los pendientes: lo que falta es selector/routing avanzado de salida
  y CUE/latencia, no el reproductor local basico.

### Waveforms — cadencia visual

- Cambiado el intervalo solicitado del tick principal de UI de 15 ms a 8 ms.
  En este entorno Qt estaba entregando 15/16 ms como ~30 Hz, aunque el paint
  costaba menos de 1 ms.
- Cambiado el timer del zoom waveform QWidget de 15 ms a 8 ms y los timers QML
  de deck/LAB de 16 ms a 8 ms para evitar saltitos por frames alternos.
- Ajustado de nuevo el zoom/QML a timer de despertar de 1 ms con pacing interno
  a 60 FPS (`WREKKER_ZOOM_TARGET_FPS`) porque el backend seguia cuantizando 8 ms
  a ~37-43 Hz.
- Desactivado por defecto el timer propio del zoom QWidget: ahora ambos decks se
  animan desde el tick principal para que Qt pueda coalescer repaints en un solo
  flush. `WREKKER_ZOOM_OWN_TIMER=1` queda como modo de profiling aislado.
- El log `WREKKER_ZOOM_FPS_LOG=1` ahora reporta `dt_avg` y `dt_max` entre paints
  para distinguir costo de render de cadencia real.
- Reemplazado el reloj visual principal por `_UiTickThread` por defecto
  (`WREKKER_UI_CLOCK=thread`, `WREKKER_UI_TARGET_FPS=60`) para evitar que el
  `QTimer` del hilo UI quede cuantizado a ~35-40 Hz.
- Cambiado el default del reloj visual a `WREKKER_UI_CLOCK=pipe`: un thread
  escribe en un pipe y Qt despierta por `QSocketNotifier`, evitando también la
  cuantización observada con señales queued desde QThread.
- Añadidos diagnósticos `WREKKER_UI_PLATFORM_LOG=1` y
  `WREKKER_ZOOM_DISABLE_REPAINT=1` para aislar si el límite restante está en
  backing-store/compositor o en el resto del event loop.
- Añadido `WREKKER_ZOOM_CACHE_SCALE` con default `2`: el pixmap cache del zoom se
  genera con supersampling horizontal e interpolación de peaks/colores para
  reducir micro-saltos de columna cuando el compositor entrega ~40 FPS.
- Añadido `WREKKER_ZOOM_PEAK_SMOOTH` con default `3`: aplica un filtro triangular
  suave a la envolvente visual del zoom para reducir shimmer/temblor de picos
  durante scroll subpixel.
- Añadido renderer alternativo `TextureZoomWaveformWidget` en
  `wrekker/ui/widgets/texture_zoom_waveform.py`, seleccionable con
  `WREKKER_ZOOM_RENDERER=texture`. Mantiene la API del renderer actual y deja
  aislado el punto para cargar tiles/pre-render desde `.wrk`.
- `TextureZoomWaveformWidget` pasa a ser el renderer por defecto. El método
  anterior queda disponible como fallback legacy con
  `WREKKER_ZOOM_RENDERER=classic` o `WREKKER_ZOOM_RENDERER=legacy`.
- Añadidas flags específicas del renderer texture:
  `WREKKER_TEXTURE_ZOOM_CACHE_SCALE` y `WREKKER_TEXTURE_ZOOM_PEAK_SMOOTH`.
- Actualizada la documentacion de `WREKKER_UI_TICK_MS` y
  `WREKKER_ZOOM_ANIM_MS`.

## 2026-05-27

### WREKKER LAB — workspace de corrección de análisis

- Añadido paquete `wrekker.lab` con `LabEditSession`, `LabAnalysisState`,
  `AnalysisChange` y `AnalysisRevision`.
- WREKKER LAB preserva el análisis automático original dentro del `.wrk`:
  `analysis/beatgrid_auto.json` y `analysis/markers_auto.json`.
- La capa activa de performance sigue en `analysis/beatgrid.json` y
  `analysis/markers.json`.
- Añadidos `analysis/corrections.json` y `analysis/changelog.json` con revision
  persistente, timestamp, summary y cambios auditable por entidad/operación.
- Añadido guardado transaccional de `.wrk`: se escribe un ZIP temporal,
  preservando audio/stems/binarios y reemplazando solo JSON; si falla, el
  original queda intacto.
- Fastload se refresca solo en metadata de análisis (`metadata.json`,
  `beatgrid.json`, `markers.json`, `cues.json`, `loops.json`, `ready.flag`) y no
  reconstruye PCM.
- Añadida ventana standalone `WREKKER LAB` con waveform source selector,
  compare AUTO/ACTIVE, controles de grid constante, phrase tools, marker editor,
  hot cues/loops y revision history.
- Entry points añadidos: fila WREKKED, Manage WREKKED Library y menú de deck
  cuando hay un `.wrk` cargado.
- PreparedDB añade campos LAB (`analysis_revision`, `lab_status`,
  `lab_edited_at`, `hot_cue_count`, `saved_loop_count`) y WREKKED muestra badges
  LAB como `VERIFIED`, `LAB EDITED`, `GRID EDITED`, `MARKERS EDITED`,
  `CUES READY` y `DYN TODO`.
- Limitación MVP documentada: preview audio/metronome y warp-anchor dynamic
  tempo quedan preparados en UI/arquitectura, pero no conectados aún.

### Documentacion — auditoria de estado real

- Revisado el estado actual de Transport, UI, engine Rust y driver DDJ-FLX4
  contra README, WREKKER.md y mapas de hardware.
- Corregido el mapa DDJ-FLX4: PFL A/B, MST CUE, headphone mix/level, master
  volume, browse knob/press, Smart CFX/WREKK y BeatFX BEAT LEFT/RIGHT estan
  documentados como implementados.
- Actualizado el documento legado `tools/flx4_mapping copy.md` para que ya no
  contradiga el mapa canonico ni marque como TODO controles que ya existen.
- Aclarado que el driver FLX4 es bidireccional para transporte, mixer, pads,
  jog, PFL, BeatFX normal, browser/load y feedback LED/VU, pero que siguen
  pendientes SHIFT+FX para WREKK FX, delete hot cue, reverse/censor, tempo range,
  fine loop adjust y SHIFT+browse zoom.
- Aclarado WREKK mode: los labels visuales de EQ permanecen HIGH/MID/LOW; desde
  hardware EQ controla vocals/drums/bass y TRIM controla `other`, sin mover los
  sliders visuales de EQ/pregain.

### Auto Markers — HUD por jerarquía, confianza y waveform limpia

- Separado el HUD de Auto Markers en tres indicadores independientes:
  `P` primario, `S` secundario y `G` guía. Cada fila muestra su próximo marker
  alcanzable y countdown compacto, por ejemplo `P  WREKK  0:18`.
- Cada jerarquía tiene su propio LED de confianza. Amarillo representa 70% a
  <85%; verde representa >=85%; gris indica que no hay marker próximo.
- El umbral mínimo de Auto Markers queda centralizado en 70%. El detector no
  emite markers por debajo de ese valor; carga `.wrk`, fastload, regeneración y
  guardado también filtran markers <70%.
- La waveform deja de mostrar labels de Auto Markers. Los markers se dibujan
  como colitas inferiores finas para no tapar la forma de onda; los tooltips
  conservan el significado interno, posición, confianza y razón del detector.
- Actualizada la jerarquía visual: primarios `DROP`, `MIX IN`, `MIX OUT`,
  `WREKK`, `GHOST`, `SWITCH`; secundarios `VOCAL`, `RHYTHM`, `BASS`; guía
  `PHRASE`.

### WREKK FX — banco stem-aware separado

- Añadido banco `WREKK FX` separado del banco normal. El banco normal conserva
  sus efectos, selección, parámetros y comportamiento existentes.
- Añadido selector de banco en el panel FX (`NORMAL` / `WREKK FX`) y UI de
  pruebas para seleccionar WREKK FX, target A/B/Both, parámetros y target de
  stem/layer para `STEM ROLL`.
- Añadidos ocho WREKK FX stem-aware en Rust: `VOCAL GHOST`, `TOP WASH`,
  `DRUM CRUSH`, `RHYTHM GATE`, `STEM ROLL`, `BASS LOCK`, `DECONSTRUCT` y
  `REBUILD`.
- El DSP de WREKK FX corre en Rust dentro del render por stems, antes de sumar
  el deck. No hay DSP Python en el audio path.
- WREKK FX requieren stems. Si el target no tiene stems, el UI muestra
  `STEMS REQUIRED` y no procesa el mix completo como sustituto.
- `FXState` ahora conserva estado separado para banco normal y WREKK FX. Cambiar
  de banco no pierde selección ni parámetros del otro banco; solo el banco activo
  se aplica al motor.
- Preparada API de Transport y PyO3 para futuro mapeo SHIFT + FX del DDJ-FLX4,
  sin cambiar el mapping MIDI existente.

### WREKK mode y visuales

- Los labels visuales de EQ permanecen siempre `HIGH`, `MID`, `LOW`. Activar
  WREKK mode ya no cambia los títulos a stems; los faders dedicados de stems
  siguen siendo la superficie visual para stems.
- Mejorada la claridad de los osciloscopios A/M/B con colores explícitos:
  Deck A cyan/azul, Deck B magenta/rojo y Master blanco/gris.

## 2026-05-21

### UI de performance — zoom suave, medidores y próximo auto marker

- Añadida jerarquía visual de Auto Markers con modo por defecto `ESSENTIAL`:
  markers primarios (DROP, MIX IN/OUT, WREKK, GHOST, SWITCH), secundarios
  (VOCAL, RHYTHM, BASS) y guía (PHRASE) quedan separados visualmente.
- El botón `MKRS` prepara modos `OFF`, `ESSENTIAL`, `FULL` y `DEBUG` desde menú
  contextual.
- Restaurada claridad de los osciloscopios A/M/B con fondo más oscuro, borde y
  centro sutiles, trazo con mayor contraste y normalización visual estable.
- `MainWindow._tick()` mantiene el path de playhead/zoom waveform a 60 Hz y
  separa spectrum, peak meters, mini meters y osciloscopios en una ruta visual
  dedicada de ~45 Hz para conservar fluidez sin meterlos en el path pesado de
  métricas/LUFS (~10 Hz).
- `DeckWidget` muestra debajo del overview waveform los próximos markers
  alcanzables por jerarquía (`P`, `S`, `G`). Si hay loop activo, no muestra
  markers fuera del loop.
- Añadidos LEDs de confianza por jerarquía; el tooltip conserva tipo, posición,
  confianza y razón del detector.
- `ZoomWaveformWidget` conserva el render con pixmap cache y añade métricas
  opcionales de FPS/pintado con `WREKKER_ZOOM_FPS_LOG=1`.

### Preparación .wrk — empaquetado por artefactos

- `PrepareWorker` genera FLAC de mix y stems como artefactos paralelos antes del
  empaquetado final; `create_wrk()` ahora puede ensamblar esos bytes ya listos.
- Los FLAC y binarios grandes dentro del ZIP `.wrk` usan `ZIP_STORED`, evitando
  recomprimir audio ya comprimido. Solo JSON pequeño usa deflate.
- Añadidos modos `WREKKER_PREPARE_MODE=fast|balanced|archive`, knobs de threads
  y compresión FLAC, desglose de timings `packing.*`, logging de tamaños `.wrk`
  + fastload + total, y limpieza de temporales de stem cache creados durante
  preparación.

### Auditoría de documentación — comparación código vs docs

Revisión completa del código fuente contra `WREKKER.md`, `changelog.md` y
`README.md`. Detectados y documentados los siguientes elementos que existían en
el código pero no estaban reflejados en la documentación técnica:

**Modelos de datos (`deck.py`):**
- `TrackInfo`: campos `sample_rate: int` y `channels: int` añadidos a la tabla de referencia.
- `BeatGrid`: campos `dynamic_tempo`, `bpm_min`, `bpm_max` y propiedad
  `bpm_display` (muestra "min–max" para tempo variable) añadidos con descripción
  completa. Tabla ampliada con todos los campos del schema v2.
- `WaveformData`: campos `zoom_peaks`, `zoom_colors`, `zoom_chunk` documentados
  en la sección de datos (antes solo aparecían en la sección de UI).
- `DeckState`: propiedad `dynamic_tempo` (delegación a `beatgrid.dynamic_tempo`)
  añadida.
- `DeckID.C` y `DeckID.D`: documentados como reservados para futura expansión a
  4 decks.

**Transport (`transport.py`):**
- API de mezcla completa: `set_channel_volume()`, `set_pregain()`,
  `set_channel_filter()` añadidos a la tabla de métodos.
- `set_stem_gain_from_hardware()`: alias de `set_stem_gain` usado por FLX4,
  documentado explícitamente.
- Métodos de sync: `unsync()`, `get_all_states()`, `get_sync_master()`
  añadidos a la tabla de API.
- `_SyncPLL` integral gain `Ki = 0.0120` documentado. Comportamiento de
  anti-windup e integrador explicado.
- `_FollowerSync.absorb_correction()`: mecanismo de traslado de drift PLL al
  `rate_bias` documentado en detalle.
- `tick_sync()`: FX BPM tracking (`engine.fx_set_bpm()`) al final de cada tick
  documentado.
- `MonitorCueState`: campo `cue_master: bool` añadido al schema del dataclass.
  Target `"master"` en `toggle_monitor_cue()` y `engine.set_headphone_cue_master()`
  documentados.
- Documentación corregida: PFL/MST CUE ya no se describe como pendiente. El
  engine Rust tiene stream CUE dedicado para salidas FLX4/multicanal y escribe
  auriculares en canales 2-3; queda pendiente solo un selector UI avanzado de
  dispositivo cuando hay varias interfaces.
- `FXState`: campos `feedback`, `time_division`, `color` documentados. Tabla
  completa de métodos FX en Transport añadida.
- `FX_NAMES`, `FX_TARGET_*`, `FX_TIME_DIVISIONS` documentados como constantes
  públicas del módulo.

**Fastload (`formats/fastload.py`):**
- Estructura de directorio de caché expandida: ahora incluye `metadata.json`,
  `waveform_peaks.f32`, `waveform_colors.bin`, `stem_energy.f32`, `beatgrid.json`,
  `cues.json`, `loops.json`, `artwork.jpg/png`. Un cache HIT sirve toda la Fase 1
  sin abrir el ZIP `.wrk`.
- `FastloadSettings` dataclass documentado (enabled, audio_format, cache_stems,
  cache_root, effective_root()).
- Métodos nuevos documentados: `load_metadata()`, `load_all_stems()`,
  `total_size_bytes()`, `clean_orphans()`, `clean_older_than(max_age_days)`.
- Staged loading actualizado: Phase 0 (lookup), Phase 1 diferenciada entre
  cache_hit (load_metadata) y MISS (load_wrk_metadata).
- Guard de track hash en Phase 3 documentado.

**Stem status:**
- Nuevos valores `WRK_LOADING`, `MIX_READY`, `STEMS_LOADING`, `FAILED`
  documentados en sección 25.

**Zoom waveform:**
- `_compute_zoom_peaks()` y `_ZOOM_CHUNK = 256` documentados en sección 25.
- Flujo de construcción de waveform actualizado para incluir zoom.

**Resampling:**
- `_resample_audio()` y `_resample_stem_result()` documentados en sección 25.

**Actualizado `README.md`:**
- Sección de rendimiento: añadida referencia a fastload cache completa
  (metadata + audio).
- Sección de decks: añadido BPM display dinámico ("min–max" para tempo variable).
- Sección de auriculares: MST CUE documentado.

## 2026-05-20

### Motor Rust, mezcla y beatmatching

- Añadido componente 1 del nuevo beatmatching state-of-the-art:
  `wrekker/analysis/beat_tracker.py`, wrapper offline de Beat This! para
  análisis WREKKED en CPU.
- Añadidas dataclasses `BeatGrid` y `PhraseMark` de análisis, con serialización
  schema v2 para `analysis/beatgrid.json`: beats, downbeats, BPM variable,
  confidence, phrase markers, swing factor y timestamp ISO.
- Instalado y declarado `beat-this>=1.1.0` y `torchaudio>=2.0` como
  dependencias Python.
- Integrado `BeatTracker` en `PrepareWorker`: los `.wrk` nuevos se preparan con
  beatgrid schema v2, y los `.wrk` existentes con beatgrid viejo se marcan para
  reanálisis aunque el source audio siga vigente.
- Extendido `wrekker.core.deck.BeatGrid` con downbeats, phrase markers, swing,
  schema version, analysis model y low-confidence flag para compatibilidad con
  phrase-locked sync futuro.
- Añadidos tests `tests/test_beat_tracker.py` para techno straight, swing,
  tempo variable, serialización `.wrk` y phrase detection usando un modelo fake.
- Añadido componente 2 del nuevo beatmatching: `wrekker/engine_rs/src/time_stretch.rs`
  implementa `WrekkerTimeStretch` en Rust con backend Rubber Band por FFI,
  modos `Faster`/`Finer`, ratio 0.5x-2.0x, pitch ±12 semitonos y preservación
  de formantes.
- Añadido `build.rs` para enlazar `librubberband` vía `pkg-config` cuando está
  disponible, con fallback passthrough explícito si la librería no existe.
- Expuesto `NativeTimeStretch` por PyO3 para controlar time stretch/pitch desde
  Python sin procesar audio en Python ni meter el GIL en el hot path.
- Reconstruido `wrekker_engine.so`; verificado desde Python que
  `NativeTimeStretch(...).rubberband_active == True`.
- Añadido componente 3: `wrekker/engine_rs/src/phase_sync.rs` implementa
  `PhaseSync` en Rust con zona muerta, snap, pull-in, cálculo de error en ms y
  limitación de corrección por semitonos/segundo.
- Expuesto `NativePhaseSync` por PyO3 y conectado `Transport._SyncPLL` a ese
  controlador nativo; Python conserva decisiones de estado/UI, pero la
  corrección de fase ya no vive en lógica Python cuando el `.so` está cargado.
- Añadidos tests Rust para convergencia del PLL, zona muerta y `snap_to_grid`.
- Añadido componente 4 base: `wrekker/sync/phrase_sync.py` con
  `PhraseLockSync` para calcular boundaries de frase 8/16 compases,
  progreso de frase y offset master/slave usando `phrase_markers`, downbeats o
  fallback a beats regulares.
- Integrado Phrase-Locked Sync en `Transport.sync()`: al activar SYNC con
  beatgrids disponibles, el follower busca el beat equivalente dentro de la
  frase musical antes del snap fino de fase.
- Añadido phrase meter visual en cada deck, actualizado en el path de 60 Hz:
  progreso de 8/16 compases con estado verde si phrase-locked, amarillo si
  beat-locked sin frase alineada, rojo si no hay sync e idle si no hay beatgrid.
- Añadidos tests `tests/test_phrase_sync.py` para progreso de frase,
  detección de phrase-lock y snap del slave al beat equivalente más cercano.
- Añadido `README.md` orientado a DJs con guía de uso: instalación, preparación
  WREKKED, carga, mezcla, stems, beat/phase/phrase sync, waveforms, scratch,
  DDJ-FLX4, `.wrk`, rendimiento y troubleshooting.
- Actualizado `WREKKER.md` con la arquitectura actual de Beat This!,
  Rubber Band, `NativePhaseSync`, `PhraseLockSync`, phrase meter a 60 Hz y la
  nueva cadencia real del loop UI.
- `WrekkedScanner` ahora inspecciona `analysis/beatgrid.json` durante RESCAN:
  los `.wrk` legibles con beatgrid ausente o `schema_version < 2` se registran
  como `OUTDATED` en `PreparedDB` para poder regenerarlos con Beat This!.
- Añadida acción contextual en WREKKED para actualizar `.wrk` seleccionados a
  beatgrid schema v2 cuando el source original está disponible.
- Añadidos tests `tests/test_wrekked_scanner.py` para rescan de `.wrk` con
  beatgrid viejo y schema v2 actual.
- Confirmado que la app principal usa `wrekker.core.engine_v2.AudioEngine`
  backed por `NativeEngine` Rust; el callback de audio Python en
  `wrekker/core/engine.py` queda como motor legacy no importado por la app.
- Movido el mixer crítico de control a rampas sample-a-sample en Rust para
  crossfader, master gain, channel gain y pregain, evitando zipper/clicks al
  mover faders con dos decks sonando.
- Añadido limitador final en Rust para evitar clipping cuando dos tracks
  full-scale coinciden cerca del centro del crossfader.
- Reconstruido `wrekker_engine.so` desde `wrekker/engine_rs` tras cambios de
  mixer.
- El overlay cross-deck ya conserva beatgrid/BPM válidos ante estados
  transitorios de stems, pausa o EQ, y actualiza la posición del otro deck aunque
  esté pausado.
- El refresco visual de faders/mixer pasó al path de 60 Hz; labels/metrics
  pesados permanecen en el path de 10 Hz.
- Separado el update liviano del zoom waveform para que posición/playhead se
  repinten a 60 Hz sin depender del repaint completo del deck.
- Ajustado el blit del zoom waveform para evitar estiramientos/saltos cerca del
  inicio o final del track.
- Scratch en Rust: añadido suavizado de rate, clamp de velocidades extremas y
  soporte para hard-flick de jogwheel sin touch capacitivo desde FLX4.
- Beatmatching: el snap inicial de sync ahora corrige offsets mayores a 1 % de
  beat en vez de tolerar 5 %, y el PLL de fase responde más rápido para evitar
  flam audible persistente.
- La fase/snap de sync vuelve a delegar en los métodos canónicos de `BeatGrid`
  para no duplicar lógica entre modelo y transporte.

### Correcciones de revisión de Library/WREKKED

- Corregido `LibraryDB.upsert()` / `upsert_many()`: ahora actualizan por `path`
  para que un archivo modificado en disco no rompa el rescan por conflicto
  `UNIQUE(path)`.
- Corregidos filtros de raíz en `known_ids()` y `remove_missing()` para no
  mezclar carpetas hermanas con prefijos parecidos.
- `LibraryDB.find_duplicates()` ya no se limita por defecto a 20.000 filas.
- Añadido `LibraryDB.search_in_folder()` para búsqueda scoped por carpeta sin
  materializar toda la carpeta en memoria.
- WREKKED y Manage WREKKED respetan el scope activo al buscar dentro de carpetas
  y playlists.
- Añadido cache de playlists en WREKKED e invalidación tras rescan de Library.
- `WrekkedScanner` ahora mueve `.wrk` corruptos a `_corrupt_wrks/` en vez de
  borrarlos permanentemente.
- El texto de rescan informa `.wrk` corruptos puestos en cuarentena.
- Añadido diálogo de resultados de rescan de Library con procesados, nuevos,
  skipped, errores y grupos duplicados.
- Corregido `PreparedDB.move_tracks_to_set()`: mover tracks al mismo set ahora
  es no-op y no elimina entradas.
- `PreparedDB.copy_tracks_to_set()` omite duplicados existentes y agrega nuevos
  tracks al final del set.
- El menú de Manage WREKKED deshabilita mover al set actual.
- `MainWindow._on_prepare_tracks()` deduplica el batch de preparación por
  `wrk_id` antes de lanzar `PrepareWorker`.
- `LibraryWidget` legacy actualiza progreso/fin de scan mediante señales Qt,
  evitando modificar widgets desde un thread de scanner.
- `ManageWrekkedLibraryDialog` reemplaza refrescos desde threads con señales Qt
  para fastload y filtros `No Fastload Cache`.

### PrepareDialog

- Corregido `Pause` en la ventana de preparación: ahora alterna pause/resume y
  actualiza el header.
- Corregido `Cancel`: ahora solicita cancelación, desactiva pause y convierte el
  botón en `Close` inmediatamente.
- Corregido cierre con la X de la ventana: solicita cancelación cooperativa y
  permite cerrar/ocultar la ventana.
- `PrepareWorker` ahora tiene checkpoints cooperativos de pause/cancel entre
  fases de preparación.
- Si se cancela durante un track, se marca como `SKIP` y el worker sale del loop
  sin reportarlo como fallo.

### WREKKED como browser único

- `RESCAN` ahora es contextual: en vistas `LIBRARY` rescanea LibraryDB; en
  vistas `SETS` rescanea WREKKED.
- Añadido `LibraryDB.find_duplicates()` para detectar duplicados por
  artist/title/duration normalizados.
- Añadidos `LibraryDB.remove_track()` y `remove_tracks()` para limpiar duplicados
  del índice sin borrar audio del disco.
- Añadido `DuplicateLibraryDialog` para revisar duplicados encontrados tras
  rescan de Library.
- El diálogo de duplicados permite:
  - eliminar filas seleccionadas de LibraryDB
  - conservar solo la primera entrada de cada grupo
  - enviar seleccionados a un set WREKKED, preparando los que no tengan `.wrk`
- Añadidos chequeos explícitos de duplicados al añadir/importar tracks a sets.
- Playlists deduplican rutas repetidas antes de importar.
- `Add to Set` e `Import Scope` saltan tracks cuyo `wrk_id` ya exista en el set.
- Para tracks sin `.wrk`, se calcula el futuro `wrk_id` antes de procesar para
  evitar preparar duplicados innecesarios.
- `MainWindow` vuelve a revisar duplicados al terminar `PrepareWorker` antes de
  insertar los `.wrk` resultantes en el set.
- La UI reporta cuántos duplicados fueron omitidos.
- Añadida detección de playlists `.m3u/.m3u8` dentro de las raíces de Library.
- Añadida subsección `LIBRARY → Playlists` en WREKKED.
- Añadida acción contextual `Import Playlist as WREKKED Set`.
- La importación de playlist crea un set completo, añade los tracks ya
  preparados y procesa automáticamente los que aún no tienen `.wrk`.
- Añadida exploración de Library por alcance en `ManageWrekkedLibraryDialog`:
  All Library, carpetas y playlists.
- Añadido botón `Import Scope` en el manager para importar una playlist completa
  como set WREKKED.
- `WrekkedScanner` pone en cuarentena `.wrk` corruptos durante rescan y limpia
  sus referencias en PreparedDB.
- `WrekkedWidget.on_rescan_done()` notifica cuántos `.wrk` corruptos fueron
  puestos en cuarentena.
- `Reveal .wrk in Files` ahora abre la carpeta contenedora de forma más robusta
  y silencia warnings de `xdg-open` como `kf.iconthemes`.
- `Delete .wrk` queda habilitado para archivos `.wrk` existentes aunque el
  registro esté marcado como roto.
- Reordenado el panel izquierdo: `SETS` aparece primero y `LIBRARY` debajo.
- `SETS` y `LIBRARY` ahora son encabezados colapsables con indicador `▾/▸`.
- Corregido crash del Management UI al buscar en `GENERAL LIBRARY`: `_TrackModel`
  y `_LibrarySearchModel` vuelven a tener métodos `data()` separados.
- Corregido el fallo `NotImplementedError: QAbstractTableModel.data() is abstract`
  causado por `_TrackModel` sin implementación de `data()`.
- Eliminada la navegación por pestañas separadas `LIBRARY` / `WREKKED` cuando
  existe `PreparedDB`.
- WREKKED pasa a ser el browser principal y recibe `LibraryDB`.
- Añadida sección `LIBRARY` dentro del panel izquierdo de WREKKED, con `All
  Library` y carpetas de la biblioteca general.
- Añadidas filas virtuales de Library dentro de la tabla WREKKED con estado
  `WRK`, `FASTLOAD`, `NO WRK` o `BROKEN`.
- Las filas de Library con `.wrk` listo cargan desde `.wrk`; las que no tienen
  `.wrk` pueden cargar desde el source original.
- Añadida señal `prepare_library_tracks(list[LibraryTrack], set_id)` para
  preparar tracks no procesados desde WREKKED.
- `MainWindow._on_prepare_tracks()` ahora puede recibir un `set_id`; cuando
  termina `PrepareWorker`, añade automáticamente los `.wrk` resultantes al set
  WREKKED indicado.

### Búsqueda de biblioteca general en Management UI

- `ManageWrekkedLibraryDialog` ahora acepta opcionalmente `LibraryDB`.
- Añadida barra `GENERAL LIBRARY` dentro del manager para buscar tracks de la
  biblioteca general.
- Añadida tabla de resultados con columnas `Title`, `Artist`, `BPM`, `Key`,
  `Prepared` y `Source`.
- Añadido botón `Add to Set`:
  - agrega inmediatamente tracks que ya tienen `.wrk` listo
  - emite preparación automática para tracks no procesados
  - omite tracks no procesados cuyo source no esté disponible
- Añadida señal `prepare_and_add_tracks(list[LibraryTrack], set_id)` en el
  manager para encadenar preparación y alta en set.

### Documentación

- Actualizado `WREKKER.md` con la sección:
  `21. WREKKED como browser único — Library integrada`.

### Library y WREKKED

- Añadida columna de acciones `"..."` al final de las tablas de Library y WREKKED.
- Ampliados los menús por pista con carga a Deck A/B, prepare/rebuild `.wrk`,
  acciones fastload, reveal de archivos, edición de metadata, gestión de sets y
  acciones destructivas con confirmación.
- Añadida compatibilidad armónica visual en WREKKED, usando el mismo esquema de
  indicador circular que Library.
- Conectada la actualización de key reference desde `MainWindow` hacia Library y
  WREKKED.
- Añadidas acciones fastload a nivel de set WREKKED: build, rebuild, delete,
  validate y status.
- Actualizado el resumen de sets para distinguir `WRK`, `STEMS`, `FASTLOAD`,
  `NO CACHE`, `SOURCE OFFLINE` y `BROKEN`.

### Manage WREKKED Library

- Creado `wrekker/ui/widgets/wrekked_manage_dialog.py`.
- Añadido diálogo `ManageWrekkedLibraryDialog` con sets a la izquierda y tabla de
  pistas preparadas a la derecha.
- Añadidas vistas virtuales: All Prepared Tracks, Recently Prepared, Source
  Offline, No Fastload Cache y Broken Items.
- Añadida creación, renombrado, duplicado y borrado de sets.
- Añadido movimiento/copia de pistas entre sets.
- Añadida edición de metadata de PreparedDB sin depender del source file.
- Añadidas acciones batch de fastload para pistas seleccionadas.
- Añadido panel de detalles con `.wrk path`, cache path, tamaños, estado de cache
  y expected load path.
- Añadido diálogo `WrekkedPathSettingsDialog` para rutas de biblioteca preparada,
  `.wrk`, fastload, stem temp y backup/export.

### PreparedDB y fastload

- Añadida tabla `app_settings` para persistir rutas y opciones.
- Añadidos helpers `get_setting()` y `set_setting()`.
- Añadido `update_track_metadata()` para metadata override de PreparedDB.
- Añadidos `copy_tracks_to_set()` y `move_tracks_to_set()`.
- Añadido `remove_prepared_track()` para eliminar entradas PreparedDB y membresías
  WREKKED.
- `FastloadCache` ahora respeta `WREKKER_FASTLOAD_CACHE` cuando no recibe
  `cache_root` explícito.
- `MainWindow` usa la ruta configurada `wrekked_library_path` para `WrekkedScanner`
  y `PrepareWorker`.

### Documentación

- Actualizado `WREKKER.md` con la sección:
  `20. WREKKED Management UI — biblioteca local de performance`.
- Creado este `changelog.md` para registrar cada cambio aplicado en el workspace.

### Library Options y configuración SMB en UI

- `WrekkedPathSettingsDialog` renombrado a "Library Options" y ampliado con tres
  secciones nuevas accesibles desde el botón **Settings** del manager:
  - **MUSIC SOURCES**: lista de library roots (PreparedDB) con botones Add Folder,
    Remove Selected y Reveal; los cambios son inmediatos vía `add_root` /
    `remove_root`.
  - **SOURCE MODE**: combo "Local Folders" / "SMB Network Share" persistido en
    `app_settings` como `source_mode`.
  - **SMB CONFIGURATION** (visible solo en modo SMB): campos Host/IP, Share Name,
    Username, Password (enmascarado), Domain y Mount Point; botón
    **Test Connection** que intenta montar el share y muestra el resultado.
- Al guardar en modo SMB se escribe `~/.config/wrekker/smb.conf` con la
  configuración introducida.
- Añadida función `save_smb_config()` en `wrekker/library/smb.py`: lee el
  archivo existente con `configparser`, actualiza solo la sección indicada y
  lo reescribe preservando otras secciones.

## SETTINGS

- Añadido sistema SETTINGS profesional con store JSON versionado en
  `~/.config/wrekker/settings.json`.
- Añadidos perfiles persistentes: crear, duplicar, renombrar, borrar perfiles no
  protegidos, seleccionar perfil activo, startup profile, import/export de
  perfil y export/import del documento completo.
- Añadida ventana SETTINGS grande con navegacion lateral, busqueda, dirty state,
  reset por seccion, restore defaults, apply/save y separacion entre opciones
  live-safe, audio-restart y app-restart.
- Añadidas secciones: Audio & Routing, Controller & MIDI, Library & Storage,
  WREKKED & Fastload, Analysis & Preparation, Playback & Mixing, Sync &
  Quantize, Stems & WREKK, FX, Waveforms & Display, WREKKER LAB, Profiles,
  Advanced y Diagnostics.
- SETTINGS preserva defaults actuales y aplica variables de entorno como
  overrides de sesion sin escribirlas de vuelta al perfil persistente.
- El arranque carga SETTINGS antes de crear LibraryDB, PreparedDB, engine y UI.
  Sample rate/buffer y rutas principales salen del perfil cuando no hay override
  explicito.
- Sincronizacion inicial con `PreparedDB.app_settings` para WREKKED root,
  `.wrk` root, fastload root, temp stem cache, fastload mode/format y source
  mode.
- WREKKER LAB ahora lee defaults reales del perfil: waveform source, compare
  mode, phrase length, metronome enabled y click level.
- SETTINGS muestra opciones no soportadas como no disponibles cuando no existe
  API segura, por ejemplo seleccion explicita de dispositivo CPAL y test tones.
- QML deck waveforms permanecen experimentales y opt-in; el renderer estable
  `texture` sigue siendo default.
- Añadidos tests para creacion del store, roundtrip, config corrupta,
  migracion, reset, perfiles, import/export, precedencia de env vars,
  validacion y smoke tests de la ventana SETTINGS.

### Correcciones de beat tracker

- **Bug: doble carga de audio por análisis.** `BeatTracker.analyze()` cargaba el
  audio una vez para Beat This! (o librosa en fallback) y una segunda vez dentro
  de `_bar_energies()` al calcular energías de compás para la detección de frases.
  Ahora el audio se carga una sola vez al inicio de `analyze()` y se pasa por
  referencia a `_phrase_markers(audio, sr, ...)` y `_bar_energies(audio, sr, ...)`.
  Para un track de 5 minutos en FLAC esto elimina una decodificación completa por
  análisis.
- **Bug: swing_factor acumulaba deriva de tempo.** El cálculo anterior medía la
  desviación de cada beat respecto a una rejilla constante `first + i * period`,
  lo que causaba que en tracks con tempo variable los beats tardíos acumularan
  errores crecientes, contaminando la mediana. El nuevo `_swing_factor` mide, por
  cada tripleta consecutiva `(i, i+1, i+2)`, cuánto se desvía `beat[i+1]` del
  punto medio exacto entre `beat[i]` y `beat[i+2]` — completamente local e inmune
  a deriva. Escala: 0.0 = recto, ≈ 1.0 = swing 2:1 triplet.

### Headphone monitoring CUE / PFL

Implementación completa de botones CUE de auriculares con feedback LED para el
DDJ-FLX4 y la UI.

**`wrekker/hardware/midi_protocol.py`**
- Añadido `NOTE_MASTER_CUE: int | None = None` como placeholder documentado;
  el mapping hardware del FLX4 para master CUE es desconocido y queda marcado
  explícitamente hasta confirmación.

**`wrekker/core/transport.py`**
- Añadido `MonitorCueState` (frozen dataclass): `cue_deck_a`, `cue_deck_b`,
  `headphone_mix` (0.0=CUE puro, 1.0=master puro), `headphone_level` (0.0–2.0).
- Añadido `_monitor_cue = MonitorCueState()` en `Transport.__init__`.
- Añadidos métodos públicos:
  - `toggle_monitor_cue(target)` — alterna PFL para `"A"`, `"B"` o `"master"`
  - `set_monitor_cue(target, enabled)` — fija estado explícito
  - `get_monitor_state()` → `MonitorCueState`
  - `set_headphone_mix(value)` — persiste mezcla auriculares desde el fader
  - `set_headphone_level(value)` — persiste nivel de auriculares
- `MonitorCueState` añadido a `__all__`.
- El routing de audio ya tiene bus dedicado en Rust para salidas
  FLX4/multicanal: el engine consume `MonitorCueState`, lee los buses live
  pre-fader y escribe la mezcla CUE/master en canales 2-3. En sistemas sin
  salida multicanal válida, el estado sigue coherente pero no hay bus físico.

**`wrekker/hardware/flx4.py`**
- Reemplazado el stub de `NOTE_HEADPHONES_CUE` (que solo encendía el LED) con
  llamada real `self._transport.toggle_monitor_cue(deck)`. El LED es actualizado
  por `sync_leds()` a 10 Hz.
- `CC_HEADPHONES_MIX_LSB`: el combinador 14-bit ahora llama
  `self._transport.set_headphone_mix(self._14b_to_01(raw))`.
- `sync_leds()`: añadida sincronización de LEDs de CUE de auriculares para
  Deck A (ch 0, note 0x54) y Deck B (ch 1, note 0x54) desde `get_monitor_state()`.
- `_init_leds()`: inicializa `NOTE_HEADPHONES_CUE` en LED_OFF para A y B.

**`wrekker/ui/widgets/deck.py`**
- Añadida señal `monitor_cue_pressed = pyqtSignal(str)`.
- Añadido botón `PFL` checkable (w=36) en la fila de transport, con acento del
  color del deck (cyan Deck A, magenta Deck B).
- Añadido método `set_monitor_cue_active(active)` para feedback visual desde el
  loop de 60 Hz sin reemitir la señal.

**`wrekker/ui/widgets/master.py`**
- Añadida señal `master_cue_pressed = pyqtSignal()`.
- Añadido botón `MST CUE` checkable antes del label de estado FLX4, con estilo
  naranja-alerta cuando activo.
- Añadidos helpers `_cue_btn_ss(active)`, `_on_mst_cue_clicked()` y
  `set_monitor_cue_active(active)` para sincronizar el botón desde fuera sin
  reemitir la señal.

**`wrekker/ui/main_window.py`**
- Conectadas señales PFL de Deck A/B y Master CUE al transport en
  `_connect_signals()`.
- `_sync_mixer_ui()` lee `get_monitor_state()` y llama
  `set_monitor_cue_active()` en ambos decks a 60 Hz para mantener el botón en
  sync con el estado hardware/transport.

### Verificación

- Ejecutado `python -m compileall ...` sobre los módulos modificados.
- Smoke test offscreen de `ManageWrekkedLibraryDialog` con una PreparedDB temporal.
- 10/10 tests pasan tras todos los cambios.
