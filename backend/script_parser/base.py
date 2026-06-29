"""Shared data model + helpers for all script parsers."""

from __future__ import annotations

import re

from pydantic import BaseModel

# Bracketed/parenthetical tags and stage directions that are NOT part of the
# speaker's identity — a character voiced in different on-screen forms is still
# one performer/entity.
_TAG_RE = re.compile(r"\[.*?\]|\(.*?\)")
_NOISE_WORDS = ("on screen", "off screen", "voice over", "v.o", "o.s", "narration")
_SPLIT_RE = re.compile(r"\s*[/&+]\s*")


class ScriptSegment(BaseModel):
    """One scripted line: a timed span attributed to one or more characters."""
    index: int
    start_s: float
    end_s: float
    character_raw: str            # label exactly as written, e.g. "Shoma [narration]"
    characters: list[str]         # canonical keys, e.g. ["shoma"]  (a row may name >1)
    text: str = ""

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


class ScriptDoc(BaseModel):
    source_format: str            # "docx" | "srt" | "csv"
    fps: float | None = None      # set when timecodes were frame-based (HH:MM:SS:FF)
    segments: list[ScriptSegment]

    def characters(self) -> set[str]:
        return {c for s in self.segments for c in s.characters}


def normalize_character(raw: str) -> list[str]:
    """Map a raw character cell to one or more canonical keys.

    'Shoma [narration]' -> ['shoma']   ;   'Amane/Shoma' -> ['amane', 'shoma'].
    """
    c = _TAG_RE.sub(" ", raw.lower())
    for w in _NOISE_WORDS:
        c = c.replace(w, " ")
    parts = _SPLIT_RE.split(c)
    keys = []
    for p in parts:
        k = canonical_key(p)
        if k and k not in keys:
            keys.append(k)
    return keys


def canonical_key(name: str) -> str:
    """Lowercase alphanumeric slug used as a character's stable identity key."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip().replace(" ", "_")


def parse_timecode(tc: str, fps: float | None = None) -> float:
    """Parse a timecode string into seconds. Accepts:

      HH:MM:SS:FF   (frame-based — needs fps)
      HH:MM:SS,mmm / HH:MM:SS.mmm   (milliseconds)
      HH:MM:SS / MM:SS
      a bare float (already seconds)
    """
    tc = tc.strip()
    if not tc:
        raise ValueError("empty timecode")
    # bare seconds
    if re.fullmatch(r"\d+(\.\d+)?", tc):
        return float(tc)

    # HH:MM:SS,mmm or HH:MM:SS.mmm  -> milliseconds in the last field
    m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})", tc)
    if m:
        h, mi, s, ms = m.groups()
        return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0

    parts = re.split(r"[:;]", tc)
    if len(parts) == 4:                          # HH:MM:SS:FF (frames)
        h, mi, s, f = (int(p) for p in parts)
        if not fps:
            raise ValueError(f"frame timecode '{tc}' needs fps")
        return h * 3600 + mi * 60 + s + f / fps
    if len(parts) == 3:                          # HH:MM:SS
        h, mi, s = (int(p) for p in parts)
        return h * 3600 + mi * 60 + s
    if len(parts) == 2:                          # MM:SS
        mi, s = (int(p) for p in parts)
        return mi * 60 + s
    raise ValueError(f"unrecognised timecode '{tc}'")


def infer_fps_from_frames(max_frame: int) -> float:
    """Guess the frame rate from the largest frame field seen (FF in HH:MM:SS:FF)."""
    for rate in (24, 25, 30, 48, 50, 60):
        if max_frame < rate:
            return float(rate)
    return 30.0
