"""Scriptless QC — compare the ORIGINAL episode audio against the dub, no script needed.

When there is no script (so no timecoded lines), the original episode audio is itself
the timeline of truth: wherever the ORIGINAL has speech, the dub should have speech
within tolerance. We VAD both sides and compare timelines:

    original speech, dub silent            -> MISSING     (timestamped for review)
    original speech, dub partial/shifted   -> MISALIGNED
    dub speech where the original silent   -> EXTRA       (attributed to its track)

The dub side is either ONE full-episode dub file, or a FOLDER of per-speaker tracks —
for comparison the tracks are combined by taking the UNION of their speech regions
(no audio mixdown needed; QC only cares about *when* someone speaks).

Reuses the tested alignment core: the original's VAD regions become pseudo "script
lines" fed to align_channel, which also auto-estimates a constant original-vs-dub
offset (so a shifted delivery isn't flagged line-by-line, and is surfaced as a sync
warning instead).

Honest limits (documented for the report): no character names or line text (that needs
a script), and the original is usually a full mix — Silero targets speech so music is
mostly ignored, but vocal efforts (shouts/grunts) do fire, and dubs often legitimately
skip those; every flag carries timestamps so a reviewer can verify in Audacity.
"""

from __future__ import annotations

from typing import Any

from .alignment import SYNC_WARN_OFFSET_S, align_channel

Interval = tuple[float, float]

# Original-side speech shorter than this is ignored as a "line" — sub-0.35 s blips are
# mostly breaths/efforts that a dub legitimately drops; flagging them buries real finds.
MIN_LINE_S = 0.35
# Dub-side speech must be at least this long (and outside the original's speech) to be
# EXTRA — same floor the script-based mode uses for unscripted-speech flags.
MIN_EXTRA_S = 0.6
# NOTE: we deliberately do NOT reclassify a coverage-MISSING slot to "present but shifted"
# using nearby dub speech. Three adversarial-review rounds showed every such heuristic
# HIDES real dropped lines — VAD timelines alone cannot distinguish a line's shifted
# redelivery from unrelated/added dub audio nearby. So MISSING stays MISSING (a candidate
# to verify). Cross-language timing drift therefore inflates the missing count; that is an
# honest limitation of scriptless comparison, not something to paper over by hiding drops.


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Union of possibly-overlapping intervals, sorted."""
    out: list[Interval] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _overlaps(a: Interval, b: Interval) -> bool:
    return min(a[1], b[1]) - max(a[0], b[0]) > 0


def compare_original_to_dub(
    original_regions: list[Interval],
    dub_regions_by_channel: dict[str, list[Interval]],
    *,
    tol_s: float = 1.0,
) -> dict[str, Any]:
    """Score the dub's speech timeline against the original's. Returns the same
    alignment-report shape /api/analyze produces, so the UI renders it unchanged."""
    lines = [(a, b) for a, b in original_regions if (b - a) >= MIN_LINE_S]
    spans = [(i, a, b) for i, (a, b) in enumerate(lines)]
    union = merge_intervals([r for regs in dub_regions_by_channel.values() for r in regs])

    # One channel -> name it (so the UI's dub player can slice it); several -> the
    # union has no single source file, leave channel unset on original-side findings.
    single = list(dub_regions_by_channel)[0] if len(dub_regions_by_channel) == 1 else None
    dub_label = single or "dub (combined tracks)"

    # Same tolerance->coverage mapping the script-based mode uses.
    aligned_cov = max(0.2, min(0.8, 1.0 - tol_s * 0.5))
    ca = align_channel(
        "original", dub_label, spans, union,
        tol_s=tol_s, aligned_coverage=aligned_cov,
        min_extra_s=1e18,  # suppress union-level extras; recomputed per track below
    )

    # The finding times are ORIGINAL-timeline; the UI's dub player slices the dub file
    # raw at those times. With a big original-vs-dub offset the sliced window would miss
    # the scored slot (the ±2.5s player pad absorbs small shifts), so only attach the
    # dub channel when the offset is comfortably inside the pad — the original player
    # (always correct) remains the primary evidence.
    finding_channel = single if abs(ca.offset_s) <= 0.5 else None

    errors: list[dict[str, Any]] = []
    for e in ca.errors:
        d = e.model_dump()
        d["character"] = None
        d["channel"] = finding_channel
        a, b = d.get("script_start_s") or 0.0, d.get("script_end_s") or 0.0
        if d["type"] == "MISSING":
            d["message"] = (f"The original has speech at {a:.2f}–{b:.2f}s "
                            f"but the dub is silent there (coverage {(d.get('coverage') or 0):.0%}).")
        elif d["type"] == "MISALIGNED":
            shift = d.get("drift_s") or 0.0
            d["message"] = (f"Original speech at {a:.2f}–{b:.2f}s is only partly covered by the dub "
                            f"(coverage {(d.get('coverage') or 0):.0%}, sits "
                            f"{'late' if shift > 0 else 'early'}).")
        errors.append(d)

    # EXTRA per TRACK (not the union) so each finding names the file to open in an
    # editor. Overlap is tested against ALL original speech (not the MIN_LINE_S-filtered
    # lines) — dub speech covering a short original utterance is NOT extra. Times are
    # reported in the DUB file's own timeline (raw r): that's the file the finding names,
    # the one the UI player slices, and the one a reviewer opens in an editor.
    shifted_all = [(a + ca.offset_s, b + ca.offset_s) for a, b in original_regions]
    n_extra = 0
    for ch, regs in dub_regions_by_channel.items():
        for r in regs:
            if (r[1] - r[0]) < MIN_EXTRA_S:
                continue
            if all(not _overlaps(r, s) for s in shifted_all):
                n_extra += 1
                errors.append({
                    "type": "EXTRA", "subtype": None, "severity": "info",
                    "character": None, "channel": ch, "script_index": None,
                    "script_start_s": None, "script_end_s": None,
                    "audio_start_s": round(r[0], 3),
                    "audio_end_s": round(r[1], 3),
                    "drift_s": None, "coverage": None, "text": None,
                    "message": f"Dub speech in '{ch}' at {r[0]:.2f}–{r[1]:.2f}s where the original is silent.",
                })

    sync_warnings = []
    if abs(ca.offset_s) >= SYNC_WARN_OFFSET_S:
        sync_warnings.append({
            "character": None, "channel": dub_label, "offset_s": ca.offset_s,
            "message": (f"The dub runs {abs(ca.offset_s):.2f}s "
                        f"{'late' if ca.offset_s > 0 else 'early'} versus the original — "
                        f"results below are scored AFTER correcting for this."),
        })

    return {
        "tol_s": tol_s,
        "channels": [],
        "errors": errors,
        "unmapped_characters": [],
        "sync_warnings": sync_warnings,
        "summary": {
            "n_characters_checked": 0,
            # counted from the FINAL error list (not ca.n_missing): EXTRA is recomputed
            # per-track below, so the summary must reflect what's actually reported.
            "n_missing": sum(1 for d in errors if d["type"] == "MISSING"),
            "n_misaligned": sum(1 for d in errors if d["type"] == "MISALIGNED"),
            "n_extra": n_extra,
            "n_unmapped": 0,
            "n_sync_warnings": len(sync_warnings),
            "n_original_regions": len(lines),
        },
    }
