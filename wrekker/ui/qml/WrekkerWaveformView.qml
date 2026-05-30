import QtQuick 2.15

Canvas {
    id: view

    property var model
    property bool overview: false
    property real visualPosition: model ? model.positionSeconds : 0
    property string accentColor: model ? model.accentColor : "#18d8ff"
    property real zoomBeats: 8

    antialiasing: true

    onPaint: {
        var ctx = getContext("2d")
        draw(ctx, width, height)
    }

    function timelineWindow() {
        var duration = Math.max(0.01, model ? model.durationSeconds : 1)
        if (overview)
            return [0, duration]
        var bpm = Math.max(1, model ? model.bpm : 120)
        var windowS = zoomBeats * (60.0 / bpm)
        var start = Math.max(0, Math.min(duration - windowS, visualPosition - windowS / 2))
        return [start, Math.min(duration, start + windowS)]
    }

    function timeFromX(x) {
        var win = timelineWindow()
        return Math.max(0, Math.min(model.durationSeconds, win[0] + (x / Math.max(1, width)) * (win[1] - win[0])))
    }

    function xForTime(t, start, span, w) {
        return (t - start) / span * w
    }

    function draw(ctx, w, h) {
        ctx.clearRect(0, 0, w, h)
        ctx.fillStyle = overview ? "#07090b" : "#050607"
        ctx.fillRect(0, 0, w, h)
        ctx.strokeStyle = "#1e262c"
        ctx.lineWidth = 1
        ctx.strokeRect(0.5, 0.5, w - 1, h - 1)
        if (!model)
            return

        var duration = Math.max(0.01, model.durationSeconds)
        var win = timelineWindow()
        var start = win[0]
        var end = win[1]
        var span = Math.max(0.1, end - start)
        var peaks = overview ? model.overviewPeaks : model.zoomPeaks
        drawAnchoredWaveform(ctx, peaks, start, span, duration, w, h, overview ? 0.30 : 0.58)
        drawLoop(ctx, start, span, w, h)
        drawBeats(ctx, model.beats, start, span, w, h)
        if (!overview)
            drawOtherBeats(ctx, start, span, w, h)
        drawMarkers(ctx, model.markers, start, span, w, h)
        drawCues(ctx, model.cues, start, span, w, h)

        if (overview)
            drawOverviewViewport(ctx, duration, w, h)

        var playX = overview ? xForTime(visualPosition, start, span, w) : w / 2
        if (playX >= 0 && playX <= w) {
            ctx.strokeStyle = "#f7fbff"
            ctx.lineWidth = 2
            ctx.beginPath()
            ctx.moveTo(playX, 0)
            ctx.lineTo(playX, h)
            ctx.stroke()
            if (overview) {
                ctx.fillStyle = "#f7fbff"
                ctx.beginPath()
                ctx.moveTo(playX, 0)
                ctx.lineTo(playX - 5, 8)
                ctx.lineTo(playX + 5, 8)
                ctx.closePath()
                ctx.fill()
            }
        }
    }

    function drawAnchoredWaveform(ctx, peaks, start, span, duration, w, h, alpha) {
        if (!peaks || peaks.length < 2)
            return
        var mid = h * 0.50
        var maxH = h * (overview ? 0.30 : 0.40)
        var targetGapPx = overview ? 2.4 : 4.0
        var secondsPerPixel = span / Math.max(1, w)
        var colSeconds = duration / Math.max(1, peaks.length)
        var stride = Math.max(1, Math.round((secondsPerPixel * targetGapPx) / Math.max(0.0001, colSeconds)))
        var barSeconds = stride * colSeconds
        var barWidth = Math.max(1.2, Math.min(overview ? 2.6 : 3.8, (barSeconds / span) * w * 0.58))
        var first = Math.max(0, Math.floor((start / colSeconds) / stride) * stride - stride)
        var last = Math.min(peaks.length - 1, Math.ceil(((start + span) / colSeconds) / stride) * stride + stride)

        ctx.fillStyle = model.withAlpha(accentColor, alpha)
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

    function drawBeats(ctx, beats, start, span, w, h) {
        ctx.lineWidth = 1
        for (var i = 0; i < beats.length; i++) {
            var t = beats[i]
            if (t < start || t > start + span)
                continue
            var x = xForTime(t, start, span, w)
            var down = i % 4 === 0
            ctx.strokeStyle = down ? "rgba(255,176,0,0.58)" : "rgba(255,255,255,0.18)"
            ctx.beginPath()
            ctx.moveTo(x, down ? 0 : h * 0.34)
            ctx.lineTo(x, h)
            ctx.stroke()
        }
    }

    function drawOtherBeats(ctx, start, span, w, h) {
        if (!model.otherBeats || model.otherBeats.length === 0 || model.otherBpm <= 0)
            return
        var half = span / 2.0
        ctx.lineWidth = 1
        for (var i = 0; i < model.otherBeats.length; i++) {
            var offset = model.otherBeats[i] - model.otherPositionSeconds
            if (offset < -half || offset > half)
                continue
            var x = (offset + half) / span * w
            ctx.strokeStyle = i % 4 === 0 ? model.withAlpha(accentColor, 0.34) : model.withAlpha(accentColor, 0.16)
            ctx.beginPath()
            ctx.moveTo(x, i % 4 === 0 ? 0 : h * 0.40)
            ctx.lineTo(x, h)
            ctx.stroke()
        }
    }

    function drawMarkers(ctx, markers, start, span, w, h) {
        for (var i = 0; i < markers.length; i++) {
            var m = markers[i]
            if ((overview && !m.showOverview) || (!overview && !m.showZoom))
                continue
            if (m.position < start || m.position > start + span)
                continue
            var x = xForTime(m.position, start, span, w)
            var tail = m.tier === "primary" ? 18 : m.tier === "secondary" ? 13 : 8
            ctx.strokeStyle = model.withAlpha(m.color, m.tier === "primary" ? 0.86 : m.tier === "secondary" ? 0.56 : 0.30)
            ctx.lineWidth = m.tier === "primary" && !overview ? 2 : 1
            ctx.beginPath()
            ctx.moveTo(x, h - tail)
            ctx.lineTo(x, h)
            ctx.stroke()
            if (!overview && m.tier === "primary") {
                ctx.font = "9px sans-serif"
                var label = m.label || ""
                var tw = Math.min(70, Math.max(26, ctx.measureText(label).width + 8))
                ctx.fillStyle = "rgba(5,7,8,0.72)"
                ctx.fillRect(Math.min(w - tw - 1, x + 3), 2, tw, 13)
                ctx.fillStyle = m.color
                ctx.fillText(label, Math.min(w - tw - 1, x + 3) + 4, 12)
            }
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
            ctx.moveTo(x, 1)
            ctx.lineTo(x - 5, 10)
            ctx.lineTo(x + 5, 10)
            ctx.closePath()
            ctx.fill()
        }
    }

    function drawLoop(ctx, start, span, w, h) {
        if (model.loopEnd <= model.loopStart)
            return
        if (model.loopEnd < start || model.loopStart > start + span)
            return
        var x1 = xForTime(Math.max(model.loopStart, start), start, span, w)
        var x2 = xForTime(Math.min(model.loopEnd, start + span), start, span, w)
        ctx.fillStyle = "rgba(255,159,67,0.18)"
        ctx.fillRect(x1, 0, Math.max(2, x2 - x1), h)
        ctx.strokeStyle = "rgba(255,159,67,0.84)"
        ctx.lineWidth = 1
        ctx.strokeRect(x1 + 0.5, 0.5, Math.max(2, x2 - x1), h - 1)
    }

    function drawOverviewViewport(ctx, duration, w, h) {
        var zw = timelineWindowForZoom(duration)
        var x1 = xForTime(zw[0], 0, duration, w)
        var x2 = xForTime(zw[1], 0, duration, w)
        ctx.fillStyle = "rgba(255,176,0,0.08)"
        ctx.fillRect(x1, 3, Math.max(4, x2 - x1), h - 6)
        ctx.strokeStyle = "rgba(255,176,0,0.48)"
        ctx.strokeRect(x1 + 0.5, 3.5, Math.max(4, x2 - x1), h - 7)
    }

    function timelineWindowForZoom(duration) {
        var bpm = Math.max(1, model ? model.bpm : 120)
        var windowS = zoomBeats * (60.0 / bpm)
        var start = Math.max(0, Math.min(duration - windowS, visualPosition - windowS / 2))
        return [start, Math.min(duration, start + windowS)]
    }
}
