import QtQuick 2.15

Item {
    id: root
    objectName: "DeckTimeline"
    property var model: deckTimelineModel
    property real visualPosition: model ? model.positionSeconds : 0
    property real lastModelPosition: model ? model.positionSeconds : 0
    property double lastSyncMs: Date.now()
    property double nextFrameMs: Date.now()
    property double targetFrameMs: 1000.0 / 60.0

    signal seekRequested(real position)
    signal markerContextRequested(string markerId)

    Timer {
        id: fpsTimer
        interval: 1000
        running: model ? model.fpsLogEnabled : false
        repeat: true
        property int frames: 0
        property double lastMs: Date.now()
        onTriggered: {
            var now = Date.now()
            console.log("[qml-deck-waveform " + model.deckId + "] fps=" + frames + " dt_avg_ms=" + ((now - lastMs) / Math.max(1, frames)).toFixed(2))
            frames = 0
            lastMs = now
        }
    }

    Connections {
        target: root.model || null
        ignoreUnknownSignals: true
        function onPositionSecondsChanged() {
            if (!model)
                return
            var now = Date.now()
            var delta = Math.abs(model.positionSeconds - root.lastModelPosition)
            root.lastModelPosition = model.positionSeconds
            root.lastSyncMs = now
            if (!model.playing || delta > 0.30)
                root.visualPosition = model.positionSeconds
        }
        function onTimelineRevisionChanged() {
            zoom.requestPaint()
            overview.requestPaint()
        }
        function onOtherDeckChanged() {
            zoom.requestPaint()
        }
    }

    Timer {
        interval: 1
        running: true
        repeat: true
        onTriggered: {
            if (!model)
                return
            var frameNow = Date.now()
            if (frameNow < root.nextFrameMs)
                return
            if (frameNow - root.nextFrameMs > root.targetFrameMs * 2.0)
                root.nextFrameMs = frameNow + root.targetFrameMs
            else
                root.nextFrameMs += root.targetFrameMs
            fpsTimer.frames += 1
            if (model.playing) {
                var now = frameNow
                var predicted = model.positionSeconds + (now - root.lastSyncMs) / 1000.0
                var dur = Math.max(0.01, model.durationSeconds)
                if (predicted > dur)
                    predicted = dur
                root.visualPosition = predicted
            } else {
                root.visualPosition = model.positionSeconds
            }
            zoom.visualPosition = root.visualPosition
            overview.visualPosition = root.visualPosition
            zoom.requestPaint()
            overview.requestPaint()
        }
    }

    WrekkerWaveformView {
        id: zoom
        model: root.model
        overview: false
        visualPosition: root.visualPosition
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 92

        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            onPressed: {
                if (mouse.button === Qt.RightButton) {
                    var markerId = root.markerAt(mouse.x, false)
                    root.markerContextRequested(markerId)
                } else {
                    root.seekRequested(zoom.timeFromX(mouse.x))
                }
            }
            onPositionChanged: {
                if (pressed && mouse.buttons & Qt.LeftButton)
                    root.seekRequested(zoom.timeFromX(mouse.x))
            }
        }
    }

    WrekkerWaveformView {
        id: overview
        model: root.model
        overview: true
        visualPosition: root.visualPosition
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: zoom.bottom
        anchors.topMargin: 6
        height: 66

        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            onPressed: {
                if (mouse.button === Qt.RightButton) {
                    var markerId = root.markerAt(mouse.x, true)
                    root.markerContextRequested(markerId)
                } else {
                    root.seekRequested(overview.timeFromX(mouse.x))
                }
            }
            onPositionChanged: {
                if (pressed && mouse.buttons & Qt.LeftButton)
                    root.seekRequested(overview.timeFromX(mouse.x))
            }
        }
    }

    function markerAt(x, overviewMode) {
        if (!model || !model.markers)
            return ""
        var item = overviewMode ? overview : zoom
        var win = item.timelineWindow()
        var start = win[0]
        var span = Math.max(0.1, win[1] - win[0])
        var best = ""
        var bestDx = 9
        for (var i = 0; i < model.markers.length; i++) {
            var m = model.markers[i]
            if ((overviewMode && !m.showOverview) || (!overviewMode && !m.showZoom))
                continue
            var px = item.xForTime(m.position, start, span, item.width)
            var dx = Math.abs(px - x)
            if (dx < bestDx) {
                bestDx = dx
                best = m.id || ""
            }
        }
        return best
    }
}
