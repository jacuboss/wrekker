import QtQuick 2.15
import QtQuick.Controls 2.15

Item {
    id: root
    objectName: "LabTimeline"
    property var model: labTimelineModel
    property real visualPosition: model ? model.positionSeconds : 0
    property real lastModelPosition: model ? model.positionSeconds : 0
    property double lastSyncMs: Date.now()
    property double nextFrameMs: Date.now()
    property double targetFrameMs: 1000.0 / 60.0
    property bool markerTipVisible: false
    property string markerTipText: ""
    property real markerTipX: 0
    property real markerTipY: 0

    signal seekRequested(real position)
    signal sourceSelected(string source)
    signal viewportMoved(real start, real end)

    Timer {
        id: fpsTimer
        interval: 1000
        running: model ? model.fpsLogEnabled : false
        repeat: true
        property int frames: 0
        property double lastMs: Date.now()
        onTriggered: {
            var now = Date.now()
            console.log("[qml-lab] fps=" + frames + " dt_avg_ms=" + ((now - lastMs) / Math.max(1, frames)).toFixed(2))
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
            if (!model.playing || delta > 0.35)
                root.visualPosition = model.positionSeconds
        }
        function onTimelineRevisionChanged() {
            zoomCanvas.requestPaint()
            overviewCanvas.requestPaint()
        }
        function onSelectedSourceChanged() {
            zoomCanvas.requestPaint()
            overviewCanvas.requestPaint()
        }
        function onCompareAutoChanged() {
            zoomCanvas.requestPaint()
            overviewCanvas.requestPaint()
        }
        function onStemMonitorChanged() {
            zoomCanvas.requestPaint()
            overviewCanvas.requestPaint()
        }
    }

    NumberAnimation {
        id: correctionAnim
        target: root
        property: "visualPosition"
        duration: 80
        easing.type: Easing.OutCubic
    }

    Timer {
        id: renderTimer
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
            zoomCanvas.requestPaint()
            overviewCanvas.requestPaint()
        }
    }

    Rectangle {
        anchors.fill: parent
        color: "#050607"
        border.color: "#1e262c"
        border.width: 1
    }

    Row {
        id: sourceRow
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 4
        spacing: 6
        height: 42

        Repeater {
            model: root.model ? root.model.sources : []
            delegate: Rectangle {
                width: 130
                height: 36
                radius: 4
                property int monitorRevision: root.model ? root.model.stemMonitorRevision : 0
                property bool muted: root.model ? root.model.sourceMuted(modelData) : false
                property bool isolated: root.model ? root.model.sourceIsolated(modelData) : false
                color: modelData === root.model.selectedSource ? "#101820" : "#090d10"
                border.width: 1
                border.color: modelData === root.model.selectedSource ? root.model.sourceColor(modelData) : "#26323a"
                opacity: root.model.sourceAvailable(modelData) ? 1.0 : 0.36
                onMonitorRevisionChanged: {
                    muted = root.model ? root.model.sourceMuted(modelData) : false
                    isolated = root.model ? root.model.sourceIsolated(modelData) : false
                }

                MouseArea {
                    id: sourceHit
                    anchors.fill: parent
                    anchors.rightMargin: 48
                    enabled: root.model.sourceAvailable(modelData)
                    onClicked: {
                        root.sourceSelected(modelData)
                        root.model.requestSource(modelData)
                    }
                }

                Row {
                    anchors.fill: parent
                    anchors.margins: 4
                    spacing: 4

                    Text {
                        width: parent.width - 48
                        anchors.verticalCenter: parent.verticalCenter
                        text: modelData
                        color: root.model.sourceColor(modelData)
                        elide: Text.ElideRight
                        font.pixelSize: 10
                        font.bold: true
                    }

                    Rectangle {
                        width: 20
                        height: 22
                        anchors.verticalCenter: parent.verticalCenter
                        radius: 3
                        color: muted ? "#411016" : "#11171b"
                        border.width: 1
                        border.color: muted ? "#ff5c7a" : "#33404a"
                        Text {
                            anchors.centerIn: parent
                            text: "M"
                            color: muted ? "#ff9aae" : "#8a959d"
                            font.pixelSize: 10
                            font.bold: true
                        }
                        MouseArea {
                            anchors.fill: parent
                            enabled: root.model.sourceAvailable(modelData)
                            onClicked: root.model.requestStemMute(modelData)
                        }
                    }

                    Rectangle {
                        width: 20
                        height: 22
                        anchors.verticalCenter: parent.verticalCenter
                        radius: 3
                        color: isolated ? "#342207" : "#11171b"
                        border.width: 1
                        border.color: isolated ? "#ffb000" : "#33404a"
                        Text {
                            anchors.centerIn: parent
                            text: "S"
                            color: isolated ? "#ffd166" : "#8a959d"
                            font.pixelSize: 10
                            font.bold: true
                        }
                        MouseArea {
                            anchors.fill: parent
                            enabled: root.model.sourceAvailable(modelData)
                            onClicked: root.model.requestStemIsolate(modelData)
                        }
                    }
                }
            }
        }
    }

    Canvas {
        id: zoomCanvas
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: sourceRow.bottom
        anchors.bottom: overviewCanvas.top
        anchors.margins: 4
        antialiasing: true

        onPaint: {
            var ctx = getContext("2d")
            root.drawTimeline(ctx, width, height, false)
        }

        MouseArea {
            anchors.fill: parent
            onPressed: root.seekRequested(root.timeFromX(mouse.x, false))
            onPositionChanged: {
                if (pressed)
                    root.seekRequested(root.timeFromX(mouse.x, false))
            }
        }
    }

    Canvas {
        id: overviewCanvas
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 4
        height: 104
        antialiasing: true

        onPaint: {
            var ctx = getContext("2d")
            root.drawTimeline(ctx, width, height, true)
        }

        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onPressed: root.seekRequested(root.timeFromX(mouse.x, true))
            onPositionChanged: {
                if (pressed)
                    root.seekRequested(root.timeFromX(mouse.x, true))
                root.updateMarkerTooltip(mouse.x, mouse.y)
            }
            onExited: root.markerTipVisible = false
        }
    }

    Rectangle {
        id: markerTooltip
        visible: root.markerTipVisible
        x: Math.max(6, Math.min(root.width - width - 6, overviewCanvas.x + root.markerTipX + 10))
        y: overviewCanvas.y + Math.max(4, root.markerTipY - height - 8)
        width: tipText.implicitWidth + 16
        height: 24
        radius: 4
        color: "#050708"
        border.width: 1
        border.color: "#39444c"
        z: 20

        Text {
            id: tipText
            anchors.centerIn: parent
            text: root.markerTipText
            color: "#f7fbff"
            font.pixelSize: 11
            font.bold: true
        }
    }

    function sourcePeaks() {
        return model ? model.waveformPeaks : []
    }

    function timelineWindow(overview) {
        var duration = Math.max(0.01, model ? model.durationSeconds : 1)
        if (overview)
            return [0, duration]
        var windowS = model ? model.zoomWindowSeconds : 12
        var start = Math.max(0, Math.min(duration - windowS, visualPosition - windowS / 2))
        var end = Math.min(duration, Math.max(windowS, start + windowS))
        return [start, Math.max(start + 0.1, end)]
    }

    function xForTime(t, start, span, width) {
        return (t - start) / span * width
    }

    function timeFromX(x, overview) {
        var win = timelineWindow(overview)
        return Math.max(0, Math.min(model.durationSeconds, win[0] + (x / Math.max(1, overview ? overviewCanvas.width : zoomCanvas.width)) * (win[1] - win[0])))
    }

    function updateMarkerTooltip(mx, my) {
        if (!model || !model.markers || model.markers.length === 0) {
            markerTipVisible = false
            return
        }
        var win = timelineWindow(true)
        var start = win[0]
        var span = Math.max(0.1, win[1] - win[0])
        var best = null
        var bestDx = 999999
        for (var i = 0; i < model.markers.length; i++) {
            var marker = model.markers[i]
            var x = xForTime(marker.position, start, span, overviewCanvas.width)
            var dx = Math.abs(x - mx)
            if (dx < bestDx) {
                bestDx = dx
                best = marker
            }
        }
        if (best && bestDx <= 8) {
            markerTipText = (best.label || "MARKER") + " · " + formatTime(best.position)
            markerTipX = mx
            markerTipY = my
            markerTipVisible = true
        } else {
            markerTipVisible = false
        }
    }

    function formatTime(seconds) {
        var total = Math.max(0, seconds)
        var m = Math.floor(total / 60)
        var s = total - m * 60
        return m + ":" + (s < 10 ? "0" : "") + s.toFixed(1)
    }

    function drawTimeline(ctx, w, h, overview) {
        ctx.clearRect(0, 0, w, h)
        ctx.fillStyle = overview ? "#07090b" : "#050607"
        ctx.fillRect(0, 0, w, h)
        ctx.strokeStyle = "#1e262c"
        ctx.lineWidth = 1
        ctx.strokeRect(0.5, 0.5, w - 1, h - 1)
        if (!model)
            return

        var duration = Math.max(0.01, model.durationSeconds)
        var win = timelineWindow(overview)
        var start = win[0]
        var end = win[1]
        var span = Math.max(0.1, end - start)
        var peaks = sourcePeaks()
        var mid = h * 0.52
        var maxH = h * (overview ? 0.27 : 0.35)
        var color = model.sourceColor(model.selectedSource)
        drawAnchoredWaveform(ctx, peaks, start, span, duration, w, mid, maxH, color, overview)

        drawGrid(ctx, model.compareAuto ? model.autoBeats : [], start, span, w, h, "#737d84", 0.32, overview ? 8 : 4)
        drawGrid(ctx, model.activeBeats, start, span, w, h, "#ffffff", overview ? 0.16 : 0.22, overview ? 4 : 1)
        drawGrid(ctx, model.downbeats, start, span, w, h, "#ffb000", overview ? 0.38 : 0.62, 1)

        ctx.fillStyle = "rgba(210,190,95,0.22)"
        for (var p = 0; p < model.phrases.length; p++) {
            var ph = model.phrases[p]
            if (ph < start || ph > end)
                continue
            var px = xForTime(ph, start, span, w)
            ctx.fillRect(px, 2, overview ? 1 : 2, overview ? 9 : 14)
        }

        drawMarkers(ctx, model.markers, start, span, w, h, overview)
        drawCues(ctx, model.cues, start, span, w, h)
        drawLoops(ctx, model.loops, start, span, w, h)
        drawBeatgridEditMarkers(ctx, model.beatgridEdits, start, span, w, h, overview)

        if (overview) {
            var zw = timelineWindow(false)
            var vx1 = xForTime(zw[0], 0, duration, w)
            var vx2 = xForTime(zw[1], 0, duration, w)
            ctx.fillStyle = "rgba(255,176,0,0.10)"
            ctx.fillRect(vx1, 3, Math.max(4, vx2 - vx1), h - 6)
            ctx.strokeStyle = "rgba(255,176,0,0.58)"
            ctx.strokeRect(vx1 + 0.5, 3.5, Math.max(4, vx2 - vx1), h - 7)
        }

        var playX = xForTime(visualPosition, start, span, w)
        if (playX >= 0 && playX <= w) {
            ctx.strokeStyle = "#ff4fd8"
            ctx.lineWidth = 4
            ctx.beginPath()
            ctx.moveTo(playX, 0)
            ctx.lineTo(playX, h)
            ctx.stroke()
            ctx.strokeStyle = "#f7fbff"
            ctx.lineWidth = 2
            ctx.beginPath()
            ctx.moveTo(playX, 0)
            ctx.lineTo(playX, h)
            ctx.stroke()
            ctx.fillStyle = "#f7fbff"
            ctx.fillRect(playX - 4, 2, 8, 14)
        }

        ctx.fillStyle = "#7e8a92"
        ctx.font = "11px sans-serif"
        ctx.fillText((overview ? "OVERVIEW / STRUCTURE" : "ZOOM EDITOR") + " · " + model.selectedSource, 8, 16)
    }

    function drawAnchoredWaveform(ctx, peaks, start, span, duration, w, mid, maxH, color, overview) {
        if (!peaks || peaks.length < 2)
            return
        var targetGapPx = overview ? 3.0 : 24.0
        var secondsPerPixel = span / Math.max(1, w)
        var colSeconds = duration / Math.max(1, peaks.length)
        var stride = Math.max(1, Math.round((secondsPerPixel * targetGapPx) / Math.max(0.0001, colSeconds)))
        var barSeconds = stride * colSeconds
        var barWidth = overview ? Math.max(1.4, Math.min(3.0, (barSeconds / span) * w * 0.62)) : 2.0
        var first = Math.max(0, Math.floor((start / colSeconds) / stride) * stride - stride)
        var last = Math.min(peaks.length - 1, Math.ceil(((start + span) / colSeconds) / stride) * stride + stride)

        ctx.fillStyle = model.withAlpha(color, overview ? 0.30 : 0.44)
        for (var i = first; i <= last; i += stride) {
            var amp = 0
            var end = Math.min(peaks.length, i + stride)
            for (var j = i; j < end; j++)
                amp = Math.max(amp, Math.abs(peaks[j]))
            var centerTime = (i + stride * 0.5) * colSeconds
            var x = xForTime(centerTime, start, span, w)
            if (x < -barWidth || x > w + barWidth)
                continue
            var y = Math.max(1, amp * maxH)
            ctx.fillRect(x - barWidth * 0.5, mid - y, barWidth, y * 2)
        }
    }

    function drawGrid(ctx, values, start, span, w, h, color, alpha, every) {
        ctx.strokeStyle = model.withAlpha(color, alpha)
        ctx.lineWidth = 1
        for (var i = 0; i < values.length; i += every) {
            var t = values[i]
            if (t < start || t > start + span)
                continue
            var x = xForTime(t, start, span, w)
            ctx.beginPath()
            ctx.moveTo(x, h * 0.16)
            ctx.lineTo(x, h)
            ctx.stroke()
        }
    }

    function drawBeatgridEditMarkers(ctx, edits, start, span, w, h, overview) {
        if (!edits)
            return
        ctx.font = overview ? "9px sans-serif" : "10px sans-serif"
        ctx.textBaseline = "top"
        for (var i = 0; i < edits.length; i++) {
            var e = edits[i]
            var t = e.position
            if (t < start || t > start + span)
                continue
            var x = xForTime(t, start, span, w)
            var color = e.color || "#ffb000"
            var top = e.kind === "firstBeat" ? 22 : e.kind === "downbeat" ? 39 : 56
            if (overview && e.kind === "phrase")
                continue
            ctx.strokeStyle = model.withAlpha(color, overview ? 0.48 : 0.86)
            ctx.lineWidth = e.kind === "phrase" ? 1 : 2
            ctx.beginPath()
            ctx.moveTo(x, top)
            ctx.lineTo(x, h - 22)
            ctx.stroke()
            if (!overview) {
                var label = e.label || ""
                var tw = ctx.measureText(label).width + 10
                ctx.fillStyle = "rgba(5,7,8,0.86)"
                ctx.fillRect(x + 4, top - 2, tw, 15)
                ctx.fillStyle = color
                ctx.fillText(label, x + 8, top)
            }
        }
    }

    function drawMarkers(ctx, markers, start, span, w, h, overview) {
        for (var i = 0; i < markers.length; i++) {
            var m = markers[i]
            var t = m.position
            if (t < start || t > start + span)
                continue
            var x = xForTime(t, start, span, w)
            var tail = m.tier === "primary" ? 18 : m.tier === "secondary" ? 13 : 8
            ctx.strokeStyle = model.withAlpha(m.color, m.tier === "primary" ? 0.82 : m.tier === "secondary" ? 0.52 : 0.28)
            ctx.lineWidth = m.tier === "primary" && !overview ? 2 : 1
            ctx.beginPath()
            ctx.moveTo(x, h - tail)
            ctx.lineTo(x, h)
            ctx.stroke()
        }
    }

    function drawCues(ctx, cues, start, span, w, h) {
        for (var i = 0; i < cues.length; i++) {
            var c = cues[i]
            if (c.position < start || c.position > start + span)
                continue
            var x = xForTime(c.position, start, span, w)
            ctx.fillStyle = c.color
            ctx.beginPath()
            ctx.moveTo(x, 4)
            ctx.lineTo(x - 5, 14)
            ctx.lineTo(x + 5, 14)
            ctx.closePath()
            ctx.fill()
        }
    }

    function drawLoops(ctx, loops, start, span, w, h) {
        ctx.fillStyle = "rgba(24,216,255,0.20)"
        for (var i = 0; i < loops.length; i++) {
            var l = loops[i]
            if (l.end < start || l.start > start + span)
                continue
            var x1 = xForTime(Math.max(l.start, start), start, span, w)
            var x2 = xForTime(Math.min(l.end, start + span), start, span, w)
            ctx.fillRect(x1, 18, Math.max(2, x2 - x1), 8)
        }
    }
}
