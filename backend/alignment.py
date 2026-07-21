"""Compare scripted dialogue to the processed waveform (channels mode) and emit
timestamped error records for missing / misaligned / extra speech.

Method (VAD, no ASR):
  * Each character has an isolated channel WAV, so VAD tells us *when that voice
    actually speaks*.
  * For every scripted line we check whether speech is present in the right place:
      - no speech where the script expects it          -> MISSING
      - speech present but its start/end drifts > tol   -> MISALIGNED (onset/offset)
      - speech much shorter than the scripted span      -> MISALIGNED/truncated
      - VAD speech with no scripted line for that char   -> EXTRA
  * A constant capture offset (script-TC zero vs audio zero) would otherwise mark
    every line misaligned, so we ESTIMATE the per-channel offset and subtract it
    before scoring drift.
"""

from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Any, Callable

from pydantic import BaseModel

from .characters import CharacterEntity
from .content_map import _is_group_stem
from .script_parser import ScriptDoc
from .vad import detect_speech_regions

Interval = tuple[float, float]

# The per-track capture offset is auto-estimated and subtracted before scoring so a
# constant script-vs-audio shift doesn't false-flag every line. But a LARGE offset is
# itself a finding — the whole track is out of sync with the script — and silently
# correcting it would hide a real delivery problem. Above this, we emit a warning.
SYNC_WARN_OFFSET_S = 0.75


class AlignError(BaseModel):
    type: str                       # MISSING | MISMATCH | MISALIGNED | EXTRA
    subtype: str | None = None      # onset_drift | offset_drift | truncated
    severity: str                   # error | warn | info
    character: str | None = None
    channel: str | None = None
    script_index: int | None = None
    script_start_s: float | None = None
    script_end_s: float | None = None
    audio_start_s: float | None = None
    audio_end_s: float | None = None
    drift_s: float | None = None
    coverage: float | None = None   # fraction of the scripted span covered by speech
    text: str | None = None         # the scripted dialogue line (for MISSING/MISALIGNED)
    # MISMATCH only: the line was silent in the character's OWN track but delivered on
    # another speaker's track (wrong-speaker delivery) — record which one.
    delivered_by_channel: str | None = None
    delivered_by_character: str | None = None
    # MISSING only: an UNCLAIMED track (delivered but mapped to nobody) has speech right
    # at this line's slot — the line may live there under a bad label. Downgrades the
    # finding from "confirmed missing" to "check delivery" in the report.
    possibly_in_channel: str | None = None
    message: str = ""


class ChannelAlignment(BaseModel):
    character: str
    channel: str
    offset_s: float                 # estimated script->audio offset applied
    n_lines: int
    n_missing: int
    n_mismatch: int = 0
    n_misaligned: int
    n_extra: int
    errors: list[AlignError]


# --------------------------------------------------------------------------- #
# interval helpers
# --------------------------------------------------------------------------- #
def _overlap(a: Interval, b: Interval) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _coverage(span: Interval, regions: list[Interval]) -> tuple[float, Interval | None]:
    """Return (covered_fraction, merged_matched_span) for `span` against regions."""
    covered = 0.0
    lo = hi = None
    for r in regions:
        ov = _overlap(span, r)
        if ov > 0:
            covered += ov
            lo = r[0] if lo is None else min(lo, r[0])
            hi = r[1] if hi is None else max(hi, r[1])
    dur = max(1e-9, span[1] - span[0])
    return covered / dur, (None if lo is None else (lo, hi))


def _best_sibling(
    span: Interval, sibling_regions: dict[str, list[Interval]] | None
) -> tuple[str | None, float, Interval | None]:
    """Among OTHER speaker tracks, the one whose speech best covers `span` (the character's
    offset-corrected line window). Coverage is measured over the line window ITSELF — NOT a
    padded window — so a neighbour merely bleeding speech into the gap just after the line
    isn't mistaken for delivering it. (`_coverage` already credits a sibling region that
    extends beyond the window, and tolerates small onset/offset shifts, so no pad is needed.)
    Returns (channel, coverage, merged_matched_span), or (None, 0.0, None) when no sibling
    speaks within the window."""
    if not sibling_regions:
        return None, 0.0, None
    best_ch, best_cov, best_span = None, 0.0, None
    for ch, regs in sibling_regions.items():
        cov, matched = _coverage(span, regs)
        if matched is not None and cov > best_cov:
            best_ch, best_cov, best_span = ch, cov, matched
    return best_ch, best_cov, best_span


def estimate_offset(
    script_spans: list[Interval], regions: list[Interval], max_offset_s: float = 5.0
) -> float:
    """Median (nearest-region-onset - script-onset) over lines that have a nearby
    region — robust to missing lines. 0.0 when there's nothing to anchor on."""
    deltas: list[float] = []
    starts = sorted(r[0] for r in regions)
    for s0, _ in script_spans:
        # nearest region start
        best = None
        for rs in starts:
            d = rs - s0
            if abs(d) <= max_offset_s and (best is None or abs(d) < abs(best)):
                best = d
        if best is not None:
            deltas.append(best)
    if len(deltas) < 3:
        return 0.0
    return round(median(deltas), 3)


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #
def align_channel(
    character: str,
    channel: str,
    script_spans: list[tuple[int, float, float]],   # (script_index, start, end)
    regions: list[Interval],
    *,
    tol_s: float = 0.5,
    missing_coverage: float = 0.15,
    aligned_coverage: float = 0.5,
    min_extra_s: float = 0.6,
    offset_s: float | None = None,
    sibling_regions: dict[str, list[Interval]] | None = None,
    sibling_owner: dict[str, str] | None = None,
    mismatch_coverage: float = 0.15,
) -> ChannelAlignment:
    """Score one character's scripted lines against their channel's VAD regions.

    Alignment is judged by COVERAGE — how much of the line's scripted slot actually
    contains speech — not by the speech region's outer edges. (VAD speech is often
    continuous across adjacent lines, so a region can stick out well beyond a single
    line even though that line is perfectly present; edge-based drift would wrongly
    flag those as misaligned.)
        coverage < missing_coverage  -> MISSING   (line absent)
        coverage < aligned_coverage  -> MISALIGNED (only partly present: shifted/clipped)
        otherwise                    -> OK
    """
    spans = [(a, b) for _, a, b in script_spans]
    if offset_s is None:
        offset_s = estimate_offset(spans, regions)

    errors: list[AlignError] = []
    n_missing = n_mismatch = n_misaligned = 0

    for idx, a, b in script_spans:
        span = (a + offset_s, b + offset_s)
        cov, matched = _coverage(span, regions)

        if cov < missing_coverage or matched is None:
            # The character's OWN track is silent here. Before calling it MISSING, check
            # whether ANOTHER speaker's track delivered the line — a wrong-speaker MISMATCH,
            # not a dropped line. Only truly-absent lines (no track speaks) stay MISSING.
            alt_ch, alt_cov, alt_span = _best_sibling(span, sibling_regions)
            if alt_ch is not None and alt_cov >= mismatch_coverage:
                n_mismatch += 1
                by = (sibling_owner or {}).get(alt_ch)
                errors.append(AlignError(
                    type="MISMATCH", severity="warn", character=character, channel=channel,
                    script_index=idx, script_start_s=round(a, 3), script_end_s=round(b, 3),
                    audio_start_s=round(alt_span[0] - offset_s, 3),
                    audio_end_s=round(alt_span[1] - offset_s, 3),
                    coverage=round(alt_cov, 3),
                    delivered_by_channel=alt_ch, delivered_by_character=by,
                    message=f"Line {idx} ({a:.2f}-{b:.2f}s) is silent in '{channel}' but "
                            f"delivered on '{alt_ch}'"
                            f"{f' ({by})' if by else ''} — wrong speaker? "
                            f"(coverage {alt_cov:.0%} there).",
                ))
                continue
            n_missing += 1
            errors.append(AlignError(
                type="MISSING", severity="error", character=character, channel=channel,
                script_index=idx, script_start_s=round(a, 3), script_end_s=round(b, 3),
                coverage=round(cov, 3),
                message=f"No speech in '{channel}' for scripted line {idx} "
                        f"({a:.2f}-{b:.2f}s); coverage {cov:.0%}.",
            ))
            continue

        if cov < aligned_coverage:
            n_misaligned += 1
            # Where does the speech sit relative to the slot? (early vs late)
            ov_lo, ov_hi = max(span[0], matched[0]), min(span[1], matched[1])
            shift = (ov_lo + ov_hi) / 2 - (span[0] + span[1]) / 2
            errors.append(AlignError(
                type="MISALIGNED", subtype=("late" if shift > 0 else "early"),
                severity="warn", character=character, channel=channel, script_index=idx,
                script_start_s=round(a, 3), script_end_s=round(b, 3),
                audio_start_s=round(matched[0] - offset_s, 3),
                audio_end_s=round(matched[1] - offset_s, 3),
                drift_s=round(shift, 3), coverage=round(cov, 3),
                message=f"Line {idx}: only {cov:.0%} of its slot has speech "
                        f"(shifted {'late' if shift > 0 else 'early'}) in '{channel}'.",
            ))

    # EXTRA: speech regions not overlapping any scripted (offset-shifted) line.
    # Skip very short blips (breaths, grunts, fight vocalisations) — those are
    # un-scripted noise, not missed dialogue.
    shifted = [(a + offset_s, b + offset_s) for a, b in spans]
    for r in regions:
        if (r[1] - r[0]) < min_extra_s:
            continue
        if all(_overlap(r, s) <= 0 for s in shifted):
            errors.append(AlignError(
                type="EXTRA", severity="info", character=character, channel=channel,
                audio_start_s=round(r[0] - offset_s, 3), audio_end_s=round(r[1] - offset_s, 3),
                message=f"Speech in '{channel}' at {r[0]:.2f}-{r[1]:.2f}s with no scripted line.",
            ))
    n_extra = sum(1 for e in errors if e.type == "EXTRA")

    return ChannelAlignment(
        character=character, channel=channel, offset_s=offset_s,
        n_lines=len(script_spans), n_missing=n_missing, n_mismatch=n_mismatch,
        n_misaligned=n_misaligned, n_extra=n_extra, errors=errors,
    )


def align_script_to_channels(
    doc: ScriptDoc,
    characters: list[CharacterEntity],
    channel_wavs: dict[str, Path],       # channel_name -> wav path
    *,
    tol_s: float = 0.5,
    vad_kwargs: dict[str, Any] | None = None,
    offset_s: float | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    region_cache: dict[str, list[Interval]] | None = None,
) -> dict[str, Any]:
    """Full pass: per character with a mapped channel, VAD the channel and score
    their lines. Returns a JSON-serialisable report.

    on_progress(done, total, channel) fires after each track's VAD completes (VAD
    is the slow step), so a UI can show real progress.

    Pass a persistent ``region_cache`` to reuse VAD results across calls — e.g. so
    re-scoring at a new tolerance is instant instead of re-running VAD.
    """
    spans_by_char: dict[str, list[tuple[int, float, float]]] = {}
    for seg in doc.segments:
        for key in seg.characters:
            spans_by_char.setdefault(key, []).append((seg.index, seg.start_s, seg.end_s))

    # Distinct tracks we'll actually VAD — drives the progress total. A character's
    # twin/pickup stems (extra_channels — split deliveries like 'X' + 'X 02') are VAD'd
    # too: their lines are scored against the UNION of all their stems' speech.
    to_vad: list[str] = []
    for ent in characters:
        if not spans_by_char.get(ent.id):
            continue
        for ch in [ent.channel, *(getattr(ent, "extra_channels", []) or [])]:
            if ch and ch in channel_wavs and ch not in to_vad:
                to_vad.append(ch)
    if region_cache is None:
        region_cache = {}
    total = sum(1 for ch in to_vad if ch not in region_cache)
    done = 0

    # VAD every mapped track FIRST — the cross-track "was this line delivered by ANOTHER
    # speaker?" check below needs every track's regions available, not just the ones
    # scored so far. (Cheap: skips tracks already in a passed-in region_cache.)
    for ch in to_vad:
        if ch not in region_cache:
            regs = detect_speech_regions(channel_wavs[ch], **(vad_kwargs or {}))
            region_cache[ch] = [(r["start"], r["end"]) for r in regs]
            done += 1
            if on_progress:
                on_progress(done, total, ch)

    # channel -> character-id for every real single-character track (the eligible "other
    # speaker" siblings for MISMATCH). Group/walla stems are excluded — they chatter across
    # many characters and would false-flag a MISMATCH on every quiet line of every character.
    # A character's twin stems map to the SAME id, so a line delivered in their own twin is
    # never mis-called a wrong-speaker MISMATCH.
    speaker_channels = {
        ch: ent.id
        for ent in characters
        for ch in [ent.channel, *(getattr(ent, "extra_channels", []) or [])]
        if ch and ch in region_cache and not _is_group_stem(ch)
    }

    channel_reports: list[ChannelAlignment] = []
    unmapped: list[str] = []
    # Map the tolerance slider to the coverage threshold: higher tol = more
    # lenient = a line only needs less of its slot covered to count as aligned.
    aligned_cov = max(0.2, min(0.8, 1.0 - tol_s * 0.5))

    for ent in characters:
        spans = spans_by_char.get(ent.id, [])
        if not spans:
            continue
        if not ent.channel or ent.channel not in channel_wavs:
            # Bit-parts delivered inside a group stem (walla/crowd) are expected, not
            # missing — surfaced as 'grouped' elsewhere, so keep them out of no-audio.
            if not getattr(ent, "grouped_in", None):
                unmapped.append(ent.id)
            continue
        if ent.channel not in region_cache:
            continue
        # Union of this character's stems: primary + any twin/pickup stems (split
        # deliveries). A line recorded in EITHER stem counts as delivered.
        own_channels = [ent.channel] + [
            ch for ch in (getattr(ent, "extra_channels", []) or []) if ch in region_cache
        ]
        regions = sorted(r for ch in own_channels for r in region_cache[ch])
        # Siblings = other CHARACTERS' tracks only — never this character's own twins.
        siblings = {ch: region_cache[ch] for ch, owner in speaker_channels.items()
                    if owner != ent.id}
        channel_reports.append(align_channel(
            ent.id, ent.channel, spans, regions,
            tol_s=tol_s, aligned_coverage=aligned_cov, offset_s=offset_s,
            sibling_regions=siblings, sibling_owner=speaker_channels,
        ))

    # Attach the scripted dialogue line to each error that references a script index.
    text_by_index = {seg.index: seg.text for seg in doc.segments}
    for cr in channel_reports:
        for e in cr.errors:
            if e.script_index is not None:
                e.text = text_by_index.get(e.script_index)

    # Confidence tier for MISSING: if an UNCLAIMED delivered track (VAD'd but mapped to
    # no character — e.g. a mislabelled or oddly-spelled stem) has speech right at the
    # line's slot, the line may exist there under a bad label. Mark it so the report can
    # say "check delivery" instead of a hard "missing" — the studio then fixes packaging
    # rather than re-recording a line that exists. Group/walla stems are excluded (their
    # chatter would tag everything); requires the pre-populated region cache (the server
    # pipeline VADs every delivered track before mapping).
    owned = {
        ch
        for ent in characters
        for ch in [ent.channel, *(getattr(ent, "extra_channels", []) or [])]
        if ch
    }
    unclaimed = [ch for ch in region_cache
                 if ch in channel_wavs and ch not in owned and not _is_group_stem(ch)]
    if unclaimed:
        for cr in channel_reports:
            for e in cr.errors:
                if e.type != "MISSING" or e.script_start_s is None:
                    continue
                win = (e.script_start_s, e.script_end_s or e.script_start_s)
                best_ch, best_cov = None, 0.0
                for ch in unclaimed:
                    cov, _ = _coverage(win, region_cache[ch])
                    if cov > best_cov:
                        best_ch, best_cov = ch, cov
                if best_ch and best_cov >= 0.3:
                    e.possibly_in_channel = best_ch
                    e.message += (f" NOTE: the unclaimed track '{best_ch}' has speech at "
                                  f"this slot ({best_cov:.0%}) — the line may be there "
                                  f"under a wrong label; check the delivery.")

    all_errors = [e for cr in channel_reports for e in cr.errors]

    # Whole-track sync warnings: a big estimated offset means the track only lines
    # up after shifting it — i.e. the delivered audio is out of sync with the
    # script. We still score with the correction (so per-line results are useful),
    # but surface the shift; otherwise a uniformly late/early track looks clean.
    sync_warnings = [
        {
            "character": cr.character,
            "channel": cr.channel,
            "offset_s": cr.offset_s,
            # offset_s = median(audio_onset - script_onset): positive => the audio
            # runs LATE vs the script. Describe the track's state (not a corrective
            # shift, which readers can apply in the wrong direction).
            "message": f"Track '{cr.channel}' runs {abs(cr.offset_s):.2f}s "
                       f"{'late' if cr.offset_s > 0 else 'early'} versus the script — "
                       f"the whole track may be out of sync. "
                       f"(Per-line results below are scored AFTER correcting for this.)",
        }
        for cr in channel_reports
        if abs(cr.offset_s) >= SYNC_WARN_OFFSET_S
    ]

    return {
        "tol_s": tol_s,
        "channels": [cr.model_dump() for cr in channel_reports],
        "errors": [e.model_dump() for e in all_errors],
        "unmapped_characters": unmapped,
        "sync_warnings": sync_warnings,
        "summary": {
            "n_characters_checked": len(channel_reports),
            "n_missing": sum(cr.n_missing for cr in channel_reports),
            "n_mismatch": sum(cr.n_mismatch for cr in channel_reports),
            "n_misaligned": sum(cr.n_misaligned for cr in channel_reports),
            "n_extra": sum(cr.n_extra for cr in channel_reports),
            "n_unmapped": len(unmapped),
            "n_sync_warnings": len(sync_warnings),
        },
    }
