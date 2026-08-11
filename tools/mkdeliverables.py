"""Build the studio deliverables for a set of episodes: marker CSV (+ the run's MIDI).

The CSV layout is the one the sound team ALREADY converted successfully to .ptx via
editingtools.io — No.,In,Color,Name,Comment with absolute SMPTE at 25 fps, zero-based
(the delivered dubs carry BWF TimeReference 0, so no session offset is needed). It is not
the Reaper layout build_marker_csv emits; matching what demonstrably worked beats handing
them a second format to discover problems in.

Theme lines are dropped, on the same rule the Pro Tools markers use: a line is only written
off when it is BOTH measured as sung AND matches lyrics the series has sung in another
episode. Mid-episode songs stay in — they are rare, and per the studio, suppressing them
would cost real findings for very little noise.
"""
import csv
import json
import os
import sys

sys.path.insert(0, "/home/ubuntu/app")
import boto3

from backend import audio_report

B = "dialogue-qc-output-848005667477"
s3 = boto3.client("s3")
OUT = "/home/ubuntu/deliverables"
FPS = 25.0
os.makedirs(OUT, exist_ok=True)


def smpte(t):
    t = max(0.0, float(t))
    h, m = int(t // 3600), int((t % 3600) // 60)
    s = int(t % 60)
    f = int(round((t - int(t)) * FPS))
    if f >= FPS:                       # rounding can tip a frame over the second boundary
        f = 0
        s += 1
        if s == 60:
            s = 0
            m += 1
    return "%02d:%02d:%02d:%02d" % (h, m, s, f)


def label(e):
    """Marker name and colour. SHORT on purpose: Pro Tools clips the label to the gap before
    the next marker, so long names go unread exactly where markers are dense. The colour
    carries the urgency and the COMMENT carries the original line, so nothing is lost."""
    if e.get("song_reprise") or e.get("sung"):
        return "Blue", "SONG"
    if e.get("block"):
        return "Orange", "BLOCK"
    if e.get("fragment"):
        return "Green", "FRAG"
    conf = (e.get("confidence") or "low").lower()
    return ({"high": "Red", "medium": "Red"}.get(conf, "Orange"),
            {"high": "MISS-HI", "medium": "MISS-MED"}.get(conf, "MISS-LOW"))


def newest_report(ep):
    pg = s3.get_paginator("list_objects_v2")
    best = None
    for page in pg.paginate(Bucket=B, Prefix="output/audioqc/"):
        for o in page.get("Contents", []):
            if o["Key"].endswith(".xlsx") and "POA" in o["Key"] and "_EP%02d_" % ep in o["Key"]:
                if best is None or o["LastModified"] > best[1]:
                    best = (o["Key"].rsplit("/", 1)[0], o["LastModified"])
    return best[0] if best else None


eps = [int(x) for x in sys.argv[1:]] or [8, 16, 17, 18, 19, 27, 28]
print("%-6s %-8s %-9s %-9s %s" % ("ep", "flags", "markers", "themes", "files"))
print("-" * 74)
for ep in eps:
    pre = newest_report(ep)
    if not pre:
        print("EP%-4d  no report" % ep)
        continue
    rep = json.loads(s3.get_object(Bucket=B, Key=pre + "/report.json")["Body"].read())
    miss = [e for e in rep["errors"] if e["type"] == "MISSING"
            and e.get("script_start_s") is not None]
    keep = [e for e in miss if not (e.get("sung") and e.get("song_reprise"))]
    keep.sort(key=lambda e: e["script_start_s"])

    base = "POA_EP%02d_English_QC-Markers" % ep
    csv_path = os.path.join(OUT, base + ".csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["No.", "In", "Color", "Name", "Comment"])
        for i, e in enumerate(keep, start=1):
            colour, name = label(e)
            w.writerow([i, smpte(e["script_start_s"]), colour, name,
                        (e.get("text") or "").replace("\n", " ")])

    # the MIDI the run already published, alongside it under the same stem
    mid_path = os.path.join(OUT, base + ".mid")
    audio_report.build_marker_midi({"errors": keep}, mid_path)

    print("EP%-4d  %-8d %-9d %-9d %s" % (ep, len(miss), len(keep), len(miss) - len(keep),
                                         base + ".csv/.mid"))

print("\nwritten to", OUT)
