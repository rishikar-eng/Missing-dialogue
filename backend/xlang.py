"""Cross-language root cause: the one signal that only exists because every language of an
episode is analysed together.

If a character's lines are missing in EVERY language, the dub teams didn't independently make
the same mistake — the script or the character mapping is the more likely cause. If they're
missing in one language only, the languages that did deliver prove the lines exist, so it's
that vendor's gap. Which of those two it is decides who gets the work, so it's the most
valuable sentence the whole pipeline can produce.

Lifted verbatim out of ``excel_report._summary`` (which now calls this) so the chat agent can
report the same reading as the workbook instead of re-deriving it and drifting. Pure data in,
pure data out — no openpyxl, no I/O — so it's cheap to call from a webhook path.
"""

from __future__ import annotations

from typing import Any

# >= half a character's lines missing = they're effectively absent in that language.
#
# Judged on the missing RATE, never on "has >=1 missing finding": a lead with 78 lines and one
# dropped line appears in every language's missing list, and calling that "missing everywhere
# -> script problem" sends the studio chasing a phantom.
MOSTLY_ABSENT = 0.5


def character_stats(per_lang: dict[str, dict[str, Any]]) -> dict[str, dict[str, tuple[int, int]]]:
    """{character display name: {language: (missing_lines, scripted_lines)}} — only characters
    with at least one missing line somewhere."""
    stats: dict[str, dict[str, tuple[int, int]]] = {}
    for lang, res in per_lang.items():
        chars = {c.get("id"): c for c in (res.get("characters") or [])}
        missed: dict[str, int] = {}
        for e in ((res.get("alignment") or {}).get("errors") or []):
            if e.get("type") == "MISSING" and e.get("character"):
                missed[e["character"]] = missed.get(e["character"], 0) + 1
        for cid, c in chars.items():
            n = missed.get(cid, 0)
            lines = c.get("line_count") or 0
            # a character with no track at all is absent even without per-line findings
            if not (c.get("channel") or c.get("grouped_in")) and lines:
                n = lines
            if n:
                stats.setdefault(c.get("name") or cid, {})[lang] = (n, lines)
    return stats


def rows(per_lang: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per affected character, worst first (most languages affected, then miss rate).

    Each row carries the machine-readable split (`absent` / `partial` / `delivered` languages)
    AND the prose `reading`, so a caller can re-word for its own medium without re-deriving
    the judgement. `cause` is the coarse bucket: script | delivery | dub | hard-lines.
    """
    langs = list(per_lang)
    n = len(langs)
    out: list[dict[str, Any]] = []
    for name, per in character_stats(per_lang).items():
        rated = {lg: m / l for lg, (m, l) in per.items() if l}       # language -> miss rate
        worst = max(rated.values()) if rated else 0.0
        absent = [lg for lg in langs if rated.get(lg, 0) >= MOSTLY_ABSENT]      # ~fully missing
        partial = [lg for lg in langs if 0 < rated.get(lg, 0) < MOSTLY_ABSENT]  # a few lines
        delivered = [lg for lg in langs if lg not in per]            # no missing at all

        if len(absent) == n:
            cause = "script"
            reading = ("Absent in EVERY language — look at the script/mapping first, "
                       "not the dub")
        elif absent:
            # Fully missing in SOME languages but not others: the languages that DID deliver
            # prove the line exists, so the fully-missing ones are a delivery/mapping gap
            # there — not a script problem. Name them explicitly.
            cause = "delivery"
            bits = [f"fully missing in {', '.join(absent)}"]
            if partial:
                bits.append(f"a few lines in {', '.join(partial)}")
            if delivered:
                bits.append(f"delivered in {', '.join(delivered)}")
            reading = "; ".join(bits) + f" — check delivery/mapping for {', '.join(absent)}"
        elif len(per) == n and n > 1:
            cause = "hard-lines"
            reading = ("A few lines drop in every language — usually the same hard lines; "
                       "check those timings in the script")
        else:
            cause = "dub"
            reading = (f"Gap in {', '.join(per)} only — the other languages delivered these "
                       f"lines, so it looks like a real dub gap")
        out.append({
            "character": name,
            "per_language": {lg: {"missing": m, "lines": l} for lg, (m, l) in per.items()},
            "languages_affected": len(per),
            "worst_rate": worst,
            "absent": absent, "partial": partial, "delivered": delivered,
            "cause": cause, "reading": reading,
        })
    return sorted(out, key=lambda r: (-r["languages_affected"], -r["worst_rate"], r["character"]))


def headline(per_lang: dict[str, dict[str, Any]], limit: int = 3) -> list[str]:
    """A few chat-sized lines: the script-level problems first (they block every language), then
    the worst single-vendor gaps. Empty when nothing is missing anywhere."""
    rs = rows(per_lang)
    if not rs:
        return []
    script_side = [r for r in rs if r["cause"] in ("script", "hard-lines")]
    dub_side = [r for r in rs if r["cause"] in ("delivery", "dub")]
    out: list[str] = []
    for r in script_side[:limit]:
        out.append(f"📄 **{r['character']}** — {r['reading']}")
    for r in dub_side[:limit]:
        out.append(f"🎙 **{r['character']}** — {r['reading']}")
    extra = len(rs) - len(script_side[:limit]) - len(dub_side[:limit])
    if extra > 0:
        out.append(f"…and {extra} more character{'s' if extra != 1 else ''} in the Summary sheet.")
    return out
