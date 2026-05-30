"""
Auto marker detection — analyzes beatgrid and stem energy to suggest
DJ cue points, switch points, and WREKK performance points.

All detection runs offline during WREKKED preparation. Zero audio-callback impact.
Results are stored in analysis/markers.json inside the .wrk ZIP.
"""
from __future__ import annotations

import bisect
import hashlib
import os
from typing import TYPE_CHECKING

import numpy as np

from wrekker.core.deck import MARKER_MIN_CONFIDENCE, AutoMarker, MarkerType

__all__ = ["AutoMarkerDetector"]


def _make_id(marker_type: MarkerType, position_s: float) -> str:
    key = f"{marker_type.value}:{position_s:.4f}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class AutoMarkerDetector:
    """
    Analyzes beatgrid and stem energy to produce AutoMarker suggestions.

    Inputs are the same data already computed during WREKKED preparation:
    the beatgrid dict (schema v2) and the stem_energy array from _compute_stem_energy().
    Detection is deterministic — same input always produces the same markers.
    """

    # Stem energy thresholds (fraction of total per-column energy)
    _STEM_PRESENT  = 0.08   # minimum to consider a stem present
    _STEM_ACTIVE   = 0.18   # clearly contributing
    _STEM_DOMINANT = 0.40   # one stem rules the mix

    # Energy transition detection
    _DROP_RATIO  = 1.40   # post/pre total energy ratio → DROP
    _BREAK_RATIO = 0.62   # post/pre total energy ratio → BREAKDOWN (below this)

    # Vocal threshold for crossings
    _VOCAL_CROSS = 0.11
    _STEM_CROSS  = 0.09

    # Minimum confidence to emit any marker
    _MIN_CONF = MARKER_MIN_CONFIDENCE

    # Column indices into stem_energy (N, 4): vocals, drums, bass, other
    _VOC = 0
    _DRM = 1
    _BSS = 2
    _OTH = 3

    def analyze(
        self,
        beatgrid:       dict,
        stem_energy:    "np.ndarray",   # (N, 4) float32
        waveform_peaks: "np.ndarray",   # (N,) float32
        duration_s:     float,
    ) -> list[AutoMarker]:
        """
        Run all detection passes and return markers sorted by position.

        beatgrid    : raw dict from analysis/beatgrid.json (schema v2 preferred)
        stem_energy : (N, 4) float32 — per-waveform-column per-stem energy
        waveform_peaks: (N,) float32 — amplitude envelope 0-1
        duration_s  : track duration in seconds
        """
        beats      = beatgrid.get("beats",         [])
        downbeats  = beatgrid.get("downbeats",      [])
        phrases    = beatgrid.get("phrase_markers", [])
        bpm        = float(beatgrid.get("bpm", 120.0) or 120.0)
        confidence = float(beatgrid.get("confidence", 0.5) or 0.5)

        structural = self._detect_structural(beats, downbeats, phrases, confidence)

        if stem_energy is None or stem_energy.shape[0] < 16:
            result = [m for m in structural if m.confidence >= self._MIN_CONF]
            result.sort(key=lambda m: m.position_s)
            return result

        n   = stem_energy.shape[0]
        dur = max(float(duration_s), 1.0)

        def col_to_s(i: float) -> float:
            return float(i) / n * dur

        def s_to_col(t: float) -> int:
            return max(0, min(n - 1, int(float(t) / dur * n)))

        # Normalize: per-column fractions (N, 4), clamped so each row sums to 1
        total_raw = stem_energy.sum(axis=1)                      # (N,)
        valid     = total_raw > 1e-6
        norm      = np.zeros_like(stem_energy)
        norm[valid] = stem_energy[valid] / total_raw[valid, np.newaxis]

        beat_period_s = 60.0 / max(bpm, 40.0)
        win_s         = beat_period_s * 16     # ~4 bars for energy windows

        markers: list[AutoMarker] = []
        markers.extend(structural)
        markers.extend(self._detect_energy_transitions(
            beats, downbeats, phrases, norm, total_raw,
            col_to_s, s_to_col, n, dur, win_s, beat_period_s,
        ))
        w_structural = self._detect_wrekk_structural_events(
            beats, downbeats, phrases, norm, total_raw,
            col_to_s, s_to_col, n, dur, beat_period_s,
        )
        markers.extend(w_structural)
        markers.extend(self._detect_wrekk_opportunities(
            w_structural, phrases, norm, total_raw, s_to_col, n, dur, beat_period_s,
        ))
        markers.extend(self._detect_mix_points(
            beats, downbeats, phrases, norm, total_raw,
            col_to_s, s_to_col, n, dur, beat_period_s,
        ))
        markers.extend(self._detect_switch_points(
            beats, downbeats, phrases, norm, total_raw,
            col_to_s, s_to_col, n, dur, beat_period_s,
        ))

        markers = [m for m in markers if m.confidence >= self._MIN_CONF]
        markers.sort(key=lambda m: m.position_s)
        if os.environ.get("WREKKER_W_MARKER_DEBUG", "0") == "1":
            w_struct = sum(1 for m in markers if getattr(m, "category", None) == "wrekk" and getattr(m, "family", None) == "structural")
            w_opp = sum(1 for m in markers if getattr(m, "category", None) == "wrekk" and getattr(m, "family", None) == "opportunity")
            print(f"[w-markers] structural={w_struct} opportunities={w_opp} total={len(markers)}", flush=True)
        return markers

    # ── Structural markers ────────────────────────────────────────────────────

    def _detect_structural(
        self,
        beats:      list,
        downbeats:  list,
        phrases:    list,
        confidence: float,
    ) -> list[AutoMarker]:
        out: list[AutoMarker] = []

        if beats:
            pos = float(beats[0])
            out.append(AutoMarker(
                id=_make_id(MarkerType.FIRST_BEAT, pos),
                type=MarkerType.FIRST_BEAT,
                label="1",
                position_s=pos,
                confidence=confidence,
                reason="first detected beat",
                beat_index=0,
            ))

        if downbeats:
            pos = float(downbeats[0])
            out.append(AutoMarker(
                id=_make_id(MarkerType.FIRST_DOWNBEAT, pos),
                type=MarkerType.FIRST_DOWNBEAT,
                label="↓1",
                position_s=pos,
                confidence=confidence,
                reason="first detected downbeat",
                beat_index=0,
            ))

        for pi, ph in enumerate(phrases):
            if isinstance(ph, dict):
                pos    = float(ph.get("position_sec", 0.0))
                ph_len = int(ph.get("phrase_length", 8))
            else:
                pos    = float(ph)
                ph_len = 8
            out.append(AutoMarker(
                id=_make_id(MarkerType.PHRASE, pos),
                type=MarkerType.PHRASE,
                label="PHRASE",
                position_s=pos,
                confidence=min(0.90, confidence),
                reason=f"phrase boundary ({ph_len} bars)",
                phrase_index=pi,
            ))

        return out

    # ── Energy transition detection ───────────────────────────────────────────

    def _detect_energy_transitions(
        self,
        beats, downbeats, phrases,
        norm, total_raw,
        col_to_s, s_to_col, n, dur, win_s, beat_period_s,
    ) -> list[AutoMarker]:
        out: list[AutoMarker] = []

        # Candidate positions: phrase boundaries + every 4th downbeat
        candidates: list[float] = []
        for ph in phrases:
            pos = float(ph.get("position_sec", 0.0) if isinstance(ph, dict) else ph)
            candidates.append(pos)
        for i in range(0, len(downbeats), 4):
            candidates.append(float(downbeats[i]))
        candidates = sorted(set(candidates))

        min_gap   = beat_period_s * 16   # minimum gap between same-type markers
        last_drop = last_break = -999.0

        for pos in candidates:
            margin = beat_period_s * 4
            if pos < margin or pos > dur - margin:
                continue

            pc1 = s_to_col(max(0.0, pos - win_s))
            pc2 = s_to_col(pos)
            qc1 = s_to_col(pos)
            qc2 = s_to_col(min(dur, pos + win_s))

            if pc2 <= pc1 or qc2 <= qc1:
                continue

            pre_e  = float(total_raw[pc1:pc2].mean())
            post_e = float(total_raw[qc1:qc2].mean())
            ratio  = post_e / max(pre_e, 1e-6)

            pre_drm  = float(norm[pc1:pc2, self._DRM].mean())
            post_drm = float(norm[qc1:qc2, self._DRM].mean())

            # DROP: energy jumps up, drums increase
            if (ratio >= self._DROP_RATIO
                    and post_drm > pre_drm + 0.04
                    and pos - last_drop > min_gap):
                conf = min(0.95, (ratio - 1.0) * 0.35 + 0.45)
                sp   = self._stem_profile(norm, qc1, qc2)
                out.append(AutoMarker(
                    id=_make_id(MarkerType.DROP, pos),
                    type=MarkerType.DROP,
                    label="DROP",
                    position_s=pos,
                    confidence=conf,
                    reason=f"energy ×{ratio:.1f} at phrase boundary",
                    stem_profile=sp,
                    energy_before=pre_e,
                    energy_after=post_e,
                ))
                last_drop = pos

            # BREAKDOWN: energy drops, pre-window was active
            elif (ratio <= self._BREAK_RATIO
                    and pre_e > 0.03
                    and pos - last_break > min_gap):
                conf = min(0.92, (1.0 - ratio) * 0.50 + 0.32)
                sp   = self._stem_profile(norm, qc1, qc2)
                out.append(AutoMarker(
                    id=_make_id(MarkerType.BREAKDOWN, pos),
                    type=MarkerType.BREAKDOWN,
                    label="BRK",
                    position_s=pos,
                    confidence=conf,
                    reason=f"energy drops to {ratio:.0%} at phrase boundary",
                    stem_profile=sp,
                    energy_before=pre_e,
                    energy_after=post_e,
                ))
                last_break = pos

        return out

    # ── Stem activity markers ─────────────────────────────────────────────────

    def _detect_wrekk_structural_events(
        self,
        beats, downbeats, phrases,
        norm, total_raw,
        col_to_s, s_to_col, n, dur, beat_period_s,
    ) -> list[AutoMarker]:
        """Detect persistent stem state changes at musical boundaries."""
        out: list[AutoMarker] = []
        candidates = self._wrekk_candidates(downbeats, phrases)
        if not candidates:
            candidates = [float(b) for b in beats[::16]]

        bars4 = beat_period_s * 16
        cooldown = {
            MarkerType.VOCAL_IN: beat_period_s * 32,
            MarkerType.VOCAL_OUT: beat_period_s * 32,
            MarkerType.BASS_IN: beat_period_s * 32,
            MarkerType.BASS_OUT: beat_period_s * 32,
            MarkerType.KICK_IN: beat_period_s * 24,
            MarkerType.KICK_OUT: beat_period_s * 24,
            MarkerType.TOP_IN: beat_period_s * 32,
            MarkerType.TOP_OUT: beat_period_s * 32,
        }
        last: dict[MarkerType, float] = {}
        rules = (
            (self._VOC, MarkerType.VOCAL_IN, MarkerType.VOCAL_OUT, "VOC IN", "VOC OUT", "VOCALS", 0.06, 0.15, 0.16),
            (self._BSS, MarkerType.BASS_IN, MarkerType.BASS_OUT, "BSS IN", "BSS OUT", "BASS", 0.08, 0.18, 0.18),
            (self._DRM, MarkerType.KICK_IN, MarkerType.KICK_OUT, "KICK IN", "KICK OUT", "DRUMS", 0.16, 0.34, 0.20),
            (self._OTH, MarkerType.TOP_IN, MarkerType.TOP_OUT, "TOP IN", "TOP OUT", "OTHER", 0.08, 0.18, 0.16),
        )

        phrase_positions = {
            round(float(ph.get("position_sec", 0.0) if isinstance(ph, dict) else ph), 3)
            for ph in phrases
        }

        for pos in candidates:
            if pos < bars4 or pos > dur - bars4:
                continue
            pc1, pc2 = s_to_col(pos - bars4), s_to_col(pos)
            qc1, qc2 = s_to_col(pos), s_to_col(pos + bars4)
            if pc2 <= pc1 or qc2 <= qc1:
                continue
            phrase_aligned = any(abs(pos - pp) <= beat_period_s * 0.5 for pp in phrase_positions)
            for col, type_in, type_out, label_in, label_out, stem, low_thr, high_thr, delta_thr in rules:
                before = float(norm[pc1:pc2, col].mean())
                after = float(norm[qc1:qc2, col].mean())
                delta = after - before
                marker_type = None
                label = ""
                direction = ""
                if before <= low_thr and after >= high_thr and delta >= delta_thr:
                    marker_type, label, direction = type_in, label_in, "increased"
                elif before >= high_thr and after <= low_thr and -delta >= delta_thr:
                    marker_type, label, direction = type_out, label_out, "dropped"
                if marker_type is None:
                    continue
                if pos - last.get(marker_type, -99999.0) < cooldown[marker_type]:
                    continue
                last[marker_type] = pos
                conf = min(0.96, 0.60 + abs(delta) * 1.25 + (0.08 if phrase_aligned else 0.0))
                evidence = {
                    "before_activity": round(before, 4),
                    "after_activity": round(after, 4),
                    "delta": round(delta, 4),
                    "persistence_bars": 4,
                    "phrase_alignment": phrase_aligned,
                    "stem": stem,
                }
                reason = (
                    f"{stem.title()} activity {direction} from {before:.2f} to {after:.2f} "
                    f"and persisted for 4 bars"
                    + (" at a phrase boundary." if phrase_aligned else ".")
                )
                out.append(AutoMarker(
                    id=_make_id(marker_type, pos),
                    type=marker_type,
                    label=label,
                    position_s=pos,
                    confidence=conf,
                    reason=reason,
                    stem_profile=self._stem_profile(norm, qc1, qc2),
                    energy_before=float(total_raw[pc1:pc2].mean()),
                    energy_after=float(total_raw[qc1:qc2].mean()),
                    category="wrekk",
                    family="structural",
                    stem_targets=(stem,),
                    evidence=evidence,
                    live_visibility="expanded",
                ))
        return out

    def _detect_wrekk_opportunities(
        self,
        events: list[AutoMarker],
        phrases,
        norm,
        total_raw,
        s_to_col,
        n,
        dur,
        beat_period_s,
    ) -> list[AutoMarker]:
        """Derive sparse live-use WREKK opportunities from structural events."""
        out: list[AutoMarker] = []
        phrase_positions = [
            float(ph.get("position_sec", 0.0) if isinstance(ph, dict) else ph)
            for ph in phrases
        ]

        def phrase_aligned(pos: float) -> bool:
            return any(abs(pos - pp) <= beat_period_s * 0.75 for pp in phrase_positions)

        def nearby(pos: float, types: set[MarkerType], width: float = 2.0) -> list[AutoMarker]:
            return [
                ev for ev in events
                if ev.type in types and abs(ev.position_s - pos) <= beat_period_s * width
            ]

        used: dict[MarkerType, float] = {}

        for ev in events:
            if ev.type == MarkerType.VOCAL_OUT and ev.confidence >= 0.84 and phrase_aligned(ev.position_s):
                if ev.position_s - used.get(MarkerType.VOCAL_GHOST, -99999.0) < beat_period_s * 64:
                    continue
                used[MarkerType.VOCAL_GHOST] = ev.position_s
                ci = s_to_col(ev.position_s)
                post = norm[ci:min(n, s_to_col(ev.position_s + beat_period_s * 32))]
                space = 1.0 - float(post[:, self._VOC].mean()) if len(post) else 0.0
                conf = min(0.97, ev.confidence * 0.72 + space * 0.24 + 0.08)
                if conf >= 0.88:
                    out.append(self._opportunity_marker(
                        MarkerType.VOCAL_GHOST, "GHOST", ev.position_s, conf,
                        "Vocal activity ended at a phrase boundary, leaving space for VOCAL GHOST.",
                        ("VOCALS",), (ev.id,), {"space_after": round(space, 4), "derived_from": ev.type.value},
                    ))

        boundary_positions = sorted({round(ev.position_s, 3) for ev in events})
        for pos in boundary_positions:
            if not phrase_aligned(pos):
                continue
            outs = nearby(pos, {MarkerType.BASS_OUT, MarkerType.KICK_OUT, MarkerType.VOCAL_OUT})
            ins = nearby(pos, {MarkerType.BASS_IN, MarkerType.KICK_IN, MarkerType.VOCAL_IN, MarkerType.TOP_IN})
            if outs:
                conf = min(0.96, 0.74 + 0.08 * len(outs) + max(ev.confidence for ev in outs) * 0.12)
                if conf >= 0.88 and pos - used.get(MarkerType.DECONSTRUCT, -99999.0) >= beat_period_s * 64:
                    used[MarkerType.DECONSTRUCT] = pos
                    stems = tuple(sorted({s for ev in outs for s in (ev.stem_targets or ())}))
                    out.append(self._opportunity_marker(
                        MarkerType.DECONSTRUCT, "DECONSTRUCT", pos, conf,
                        "Major stems leave together at a phrase boundary; track is naturally deconstructing.",
                        stems, tuple(ev.id for ev in outs),
                        {"event_count": len(outs), "events": [ev.type.value for ev in outs]},
                    ))
            has_kick = any(ev.type == MarkerType.KICK_IN for ev in ins)
            has_bass = any(ev.type == MarkerType.BASS_IN for ev in ins)
            if has_kick and has_bass:
                conf = min(0.98, 0.78 + 0.06 * len(ins) + max(ev.confidence for ev in ins) * 0.12)
                if conf >= 0.88 and pos - used.get(MarkerType.REBUILD, -99999.0) >= beat_period_s * 64:
                    used[MarkerType.REBUILD] = pos
                    stems = tuple(sorted({s for ev in ins for s in (ev.stem_targets or ())}))
                    out.append(self._opportunity_marker(
                        MarkerType.REBUILD, "REBUILD", pos, conf,
                        "Kick and bass return together at a phrase boundary; strong rebuild point.",
                        stems, tuple(ev.id for ev in ins),
                        {"event_count": len(ins), "events": [ev.type.value for ev in ins]},
                    ))
        return out

    @staticmethod
    def _wrekk_candidates(downbeats, phrases) -> list[float]:
        candidates: list[float] = []
        for ph in phrases or []:
            candidates.append(float(ph.get("position_sec", 0.0) if isinstance(ph, dict) else ph))
        for i in range(0, len(downbeats or ()), 4):
            candidates.append(float(downbeats[i]))
        return sorted(set(round(c, 6) for c in candidates))

    @staticmethod
    def _opportunity_marker(
        marker_type: MarkerType,
        label: str,
        pos: float,
        confidence: float,
        reason: str,
        stems: tuple,
        related: tuple,
        evidence: dict,
    ) -> AutoMarker:
        return AutoMarker(
            id=_make_id(marker_type, pos),
            type=marker_type,
            label=label,
            position_s=pos,
            confidence=confidence,
            reason=reason,
            category="wrekk",
            family="opportunity",
            stem_targets=stems,
            evidence=evidence,
            related_events=related,
            live_visibility="default",
        )

    def _detect_stem_markers(
        self,
        beats, norm, col_to_s, s_to_col, n, dur, beat_period_s, bg_conf,
    ) -> list[AutoMarker]:
        out: list[AutoMarker] = []

        # Smooth over ~2 seconds
        sw = max(1, int(2.0 / dur * n))
        k  = np.ones(sw) / sw

        vocal_f = np.convolve(norm[:, self._VOC], k, mode="same")
        drum_f  = np.convolve(norm[:, self._DRM], k, mode="same")
        bass_f  = np.convolve(norm[:, self._BSS], k, mode="same")

        gap_beats = beat_period_s * 8

        # VOCAL_IN / VOCAL_OUT
        out.extend(self._threshold_crossings(
            vocal_f, self._VOCAL_CROSS, col_to_s,
            MarkerType.VOCAL_IN,  "VOCAL", "vocals enter mix",
            MarkerType.VOCAL_OUT, "VOCAL", "vocals leave mix",
            beats, beat_period_s, gap_beats, bg_conf * 0.80,
        ))

        # RHYTHM_IN (drums re-enter after low period)
        out.extend(self._rising_edges(
            drum_f, self._STEM_CROSS, col_to_s,
            MarkerType.RHYTHM_IN, "RHYTHM", "rhythm returns",
            beats, beat_period_s, gap_beats, bg_conf * 0.78,
        ))

        # BASS_IN
        out.extend(self._rising_edges(
            bass_f, self._STEM_CROSS, col_to_s,
            MarkerType.BASS_IN, "BASS", "bass enters",
            beats, beat_period_s, gap_beats, bg_conf * 0.74,
        ))

        # VOCAL_GHOST: vocals barely present (ghost effect zone)
        ghost_lo, ghost_hi = 0.02, 0.08
        ghost_starts = np.where(
            np.diff(((vocal_f >= ghost_lo) & (vocal_f < ghost_hi)).astype(np.int8)) == 1
        )[0]
        seen: list[float] = []
        for ci in ghost_starts:
            pos = self._snap(col_to_s(ci), beats, beat_period_s)
            if any(abs(pos - p) < gap_beats for p in seen):
                continue
            seen.append(pos)
            out.append(AutoMarker(
                id=_make_id(MarkerType.VOCAL_GHOST, pos),
                type=MarkerType.VOCAL_GHOST, label="GHOST",
                position_s=pos, confidence=bg_conf * 0.64,
                reason="vocals barely audible — ghost effect zone",
            ))

        # BASS_LOCK: bass dominates for an extended window
        lock_win = max(1, int(beat_period_s * 8 / dur * n))
        rolling  = np.convolve(
            (bass_f > self._STEM_DOMINANT).astype(np.float32),
            np.ones(lock_win) / lock_win, mode="same"
        )
        lock_starts = np.where(np.diff((rolling > 0.65).astype(np.int8)) == 1)[0]
        seen = []
        for ci in lock_starts:
            pos = self._snap(col_to_s(ci), beats, beat_period_s)
            if any(abs(pos - p) < beat_period_s * 16 for p in seen):
                continue
            seen.append(pos)
            out.append(AutoMarker(
                id=_make_id(MarkerType.BASS_LOCK, pos),
                type=MarkerType.BASS_LOCK, label="BASS",
                position_s=pos, confidence=bg_conf * 0.70,
                reason="bass dominates mix — bass lock zone",
            ))

        # DRUM_SWAP: high variance in drum fraction → pattern change
        if n > 32:
            step = max(1, int(beat_period_s * 2 / dur * n))
            win  = max(4, step * 2)
            cols = range(win, n - win, step)
            var  = np.array([
                float(norm[max(0, i - win):i + win, self._DRM].std())
                for i in cols
            ])
            if len(var) > 2 and var.max() > 0.02:
                thresh = float(np.percentile(var, 82))
                if thresh > 0.015:
                    swap_on = var > thresh
                    edges   = np.where(np.diff(swap_on.astype(np.int8)) == 1)[0]
                    seen = []
                    for vi in edges:
                        ci  = list(cols)[min(vi, len(cols) - 1)]
                        pos = self._snap(col_to_s(ci), beats, beat_period_s)
                        if any(abs(pos - p) < beat_period_s * 8 for p in seen):
                            continue
                        seen.append(pos)
                        out.append(AutoMarker(
                            id=_make_id(MarkerType.DRUM_SWAP, pos),
                            type=MarkerType.DRUM_SWAP, label="RHYTHM",
                            position_s=pos, confidence=bg_conf * 0.60,
                            reason="drum pattern variance — section change",
                        ))

        return out

    # ── WREKK performance markers ─────────────────────────────────────────────

    def _detect_wrekk_markers(
        self,
        beats, norm, total_raw, col_to_s, s_to_col, n, dur, beat_period_s,
    ) -> list[AutoMarker]:
        out: list[AutoMarker] = []

        sw = max(1, int(1.0 / dur * n))
        k  = np.ones(sw) / sw

        vf = np.convolve(norm[:, self._VOC], k, mode="same")
        df = np.convolve(norm[:, self._DRM], k, mode="same")
        bf = np.convolve(norm[:, self._BSS], k, mode="same")
        of = np.convolve(norm[:, self._OTH], k, mode="same")
        te = np.convolve(total_raw,           k, mode="same")

        med_e = float(np.median(te[te > 1e-6])) if (te > 1e-6).any() else 0.01
        gap   = beat_period_s * 16

        # WREKK_TOP: all 4 stems active + above median energy
        top_mask   = (vf > self._STEM_PRESENT) & (df > self._STEM_PRESENT) \
                   & (bf > self._STEM_PRESENT) & (of > self._STEM_PRESENT) \
                   & (te > med_e)
        top_starts = np.where(np.diff(top_mask.astype(np.int8)) == 1)[0]
        seen: list[float] = []
        for ci in top_starts:
            pos = self._snap(col_to_s(ci), beats, beat_period_s)
            if any(abs(pos - p) < gap for p in seen):
                continue
            seen.append(pos)
            sp = (float(vf[ci]), float(df[ci]), float(bf[ci]), float(of[ci]))
            out.append(AutoMarker(
                id=_make_id(MarkerType.WREKK_TOP, pos),
                type=MarkerType.WREKK_TOP, label="WREKK",
                position_s=pos, confidence=0.70,
                reason="all stems active — full WREKK zone",
                stem_profile=sp,
            ))

        # WREKK_RHYTHM: drums dominant + vocals minimal
        rhy_mask   = (df > self._STEM_DOMINANT) & (vf < self._STEM_PRESENT)
        rhy_starts = np.where(np.diff(rhy_mask.astype(np.int8)) == 1)[0]
        seen = []
        for ci in rhy_starts:
            pos = self._snap(col_to_s(ci), beats, beat_period_s)
            if any(abs(pos - p) < gap for p in seen):
                continue
            seen.append(pos)
            sp = (float(vf[ci]), float(df[ci]), float(bf[ci]), float(of[ci]))
            out.append(AutoMarker(
                id=_make_id(MarkerType.WREKK_RHYTHM, pos),
                type=MarkerType.WREKK_RHYTHM, label="WREKK",
                position_s=pos, confidence=0.72,
                reason="drums dominant, vocals out — WREKK rhythm zone",
                stem_profile=sp,
            ))

        return out

    # ── MIX_IN / MIX_OUT ─────────────────────────────────────────────────────

    def _detect_mix_points(
        self,
        beats, downbeats, phrases,
        norm, total_raw, col_to_s, s_to_col, n, dur, beat_period_s,
    ) -> list[AutoMarker]:
        out: list[AutoMarker] = []

        ph_positions = [
            float(ph.get("position_sec", 0.0) if isinstance(ph, dict) else ph)
            for ph in phrases
        ]
        if not ph_positions:
            ph_positions = [float(d) for d in downbeats[::4]]
        if not ph_positions:
            return out

        global_max = float(total_raw.max()) or 1.0
        win_cols   = max(1, int(beat_period_s * 8 / dur * n))

        # MIX_IN candidates: first 35% of track
        mix_in: list[tuple[float, float]] = []
        for pos in ph_positions:
            if pos >= dur * 0.35:
                continue
            ci        = s_to_col(pos)
            post      = norm[ci:ci + win_cols]
            if len(post) == 0:
                continue
            has_rhy   = float(post[:, self._DRM].mean()) > 0.08
            early_s   = 1.0 - pos / max(dur * 0.35, 1e-6)
            eng_raw   = float(total_raw[ci:ci + win_cols].mean()) if ci + win_cols <= n else 0.0
            low_e_s   = 1.0 - eng_raw / global_max
            conf      = min(0.90, early_s * 0.40 + low_e_s * 0.30 + (0.30 if has_rhy else 0.0))
            mix_in.append((pos, conf))

        mix_in.sort(key=lambda x: -x[1])
        for pos, conf in mix_in[:2]:
            if conf >= self._MIN_CONF:
                out.append(AutoMarker(
                    id=_make_id(MarkerType.MIX_IN, pos),
                    type=MarkerType.MIX_IN, label="MIX IN",
                    position_s=pos, confidence=conf,
                    reason="phrase boundary early in track — suggested mix-in",
                ))

        # MIX_OUT candidates: last 35% of track
        mix_out: list[tuple[float, float]] = []
        for pos in ph_positions:
            if pos <= dur * 0.65:
                continue
            ci       = s_to_col(pos)
            pre      = norm[max(0, ci - win_cols):ci]
            if len(pre) == 0:
                continue
            sust_drm = float(pre[:, self._DRM].mean()) > 0.10
            late_s   = (pos - dur * 0.65) / max(dur * 0.35, 1e-6)
            eng_raw  = float(total_raw[max(0, ci - win_cols):ci].mean())
            eng_s    = eng_raw / global_max
            conf     = min(0.90, late_s * 0.40 + eng_s * 0.30 + (0.30 if sust_drm else 0.0))
            mix_out.append((pos, conf))

        mix_out.sort(key=lambda x: -x[1])
        for pos, conf in mix_out[:2]:
            if conf >= self._MIN_CONF:
                out.append(AutoMarker(
                    id=_make_id(MarkerType.MIX_OUT, pos),
                    type=MarkerType.MIX_OUT, label="MIX OUT",
                    position_s=pos, confidence=conf,
                    reason="phrase boundary late in track — suggested mix-out",
                ))

        return out

    # ── SWITCH_POINT ──────────────────────────────────────────────────────────

    def _detect_switch_points(
        self,
        beats, downbeats, phrases,
        norm, total_raw, col_to_s, s_to_col, n, dur, beat_period_s,
    ) -> list[AutoMarker]:
        out: list[AutoMarker] = []

        ph_positions = [
            float(ph.get("position_sec", 0.0) if isinstance(ph, dict) else ph)
            for ph in phrases
        ]
        if len(ph_positions) < 2:
            return out

        win_cols   = max(1, int(beat_period_s * 8 / dur * n))
        global_max = float(total_raw.max()) or 1.0
        scores: list[tuple[float, float, dict]] = []

        for pos in ph_positions:
            if pos < beat_period_s * 8 or pos > dur - beat_period_s * 8:
                continue
            ci   = s_to_col(pos)
            pre  = norm[max(0, ci - win_cols):ci]
            post = norm[ci:min(n, ci + win_cols)]
            if len(pre) == 0 or len(post) == 0:
                continue

            pre_e  = float(total_raw[max(0, ci - win_cols):ci].mean())
            post_e = float(total_raw[ci:min(n, ci + win_cols)].mean())

            # Consistent energy on both sides
            consistency = 1.0 - abs(pre_e - post_e) / max(pre_e + post_e, 1e-6)

            # Stem balance via entropy (higher = more even spread)
            pm = post.mean(axis=0).clip(1e-9, 1.0)
            pm /= pm.sum()
            entropy    = -float(np.sum(pm * np.log(pm)))
            stem_bal   = min(1.0, entropy / 1.386)   # log(4)

            energy_s   = min(1.0, post_e / max(global_max * 0.25, 1e-6))
            all_pres   = float(np.all(post.mean(axis=0) > self._STEM_PRESENT))
            score      = consistency * 0.30 + stem_bal * 0.25 + energy_s * 0.25 + all_pres * 0.20
            scores.append((pos, score, {"pre_e": pre_e, "post_e": post_e}))

        if not scores:
            return out

        scores.sort(key=lambda x: -x[1])
        for pos, score, info in scores[:3]:
            if score < self._MIN_CONF:
                continue
            ci   = s_to_col(pos)
            post = norm[ci:min(n, ci + win_cols)]
            sp   = self._stem_profile(norm, ci, min(n, ci + win_cols)) if len(post) > 0 else None
            out.append(AutoMarker(
                id=_make_id(MarkerType.SWITCH_POINT, pos),
                type=MarkerType.SWITCH_POINT, label="SWITCH",
                position_s=pos,
                confidence=min(0.92, score),
                reason=f"switch score {score:.2f}: balanced stems + consistent energy",
                stem_profile=sp,
                energy_before=info["pre_e"],
                energy_after=info["post_e"],
            ))

        return out

    # ── Utility helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _snap(pos: float, beats: list, beat_period_s: float) -> float:
        """Snap position to nearest beat, or return as-is if no beats."""
        if not beats:
            return pos
        i  = bisect.bisect_left(beats, pos)
        cs = []
        if i > 0:
            cs.append(float(beats[i - 1]))
        if i < len(beats):
            cs.append(float(beats[i]))
        return min(cs, key=lambda b: abs(b - pos)) if cs else pos

    @staticmethod
    def _stem_profile(norm: "np.ndarray", c1: int, c2: int) -> tuple | None:
        if c2 <= c1:
            return None
        m = norm[c1:c2].mean(axis=0)
        return (float(m[0]), float(m[1]), float(m[2]), float(m[3]))

    def _threshold_crossings(
        self,
        signal, thresh, col_to_s,
        type_rise, label_rise, reason_rise,
        type_fall, label_fall, reason_fall,
        beats, beat_period_s, gap, conf_base,
    ) -> list[AutoMarker]:
        out: list[AutoMarker] = []
        above = (signal > thresh).astype(np.int8)
        diff  = np.diff(above, prepend=above[0])

        seen_r: list[float] = []
        for ci in np.where(diff == 1)[0]:
            pos = self._snap(col_to_s(ci), beats, beat_period_s)
            if any(abs(pos - p) < beat_period_s * 4 for p in seen_r):
                continue
            seen_r.append(pos)
            out.append(AutoMarker(
                id=_make_id(type_rise, pos), type=type_rise, label=label_rise,
                position_s=pos, confidence=conf_base, reason=reason_rise,
            ))

        seen_f: list[float] = []
        for ci in np.where(diff == -1)[0]:
            pos = self._snap(col_to_s(ci), beats, beat_period_s)
            if any(abs(pos - p) < beat_period_s * 4 for p in seen_f):
                continue
            seen_f.append(pos)
            out.append(AutoMarker(
                id=_make_id(type_fall, pos), type=type_fall, label=label_fall,
                position_s=pos, confidence=conf_base, reason=reason_fall,
            ))

        return out

    def _rising_edges(
        self,
        signal, thresh, col_to_s,
        marker_type, label, reason,
        beats, beat_period_s, gap, conf_base,
    ) -> list[AutoMarker]:
        out: list[AutoMarker] = []
        above = (signal > thresh).astype(np.int8)
        diff  = np.diff(above, prepend=above[0])

        seen: list[float] = []
        for ci in np.where(diff == 1)[0]:
            pos = self._snap(col_to_s(ci), beats, beat_period_s)
            if any(abs(pos - p) < gap for p in seen):
                continue
            seen.append(pos)
            out.append(AutoMarker(
                id=_make_id(marker_type, pos), type=marker_type, label=label,
                position_s=pos, confidence=conf_base, reason=reason,
            ))

        return out
