"""Pluggable script parsers: turn a dub script (DOCX table / SRT / CSV) into a
common ``ScriptDoc`` of timecoded, speaker-attributed segments.

Public surface::

    from backend.script_parser import parse_script
    doc = parse_script(path, fmt="auto", fps=24)
"""

from __future__ import annotations

from pathlib import Path

from .base import ParseStats, ScriptDoc, ScriptSegment, canonical_key, normalize_character, parse_timecode

__all__ = [
    "ParseStats", "ScriptDoc", "ScriptSegment", "parse_script",
    "canonical_key", "normalize_character", "parse_timecode",
]


def parse_script(path: str | Path, fmt: str = "auto", fps: float | None = None) -> ScriptDoc:
    """Dispatch to the right parser by ``fmt`` (or the file extension when 'auto')."""
    path = Path(path)
    if fmt == "auto":
        ext = path.suffix.lower().lstrip(".")
        fmt = {"docx": "docx", "srt": "srt", "csv": "csv", "tsv": "csv"}.get(ext, "")
        if not fmt:
            raise ValueError(f"Cannot infer script format from '{path.name}'. Pass fmt=docx|srt|csv.")

    if fmt == "docx":
        from .docx_table import parse as _parse
        return _parse(path, fps=fps)
    if fmt == "srt":
        from .srt import parse as _parse
        return _parse(path)
    if fmt == "csv":
        from .csv_parser import parse as _parse
        return _parse(path, fps=fps)
    raise ValueError(f"Unknown script format '{fmt}'")
