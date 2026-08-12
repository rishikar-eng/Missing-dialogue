"""Write Pro Tools .ptx marker sessions — the library the QC pipeline calls.

The sound engineer opens these directly in Pro Tools to click through the flags, so this is
the last deliverable in the chain: run -> marker CSV -> .ptx. It carries every marker named,
positioned to the sample, and holding its original-language line as the marker comment, so
the engineer sees what should be there without opening the Excel report.

This module is the FORMAT; `tools/ptx_markers.py` is a CLI over it for local batches. The
implementation lived there first — it was moved here unchanged (proven by the round-trip
check below plus a regression test that rebuilds the seven delivered POA sessions
byte-for-byte) so the hosted pipeline can produce the same files a Teams run publishes.

FORMAT
------
A .ptx session is a flat sequence of length-prefixed blocks after a 20-byte header:

    u8 0x5A | u16 block_type | u32 size | u16 content_type | <size-2 bytes of content>

The blocks this writer cares about, in file order:

    0x0827..  ptr      u32 offset of the trailing index block
    0x2067    info     the session filename, u32-length-prefixed and zero-padded
    0x204D    timecode session-start position, as a u32 frame count at 25 fps (x2 copies)
    0x2030    markers  u32 count, then N marker blocks, then an 8-byte zero footer
    0x0002    index    u32 offsets of the blocks that follow the marker list

A marker block (content_type 0x2077) is, all little-endian:

    u16 0x2077 | u16 marker_number | 03 09 00 00
    u32 name_len  | name (UTF-8)
    u64 position  | u64 position          -- sample offset at 48 kHz, stored twice
    CONST_B (59 bytes)
    u32 comment_len | comment (UTF-8)
    CONST_D (50 bytes)
    u32 comment_len | comment (UTF-8)     -- second identical copy
    CONST_TAIL (116 bytes, containing 0x4826/0x4826/0x4827 sub-blocks)

Because the marker list is the only block whose size changes, writing a session is:
rebuild that one block, then shift every offset after it — the u32 index-block entries
and the header pointer — by the size delta. Everything else is copied through untouched.

CORRECTNESS
-----------
The layout above is documented from the reference sessions in REF_DIR, and every run
re-checks it against them before writing anything: each reference is parsed, rebuilt from
its own parsed markers, and required to come back byte-for-byte identical. A change that
breaks the model therefore fails loudly on known-good files rather than silently
producing a session Pro Tools cannot open. Each file written is then re-parsed and its
markers compared against the rows they came from.

The fields that vary between sessions (session start, index offsets) are located by
differencing the three references rather than being hardcoded to an offset, so the
positions are derived from the files themselves.

COLOUR IS NOT WRITTEN
---------------------
Every marker in the reference sessions is the same colour and their per-marker constant
blocks are byte-identical, so there is no reference for where a colour is stored. Writing
an unverified value risks a session that will not open, in exchange for something purely
cosmetic — and the colour's meaning is already carried by the marker NAME, which is what
the studio's own legend maps ("SONG - not dubbed (ignore)", "MISSING LOW",
"FRAGMENT - 1 word, check last"). To add colour, export one marker of each colour from
the studio's converter and difference the results; the field follows in minutes.
"""
from __future__ import annotations

import csv
import os
import struct

# The reference sessions ship WITH the code (3 files, 8 KB) rather than sitting on someone's
# desktop: the hosted pipeline writes these files on the server, and the round-trip check
# below is only a safety net if the references it checks against are actually present.
REF_DIR = os.path.join(os.path.dirname(__file__), "data", "ptx_refs")
REF_NAMES = ("POA_EP11_English_TOP3_TIMECODE.ptx",
             "POA_EP12_English_TOP3_TIMECODE.ptx",
             "POA_EP25_English_TOP3_TIMECODE.ptx")

SR = 48000
FPS = 25
SAMPLES_PER_FRAME = SR // FPS          # 1920 — exact at 25 fps / 48 kHz, so no rounding


# ------------------------------------------------------------------ block layer

def scan_blocks(data, start, end):
    """Every top-level block in [start, end). Raises rather than resynchronising:
    a mis-parse means the model is wrong, and skipping ahead would hide that."""
    blocks, pos = [], start
    while pos < end:
        if data[pos] != 0x5A:
            raise RuntimeError(f"expected a block marker 0x5A at 0x{pos:X}")
        size = struct.unpack_from("<I", data, pos + 3)[0]
        blocks.append({"off": pos, "size": size,
                       "btype": struct.unpack_from("<H", data, pos + 1)[0],
                       "ctype": struct.unpack_from("<H", data, pos + 7)[0]})
        pos += 7 + size
    if pos != end:
        raise RuntimeError("block scan overran the file")
    return blocks


class Session:
    """A parsed .ptx — used both to read the references and to verify what we write."""

    def __init__(self, path):
        data = open(path, "rb").read()
        self.data = data
        blocks = scan_blocks(data, 20, len(data))
        by_ctype = {b["ctype"]: b for b in blocks}
        self.list_blk = by_ctype[0x2030]

        idx_blk = blocks[-1]
        assert idx_blk["ctype"] == 0x0002, "last block is not the index"
        self.idx_off = idx_blk["off"]

        ptr = blocks[0]
        assert ptr["size"] == 4
        assert struct.unpack_from("<I", data, ptr["off"] + 7)[0] == self.idx_off
        self.ptr_field = ptr["off"] + 7

        # session filename, length-prefixed inside the 0x2067 block
        info = by_ctype[0x2067]
        base = os.path.basename(path).encode()
        npos = data.find(base, info["off"], info["off"] + 7 + info["size"])
        assert npos > 0, "session name not found in the info block"
        assert struct.unpack_from("<I", data, npos - 4)[0] == len(base)
        self.name_field, self.name_len = npos, len(base)
        # The name is zero-padded to a fixed width and more fields follow the padding.
        # Capacity = the name plus the zero run after it, so writing name.ljust(cap, 0)
        # leaves every byte outside the name untouched whatever the true field width is.
        end = npos + self.name_len
        while data[end] == 0:
            end += 1
        self.name_cap = end - npos

        self.tc_blk = by_ctype[0x204D]
        self.start_fields = None            # located by load_references()
        self.session_start_frames = None

        lb = self.list_blk
        self.pre = data[:lb["off"]]
        self.post = data[lb["off"] + 7 + lb["size"]: self.idx_off]
        self.index = data[self.idx_off:]
        self.markers = self._read_markers(data, lb)

    @staticmethod
    def _read_markers(data, lb):
        markers = []
        p = lb["off"] + 9
        count = struct.unpack_from("<I", data, p)[0]
        p += 4
        for _ in range(count):
            assert data[p] == 0x5A and struct.unpack_from("<H", data, p + 1)[0] == 0x11
            msize = struct.unpack_from("<I", data, p + 3)[0]
            c = data[p + 7: p + 7 + msize]
            q = 2
            num = struct.unpack_from("<H", c, q)[0]; q += 2
            assert c[q:q + 4] == b"\x03\x09\x00\x00"; q += 4
            nlen = struct.unpack_from("<I", c, q)[0]; q += 4
            name = c[q:q + nlen]; q += nlen
            pos1, pos2 = struct.unpack_from("<QQ", c, q); q += 16
            assert pos1 == pos2, "the two position copies disagree"
            const_b = c[q:q + 59]; q += 59
            clen = struct.unpack_from("<I", c, q)[0]; q += 4
            comment = c[q:q + clen]; q += clen
            const_d = c[q:q + 50]; q += 50
            clen2 = struct.unpack_from("<I", c, q)[0]; q += 4
            comment2 = c[q:q + clen2]; q += clen2
            tail = c[q:]
            assert comment == comment2, "the two comment copies differ"
            assert len(tail) == 116, f"unexpected marker tail length {len(tail)}"
            markers.append({"num": num, "name": name, "pos": pos1, "comment": comment,
                            "B": const_b, "D": const_d, "TAIL": tail})
            p += 7 + msize
        assert data[p:lb["off"] + 7 + lb["size"]] == b"\x00" * 8, "unexpected list footer"
        return markers


def build_marker_block(num, name, pos, comment, const_b, const_d, tail):
    body = (b"\x77\x20" + struct.pack("<H", num) + b"\x03\x09\x00\x00"
            + struct.pack("<I", len(name)) + name
            + struct.pack("<QQ", pos, pos) + const_b
            + struct.pack("<I", len(comment)) + comment + const_d
            + struct.pack("<I", len(comment)) + comment + tail)
    return b"\x5A\x11\x00" + struct.pack("<I", len(body)) + body


def build_ptx(ref, session_name, markers, offset_fields, session_start_frames):
    """Bytes of a .ptx holding `markers` = [(name bytes, comment bytes, sample pos)]."""
    name_b = session_name.encode()
    assert len(name_b) <= ref.name_cap, "session name too long for the name field"
    proto = ref.markers[0]
    inner = b"".join(
        build_marker_block(i + 1, n, p, c, proto["B"], proto["D"], proto["TAIL"])
        for i, (n, c, p) in enumerate(markers))
    list_body = b"\x30\x20" + struct.pack("<I", len(markers)) + inner + b"\x00" * 8
    list_blk = b"\x5A\x05\x00" + struct.pack("<I", len(list_body)) + list_body
    delta = len(list_blk) - (7 + ref.list_blk["size"])

    pre = bytearray(ref.pre)
    pre[ref.name_field:ref.name_field + ref.name_cap] = name_b.ljust(ref.name_cap, b"\x00")
    struct.pack_into("<I", pre, ref.name_field - 4, len(name_b))
    struct.pack_into("<I", pre, ref.ptr_field, ref.idx_off + delta)
    for f in ref.start_fields:
        struct.pack_into("<I", pre, f, session_start_frames)

    index = bytearray(ref.index)
    for f in offset_fields:
        old = struct.unpack_from("<I", index, f)[0]
        if old > ref.list_blk["off"]:
            struct.pack_into("<I", index, f, old + delta)
    return bytes(pre) + list_blk + ref.post + bytes(index)


def find_offset_fields(refs):
    """Positions within the index block that hold a u32 offset, identified as the entries
    that move between references by exactly the marker-list size difference. Asserts that
    every differing byte is accounted for, so an unmodelled field can't slip through."""
    a, b, c = (r.index for r in refs)
    assert len(a) == len(b) == len(c), "index blocks differ in length"
    differing = {i for i in range(len(a)) if not (a[i] == b[i] == c[i])}
    expect_b = refs[1].list_blk["size"] - refs[0].list_blk["size"]
    expect_c = refs[2].list_blk["size"] - refs[0].list_blk["size"]
    fields = set()
    for i in sorted(differing):
        for start in range(max(0, i - 3), i + 1):
            if start + 4 > len(a):
                continue
            va, vb, vc = (struct.unpack_from("<I", x, start)[0] for x in (a, b, c))
            if vb - va == expect_b and vc - va == expect_c and va > refs[0].list_blk["off"]:
                fields.add(start)
    explained = {j for f in fields for j in range(f, f + 4)}
    unexplained = differing - explained
    assert not unexplained, f"unaccounted index differences at {sorted(unexplained)}"
    return sorted(fields)


_REFS: tuple[list, list] | None = None


def load_references(verbose=False):
    """Parse the reference sessions, locate the fields that vary between them, and prove
    the model by rebuilding each one byte-for-byte from its own parsed markers.

    Returns (references, offset_fields). Every caller writing a .ptx goes through this so
    the check runs first. The result is cached per process — the check is a guard against a
    broken FORMAT MODEL, which cannot change between two calls in one process, and the
    hosted pipeline converts on a request path.
    """
    global _REFS
    if _REFS is not None:
        return _REFS
    refs = [Session(os.path.join(REF_DIR, f)) for f in REF_NAMES]

    proto = refs[0].markers[0]
    for r in refs:
        for m in r.markers:
            for k in ("B", "D", "TAIL"):
                assert m[k] == proto[k], f"per-marker constant {k} differs between markers"

    # Outside the marker list the references differ only in session name, index pointer
    # and session start. Mask the first two; whatever still differs is the session start.
    masked = []
    for r in refs:
        buf = bytearray(r.pre)
        buf[r.name_field:r.name_field + r.name_len] = b"\x00" * r.name_len
        struct.pack_into("<I", buf, r.ptr_field, 0)
        masked.append(buf)
    differing = [i for i in range(len(masked[0]))
                 if not (masked[0][i] == masked[1][i] == masked[2][i])]
    start_fields, i = [], 0
    while i < len(differing):
        f = differing[i]
        start_fields.append(f)
        i += 1
        while i < len(differing) and differing[i] < f + 4:
            i += 1
    assert len(start_fields) == 2, f"unexpected differences before the markers: {start_fields}"
    tc = refs[0].tc_blk
    for f in start_fields:
        assert tc["off"] < f < tc["off"] + 7 + tc["size"], \
            f"difference at 0x{f:X} falls outside the timecode block"

    for r in refs:
        r.start_fields = start_fields
        vals = {struct.unpack_from("<I", r.pre, f)[0] for f in start_fields}
        assert len(vals) == 1, "the session-start copies disagree"
        r.session_start_frames = vals.pop()
        assert refs[0].post == r.post, "the blocks after the marker list differ"

    offset_fields = find_offset_fields(refs)

    for r, fname in zip(refs, REF_NAMES):
        rebuilt = build_ptx(r, fname,
                            [(m["name"], m["comment"], m["pos"]) for m in r.markers],
                            offset_fields, r.session_start_frames)
        # rebuilding a reference from itself is a zero-delta write, so it must be identical
        assert rebuilt == r.data, f"round-trip mismatch for {fname}"
        if verbose:
            print(f"round-trip OK: {fname}")
    _REFS = (refs, offset_fields)
    return _REFS


# ------------------------------------------------------------------- CSV input

def parse_tc(tc):
    """'HH:MM:SS:FF' at 25 fps -> absolute sample index at 48 kHz."""
    parts = tc.strip().split(":")
    if len(parts) != 4:
        raise ValueError(f"bad timecode {tc!r}")
    hh, mm, ss, ff = (int(x) for x in parts)
    if not (0 <= mm < 60 and 0 <= ss < 60 and 0 <= ff < FPS):
        raise ValueError(f"out-of-range timecode {tc!r}")
    return (((hh * 60 + mm) * 60 + ss) * FPS + ff) * SAMPLES_PER_FRAME


def read_marker_csv(path):
    """Columns: No.,In,Color,Name,Comment -> [(name bytes, comment bytes, sample pos)].

    `In` is absolute SMPTE at 25 fps, zero-based: the delivered dubs carry BWF
    TimeReference 0, so the session starts at 00:00:00:00 and markers need no offset.
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        cols = set(rdr.fieldnames or ())
        rows = list(rdr)
    # Checked against the HEADER, not the first row: a marker CSV for a clean episode has a
    # header and no rows, and reading the columns off row zero turned that into a bogus
    # "missing columns In, Name, Comment" instead of an empty (valid) marker list.
    missing = {"In", "Name", "Comment"} - cols
    if missing:
        raise ValueError(f"{os.path.basename(path)} is missing columns {missing}")
    out = []
    for i, r in enumerate(rows, start=1):
        name = (r["Name"] or "").strip()
        if not name:
            raise ValueError(f"{os.path.basename(path)} row {i} has a blank Name — the "
                             "input was mis-parsed and the markers would be unnamed")
        out.append((name.encode("utf-8"),
                    (r["Comment"] or "").strip().encode("utf-8"),
                    parse_tc(r["In"])))
    positions = [p for _, _, p in out]
    if positions != sorted(positions):
        raise ValueError(f"{os.path.basename(path)} markers are not in time order")
    return out


# ----------------------------------------------------------------------- write

def session_name_for(stem: str) -> str:
    """A .ptx filename that fits the session-name field (58 bytes in the references).

    The name is stored inside the file, in a fixed-width slot, so a long one cannot simply
    be written — and the run's own workbook stem is already 74 bytes for POA. Trim from the
    LEFT, keeping the episode/language/kind tail that tells the engineer which session this
    is, rather than truncating that away.
    """
    name = stem + ".ptx"
    cap = load_references()[0][0].name_cap
    if len(name.encode()) <= cap:
        return name
    b = name.encode()[-cap:]
    # never cut mid-codepoint: drop leading continuation bytes
    while b and (b[0] & 0xC0) == 0x80:
        b = b[1:]
    return b.decode("utf-8", "ignore")


def write_ptx(markers, out_path, session_name=None):
    """Write one .ptx from `markers` = [(name bytes, comment bytes, sample pos)] and verify
    it re-parses to exactly the markers it was given. Returns the path written."""
    refs, offset_fields = load_references()
    session = session_name or os.path.basename(out_path)
    data = build_ptx(refs[0], session, markers, offset_fields, session_start_frames=0)
    with open(out_path, "wb") as f:
        f.write(data)
    check = Session(out_path)
    got = [(m["num"], m["name"], m["comment"], m["pos"]) for m in check.markers]
    expect = [(i + 1, n, c, p) for i, (n, c, p) in enumerate(markers)]
    if got != expect:
        raise AssertionError(f"{session}: the file written does not match its marker list")
    return out_path


def csv_to_ptx(csv_path, out_path, session_name=None):
    """Marker CSV -> .ptx session. Returns the path written, or None when the CSV holds no
    markers (a clean episode): a session with nothing in it is not worth handing over, and
    the zero-marker list is the one shape the references give no evidence for."""
    markers = read_marker_csv(csv_path)
    if not markers:
        return None
    name = session_name or session_name_for(
        os.path.splitext(os.path.basename(out_path))[0])
    return write_ptx(markers, out_path, name)
