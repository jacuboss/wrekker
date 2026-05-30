# Wrekker

Wrekker es un software DJ open source para Linux. Está pensado para pinchar en
vivo con dos decks, stems, biblioteca preparada `.wrk`, beatmatching avanzado,
sync por fase y sync por frases musicales.

El audio crítico corre en Rust con CPAL/PyO3. Python y PyQt6 manejan UI,
biblioteca, preparación de tracks y control, pero no procesan audio en el hot
path.

## Para Quién Es

Wrekker está diseñado para DJs que quieren:

- Preparar una biblioteca local lista para performance.
- Cargar pistas rápido desde archivos `.wrk`.
- Mezclar con stems separados: vocals, drums, bass y other.
- Ver waveform, beats, overlay del otro deck y frase musical.
- Usar sync de BPM, sync de fase y phrase sync sin depender de software cerrado.
- Usar hardware Pioneer DDJ-FLX4 en Linux.

## Conceptos Principales

### Decks

Wrekker tiene dos decks principales, A y B. Cada deck incluye:

- Track title, artist y artwork.
- Waveform general con colores espectrales, beat markers, overlay de stems y
  Auto Markers jerarquizados.
- HUD compacto de Auto Markers bajo el overview con countdowns independientes
  por jerarquía: `P`, `W` y `G`, cada uno con LED de confianza.
- Zoom waveform centrado en el playhead (alta resolución, 256 samples/columna).
- Overlay de beats del otro deck.
- BPM (con rango "min–max" si el track tiene tempo variable), key, LUFS y pitch.
- Phrase meter.
- Faders de stems (VOC, DRM, BSS, OTH) con mute, solo y WREKK Stem Horizon.
- Spectrum.
- Transport: play, cue, loop, sync y master.
- Botón PFL (auriculares) por deck.

### WREKKED

WREKKED es el flujo de preparación de Wrekker. Convierte un audio normal en un
archivo `.wrk`, que contiene:

- Audio FLAC.
- Metadata.
- Beatgrid schema v2.
- Beats y downbeats.
- Phrase markers.
- Waveform precalculada.
- Stems.
- Fastload cache.

Cuando una pista ya tiene `.wrk` actualizado, Wrekker puede cargarla mucho más
rápido que haciendo análisis en vivo.

### WREKKER LAB

WREKKER LAB es el workspace de corrección y preparación de performance para
pistas `.wrk`. WREKKED prepara el archivo; WREKKER LAB verifica y corrige cómo
Wrekker entiende la pista.

En esta primera versión está optimizado para techno y música electrónica de
tempo esencialmente constante. Permite abrir un `.wrk` desde WREKKED, preservar
la capa automática original, editar una capa activa, comparar AUTO vs ACTIVE,
ajustar BPM/primer beat/downbeats/frases, editar markers, preparar hot cues y
loops, marcar una pista como `MANUAL VERIFIED` y guardar un changelog auditable
dentro del `.wrk`.

LAB también tiene preview de audio aislado: reproduce el audio del `.wrk` con el
engine de audio, permite play/pause/stop/seek, usa el beatgrid draft para el
metrónomo, permite ajustar nivel de click y puede monitorizar stems con mute o
isolate desde las fuentes `VOCALS`, `DRUMS`, `BASS` y `OTHER`. Los stems del
preview se cargan desde fastload cuando existe, o desde el `.wrk` como fallback.

### Auto Markers y WREKK Markers

La taxonomía actual separa tres lenguajes:

- `P` Primary: estructura grande para mezclar (`DROP`, `MIX IN`, `MIX OUT`,
  `SWITCH`).
- `W` WREKK: cambios internos de stems y oportunidades de manipulación.
- `G` Guide: navegación de frase (`PHRASE`).

`WREKK` genérico ya no es un marker Primary visible. `GHOST` tampoco compite con
`DROP` o `MIX OUT`; ahora es una oportunidad WREKK de alta confianza. Los
markers W estructurales activos son `VOCAL IN/OUT`, `BASS IN/OUT`,
`KICK IN/OUT` y `TOP IN/OUT`. Las oportunidades live son `GHOST`,
`DECONSTRUCT` y `REBUILD`.

El detector W es rule-based, stem-aware y beat/phrase-aligned. Compara ventanas
persistentes de varios compases usando stems y `stem_energy` ya preparados; no
rerun stem separation. Cada W marker guarda confianza, familia (`structural` u
`opportunity`), stems asociados y evidencia legible. El live UI muestra por
defecto oportunidades W de alta confianza para evitar saturar la waveform; LAB
permite inspeccionar y editar la estructura completa.

### WREKK Stem Horizon

Stem Horizon es la vista compacta de actividad futura de stems dentro del area
`STEMS` de cada deck. Cada lane vive encima de su fader correspondiente: `VOC`
encima del fader de vocals, `DRM` encima de drums, `BSS` encima de bass y `OTH`
encima de other. No reemplaza el HUD `P` / `W` / `G`: `P` sigue indicando
estructura grande de mezcla, `W` el proximo evento u oportunidad WREKK, y `G`
la frase.

El modo recomendado por defecto es `LED Blocks`: cada fila muestra los proximos
compases como bloques de actividad stem-aware. `Future Bars` usa bandas
continuas para leer transiciones largas, `Stem Waveforms` muestra mini-overviews
por stem, y `Off` colapsa la vista para maxima limpieza. Los colores siguen la
identidad de stems: vocals coral, drums cyan, bass amarillo, other violeta y
oportunidades WREKK en naranja.

La informacion viene de una linea de tiempo bar-synchronous persistida en el
`.wrk` como `analysis/stem_horizon.json` y cacheada en fastload como
`stem_horizon.json`. Se genera desde stems ya preparados, `stem_energy`,
beatgrid y frases; no rerun stem separation ni analiza audio en vivo. Tracks
legacy sin Horizon siguen cargando normalmente y simplemente muestran Horizon
no generado hasta regenerarlo desde preparacion/LAB.

Dynamic-tempo/warp-anchor editing queda preparado a nivel de arquitectura, pero
no está implementado todavía. Si un track marca tempo variable, LAB permite
preview, inspección, markers, cues y loops, y muestra estado
`DYNAMIC TEMPO TODO`.

### Stems

Los stems se separan con HTDemucs:

- Vocals.
- Drums.
- Bass.
- Other.

Cada stem tiene fader, mute, solo y medición visual. El motor Rust mezcla los
stems en tiempo real.

### Beatgrid

El beatgrid guarda la estructura rítmica real del track:

- BPM global.
- Beats explícitos.
- Downbeats.
- Tempo variable.
- Swing factor.
- Confidence.
- Phrase markers de 8/16 compases.

El análisis nuevo usa Beat This! offline durante la preparación WREKKED. No corre
en el callback de audio.

### Sync

Wrekker tiene tres niveles de sync:

- BPM sync: iguala tempo entre master y follower.
- Phase sync: mantiene beats alineados continuamente con un PLL en Rust.
- Phrase sync: alinea el punto musical dentro de frases de 8/16 compases.

## Instalación

### Dependencias Python

```bash
pip install -e .
pip install beat-this torch torchaudio librosa numpy pytest
```

### Dependencias del sistema

Para el motor Rust y time stretching:

```bash
sudo apt install build-essential pkg-config librubberband-dev
```

Para audio:

```bash
sudo apt install ffmpeg
```

### Compilar el Engine Rust

Desde la raíz del proyecto:

```bash
cd wrekker/engine_rs
cargo build --release
cp target/release/libwrekker_engine.so ../../wrekker_engine.so
```

También puede usarse maturin si el entorno está configurado:

```bash
cd wrekker/engine_rs
maturin develop --release
```

## Ejecutar Wrekker

Desde la raíz del proyecto:

```bash
python -m wrekker.ui.app
```

`python -m wrekker` no es un entry point actualmente; la app se lanza desde
`wrekker.ui.app`.

### Flags de la App

```bash
# Modo normal: UI, audio Rust y detección DDJ-FLX4
python -m wrekker.ui.app

# UI sin arrancar el engine de audio; útil para revisar biblioteca/WREKKED
python -m wrekker.ui.app --no-audio

# Evita abrir/detectar el controlador Pioneer DDJ-FLX4
python -m wrekker.ui.app --no-controller

# Activa diagnóstico de timing del callback de audio y del tick de UI
python -m wrekker.ui.app --debug
```

Las flags pueden combinarse. Por ejemplo:

```bash
python -m wrekker.ui.app --no-audio --no-controller --debug
```

### SETTINGS

Wrekker incluye una ventana profesional de SETTINGS accesible desde el boton
`SET` del header, desde el menu `Wrekker > Settings`, o con `Ctrl+,`.

La configuracion persistente vive en:

```text
~/.config/wrekker/settings.json
```

El archivo es versionado, se crea automaticamente en el primer arranque y
guarda perfiles de setup. El perfil `Default` esta protegido; puedes crear,
duplicar, renombrar, importar/exportar y seleccionar perfiles adicionales para
escenarios como casa, preparacion o directo con DDJ-FLX4.

SETTINGS usa lenguaje de DJ para la experiencia normal: `Preparation Quality`,
`Visual Performance`, `Waveform Renderer`, `Audio & Routing`, `WREKKED &
Fastload`, `WREKKER LAB` y `Stem Horizon`. Los nombres internos de flags quedan
en Advanced y Diagnostics.

Precedencia de configuracion:

1. Defaults del codigo.
2. Perfil persistente seleccionado.
3. Variables de entorno explicitas al arrancar.
4. Acciones temporales en runtime.

Si una variable de entorno pisa un valor persistente, SETTINGS muestra que esa
opcion esta overridden para la sesion actual y no modifica el perfil guardado.

Primer setup recomendado:

1. Abre SETTINGS.
2. Selecciona o crea un perfil, por ejemplo `LIVE - DDJ-FLX4`.
3. Revisa `Audio & Routing`: sample rate, buffer, master 0/1 y CUE 2/3.
4. Verifica que CUE indique FLX4/multicanal disponible cuando aplique.
5. Agrega fuentes de musica en `Library & Storage`.
6. Elige carpetas para WREKKED `.wrk`, fastload y cache temporal de stems.
7. Define `Preparation Quality`: FAST, BALANCED o ARCHIVE.
8. Ajusta defaults de WREKKER LAB: source, compare, metronomo y waveform.
9. En `Stems & WREKK`, deja `Stem Horizon` en `LED Blocks` o selecciona
   `Future Bars`, `Stem Waveforms` u `Off`.
10. Guarda el perfil.
11. Carga un `.wrk` y verifica playback/PFL antes del set.

Los cambios de dispositivo, sample rate, buffer, routing, renderer experimental
o diagnosticos de arranque se guardan de forma segura y se aplican en el
siguiente arranque o tras reiniciar audio; Wrekker no interrumpe playback
silenciosamente.

### Flags de Entorno

Estas variables siguen siendo compatibles para desarrollo, diagnostico y
overrides de lanzamiento. Para uso normal, configura Wrekker desde SETTINGS:

| Variable | Valores | Uso |
|----------|---------|-----|
| `WREKKER_PREPARE_MODE` | `fast`, `balanced`, `archive` | Define el perfil de compresión FLAC al crear `.wrk`. Valores inválidos vuelven a `fast`. |
| `WREKKER_WRK_FLAC_COMPRESSION_LEVEL` | `0`-`8` | Sobrescribe el nivel FLAC usado por el perfil de preparación. |
| `WREKKER_WRK_AUDIO_ENCODE_THREADS` | `1`-`4` | Número de hilos para codificar audio/stems FLAC dentro del `.wrk`. |
| `WREKKER_PREPARE_CPU_WORKERS` | `2`-`6` | Workers CPU del pipeline WREKKED. El código limita el valor a ese rango. |
| `WREKKER_PREPARE_GPU_POLICY` | `beat_cpu`, `parallel_gpu`, otro | `beat_cpu` fuerza Beat This! en CPU. `parallel_gpu` permite que beatgrid y stems usen GPU en paralelo. Otros valores usan `WREKKER_BEAT_DEVICE` para Beat This! y serializan el acceso GPU con stems. |
| `WREKKER_BEAT_DEVICE` | `cpu`, `cuda`, `cuda:0`, etc. | Dispositivo para Beat This! cuando `WREKKER_PREPARE_GPU_POLICY` no es `beat_cpu`. |
| `WREKKER_BEAT_CHECKPOINT` | checkpoint Beat This! | Checkpoint del modelo Beat This!; por defecto `final0`. |
| `WREKKER_BEAT_USE_DBN` | `0`, `1` | Activa postproceso DBN de Beat This! cuando vale `1`. |
| `WREKKER_KEEP_PREP_TEMP` | `0`, `1` | Conserva temporales de preparación si vale `1`; útil para debug. |
| `WREKKER_W_MARKER_DEBUG` | `0`, `1` | Log resumido del detector WREKK Markers: estructurales, oportunidades y total emitido. |
| `WREKKER_STEM_HORIZON_DEBUG` | `0`, `1` | Log de diagnóstico del widget Stem Horizon y estado de datos/cadencia. |
| `WREKKER_FASTLOAD_CACHE` | ruta | Cambia la raíz del fastload cache; por defecto `~/.cache/wrekker/fastload`. |
| `WREKKER_STEM_CACHE_PATH` | ruta | Cambia la raíz del cache temporal de stems. |
| `WREKKER_TEMP_STEM_CACHE_PATH` | ruta | Alias legacy para el cache temporal de stems. |
| `WREKKER_UI_TICK_MS` | milisegundos | Intervalo solicitado del tick principal de UI; por defecto `8` para evitar cuantización a ~30 Hz en algunos backends Qt. |
| `WREKKER_UI_CLOCK` | `pipe`, `thread`, `qtimer` | Reloj del tick visual principal. Por defecto `pipe`, que despierta Qt por `QSocketNotifier` para evitar cuantización de timers/señales queued. |
| `WREKKER_UI_TARGET_FPS` | FPS | Cadencia objetivo del reloj visual cuando `WREKKER_UI_CLOCK=pipe` o `thread`; por defecto `60`. |
| `WREKKER_UI_TICK_LOG` | `0`, `1` | Log resumido del tick principal de UI. |
| `WREKKER_UI_TICK_PROFILE` | `0`, `1` | Perfilado por secciones del tick de UI. |
| `WREKKER_DISABLE_CROSS_OVERLAY` | `0`, `1` | Desactiva overlay visual del otro deck. |
| `WREKKER_DISABLE_DECK_REALTIME` | `0`, `1` | Desactiva actualizaciones visuales realtime de decks para aislar carga de UI. |
| `WREKKER_ZOOM_FPS_LOG` | `0`, `1` | Log de FPS/pintado del zoom waveform QWidget. |
| `WREKKER_ZOOM_RENDERER` | `texture`, `classic`, `legacy` | Renderer del zoom waveform QWidget. Por defecto `texture`; `classic`/`legacy` conserva el método anterior como fallback. |
| `WREKKER_ZOOM_CACHE_SCALE` | `1`-`4` | Supersampling horizontal del cache visual del zoom; por defecto `2` para reducir saltitos de columna. |
| `WREKKER_ZOOM_PEAK_SMOOTH` | `0`-`9` | Suavizado visual de peaks del zoom; por defecto `3` para reducir shimmer/temblor temporal. |
| `WREKKER_TEXTURE_ZOOM_CACHE_SCALE` | `1`-`8` | Supersampling horizontal específico del renderer `texture`; por defecto `4`. |
| `WREKKER_TEXTURE_ZOOM_PEAK_SMOOTH` | `0`-`15` | Suavizado específico del renderer `texture`; por defecto `5`. |
| `WREKKER_LAB_FORCE_WIDGET_TIMELINE` | `0`, `1` | Fuerza el timeline QWidget de WREKKER LAB aunque Qt Quick esté disponible; útil para probar renderers LAB. |
| `WREKKER_LAB_WAVEFORM_RENDERER` | `texture`, `classic`, `legacy` | Renderer de waveform del timeline QWidget de WREKKER LAB. Por defecto `texture`; `classic`/`legacy` usa el método anterior. |
| `WREKKER_LAB_TEXTURE_CACHE_SCALE` | `1`-`8` | Multiplicador de cache horizontal del renderer `texture` de WREKKER LAB; por defecto hereda `WREKKER_TEXTURE_ZOOM_CACHE_SCALE` o usa `4`. |
| `WREKKER_LAB_TEXTURE_PX_PER_SECOND` | `64`-`1024` | Resolución horizontal de la textura LAB; por defecto `256` px/s para zoom tipo SoundCloud sin borrar picos. |
| `WREKKER_LAB_TEXTURE_PEAK_SMOOTH` | `0`-`15` | Suavizado de peaks del renderer `texture` de WREKKER LAB; por defecto `0` para preservar transientes y evitar apariencia de polígonos. |
| `WREKKER_ZOOM_DISABLE_REPAINT` | `0`, `1` | Diagnóstico: avanza posición visual sin solicitar repaint. Si el `ui-tick` sube a 60, el límite está en backing-store/compositor. |
| `WREKKER_ZOOM_ANIM_MS` | milisegundos | Intervalo de despertar del timer de zoom; por defecto `1` con pacing interno. |
| `WREKKER_ZOOM_TARGET_FPS` | FPS | Cadencia visual objetivo del zoom waveform; por defecto `60`. |
| `WREKKER_ZOOM_OWN_TIMER` | `0`, `1` | Activa el timer propio del zoom para profiling aislado. Por defecto `0`: el zoom se anima desde el tick principal para coalescer repaints de ambos decks. |
| `WREKKER_WAVEFORM_POSITION_DEBUG` | `0`, `1` | Diagnóstico de posición visual del waveform QWidget. |
| `WREKKER_UI_PLATFORM_LOG` | `0`, `1` | Imprime plataforma Qt, pantalla, DPR y refresh reportado. También se imprime con `--debug`. |
| `WREKKER_ENABLE_QML_DECK_WAVEFORMS` | `0`, `1` | Solicita renderer QML para timelines de deck. |
| `WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS` | `0`, `1` | Permite realmente el renderer QML de decks; está marcado como ruta inestable/perfilado. |
| `WREKKER_WAVEFORM_RENDER_DEBUG` | `0`, `1` | Log del renderer usado por timeline QML/fallback. |
| `WREKKER_WAVEFORM_FPS_LOG` | `0`, `1` | Log FPS de modelos QML de waveform/deck. |
| `WREKKER_QML_FPS_LOG` | `0`, `1` | Log FPS del timeline QML de WREKKER LAB. |

Ejemplos:

```bash
WREKKER_PREPARE_MODE=balanced python -m wrekker.ui.app
WREKKER_FASTLOAD_CACHE=/mnt/ssd/wrekker-fastload python -m wrekker.ui.app
WREKKER_UI_TICK_PROFILE=1 WREKKER_ZOOM_FPS_LOG=1 python -m wrekker.ui.app --debug
WREKKER_ENABLE_QML_DECK_WAVEFORMS=1 WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS=1 python -m wrekker.ui.app
```

### Utilidades de Diagnóstico

```bash
# Monitor MIDI DDJ-FLX4
python tools/flx4_monitor.py

# Vista deduplicada del mapa MIDI recibido
python tools/flx4_monitor.py --map

# Probar LED por número de nota; acepta decimal o hex, por ejemplo 0x0B
python tools/flx4_monitor.py --led NOTE

# Imprimir CC crudos, sin decodificación 14-bit
python tools/flx4_monitor.py --raw-cc

# Smoke test del driver FLX4 contra Transport/Engine stub
python tools/test_flx4.py

# Probar scanner de biblioteca local
python tools/test_library.py /ruta/a/musica

# Montar y escanear un SMB configurado en ~/.config/wrekker/smb.conf
python tools/test_library.py --smb NOMBRE

# Imprimir ejemplo de configuración SMB
python tools/test_library.py --smb-info

# POC interactivo de stems sobre un archivo de audio
python tools/test_stems.py /ruta/al/audio.flac
```

## Flujo Básico Para DJs

### 1. Añadir Música a la Biblioteca

Usa la biblioteca integrada para escanear carpetas con tu música. Wrekker lee:

- Title.
- Artist.
- BPM de metadata si existe.
- Key si está disponible.
- Artwork.
- Duración.

La biblioteca general sirve como fuente para preparar tracks y construir sets.
Cuando se crea un set desde una playlist o carpeta SMB, Wrekker guarda un
`position` explicito por track y preserva el orden recibido de la fuente; no lo
convierte a orden alfanumerico. Los rescans actualizan metadata/estado, pero no
pisan el orden manual del set.

Los sets preparados tambien pueden reordenarse despues: en WREKKED usa el menu
contextual de una pista dentro de un set real y selecciona `Move Up in Set` o
`Move Down in Set`, o abre `Edit Set...` para drag-and-drop completo.

### 2. Preparar Tracks con WREKKED

Selecciona tracks y usa Prepare. Durante la preparación Wrekker:

1. Carga y normaliza el audio una sola vez.
2. Genera artefactos en paralelo: waveform/zoom waveform, Beat This!,
   metadata/key/LUFS, FLAC del mix y HTDemucs.
3. Calcula `stem_energy`, frases y auto markers cuando sus dependencias están listas.
4. Ensambla y valida el `.wrk` sin recomprimir FLAC dentro del ZIP.
5. Construye fastload local y actualiza PreparedDB con estados/timings por fase.

El diálogo de preparación permite:

- Pause.
- Resume.
- Cancel.
- Cierre cooperativo.

Si un track ya tiene `.wrk` schema v2 actualizado, Wrekker respeta el fastload y
no reanaliza.

Durante RESCAN, Wrekker también inspecciona `.wrk` antiguos. Si encuentra un
beatgrid sin `schema_version: 2`, lo marca como pendiente de actualización y el
menú contextual de WREKKED permite regenerarlo con el beatgrid nuevo cuando el
source original está disponible.

### 3. Cargar Un Track

Al cargar una pista:

- Si hay `.wrk` actualizado, Wrekker carga desde ahí.
- Si no hay `.wrk`, carga el audio original y lanza análisis/stems en segundo
  plano.

La reproducción puede iniciar antes de que terminen los stems.

### UI en vivo

Wrekker separa la UI en rutas de distinta frecuencia:

- Playhead, zoom waveform, overlays y controles críticos: 60 Hz.
- Spectrum, peak meters, mini meters y osciloscopios: ~45 Hz.
- LUFS, métricas pesadas y etiquetas completas: ~10 Hz.

Cada deck muestra bajo el overview waveform tres próximos auto markers
independientes, uno por jerarquía:

```text
P  MIX OUT   0:32
W  DECON     0:16
G  PHRASE    5.3s
```

Cada fila tiene su propio LED de confianza. Solo se generan, cargan y guardan
Auto Markers con confianza >=70%. El LED es amarillo entre 70% y 85%, verde
desde 85%, y gris cuando no hay marker próximo para esa jerarquía.

Los Auto Markers usan jerarquía visual P/W/G. Primary contiene solo `DROP`,
`MIX IN`, `MIX OUT` y `SWITCH`. WREKK contiene estructura stem-aware y
oportunidades; en vivo `ESSENTIAL` muestra oportunidades de alta confianza como
`W:GHOST`, `W:DECON` o `W:REBUILD`, mientras LAB puede revisar eventos
estructurales como `W:VOC+`, `W:BSS-`, `W:KICK+` y `W:TOP-`. `PHRASE` es guía
estructural. En la waveform se dibujan como colitas inferiores finas, sin texto
encima de la waveform, para no tapar la forma de onda. El tooltip conserva el
tipo interno, posición, confianza y razón del detector. El botón `MKRS` permite
preparar/usar modos `OFF`, `PRIMARY`, `ESSENTIAL`, `PRIMARY + WREKK`, `FULL` y
`DEBUG`; el valor por defecto live es `ESSENTIAL`.

### 4. Mezclar

Usa:

- Channel fader para nivel de deck.
- Pregain para ajuste previo.
- Crossfader para mezclar A/B.
- EQ low/mid/high.
- Channel filter.
- Master gain.

El crossfader y gains críticos se suavizan dentro del callback Rust para evitar
clicks y zipper noise.

### 5. Usar Stems

Cada deck tiene faders:

- VOC.
- DRM.
- BSS.
- OTH.

Puedes:

- Bajar vocals para hacer instrumental.
- Mantener drums/bass para transiciones.
- Solo vocals para acapella.
- Usar WREKK macro para transformaciones rápidas.

El overlay de waveform y beatgrid no se pierde al activar stems, pausar o mover
EQ.

## Beatmatching y Sync

### Master y Follower

Pulsa `M` para elegir el deck master. El master define tempo y fase musical. El
deck sincronizado se convierte en follower.

### SYNC

Al pulsar `SYNC` en un deck follower:

1. Wrekker calcula el BPM del master.
2. Ajusta el playback rate del follower.
3. Busca el beat equivalente dentro de la frase musical.
4. Hace snap fino de fase.
5. Activa el PLL Rust para mantener la alineación.

### Phase Sync

El PLL corrige diferencias pequeñas de fase mientras ambos decks suenan. Tiene
zona muerta para evitar microvariaciones audibles.

El indicador SYNC cambia según calidad de lock:

- Verde: phase locked.
- Naranja: corrigiendo.
- Rojo: drift grande.

### Phrase Sync

El phrase meter muestra progreso dentro de una frase de 8/16 compases.

Colores:

- Verde: phrase-locked.
- Amarillo: beat-locked, pero frase desalineada.
- Rojo: sin sync.
- Gris/idle: sin beatgrid.

Esto ayuda a hacer que drops, breaks y builds caigan juntos, no solo que los
bombos estén alineados.

### Tracks con Swing o Tempo Variable

Si el beatgrid detecta swing o tempo variable, Wrekker usa beats explícitos en
vez de asumir una grilla rígida. Esto mejora:

- House con groove.
- Funk/disco cuantizado de forma imperfecta.
- Jazz/electro con swing.
- Tracks editados o no cuadrados.

## Waveforms

### Waveform General

Muestra:

- Energía global.
- Color espectral.
- Beat markers.
- Posición.
- Loop.
- Cue points.
- Overlay de stems.

### Zoom Waveform

El zoom está centrado en el playhead y muestra una ventana de beats. Sirve para:

- Ajustar mezcla visual.
- Ver beats cercanos.
- Ver el overlay del otro deck.
- Ver error de fase.

El movimiento del zoom corre en el path visual de 60 Hz.

## Scratch y Jog Wheel

El scratch corre en Rust. Usa interpolación Hermite cúbica para reducir artifacts
durante movimiento lento o reversa.

Comportamiento:

- Touch/vinyl scratch controla directamente el rate.
- Si giras la jogwheel con fuerza, Wrekker permite hard flick como en un
  tocadiscos.
- Al soltar, vuelve con release suave hacia la tasa normal.
- El nudge lateral es independiente y sirve para pitch bend temporal.

## FX

El panel FX tiene dos bancos separados. El banco normal conserva los efectos
tradicionales sincronizados a BPM:

- Echo.
- Delay.
- Filter.
- Reverb.
- Roll.
- Trans.
- Flanger/phaser según disponibilidad del engine.
- Noise/Bitcrusher según disponibilidad del engine.

El banco **WREKK FX** es independiente y stem-aware. No reemplaza ni cambia el
banco normal. Contiene:

- VOCAL GHOST.
- TOP WASH.
- DRUM CRUSH.
- RHYTHM GATE.
- STEM ROLL.
- BASS LOCK.
- DECONSTRUCT.
- REBUILD.

Los WREKK FX procesan stems o capas (`TOP = vocals + other`, `RHYTHM = drums +
bass`) en Rust, antes de sumar el deck. Requieren stems; si el target no tiene
stems disponibles, el UI muestra `STEMS REQUIRED` y no aplica un falso fallback
al mix completo. El BPM de FX y WREKK FX sigue el deck o target activo.

## Auriculares / Monitor CUE

Cada deck tiene un botón **PFL** que enruta su señal pre-fader a los auriculares.
El botón **MST CUE** del master fuerza los auriculares al bus master,
independientemente de los botones de deck.

El fader de mezcla de auriculares (`headphone_mix`) va de 0.0 (CUE puro) a 1.0
(master puro). El nivel de auriculares es independiente del master.

El estado de CUE es coherente entre la UI, el hardware y el Transport. El engine
Rust crea un stream CUE dedicado para salidas FLX4/multicanal compatibles, lee
los buses live pre-fader de Deck A/B y el bus master, aplica `headphone_mix` y
`headphone_level`, y escribe la mezcla de auriculares en los canales 2-3. Si no
encuentra una salida multicanal válida, el PFL queda degradado a control de
estado sin bus físico de auriculares.

## DDJ-FLX4

Wrekker tiene soporte nativo para Pioneer DDJ-FLX4:

- Play/cue.
- Jog wheel.
- Scratch/nudge.
- Sync.
- Master.
- Faders de canal + pregain/trim.
- Crossfader.
- EQ high/mid/low.
- Channel filter (bipolar).
- Stems (pads y controles dedicados; en WREKK mode el hardware puede rutear
  HIGH/MID/LOW a stems, pero los labels visuales del EQ permanecen HIGH/MID/LOW).
- Level meters.
- LEDs de sync (verde/naranja/rojo según calidad de phase lock).
- LEDs de CUE de auriculares (deck A y deck B).
- BeatFX ON/OFF con LED por target de deck.
- Browse knob/press y LOAD A/B para navegar/cargar desde el browser activo.
- Smart CFX / WREKK desde hardware.

El hardware controla `Transport`; el audio sigue ejecutándose en Rust.

## Archivos `.wrk`

Un `.wrk` es un ZIP con estructura de performance:

```text
manifest.json
audio.flac
analysis/beatgrid.json
analysis/beatgrid_auto.json
analysis/markers.json
analysis/markers_auto.json
analysis/corrections.json
analysis/changelog.json
analysis/waveform.*
stems/
fastload/
```

WREKKER LAB migra `.wrk` legacy al abrirlos: copia el beatgrid/markers
existentes a `*_auto.json` si faltan y mantiene `beatgrid.json`/`markers.json`
como la capa activa. Las correcciones se guardan transaccionalmente reemplazando
solo JSON dentro del ZIP; no reencodea audio, no rerun HTDemucs, no rerun Beat
This y no reconstruye PCM de fastload. El cache fastload actualiza solo metadata
de análisis cuando existe.

`analysis/beatgrid.json` schema v2 incluye:

```json
{
  "schema_version": 2,
  "model": "beat_this_v1",
  "bpm": 128.0,
  "bpm_variable": false,
  "confidence": 0.94,
  "swing_factor": 0.12,
  "beat_period_ms": 468.75,
  "beats": [0.234, 0.703],
  "downbeats": [0.234],
  "phrase_markers": [
    {"position_sec": 0.234, "phrase_length": 8, "energy_level": 0.65}
  ]
}
```

## Rendimiento

Baseline esperado:

- Ryzen 7 4700U.
- 12 GB RAM.
- Sin GPU dedicada.
- Linux.

Diseño de rendimiento:

- Audio callback en Rust sin GIL de Python.
- Beat tracking offline con Beat This! durante WREKKED.
- HTDemucs fuera del callback de audio.
- UI a 60 Hz para elementos fluidos (playhead, zoom waveform, overlays).
- Spectrum, peak meters, mini meters y osciloscopios a ~45 Hz.
- Métricas pesadas (LUFS, LEDs de sync) a 10 Hz.
- Fastload cache: almacena audio PCM16 + metadata completa (waveform, beatgrid,
  artwork) en disco plano. Cache HIT en Phase 1 no abre el ZIP `.wrk`. Tiempo
  de carga reproducible < 50 ms desde cache.
- Resampling automático al SR del engine (scipy.signal.resample_poly).

## Solución de Problemas

### No carga `wrekker_engine`

Reconstruye el `.so`:

```bash
cd wrekker/engine_rs
cargo build --release
cp target/release/libwrekker_engine.so ../../wrekker_engine.so
```

### Rubber Band no está activo

Instala la librería:

```bash
sudo apt install librubberband-dev pkg-config
```

Luego recompila el engine.

### Beatgrid con baja confianza

Wrekker guarda el beatgrid aunque `confidence < 0.6`, pero puede marcarlo como
low confidence. En ese caso:

- Revisa visualmente los beat markers.
- Evita usar phrase sync ciegamente.
- Corrige metadata o reanaliza si el archivo estaba mal decodificado.

### `.wrk` corruptos

El scanner mueve `.wrk` corruptos a `_corrupt_wrks/` en vez de borrarlos. Puedes
reparar, eliminar o regenerar esos archivos.

### Artifacts en mezcla

Las rutas críticas de crossfader, gains, scratch, EQ y limiter están en Rust. Si
sigues oyendo artifacts:

- Verifica que estás usando `engine_v2`.
- Recompila `wrekker_engine.so`.
- Baja master/pregain si estás sumando dos tracks full scale.
- Revisa que no haya un `.so` viejo en la raíz del proyecto.

## Desarrollo y Tests

Tests Python:

```bash
python -m pytest -q tests/test_beat_tracker.py tests/test_phrase_sync.py
```

Tests Rust:

```bash
cd wrekker/engine_rs
cargo test
```

Compilación Rust:

```bash
cd wrekker/engine_rs
cargo fmt --check
cargo check
cargo build --release
```

## Packaging y distribución

Wrekker incluye ahora una base de distribución pública:

- First-Time Setup Wizard: en el primer arranque detecta si faltan PyTorch CPU,
  HTDemucs o Beat This! y los instala en el directorio de modelos del usuario.
  Si se cancela, Wrekker arranca en modo degradado: reproduce `.wrk`
  existentes, pero desactiva nueva separación de stems y Beat This!.
- Flatpak: manifiesto en `packaging/flatpak/io.github.wrekker.Wrekker.yml`.
- AppImage: script en `packaging/appimage/build-appimage.sh`.
- Windows beta: scripts NSIS/PowerShell en `packaging/windows/`.
- GitHub Releases: workflow en `.github/workflows/release.yml`, activado por
  tags `v*.*.*`.

Las rutas de configuración se resuelven con `wrekker.config.paths`:

- Linux config: `${XDG_CONFIG_HOME:-~/.config}/wrekker/settings.json`
- Linux data/modelos: `${XDG_DATA_HOME:-~/.local/share}/wrekker/`
- Windows config: `%APPDATA%\Wrekker\settings.json`
- Windows data/modelos: `%LOCALAPPDATA%\Wrekker\`

Antes de empaquetar, el workflow ejecuta `python packaging/check-sensitive-data.py`.
Ese gate bloquea releases si encuentra credenciales SMB, rutas locales con
usuario o configs no sanitizadas. El template público seguro vive en
`wrekker/config/default_settings.json`; no copies tu config local al repo.

Build local de artefactos:

```bash
python packaging/check-sensitive-data.py
packaging/appimage/build-appimage.sh 0.1.0-beta
```

Los builds Flatpak/Windows descargan dependencias externas y están pensados
para CI o una máquina preparada para empaquetado.

## Estado Actual

Implementado:

- Engine Rust CPAL/PyO3 con callback sin GIL.
- Mixer/crossfader Rust con rampas sample-a-sample.
- Limiter final anti-clipping.
- EQ Rust 3 bandas (biquad IIR).
- Scratch Rust con Hermite cúbico y release exponencial.
- Nudge (rim) con one-pole LP τ=60 ms.
- Beat This! offline para `.wrk` (beatgrid schema v2).
- Rubber Band time stretching wrapper (NativeTimeStretch en Rust).
- PhaseSync Rust (NativePhaseSync, PLL con zona muerta y snap).
- PhraseLockSync (Python, sin tocar audio).
- Phrase meter visual a 60 Hz.
- WREKKED prepared library con browser integrado.
- Fastload cache v2 (audio + metadata completa en disco plano).
- Monitor CUE / PFL por deck y MST CUE.
- DDJ-FLX4 driver bidireccional para transporte, mixer, pads, jog, PFL,
  BeatFX normal, browser/load y feedback LED/VU.
- Resampling automático de audio y stems al SR del engine.
- Zoom waveform de alta resolución (256 samples/col, pre-computada en carga).
- FX rack Python↔Rust con BPM tracking a 60 Hz.
- Banco WREKK FX separado, stem-aware, con DSP Rust y UI de prueba.
- WREKKER LAB standalone para corrección de análisis `.wrk`, changelog,
  compare AUTO/ACTIVE, preview con metrónomo, stem monitoring, edición básica
  de grid constante, markers, hot cues y loops.
- WREKKED Management UI (sets, metadata, fastload, rutas configurables).
- Sistema de SETTINGS persistente con perfiles y rutas centralizadas.
- First-Time Setup Wizard para instalar componentes AI pesados fuera del
  instalador base.
- Scaffold de distribución: Flatpak, AppImage, Windows NSIS y GitHub Releases.

Pendientes típicos:

- Edición manual completa de beatgrid en UI.
- UI avanzada para correction markers de beatgrid.
- Selector UI avanzado para elegir manualmente el dispositivo/salida de
  auriculares cuando hay varias interfaces multicanal.
- Mapeo hardware SHIFT + FX para acceder al banco WREKK FX desde DDJ-FLX4.
- Acciones hardware secundarias que siguen como no-op: borrar hot cues con
  SHIFT+pad, reverse/censor, cambio de tempo range y ajuste fino de loop.
- Preview LAB avanzado: selección explícita de dispositivo/salida, routing CUE
  dedicado y controles de latencia. El preview local básico con metrónomo y
  monitor de stems ya está implementado.
- Edición dynamic-tempo con warp anchors en WREKKER LAB.
- Export completo de mixes con render offline `Finer` (Rubber Band).
- Integración más profunda de time stretching independiente de pitch en el
  render principal de decks.
- Soporte de 4 decks (DeckID C/D preparado en el modelo).
