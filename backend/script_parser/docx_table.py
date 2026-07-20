"""Parse a DOCX dub script laid out as a table.

Expected columns (matched by header name, any order):
    Sr. No. | Start Time | End Time | Character | Dialogues

Timecodes are frame-based HH:MM:SS:FF; fps is inferred from the frames seen
unless passed explicitly. No python-docx dependency — we read the raw XML.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .base import ParseStats, ScriptDoc, ScriptSegment, infer_fps_from_frames, normalize_character, parse_timecode

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_TC_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}[:;]\d{1,3}$")


def _cell_text(tc) -> str:
    return "".join(t.text or "" for t in tc.iter(_W + "t")).strip()


def _find_columns(header: list[str]) -> dict[str, int]:
    """Map logical column -> index by fuzzy header match."""
    idx: dict[str, int] = {}
    for i, h in enumerate(header):
        hl = h.lower()
        if "start" in hl and "start" not in idx_keys(idx):
            idx["start"] = i
        elif "end" in hl:
            idx["end"] = i
        elif "character" in hl or "speaker" in hl or "char" in hl:
            idx["character"] = i
        elif "dialog" in hl or "dialogue" in hl or "line" in hl or "text" in hl:
            idx["text"] = i
    return idx


def idx_keys(d: dict) -> set:
    return set(d.keys())


def _flattened_rows(root) -> list[list[str]]:
    """Fallback for scripts that AREN'T a Word table — each row is a single PARAGRAPH with the
    Sr.No/Start/End/Character/Dialogue fields TAB-separated (seen on e.g. EP44). Split every
    paragraph on its tab stops into cells (joining runs within a field, since Word splits text
    into arbitrary runs); the normal two-timecode row logic then reads them like table cells.
    The header paragraph carries no timecodes, so it's skipped by that logic."""
    rows: list[list[str]] = []
    for p in root.iter(_W + "p"):
        cells: list[str] = []
        cur: list[str] = []
        for el in p.iter():                       # document order: text runs interleaved with tabs
            if el.tag == _W + "t":
                cur.append(el.text or "")
            elif el.tag == _W + "tab":
                cells.append("".join(cur).strip())
                cur = []
        cells.append("".join(cur).strip())
        cells = [c for c in cells if c]           # drop empties from padding/alignment tabs
        if len(cells) >= 3:                        # need at least start, end, character
            rows.append(cells)
    return rows


def parse(path: Path, fps: float | None = None) -> ScriptDoc:
    root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))

    table_rows: list[list[str]] = []
    for tr in root.iter(_W + "tr"):
        table_rows.append([_cell_text(tc) for tc in tr.findall(_W + "tc")])

    if table_rows:
        # Locate header (row that names Character + a time column); fall back to row 0.
        header_i = 0
        for i, r in enumerate(table_rows[:5]):
            joined = " ".join(r).lower()
            if "character" in joined and ("start" in joined or "time" in joined):
                header_i = i
                break
        cols = _find_columns(table_rows[header_i])
        data_rows = table_rows[header_i + 1:]
    else:
        # No Word table — the script flattened its rows into paragraphs. Rebuild them.
        data_rows = _flattened_rows(root)
        cols = {}
        if not data_rows:
            raise ValueError("No table rows or timecoded paragraphs found in DOCX")

    # Detect max frame value to infer fps if not given.
    max_frame = 0
    for r in data_rows:
        for cell in r:
            if _TC_RE.match(cell):
                max_frame = max(max_frame, int(re.split(r"[:;]", cell)[-1]))
    use_fps = fps or infer_fps_from_frames(max_frame)

    segments: list[ScriptSegment] = []
    n = 0
    # A row with >=2 timecode cells IS a dialogue row (every parsed row must have
    # both). Count them so callers can see how many dialogue-looking rows were
    # dropped — a dropped row is a scripted line the QC pass never checks.
    candidates = sum(1 for r in data_rows if sum(1 for c in r if _TC_RE.match(c)) >= 2)
    for r in data_rows:
        # Resolve start/end either by header columns or by "two timecode cells".
        if {"start", "end", "character"} <= idx_keys(cols) and len(r) > max(cols["start"], cols["end"], cols["character"]):
            start_cell, end_cell = r[cols["start"]], r[cols["end"]]
            char_cell = r[cols["character"]]
            text_cell = r[cols["text"]] if "text" in cols and len(r) > cols["text"] else ""
        else:
            tc_idx = [i for i, c in enumerate(r) if _TC_RE.match(c)]
            if len(tc_idx) < 2 or tc_idx[1] + 1 >= len(r):
                continue
            start_cell, end_cell = r[tc_idx[0]], r[tc_idx[1]]
            char_cell = r[tc_idx[1] + 1]
            text_cell = r[tc_idx[1] + 2] if tc_idx[1] + 2 < len(r) else ""

        if not (_TC_RE.match(start_cell) and _TC_RE.match(end_cell)) or not char_cell:
            continue
        try:
            start_s = parse_timecode(start_cell, use_fps)
            end_s = parse_timecode(end_cell, use_fps)
        except ValueError:
            continue
        if end_s <= start_s:
            continue
        segments.append(ScriptSegment(
            index=n, start_s=round(start_s, 3), end_s=round(end_s, 3),
            character_raw=char_cell, characters=normalize_character(char_cell), text=text_cell,
        ))
        n += 1

    if not segments:
        raise ValueError("No timecoded character rows parsed from DOCX")
    stats = ParseStats(candidates=candidates, parsed=len(segments),
                       dropped=max(0, candidates - len(segments)))
    return ScriptDoc(source_format="docx", fps=use_fps, segments=segments, parse_stats=stats)
