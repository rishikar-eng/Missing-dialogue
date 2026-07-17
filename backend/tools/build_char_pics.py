"""Extract character pictures from 'KAMEN RIDER CHARACTER LIST & VOICES.xlsx'.

Same source file that build_voice_bank.py reads (it already gives us the per-language
voice IDs); this pulls the IMAGE column so the QC workbook can show each character's
face next to their findings.

Output: backend/data/char_pics/<key>.png  +  backend/data/char_pics/index.json
        {"shouma": "shouma.png", ...}   keyed the same way as the voice bank / roster.

Run:
    .venv\\Scripts\\python -m backend.tools.build_char_pics "KAMEN RIDER CHARACTER LIST & VOICES.xlsx"

Why this is not a two-liner
---------------------------
The sheet stores pictures TWO different ways, and openpyxl only sees neither/one:

 1. **Floating anchors** (the bulk, ~224). Classic drawing objects anchored to a cell:
        xl/drawings/drawing1.xml   <xdr:from><xdr:col>1</xdr:col><xdr:row>N</xdr:row>
    -> r:embed="rIdX" -> drawing1.xml.rels -> ../media/imageN.png

 2. **Excel-365 "Place in Cell" images** (12 of them), stored as rich values. openpyxl
    has no support at all, so they'd silently vanish. The chain is:
        sheet cell vm="N"  ->  xl/metadata.xml (Nth <rc v="I"/>)
        ->  xl/richData/rdrichvalue.xml (Ith <rv> -> rel index)
        ->  xl/richData/richValueRel.xml (r:id) -> its .rels -> ../media/imageN.png

Also handled:
  * media parts named `.tmp` (they are real PNG/JPEG) -> sniffed by magic bytes.
  * several images anchored to the SAME row (the header row carries decorative art) and
    rows with none -> we keep the LARGEST image per row and skip the header.
  * images are 94 MB raw (median 274 KB) -> downscaled to <=THUMB_PX thumbnails, which is
    all a spreadsheet cell can show anyway, and keeps both the repo and the .xlsx small.
"""

from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "backend" / "data" / "char_pics"
THUMB_PX = 128          # longest edge; a spreadsheet cell shows ~100px
HEADER_ROWS = 1         # row 1 = "CHARACTER NAME | IMAGE | VOICE ID's | ..."

_NS = {
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


def _key(name: str) -> str:
    """The app's canonical character key, so pictures JOIN to characters/voice bank.

    Must mirror two places exactly or the join silently misses:
      * build_voice_bank.py: `re.sub(r"\\(.*?\\)", " ", raw.splitlines()[0])` — first line,
        parentheticals dropped ("ART DEALER MONSTER EP8 (00134411)" -> "ART DEALER MONSTER EP8")
      * characters.py:96:    `re.sub(r"[^a-z0-9]", "", s.lower())`
    Keeping the bracket text cost us 15 of 66 joins on the first run.
    """
    first = (name or "").splitlines()[0] if name else ""
    clean = re.sub(r"\[.*?\]|\(.*?\)", " ", first)
    return re.sub(r"[^a-z0-9]", "", clean.lower())


def _sniff_ext(blob: bytes) -> str | None:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if blob.startswith(b"BM"):
        return "bmp"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return None


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """{rId: target} for a part's .rels sidecar."""
    p = Path(part)
    rels_path = f"{p.parent.as_posix()}/_rels/{p.name}.rels"
    if rels_path not in z.namelist():
        return {}
    root = ET.fromstring(z.read(rels_path))
    return {r.get("Id"): r.get("Target") for r in root}


def _norm_media(target: str) -> str:
    """'../media/image1.png' -> 'xl/media/image1.png'"""
    return "xl/" + target.replace("../", "").lstrip("/")


def _floating(z: zipfile.ZipFile) -> list[tuple[int, str]]:
    """[(row_1based, media_part)] for classic anchored drawings in the IMAGE column."""
    if "xl/drawings/drawing1.xml" not in z.namelist():
        return []
    rel = _rels(z, "xl/drawings/drawing1.xml")
    root = ET.fromstring(z.read("xl/drawings/drawing1.xml"))
    out: list[tuple[int, str]] = []
    for anchor in root:
        frm = anchor.find("xdr:from", _NS)
        blip = anchor.find(".//a:blip", _NS)
        if frm is None or blip is None:
            continue
        rid = blip.get(f"{{{_NS['r']}}}embed")
        target = rel.get(rid)
        if not target:
            continue
        row = int(frm.find("xdr:row", _NS).text) + 1   # xdr rows are 0-based
        out.append((row, _norm_media(target)))
    return out


def _in_cell(z: zipfile.ZipFile) -> list[tuple[int, str]]:
    """[(row_1based, media_part)] for Excel-365 'Place in Cell' (richValue) images."""
    names = z.namelist()
    if "xl/metadata.xml" not in names or "xl/richData/richValueRel.xml" not in names:
        return []

    # cell -> value-metadata index (1-based into metadata's <valueMetadata> blocks)
    sheet_xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "ignore")
    cells = re.findall(r'<c r="([A-Z]+)(\d+)"[^>]*\bvm="(\d+)"', sheet_xml)

    # metadata: the vm'th <bk><rc v="I"/></bk> -> rich-value index I
    md = z.read("xl/metadata.xml").decode("utf-8", "ignore")
    rc = [int(v) for _t, v in re.findall(r'<rc t="(\d+)" v="(\d+)"/>', md)]

    # rdrichvalue: the I'th <rv> holds the index into richValueRel
    rv_xml = z.read("xl/richData/rdrichvalue.xml").decode("utf-8", "ignore")
    rv = [int(v) for v in re.findall(r"<rv[^>]*>.*?<v>(\d+)</v>", rv_xml, re.S)]

    # richValueRel: ordered r:id list -> media targets
    rvr = z.read("xl/richData/richValueRel.xml").decode("utf-8", "ignore")
    rids = re.findall(r'r:id="(rId\d+)"', rvr)
    rel = _rels(z, "xl/richData/richValueRel.xml")

    out: list[tuple[int, str]] = []
    for _col, row_s, vm_s in cells:
        vm = int(vm_s) - 1                       # vm is 1-based
        if not (0 <= vm < len(rc)):
            continue
        rv_i = rc[vm]
        if not (0 <= rv_i < len(rv)):
            continue
        rel_i = rv[rv_i]
        if not (0 <= rel_i < len(rids)):
            continue
        target = rel.get(rids[rel_i])
        if target:
            out.append((int(row_s), _norm_media(target)))
    return out


def main(xlsx: str) -> int:
    src = Path(xlsx)
    if not src.is_file():
        print(f"Not found: {src}")
        return 1
    z = zipfile.ZipFile(src)

    # row -> character name (column A)
    import openpyxl
    wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    names: dict[int, str] = {}
    for i, row in enumerate(ws.iter_rows(min_col=1, max_col=1, values_only=True), start=1):
        if i <= HEADER_ROWS:
            continue
        if row[0]:
            names[i] = str(row[0]).strip()
    wb.close()

    pics = _floating(z) + _in_cell(z)
    print(f"images found: {len(pics)}  (floating {len(_floating(z))}, in-cell {len(_in_cell(z))})")

    # Keep the LARGEST image per row: the header/decorative art stacks several on one
    # cell, and a character's real portrait is the biggest thing anchored there.
    best: dict[int, tuple[int, str]] = {}
    for row, part in pics:
        if row <= HEADER_ROWS:
            continue
        try:
            size = z.getinfo(part).file_size
        except KeyError:
            continue
        if row not in best or size > best[row][0]:
            best[row] = (size, part)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.png"):
        old.unlink()

    index: dict[str, str] = {}
    written = skipped = 0
    for row, (_size, part) in sorted(best.items()):
        name = names.get(row)
        if not name:
            skipped += 1
            continue
        key = _key(name)
        if not key or key in index:      # first row wins on duplicate names
            continue
        blob = z.read(part)
        if not _sniff_ext(blob):
            skipped += 1
            continue
        try:
            im = Image.open(io.BytesIO(blob))
            # Every source image carries alpha (character art on transparency), so JPEG is
            # out. Palette-quantise instead: PNG-8 + alpha is ~3x smaller than PNG-32 and
            # indistinguishable at thumbnail size -- keeps the repo and the .xlsx light.
            im = im.convert("RGBA")
            im.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
            im = im.quantize(colors=128, method=Image.FASTOCTREE)
            out = OUT_DIR / f"{key}.png"
            im.save(out, "PNG", optimize=True)
        except Exception as e:
            print(f"  ! {name}: {e}")
            skipped += 1
            continue
        index[key] = out.name
        written += 1

    (OUT_DIR / "index.json").write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")
    total_kb = sum(p.stat().st_size for p in OUT_DIR.glob("*.png")) / 1024
    print(f"wrote {written} thumbnails ({total_kb:.0f} KB total) to {OUT_DIR}")
    print(f"skipped {skipped} (no name / undecodable)")
    print(f"characters with a picture: {sorted(index)[:8]}{' ...' if len(index) > 8 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "KAMEN RIDER CHARACTER LIST & VOICES.xlsx"))
