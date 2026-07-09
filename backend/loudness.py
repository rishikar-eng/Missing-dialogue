"""Per-line loudness of the (target) dub tracks — flags lines that are too quiet to
hear or hot enough to clip, and reports each character's typical level.

There is no source audio to compare against, so everything here is measured *within*
the delivered dub: absolute level (dBFS) and each character's own median, which
together catch real delivery problems (an under-recorded line, a stem delivered off
level, a distorted take).

Method (no extra file reads — reuses the signal loaded once for VAD):
  * Split the track into 20 ms frames; per frame keep RMS (average level) and peak.
  * For a scripted line, measure over the frames inside its window that fall on VAD
    speech (so leading/trailing silence doesn't drag the number down).
  * level = 20*log10(rms) dBFS ; peak = 20*log10(max|sample|) dBFS.

The envelope is built from the NATIVE-rate signal (pass the file's own sample rate
to ``envelope``), so peaks are true sample peaks — resampling to 16k first would
under-read them and miss real clipping.
"""

from __future__ import annotations

from statistics import median
from typing import Any

import numpy as np

_FRAME_S = 0.02              # nominal 20 ms analysis frames; the envelope carries the
                             # TRUE per-frame duration (frame_samples/sr), which can
                             # differ slightly when sr isn't divisible by 50 — using
                             # the nominal value for indexing would drift over a long
                             # episode and mis-select frames.

# Thresholds (dBFS). Tunable.
SILENCE_FLOOR = -60.0        # at/below this the window is effectively silent => NO audio
                             #   (that's a "Missing" line, handled elsewhere — not a loudness issue)
QUIET_FLOOR = -45.0          # audible but below this absolute level = likely too quiet
REL_QUIET_DB = 12.0          # ...or this far below the character's own median
HOT_PEAK = -1.0              # peak above this risks clipping / distortion
MIN_LINES_FOR_MEDIAN = 4     # need a few lines before "relative to normal" is meaningful
WIN_PAD_S = 0.35             # widen the line window a touch (absorbs small capture offset)

Interval = tuple[float, float]
Envelope = tuple[np.ndarray, np.ndarray, float]  # (frame_rms, frame_peak, sec_per_frame)


def envelope(audio: np.ndarray, sr: int) -> Envelope:
    """(frame_rms, frame_peak, sec_per_frame) over ~20 ms frames of a mono signal
    at rate ``sr``.

    Pass the NATIVE-rate signal: peak must be a true sample peak for clipping
    detection (a 16k-resampled signal under-reads it). sec_per_frame is the TRUE
    frame duration (frame_samples/sr) — indexing with a nominal 0.02 would drift
    on rates not divisible by 50 (e.g. 11025 Hz) and mis-select frames late in
    the episode."""
    frame = max(1, int(round(sr * _FRAME_S)))
    sec_per_frame = frame / sr
    n = len(audio) // frame
    if n == 0:
        return np.zeros(0, np.float32), np.zeros(0, np.float32), sec_per_frame
    f = audio[: n * frame].reshape(n, frame).astype(np.float64)
    rms = np.sqrt((f ** 2).mean(axis=1)).astype(np.float32)
    peak = np.abs(f).max(axis=1).astype(np.float32)
    return rms, peak, sec_per_frame


def _db(x: float) -> float:
    return round(20.0 * np.log10(max(x, 1e-9)), 1)


def measure(env: Envelope, regions: list[Interval], start_s: float, end_s: float) -> tuple[float, float] | None:
    """(rms_dbfs, peak_dbfs) of the speech inside [start,end]; None if no frames."""
    rms, peak, spf = env
    if len(rms) == 0:
        return None
    # Index with the envelope's TRUE frame duration, not the nominal 20 ms — VAD
    # region times are true seconds, and any mismatch accumulates over the episode.
    a = max(0, int((start_s - WIN_PAD_S) / spf))
    b = min(len(rms), int(np.ceil((end_s + WIN_PAD_S) / spf)))
    if b <= a:
        return None
    # Measure ONLY the detected speech in the window. If the track has no speech
    # here, there's nothing to judge the loudness of — the line is Missing (silent),
    # which is a different finding. Return None so loudness ignores it.
    mask = np.zeros(b - a, dtype=bool)
    for rs, re in regions:
        i = max(a, int(rs / spf))
        j = min(b, int(np.ceil(re / spf)))
        if j > i:
            mask[i - a : j - a] = True
    if not mask.any():
        return None
    sel_rms = rms[a:b][mask]
    sel_peak = peak[a:b][mask]
    rms_val = float(np.sqrt((sel_rms.astype(np.float64) ** 2).mean()))
    return _db(rms_val), _db(float(sel_peak.max()))


def analyze_loudness(
    characters: list[Any],
    lines_by_char: dict[str, list[tuple[int, float, float, str]]],  # id -> [(index,start,end,text)]
    envelopes: dict[str, Envelope],
    region_cache: dict[str, list[Interval]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    """Return (flags, char_levels).

    flags: {type: "QUIET"|"LOUD", character, channel, script_index, script_start_s,
            script_end_s, text, level_dbfs, peak_dbfs, message}
    char_levels: {character_id: {"median","min","max"}} dBFS for mapped characters.
    """
    flags: list[dict[str, Any]] = []
    char_levels: dict[str, dict[str, float]] = {}

    for c in characters:
        ch = getattr(c, "channel", None)
        if not ch or ch not in envelopes:
            continue
        env = envelopes[ch]
        regions = region_cache.get(ch, [])
        measured: list[tuple[int, float, float, str, float, float]] = []
        for idx, s, e, text in lines_by_char.get(c.id, []):
            m = measure(env, regions, s, e)
            # Skip lines with no real audio (silent = Missing, reported separately).
            if m is not None and m[0] > SILENCE_FLOOR:
                measured.append((idx, s, e, text, m[0], m[1]))
        if not measured:
            continue
        levels = [m[4] for m in measured]
        med = round(median(levels), 1)
        # median + min/max range — the range exposes chunk-to-chunk level swings when
        # an episode is delivered as several stems per character.
        char_levels[c.id] = {"median": med, "min": round(min(levels), 1), "max": round(max(levels), 1)}
        rel_ok = len(measured) >= MIN_LINES_FOR_MEDIAN

        for idx, s, e, text, lvl, pk in measured:
            if pk >= HOT_PEAK:
                flags.append({
                    "type": "LOUD", "character": c.id, "channel": ch,
                    "script_index": idx, "script_start_s": s, "script_end_s": e, "text": text,
                    "level_dbfs": lvl, "peak_dbfs": pk,
                    "message": f"Peak {pk:.1f} dBFS — near clipping (distortion risk).",
                })
            elif lvl < QUIET_FLOOR or (rel_ok and lvl < med - REL_QUIET_DB):
                why = (f"{med - lvl:.0f} dB below {getattr(c, 'name', c.id)}'s usual {med:.0f} dBFS"
                       if rel_ok and lvl < med - REL_QUIET_DB else f"very low ({lvl:.1f} dBFS)")
                flags.append({
                    "type": "QUIET", "character": c.id, "channel": ch,
                    "script_index": idx, "script_start_s": s, "script_end_s": e, "text": text,
                    "level_dbfs": lvl, "peak_dbfs": pk,
                    "message": f"Level {lvl:.1f} dBFS — {why}. May be too quiet to hear.",
                })

    # Sort flags in episode order for a stable, scannable report.
    flags.sort(key=lambda f: f["script_start_s"])
    return flags, char_levels
