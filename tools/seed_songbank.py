"""Seed the song-bank candidate ledger from reports that already exist.

The bank promotes a lyric only after the same line has been seen in TWO different
episodes, which normally means two more runs before a series is protected. Every POA
report already in S3 carries its block lines, so the ledger can be built from them now
and the protection exists immediately.

Deliberately reads only what the engine itself would have recorded — block lines — and
applies the same two-episode rule. It does not invent entries.
"""
import collections
import json
import re
import sys

import boto3

B = "dialogue-qc-output-848005667477"
SERIES_SLUG = "poa-paulo-o-apostolo"
SERIES_MATCH = "POA"

sys.path.insert(0, "/home/ubuntu/app")
from backend.audio_qc import _norm_words  # noqa: E402

s3 = boto3.client("s3")
pg = s3.get_paginator("list_objects_v2")

# one report per folder, plus the workbook name that tells us which episode it was
folders = {}
for page in pg.paginate(Bucket=B, Prefix="output/audioqc/"):
    for o in page.get("Contents", []):
        d = o["Key"].rsplit("/", 1)[0]
        slot = folders.setdefault(d, {})
        if o["Key"].endswith("/report.json"):
            slot["report"] = o["Key"]
        elif o["Key"].endswith(".xlsx"):
            slot["xlsx"] = o["Key"].rsplit("/", 1)[-1]
        slot["when"] = max(slot.get("when", o["LastModified"]), o["LastModified"])

# newest run per episode only
best = {}
for d, v in folders.items():
    if not (v.get("report") and v.get("xlsx") and SERIES_MATCH in v["xlsx"]):
        continue
    ep = next((s.upper() for s in v["xlsx"].split("_")
               if s.upper().startswith("EP") and s[2:].isdigit()), None)
    if not ep:
        continue
    if ep not in best or v["when"] > best[ep][1]:
        best[ep] = (v["report"], v["when"])

print("episodes with a report:", sorted(best))
cands = collections.defaultdict(set)
for ep, (key, _) in sorted(best.items()):
    rep = json.loads(s3.get_object(Bucket=B, Key=key)["Body"].read())
    n = 0
    for e in rep.get("errors", []):
        if e.get("type") != "MISSING" or not e.get("block"):
            continue
        line = " ".join(sorted(w for w in _norm_words(e.get("text")) if len(w) > 2))
        if len(line.split()) < 3:
            continue
        cands[line].add(ep)
        n += 1
    print("  %s: %d block lines" % (ep, n))

promoted = sorted({w for line, eps in cands.items() if len(eps) >= 2 for w in line.split()})
recurring = {line: sorted(eps) for line, eps in cands.items() if len(eps) >= 2}

print()
print("candidate lines: %d" % len(cands))
print("seen in 2+ episodes: %d" % len(recurring))
for line, eps in sorted(recurring.items())[:12]:
    print("   %-52s %s" % (line[:52], ",".join(eps)))
print()
print("words promoted to the bank: %d" % len(promoted))
print("  ", ", ".join(promoted[:40]))

if "--write" in sys.argv:
    s3.put_object(Bucket=B, Key="songbank/%s.candidates.json" % SERIES_SLUG,
                  Body=json.dumps({k: sorted(v) for k, v in cands.items()}).encode())
    s3.put_object(Bucket=B, Key="songbank/%s.json" % SERIES_SLUG,
                  Body=json.dumps({"words": promoted}).encode())
    print("\nWRITTEN: ledger + bank")
else:
    print("\n(dry run — pass --write to publish)")
