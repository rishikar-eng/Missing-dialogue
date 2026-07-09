"""Parse a CSV/TSV dub script into a ScriptDoc.

Columns are matched by header name (case-insensitive, fuzzy):
    start  : start / start time / in / tc in
    end    : end / end time / out / tc out
    character : character / speaker / role / name
    text   : dialogue / dialogues / line / text   (optional)

Timecodes may be frame-based (needs fps), milliseconds, HH:MM:SS, or bare seconds.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .base import ParseStats, ScriptDoc, ScriptSegment, normalize_character, parse_timecode

_ALIASES = {
    "start": ("start", "start time", "in", "tc in", "start_time", "begin"),
    "end": ("end", "end time", "out", "tc out", "end_time", "finish"),
    "character": ("character", "speaker", "role", "name", "char"),
    "text": ("dialogue", "dialogues", "line", "text", "subtitle", "content"),
}


def _match_columns(header: list[str]) -> dict[str, int]:
    norm = [h.strip().lower() for h in header]
    cols: dict[str, int] = {}
    for key, names in _ALIASES.items():
        for i, h in enumerate(norm):
            if h in names or any(h == n for n in names):
                cols[key] = i
                break
        if key not in cols:  # loose contains-match fallback
            for i, h in enumerate(norm):
                if any(n in h for n in names):
                    cols[key] = i
                    break
    return cols


def parse(path: Path, fps: float | None = None) -> ScriptDoc:
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    delimiter = "\t" if path.suffix.lower() == ".tsv" or text[:1024].count("\t") > text[:1024].count(",") else ","
    rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("CSV has no data rows")

    cols = _match_columns(rows[0])
    missing = {"start", "end", "character"} - set(cols)
    if missing:
        raise ValueError(f"CSV missing required column(s): {sorted(missing)} (header={rows[0]})")

    segments: list[ScriptSegment] = []
    n = 0
    for r in rows[1:]:
        if max(cols.values()) >= len(r):
            continue
        try:
            start_s = parse_timecode(r[cols["start"]], fps)
            end_s = parse_timecode(r[cols["end"]], fps)
        except ValueError:
            continue
        char = r[cols["character"]].strip()
        if end_s <= start_s or not char:
            continue
        txt = r[cols["text"]].strip() if "text" in cols and cols["text"] < len(r) else ""
        segments.append(ScriptSegment(
            index=n, start_s=round(start_s, 3), end_s=round(end_s, 3),
            character_raw=char, characters=normalize_character(char), text=txt,
        ))
        n += 1

    if not segments:
        raise ValueError("No valid rows parsed from CSV")
    stats = ParseStats(candidates=len(rows) - 1, parsed=len(segments),
                       dropped=max(0, (len(rows) - 1) - len(segments)))
    return ScriptDoc(source_format="csv", fps=fps, segments=segments, parse_stats=stats)
