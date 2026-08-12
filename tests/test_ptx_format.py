"""What a .ptx we hand the sound engineer is allowed to be.

The engineer opens these in Pro Tools to click through the flags, so a session that will not
open — or that opens with markers in the wrong place — costs a delivery. Two things are
pinned here:

  * the FORMAT MODEL still describes the studio's own reference sessions (parse -> rebuild ->
    byte-identical), which is what makes writing one safe at all; and
  * the seven POA sessions delivered on 2026-08-10 regenerate BYTE-FOR-BYTE from the CSVs
    they were made from. That is the real guard: the writer moved out of tools/ptx_markers.py
    into backend/ptx.py so the hosted pipeline could call it, and "the engineer's files come
    back identical" is the only proof the move changed nothing.

The delivered-file check needs the POA batch on this machine and skips without it.

Run: python -m pytest tests/test_ptx_format.py -q
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from backend import ptx  # noqa: E402

DELIVERED = Path(r"C:\Users\Rishi\Desktop\poa-reports")
CSV_DIR, PTX_DIR = DELIVERED / "csv-for-ptx", DELIVERED / "ptx report"


def test_reference_sessions_round_trip():
    """Each reference parses and rebuilds from its own markers, byte for byte. load_references
    asserts this internally — the test is here so it runs without writing a file."""
    refs, offset_fields = ptx.load_references()
    assert len(refs) == 3
    assert offset_fields, "no index offset fields located"
    for r in refs:
        assert r.markers, "a reference session has no markers to model from"


def test_timecode_is_25fps_zero_based():
    """The dubs carry BWF TimeReference 0, so SMPTE maps straight to samples with no offset.
    One frame at 25 fps / 48 kHz is exactly 1920 samples."""
    assert ptx.parse_tc("00:00:00:00") == 0
    assert ptx.parse_tc("00:00:00:01") == 1920
    assert ptx.parse_tc("00:00:01:00") == 48000
    assert ptx.parse_tc("00:07:07:00") == 7 * 60 * 48000 + 7 * 48000
    for bad in ("00:00:00:25", "00:60:00:00", "1:2:3", ""):
        with pytest.raises(ValueError):
            ptx.parse_tc(bad)


def test_marker_names_and_comments_survive_a_write(tmp_path):
    """Non-ASCII comments are the point of the .ptx — they carry the original Portuguese line.
    write_ptx re-parses what it wrote, so a mangled comment raises rather than ships."""
    markers = [(b"MISSING MEDIUM - dub silent", "retiveram as chuvas,".encode(), 20544000),
               ("SONG - não dubbed (ignore)".encode(), "Não mereço.".encode(), 48000)]
    markers.sort(key=lambda m: m[2])
    out = ptx.write_ptx(markers, str(tmp_path / "T.ptx"))
    got = ptx.Session(out).markers
    assert [(m["name"], m["comment"], m["pos"]) for m in got] == markers
    assert [m["num"] for m in got] == [1, 2]


def test_empty_marker_csv_yields_no_session(tmp_path):
    """A clean episode has a header and no rows. That is a valid empty marker list, not a
    malformed file — and it produces no session rather than an empty one."""
    csv_path = tmp_path / "clean.csv"
    csv_path.write_text("No.,In,Color,Name,Comment\n", encoding="utf-8-sig")
    assert ptx.read_marker_csv(str(csv_path)) == []
    assert ptx.csv_to_ptx(str(csv_path), str(tmp_path / "clean.ptx")) is None


def test_blank_name_is_rejected(tmp_path):
    """The studio's own conversion notes say it: if NAME is blank the input was mis-parsed and
    the session opens with unnamed markers. Better to fail than to deliver that."""
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("No.,In,Color,Name,Comment\n1,00:00:01:00,Red,,text\n",
                        encoding="utf-8-sig")
    with pytest.raises(ValueError, match="blank Name"):
        ptx.read_marker_csv(str(csv_path))


def test_session_name_fits_the_field():
    """The name is stored in a fixed-width slot inside the file. A run's own workbook stem is
    longer than that slot for POA, so it must be trimmed — keeping the tail, which is what
    identifies the episode."""
    cap = ptx.load_references()[0][0].name_cap
    long_stem = "POA-Paulo-O-Apostolo_EP11_English_AudioQC-Report_2026-08-11_QC-Markers"
    assert len(long_stem.encode()) > cap, "stem no longer exercises the trim path"
    name = ptx.session_name_for(long_stem)
    assert len(name.encode()) <= cap
    assert name.endswith("_QC-Markers.ptx")
    short = "POA_EP11_English_QC-Markers"
    assert ptx.session_name_for(short) == short + ".ptx"


@pytest.mark.skipif(not PTX_DIR.is_dir(), reason="delivered POA batch not on this machine")
def test_delivered_sessions_regenerate_byte_for_byte(tmp_path):
    """The seven sessions handed to the studio on 2026-08-10, rebuilt from their source CSVs."""
    delivered = sorted(PTX_DIR.glob("*.ptx"))
    assert len(delivered) == 7, f"expected the 7-episode batch, found {len(delivered)}"
    for want in delivered:
        src = CSV_DIR / (want.stem + ".csv")
        assert src.exists(), f"no source CSV for {want.name}"
        got = ptx.csv_to_ptx(str(src), str(tmp_path / want.name))
        assert Path(got).read_bytes() == want.read_bytes(), \
            f"{want.name} no longer regenerates identically"


@pytest.mark.skipif(not PTX_DIR.is_dir(), reason="delivered POA batch not on this machine")
def test_the_studios_verification_marker():
    """The one marker the studio was asked to check on import: EP11 at 00:07:07:00, on a line
    confirmed missing by ear. If this moves, every marker in the batch has moved."""
    s = ptx.Session(str(PTX_DIR / "POA_EP11_English_QC-Markers.ptx"))
    at = [m for m in s.markers if m["pos"] == ptx.parse_tc("00:07:07:00")]
    assert len(at) == 1, "the EP11 verification marker is not at 00:07:07:00"
    assert at[0]["comment"].decode().startswith("retiveram as chuvas")
    assert at[0]["name"].decode() == "MISSING MEDIUM - dub silent"
