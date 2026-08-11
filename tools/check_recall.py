"""Post-deploy recall check: did any human-verified line get worse?

The offline suite (tests/test_engine_regression.py) replays RECORDED features, so it can only
see the ranking layer. A line killed by one of the six gates never becomes a flag and never
enters that fixture — which is exactly how EP12's verified crowd heckle was lost while every
ranking test stayed green.

Deciding a line's fate afresh needs a real run, so this cannot be a per-commit test. Run it
after a deploy, or after any change to a gate:

    python tools/check_recall.py            # compare live reports to the committed baseline
    python tools/check_recall.py --update   # re-baseline, once you have agreed with the diff

Exit code is non-zero when an ear-verified MISSING line has degraded, so it can gate a rollout.
Needs S3 credentials — run it on the server.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

BASELINE = os.path.join(ROOT, "tests", "fixtures", "ear_disposition.json")
BUCKET = os.environ.get("DQC_S3_BUCKET", "dialogue-qc-output-848005667477")

# Ordered best to worst. A verified MISSING line moving right is a regression.
RANK = {"flagged:high": 0, "flagged:medium": 1, "flagged:low": 2, "dropped": 3, "absent": 4}


def _state_key(p):
    return "flagged:%s" % p.get("tier") if p["state"] == "flagged" else p["state"]


def _newest_reports():
    import boto3
    s3 = boto3.client("s3")
    pg = s3.get_paginator("list_objects_v2")
    best = {}
    for page in pg.paginate(Bucket=BUCKET, Prefix="output/audioqc/"):
        for o in page.get("Contents", []):
            if not (o["Key"].endswith(".xlsx") and "POA" in o["Key"]):
                continue
            nm = o["Key"].split("/")[-1]
            ep = next((s for s in nm.split("_")
                       if s.upper().startswith("EP") and s[2:].isdigit()), None)
            if ep and (ep not in best or o["LastModified"] > best[ep][1]):
                best[ep] = (o["Key"].rsplit("/", 1)[0] + "/report.json", o["LastModified"])
    out = {}
    for ep, (key, when) in best.items():
        out[ep] = (json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()), when)
    return out


def current():
    """Recompute the disposition of every ear-verified line from the newest reports."""
    truth = json.load(open(os.path.join(ROOT, "tests", "fixtures", "ear_truth.json"),
                           encoding="utf-8"))
    epmap = {"poa-ep9": "EP09", "poa-ep10": "EP10", "poa-ep11": "EP11",
             "poa-ep11-stems": "EP11", "poa-ep12": "EP12", "poa-ep21": "EP21",
             "poa-ep23": "EP23", "poa-ep25": "EP25"}
    reports = _newest_reports()
    pts = []
    for case, v in truth["cases"].items():
        if v.get("INVALID") or case not in epmap:
            continue
        ep = epmap[case]
        got = reports.get(ep)
        if not got:
            continue
        rep, when = got
        miss = [e for e in rep["errors"] if e["type"] == "MISSING"]
        drops = rep.get("summary", {}).get("dropped") or []
        for kind in ("verified_missing", "verified_present"):
            for row in (v.get(kind) or []):
                if not (isinstance(row, dict) and row.get("start") is not None):
                    continue
                t = float(row["start"])
                rec = {"case": case, "episode": ep, "at": t,
                       "ear": "missing" if kind == "verified_missing" else "present",
                       "note": (row.get("note") or "")[:90],
                       "report_at": when.strftime("%Y-%m-%d %H:%M")}
                hit = [e for e in miss if abs(e["script_start_s"] - t) <= 2.5]
                if hit:
                    rec.update(state="flagged", tier=hit[0].get("confidence"),
                               sung=bool(hit[0].get("sung")))
                else:
                    d = [x for x in drops if abs(x["at"] - t) <= 2.5]
                    if d:
                        rec.update(state="dropped", gate=d[0]["gate"],
                                   why={k: val for k, val in d[0].items()
                                        if k not in ("gate", "at", "end", "text")})
                    else:
                        rec["state"] = "absent"
                pts.append(rec)
    pts.sort(key=lambda r: (r["episode"], r["at"]))
    return pts


def main(argv):
    base = json.load(open(BASELINE, encoding="utf-8"))
    old = {(p["episode"], round(p["at"], 1)): p for p in base["points"]}
    new = current()
    if not new:
        print("no reports found — nothing to compare"); return 0

    worse, better, same = [], [], 0
    for p in new:
        k = (p["episode"], round(p["at"], 1))
        o = old.get(k)
        if not o:
            print("  NEW   %s @%.1f  %s" % (p["episode"], p["at"], _state_key(p)))
            continue
        a, b = RANK.get(_state_key(o), 9), RANK.get(_state_key(p), 9)
        line = ("  %-6s @%-8.1f %-8s %-16s -> %-16s %s"
                % (p["episode"], p["at"], p["ear"], _state_key(o), _state_key(p),
                   (p.get("gate") or "") + " " + str(p.get("why") or "")))
        if b > a:
            worse.append((p, line))
        elif b < a:
            better.append(line)
        else:
            same += 1

    print("%d ear-verified lines: %d unchanged, %d improved, %d WORSE"
          % (len(new), same, len(better), len(worse)))
    for l in better:
        print("  BETTER" + l)
    for _, l in worse:
        print("  WORSE " + l)

    if "--update" in argv:
        base["points"] = new
        base["_captured"] = "updated by tools/check_recall.py"
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(base, fh, ensure_ascii=False, indent=1)
        print("baseline updated — commit the diff")
        return 0

    # Only a MISSING line getting worse is a failure. A PRESENT line being dropped is the
    # gates doing their job; a PRESENT line being promoted is noise, not a broken build.
    fatal = [p for p, _ in worse if p["ear"] == "missing"]
    if fatal:
        print("\nFAIL: %d ear-verified real drop(s) degraded. If deliberate, re-baseline with "
              "--update and say why in the commit." % len(fatal))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
