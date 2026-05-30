# Wrekker Qt Quick Visual Migration

## Architecture

Wrekker remains hybrid:

- Rust / CPAL / PyO3: real-time audio, DSP, sync, scratch, PFL, FX and live signal buffers.
- Python / PyQt6: Transport, WREKKED, library, Wrekker LAB edit sessions, `.wrk` persistence and application state.
- PyQt6 Widgets: static workflow panels, tables, forms, menus and inspectors.
- Qt Quick / QML: high-frequency visual scenes.

QML receives QObject view-models and emits interaction intents. It does not own
Transport, LAB persistence, `.wrk` writes, undo/redo or audio logic.

## Migration Rule

Any visual surface that updates, scrolls, animates or repaints continuously above
15 Hz belongs in Qt Quick/QML.

Current high-frequency surfaces identified:

- Main deck zoom waveforms and overview playheads.
- Cross-deck beat overlay and phrase progress.
- Deck spectrum bars and stem/level visual meters.
- Master A/M/B peak meters.
- Deck A / Master / Deck B oscilloscopes.
- Wrekker LAB zoom editor, overview, playhead, grid, marker/cue/loop overlays.

Static surfaces that remain Widgets:

- Library and WREKKED tables.
- Manage WREKKED Library dialogs.
- LAB Beatgrid/Markers/Cues/Loops/History inspector tabs.
- Settings, context menus, status badges and save/revert actions.

## Phase 1 Implementation

The first migrated scene is `wrekker/ui/qml/LabTimeline.qml`.

Python bridge:

- `wrekker.ui.qml_models.LabTimelineModel`
- Exposes position, playing state, selected source, waveform peaks, beatgrid,
  downbeats, phrases, markers, cues and loops.
- Large arrays are republished only on data/source/revision changes.
- Playback updates only `positionSeconds`.

QML scene:

- Renders the LAB waveform source tray.
- Renders the zoom editor and overview in one scene.
- Uses fractional visual interpolation between Python position snapshots.
- Keeps a stable high-contrast playhead.
- Supports seek and source-selection intents back to Python.

Fallback:

- If Qt Quick cannot be loaded, Wrekker LAB uses the existing QWidget timeline.
- This keeps the app runnable in environments with broken QtQuick dependencies.

## Benchmark Status

The requested QQuickWidget vs QQuickView benchmark is not complete in this
environment because importing `PyQt6.QtQuickWidgets` currently fails with:

`/usr/lib/libgssapi_krb5.so.2: undefined symbol: k5_buf_cstring, version krb5support_0_MIT`

The implementation therefore uses a lazy QQuickWidget import and logs a fallback
warning. On a working QtQuick installation, benchmark both:

- `QQuickWidget` embedded in the existing Widget shell.
- `QQuickView` via `QWidget.createWindowContainer()`.

Record:

- FPS per scene.
- Average/max frame interval.
- Position update cadence.
- Visual-position drift from engine/previews.
- Geometry rebuild count.

## Diagnostic Flags

Implemented:

- `WREKKER_QML_FPS_LOG=1`: logs QML LAB scene FPS once per second.
- `WREKKER_WAVEFORM_FPS_LOG=1`: logs FPS from QML waveform models.
- `WREKKER_ENABLE_QML_DECK_WAVEFORMS=1`: requests QML deck timelines.
- `WREKKER_FORCE_UNSTABLE_QML_DECK_WAVEFORMS=1`: enables the currently unstable
  QML deck timeline path for profiling.
- `WREKKER_WAVEFORM_RENDER_DEBUG=1`: logs whether the QML deck renderer or
  QWidget fallback was selected.
- `WREKKER_WAVEFORM_POSITION_DEBUG=1`: enables QWidget waveform position
  diagnostics.

## Next Phases

1. Complete LAB timeline interactions for marker/cue/loop dragging and transient
   feedback.
2. Migrate Deck A/B visual scenes to `DeckVisual.qml`.
3. Migrate master oscilloscopes and A/M/B meters to `MasterVisual.qml`.
4. Remove obsolete animated QWidget paint paths after equivalent QML scenes are
   verified.
