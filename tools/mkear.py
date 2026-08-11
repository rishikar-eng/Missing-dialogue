"""Capture the DISPOSITION of every ear-verified line: flagged, or dropped by which gate.

The tier fixture only holds lines that survived to become flags, so it is blind to the gates
— a line killed by slot-occupied never appears in it at all. That is precisely how EP12's
verified heckle was lost. This records, for every human-verified point, what the pipeline
currently does with it, so the answer is committed and a change to any gate shows up as a diff.
"""
import json, sys
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

truth = json.load(open("/home/ubuntu/app/tests/fixtures/ear_truth.json", encoding="utf-8"))
epmap = {"poa-ep9": "EP09", "poa-ep10": "EP10", "poa-ep11": "EP11", "poa-ep11-stems": "EP11",
         "poa-ep12": "EP12", "poa-ep21": "EP21", "poa-ep23": "EP23", "poa-ep25": "EP25"}

reports = {}
for ep, (key, when) in best.items():
    reports[ep] = json.loads(s3.get_object(Bucket=B, Key=key)["Body"].read())

out = {"_what": ("For every ear-verified line: what the pipeline currently DOES with it — "
                 "flagged (with its tier) or dropped (by which gate, with the number that "
                 "decided it). The tier fixture only holds lines that survived to become "
                 "flags, so it cannot see a gate suppressing one. EP12's verified heckle was "
                 "lost exactly that way, to slot-occupied at slot_cov 1.0."),
       "_captured": "2026-08-10",
       "_how_to_refresh": "python tools/mkear.py > tests/fixtures/ear_disposition.json (needs S3)",
       "points": []}

for case, v in truth["cases"].items():
    if v.get("INVALID") or case not in epmap:
        continue
    ep = epmap[case]
    rep = reports.get(ep)
    if not rep:
        continue
    miss = [e for e in rep["errors"] if e["type"] == "MISSING"]
    drops = rep.get("summary", {}).get("dropped") or []
    for kind in ("verified_missing", "verified_present"):
        for row in (v.get(kind) or []):
            if not (isinstance(row, dict) and row.get("start") is not None):
                continue
            t = float(row["start"])
            rec = {"case": case, "episode": ep, "at": t,
                   "ear": "missing" if kind == "verified_missing" else "present",
                   "note": (row.get("note") or "")[:90]}
            hit = [e for e in miss if abs(e["script_start_s"] - t) <= 2.5]
            if hit:
                rec["state"] = "flagged"
                rec["tier"] = hit[0].get("confidence")
                rec["sung"] = bool(hit[0].get("sung"))
            else:
                d = [x for x in drops if abs(x["at"] - t) <= 2.5]
                if d:
                    rec["state"] = "dropped"
                    rec["gate"] = d[0]["gate"]
                    rec["why"] = {k: val for k, val in d[0].items()
                                  if k not in ("gate", "at", "end", "text")}
                else:
                    rec["state"] = "absent"      # never became a candidate at all
            out["points"].append(rec)

out["points"].sort(key=lambda r: (r["episode"], r["at"]))
print(json.dumps(out, ensure_ascii=False, indent=1))
n = len(out["points"])
sys.stderr.write("captured %d ear points (%d flagged, %d dropped, %d absent)\n" % (
    n, sum(1 for r in out["points"] if r["state"] == "flagged"),
    sum(1 for r in out["points"] if r["state"] == "dropped"),
    sum(1 for r in out["points"] if r["state"] == "absent")))
