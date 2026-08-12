"""Write Pro Tools .ptx marker sessions from QC marker lists — local batch CLI.

Takes the marker CSVs the QC pipeline produces and writes a .ptx session the sound team
opens directly — every marker named, positioned to the sample, and carrying its
original-language line as the marker comment, so the engineer sees what should be there
without opening the Excel report. A legacy front-end for the older marker MIDI files is
kept behind `--midi`.

    python tools/ptx_markers.py <folder>          # CSVs -> <folder>/ptx report/
    python tools/ptx_markers.py <folder> -o <dir> # ...writing somewhere else
    python tools/ptx_markers.py                   # no folder: the original POA batch dirs
    python tools/ptx_markers.py <folder> --midi   # legacy marker MIDIs (carry no comments)

THE FORMAT LIVES IN backend/ptx.py, which a hosted run also calls to publish a .ptx
alongside its marker CSV. This file is the local batch front-end over it: the same writer,
the same reference self-check, so a session made here and one downloaded from a Teams run
are the same bytes for the same markers.
"""
import argparse
import glob
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.ptx import (SR, load_references, read_marker_csv,  # noqa: E402
                         session_name_for, write_ptx)

CSV_DIR = r"C:\Users\Rishi\Desktop\poa-reports\csv-for-ptx"
MIDI_DIR = r"C:\Users\Rishi\Desktop\poa-reports\midi report"
OUT_DIR = r"C:\Users\Rishi\Desktop\poa-reports\ptx report"
# The MIDI path writes somewhere else on purpose. Its output is superseded by the CSV
# batch (the CSVs carry song/fragment labelling and the original-language comments the
# MIDIs lack) and it re-creates EP22, which the studio asked to be dropped entirely —
# its QC ran against a dub rebuilt from a malformed stem folder. Sharing OUT_DIR meant
# a single --midi run silently put stale, withdrawn files back into the delivery folder.
MIDI_OUT_DIR = os.path.join(OUT_DIR, "_legacy-midi")
# Sessions are written into their own subfolder of the input, never in among the source
# CSVs — the output is what gets handed to the studio, and it should be one clean folder.
OUT_SUBDIR = "ptx report"

TICKS_PER_SEC = 960                    # 120 BPM @ 480 ppqn, as the marker MIDI is written


# ------------------------------------------------------------------ MIDI input

def _read_vlq(data, p):
    n = 0
    while True:
        byte = data[p]; p += 1
        n = (n << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return n, p


def read_marker_midi(path):
    """Marker meta-events from a marker MIDI -> [(name bytes, comment bytes, sample pos)].
    Legacy input: it carries no comment, so the original-language line is absent."""
    data = open(path, "rb").read()
    assert data[:4] == b"MThd", "not a MIDI file"
    hlen, _fmt, ntrk, tpq = struct.unpack(">IHHH", data[4:14])
    assert tpq == 480, f"unexpected division {tpq}"
    p, out = 8 + hlen, []
    for _ in range(ntrk):
        assert data[p:p + 4] == b"MTrk"
        tlen = struct.unpack(">I", data[p + 4:p + 8])[0]
        tp, end, tick = p + 8, p + 8 + tlen, 0
        while tp < end:
            dt, tp = _read_vlq(data, tp)
            tick += dt
            status = data[tp]
            if status == 0xFF:
                mtype = data[tp + 1]
                mlen, np_ = _read_vlq(data, tp + 2)
                if mtype == 0x06:
                    out.append((data[np_:np_ + mlen][:255], b"",
                                tick * SR // TICKS_PER_SEC))
                elif mtype == 0x54:
                    assert data[np_:np_ + 5] == bytes([0x20, 0, 0, 0, 0]), \
                        "non-zero SMPTE offset — positions would need shifting"
                tp = np_ + mlen
            elif status in (0xF0, 0xF7):
                mlen, np_ = _read_vlq(data, tp + 1)
                tp = np_ + mlen
            else:
                raise RuntimeError(f"unexpected MIDI event 0x{status:02X}")
        p = end
    return out


# ------------------------------------------------------------------------ run

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Write Pro Tools .ptx marker sessions from QC marker CSVs.")
    ap.add_argument("indir", nargs="?", help="folder of marker CSVs (or MIDIs with --midi)")
    ap.add_argument("-o", "--out", help="where to write (default: alongside the input)")
    ap.add_argument("--midi", action="store_true",
                    help="read the legacy marker MIDIs instead of CSVs (no comments)")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    # With no folder given, fall back to the original POA batch directories so the
    # delivered run stays reproducible; with one given, the sessions go to their own
    # subfolder rather than in among the sources, so what gets handed to the studio is
    # exactly one folder of .ptx and nothing else.
    if a.indir:
        in_dir = a.indir
        out_dir = a.out or os.path.join(in_dir, OUT_SUBDIR)
    else:
        in_dir = MIDI_DIR if a.midi else CSV_DIR
        out_dir = a.out or (MIDI_OUT_DIR if a.midi else OUT_DIR)

    read, pattern = (read_marker_midi, "*.mid") if a.midi else (read_marker_csv, "*.csv")
    sources = sorted(glob.glob(os.path.join(in_dir, pattern)))
    if not sources:
        print(f"no {pattern} files in {in_dir}")
        return 1

    load_references(verbose=True)
    os.makedirs(out_dir, exist_ok=True)

    total = human = 0
    for src in sources:
        markers = read(src)
        stem = os.path.splitext(os.path.basename(src))[0]
        # the legacy MIDI names collide with the CSV ones, so keep them distinguishable
        session = session_name_for(f"{stem}_FROM-MIDI" if a.midi else stem)
        if not markers:
            print(f"skipped {session}: no markers in {os.path.basename(src)}")
            continue
        write_ptx(markers, os.path.join(out_dir, session), session)
        need = sum(1 for n, _, _ in markers if not n.startswith((b"SONG", b"FRAGMENT")))
        total += len(markers)
        human += need
        print(f"wrote {session}: {len(markers)} markers ({need} need a human)")
    print(f"\nTOTAL {total} markers, {human} need a human, {total - human} song/fragment")
    print(f"in {out_dir}")
    return 0


if __name__ == "__main__":
    main()
