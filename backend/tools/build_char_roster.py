"""Convert 'GAV character list.docx' into backend/data/char_roster.json.

The doc is a 3-column table:  Character | Pic | Voice name
  Character   — the studio's canonical spelling, often with a role annotation
                ('Hanto Karakida - Writer', 'Shouma as hero', 'Little shouma').
  Voice name  — the studio's asset/track-naming convention ('KMR_GAV_Shouma').

This roster is a *mapping aid*: it bridges the terse labels a SCRIPT uses ('Hanto',
'Glotta') to the verbose names a delivered TRACK carries ('HANTO KARAKIDA.wav'),
via the canonical name + its aliases + the voice-name convention. It does NOT
affect VAD/timing/loudness — only which track a character is matched to.

No python-docx dependency — we read the raw document XML (same approach as
backend/script_parser/docx_table.py).

Run when the character list changes:
    .venv\\Scripts\\python -m backend.tools.build_char_roster "GAV character list.docx"
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
OUT = Path(__file__).resolve().parents[1] / "data" / "char_roster.json"

# Role annotations that follow a canonical name — kept as a distinct alias, but the
# part before them is the "base" identity used for looser matching.
_ROLE_SPLIT = re.compile(r"\s+-\s+|\s+\bas\b\s+", re.IGNORECASE)


def _cell_text(tc) -> str:
    return "".join(t.text or "" for t in tc.iter(_W + "t")).strip()


def _canonical_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip().replace(" ", "_")


def _aliases(name: str) -> list[str]:
    """[full name, name-without-role-annotation] (deduped, non-empty)."""
    out: list[str] = []
    for cand in (name, _ROLE_SPLIT.split(name)[0].strip()):
        cand = cand.strip()
        if cand and cand not in out:
            out.append(cand)
    return out


def build(path: Path) -> list[dict]:
    root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    tbl = next(root.iter(_W + "tbl"), None)
    if tbl is None:
        raise ValueError("No table found in the character-list DOCX")

    roster: list[dict] = []
    seen: set[str] = set()
    for tr in tbl.iter(_W + "tr"):
        cells = [_cell_text(tc) for tc in tr.findall(_W + "tc")]
        if len(cells) < 3:
            continue
        name, _pic, voice_name = cells[0], cells[1], cells[2]
        name = name.strip()
        if not name or name.lower() == "character":  # skip header / blanks
            continue
        key = _canonical_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        roster.append({
            "name": name,
            "key": key,
            "aliases": _aliases(name),
            "voice_name": voice_name.strip() or None,
        })
    return roster


def main(docx: str) -> int:
    roster = build(Path(docx))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(roster, indent=1, ensure_ascii=False), encoding="utf-8")
    n_vn = sum(1 for e in roster if e["voice_name"])
    print(f"{OUT}: {len(roster)} roster characters ({n_vn} with a voice-name)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "GAV character list.docx"))
