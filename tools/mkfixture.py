"""Snapshot real flag feature-vectors into a committable fixture.

The engine's judgement lives in a handful of pure-ish functions (_tier_from_features, _sung,
_group_missing, _tag_song_reprise). They can be tested exactly, with no S3, no audio and no
Fargate — IF we keep a sample of real feature vectors. That is what this writes.
"""
import json, sys, collections
import boto3
sys.path.insert(0, "/home/ubuntu/app")

B = "dialogue-qc-output-848005667477"
s3 = boto3.client("s3")
pg = s3.get_paginator("list_objects_v2")

best = {}
for page in pg.paginate(Bucket=B, Prefix="output/audioqc/"):
    for o in page.get("Contents", []):
        if not (o["Key"].endswith(".xlsx") and "POA" in o["Key"]):
            continue
        nm = o["Key"].split("/")[-1]
        ep = next((s for s in nm.split("_") if s.upper().startswith("EP") and s[2:].isdigit()), None)
        if ep and (ep not in best or o["LastModified"] > best[ep][1]):
            best[ep] = (o["Key"].rsplit("/", 1)[0] + "/report.json", o["LastModified"])

KEEP = ("script_start_s", "script_end_s", "text", "coverage", "slot_speech_cov", "fill",
        "fill_rel_db", "fill_dyn_db", "fragment", "sung", "sung_margin_db", "confidence")
out = {"_what": ("Real MISSING-flag feature vectors from production reports, with the tier the "
                 "engine assigned at capture time. tests/test_engine_regression.py replays them "
                 "through the pure ranking functions, so a threshold change that moves a real "
                 "line fails the build instead of being discovered on an episode weeks later."),
       "_captured": "2026-08-10",
       "_how_to_refresh": "python tools/mkfixture.py > tests/fixtures/tier_cases.json (needs S3)",
       "episodes": {}}
for ep in sorted(best):
    rep = json.loads(s3.get_object(Bucket=B, Key=best[ep][0])["Body"].read())
    miss = [e for e in rep["errors"] if e["type"] == "MISSING"]
    rows = []
    for e in sorted(miss, key=lambda x: x["script_start_s"]):
        r = {k: e.get(k) for k in KEEP if e.get(k) is not None}
        b = e.get("block")
        if b:
            r["block"] = {"id": b.get("id"), "n": b.get("n"), "first": b.get("first")}
        if e.get("song_reprise"):
            r["song_reprise"] = True
        rows.append(r)
    sy = rep.get("summary", {})
    out["episodes"][ep] = {
        "n_flags": len(miss),
        "summary": {k: sy.get(k) for k in
                    ("n_unchecked", "coverage", "n_missing_blocks", "n_song_reprise",
                     "n_judge_cleared", "n_judge_garble", "dropped_by_gate")
                    if sy.get(k) is not None},
        "flags": rows,
    }
tot = sum(len(v["flags"]) for v in out["episodes"].values())
print(json.dumps(out, ensure_ascii=False, indent=1))
sys.stderr.write("captured %d episodes, %d flags\n" % (len(out["episodes"]), tot))
