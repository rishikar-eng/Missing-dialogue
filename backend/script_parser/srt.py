"""Parse a SubRip (.srt) subtitle file into a ScriptDoc.

SRT has no native speaker column, so we extract a character from a leading
"NAME:" or "[NAME]" / "(NAME)" tag on the cue when present; otherwise the
segment is left unattributed (characters=[]).
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import ScriptDoc, ScriptSegment, normalize_character, parse_timecode

_TIME_LINE = re.compile(r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})")
# Speaker tag: "NAME:" or "- NAME:" at line start, or "[NAME]" / "(NAME)".
_SPEAKER_PREFIX = re.compile(r"^\s*-?\s*([A-Z][A-Za-z0-9 _'\-/&]{0,30}):\s*(.*)$")
_SPEAKER_BRACKET = re.compile(r"^\s*[\[(]([A-Za-z0-9 _'\-/&]{1,30})[\])]\s*(.*)$")


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse(path: Path) -> ScriptDoc:
    raw = path.read_text(encoding="utf-8-sig", errors="ignore")
    blocks = re.split(r"\r?\n\r?\n+", raw.strip())

    segments: list[ScriptSegment] = []
    n = 0
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        # Optional leading numeric index line.
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        m = _TIME_LINE.search(lines[0])
        if not m:
            continue
        start_s, end_s = parse_timecode(m.group(1)), parse_timecode(m.group(2))
        if end_s <= start_s:
            continue

        text = " ".join(_strip_tags(ln) for ln in lines[1:]).strip()
        character_raw = ""
        sm = _SPEAKER_PREFIX.match(text) or _SPEAKER_BRACKET.match(text)
        if sm:
            character_raw = sm.group(1).strip()
            text = sm.group(2).strip()

        segments.append(ScriptSegment(
            index=n, start_s=round(start_s, 3), end_s=round(end_s, 3),
            character_raw=character_raw,
            characters=normalize_character(character_raw) if character_raw else [],
            text=text,
        ))
        n += 1

    if not segments:
        raise ValueError("No cues parsed from SRT")
    return ScriptDoc(source_format="srt", fps=None, segments=segments)
