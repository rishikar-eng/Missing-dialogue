"""Offline evaluation harness for the MISSING-flag confidence tiers.

Every input a tier could use (semantic match, dub fill, slot coverage, duration, word
count, block membership, song tags) is already written to each run's report.json. So a
candidate tier rule can be scored across every episode we have WITHOUT re-running QC —
which is the only way to test a rule on more episodes than we have ear-labels for.

Two kinds of evidence, deliberately kept apart:

  LABELLED   the ear-verified lines in tests/fixtures/ear_truth.json. Few (7 on POA), so
             any rule fitted to them is overfitted by construction. Used only to check a
             rule does not LOSE a known real drop, and scored leave-one-episode-out.

  UNLABELLED every flag in every episode. No ground truth, but the DISTRIBUTION is
             informative and cannot be gamed by fitting: a tier ordering that means
             anything must be monotone in independent physical evidence (an acoustically
             silent slot is likelier to be a real drop than one full of dub speech), and
             'high' must stay rare enough to be a triage queue a human will actually work.

Usage:
    python tools/tier_eval.py fetch      # pull every report.json from S3 -> flags.jsonl
    python tools/tier_eval.py report     # score the current + candidate rules
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "_tier_data")
FLAGS = os.path.join(OUT, "flags.jsonl")
BUCKET = os.environ.get("DQC_S3_BUCKET", "dialogue-qc-output-848005667477")


# --------------------------------------------------------------------------- fetch
def fetch() -> None:
    """Pull every audio-QC report.json in the bucket into one flat flag table."""
    import boto3

    os.makedirs(OUT, exist_ok=True)
    s3 = boto3.client("s3")
    pg = s3.get_paginator("list_objects_v2")
    # report.json carries no series/episode — the workbook NAME in the same folder does
    # (e.g. POA-Paulo-O-Apostolo_EP11_English_AudioQC-Report_2026-08-10.xlsx).
    folders: dict[str, dict] = {}
    for p in pg.paginate(Bucket=BUCKET):
        for o in p.get("Contents", []):
            k = o["Key"]
            if "/" not in k:
                continue
            d = k.rsplit("/", 1)[0]
            slot = folders.setdefault(d, {})
            if k.endswith("/report.json"):
                slot["report"] = k
            elif k.endswith(".xlsx"):
                slot["xlsx"] = k.rsplit("/", 1)[-1]
    keys = [(v["report"], v.get("xlsx", "")) for v in folders.values() if v.get("report")]
    n_rows = 0
    with open(FLAGS, "w", encoding="utf-8") as fh:
        for k, xlsx in keys:
            try:
                rep = json.loads(s3.get_object(Bucket=BUCKET, Key=k)["Body"].read())
            except Exception as e:  # noqa: BLE001
                print(f"  skip {k}: {str(e)[:60]}")
                continue
            run = k.rsplit("/", 2)[-2]
            series, ep = "", ""
            parts = xlsx.split("_")
            if len(parts) >= 2:
                series = parts[0]
                for seg in parts:
                    if seg.upper().startswith("EP") and seg[2:].isdigit():
                        ep = seg.upper()
                        break
            for e in rep.get("errors", []):
                if e.get("type") != "MISSING":
                    continue
                blk = e.get("block") or {}
                fh.write(json.dumps({
                    "run": run, "series": series, "episode": ep,
                    "start": e.get("script_start_s"), "end": e.get("script_end_s"),
                    "text": e.get("text") or "",
                    "coverage": e.get("coverage"),
                    "slot_cov": e.get("slot_speech_cov"),
                    "fill": e.get("fill"),
                    "fill_rel_db": e.get("fill_rel_db"),
                    "fill_dyn_db": e.get("fill_dyn_db"),
                    "fragment": bool(e.get("fragment")),
                    "song": e.get("song") or e.get("song_reprise") or None,
                    "block_id": blk.get("id"), "block_n": blk.get("n"),
                    "block_first": blk.get("first"),
                    "confidence": e.get("confidence"),
                }, ensure_ascii=False) + "\n")
                n_rows += 1
    print(f"{len(keys)} reports -> {n_rows} MISSING flags -> {FLAGS}")


def load() -> list[dict]:
    with open(FLAGS, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


# --------------------------------------------------------------------- tier rules
def tier_current(f: dict) -> str:
    """Today's rule (audio_qc._confidence): semantic score + word count + slot coverage."""
    best = f.get("coverage")
    if best is None:
        return "low"
    words = len((f.get("text") or "").split())
    sc = f.get("slot_cov")
    if best < 0.35 and words >= 3 and (sc is None or sc < 0.2):
        return "high"
    if best < 0.5:
        return "medium"
    return "low"


def duration(f: dict) -> float:
    try:
        return max(0.0, float(f["end"]) - float(f["start"]))
    except Exception:  # noqa: BLE001
        return 0.0


def tier_acoustic(f: dict) -> str:
    """The candidate rule — imported from the ENGINE, never reimplemented here.

    A separate copy in the evaluator is how you end up validating a rule that is not the
    one that ships. tier_eval feeds the real _tier_from_features the same field names the
    report uses, so what is measured here is exactly what runs in production.
    """
    sys.path.insert(0, ROOT)
    from backend.audio_qc import _tier_from_features
    return _tier_from_features({
        "type": "MISSING",
        "text": f.get("text") or "",
        "coverage": f.get("coverage"),
        "slot_speech_cov": f.get("slot_cov"),
        "fill": f.get("fill"),
        "fragment": f.get("fragment"),
        "song_reprise": f.get("song"),
        "script_start_s": f.get("start"), "script_end_s": f.get("end"),
        "block": ({"n": f.get("block_n"), "first": f.get("block_first")}
                  if f.get("block_id") is not None else None),
    })


RULES = {"current": tier_current, "acoustic": tier_acoustic}


# ------------------------------------------------------------------- ear labels
def ear_labels() -> list[dict]:
    """Ear-verified lines mapped to (episode, time). Only POA + only entries we can
    place on a timeline are usable for scoring."""
    p = os.path.join(ROOT, "tests", "fixtures", "ear_truth.json")
    truth = json.load(open(p, encoding="utf-8"))
    out = []
    for case, v in truth["cases"].items():
        if v.get("INVALID"):
            continue
        for kind, key in (("missing", "verified_missing"), ("present", "verified_present")):
            for row in (v.get(key) or []):
                if not isinstance(row, dict) or row.get("start") is None:
                    continue
                out.append({"case": case, "label": kind, "start": float(row["start"]),
                            "note": row.get("note", "")})
    return out


def match_flag(flags: list[dict], ep_key: str, t: float, tol: float = 2.0):
    for f in flags:
        if ep_key not in (f.get("episode") or "").lower().replace("ep", "ep"):
            continue
        if f.get("start") is not None and abs(float(f["start"]) - t) <= tol:
            return f
    return None


# ---------------------------------------------------------------------- scoring
def report() -> None:
    flags = load()
    poa = [f for f in flags if "poa" in (f.get("series") or "").lower()]
    print(f"corpus: {len(flags)} flags total, {len(poa)} on POA "
          f"across {len({f['episode'] for f in poa})} episodes\n")

    for name, rule in RULES.items():
        print(f"===== rule: {name} =====")
        by_ep: dict[str, dict[str, int]] = {}
        for f in poa:
            t = rule(f)
            by_ep.setdefault(f["episode"], {}).setdefault(t, 0)
            by_ep[f["episode"]][t] = by_ep[f["episode"]].get(t, 0) + 1

        tot = {"high": 0, "medium": 0, "low": 0}
        for ep in sorted(by_ep):
            for k in tot:
                tot[k] += by_ep[ep].get(k, 0)
        n = sum(tot.values()) or 1
        print(f"  tier mix: high {tot['high']} ({tot['high']/n:.1%})  "
              f"medium {tot['medium']} ({tot['medium']/n:.1%})  "
              f"low {tot['low']} ({tot['low']/n:.1%})")

        # Per-episode 'high' load — a triage queue nobody can work is a failed design.
        highs = sorted(by_ep[e].get("high", 0) for e in by_ep)
        print(f"  per-episode HIGH: min {highs[0]}  median {highs[len(highs)//2]}  "
              f"max {highs[-1]}")

        # UNLABELLED monotonicity: does the tier track independent physical evidence?
        print("  silence-share by tier (independent evidence, not fitted):")
        for t in ("high", "medium", "low"):
            sub = [f for f in poa if rule(f) == t]
            if not sub:
                print(f"    {t:<7} n=0")
                continue
            sil = sum(1 for f in sub if f.get("fill") == "silence") / len(sub)
            spd = sum(1 for f in sub if f.get("fill") == "speech") / len(sub)
            mdur = sorted(duration(f) for f in sub)[len(sub) // 2]
            print(f"    {t:<7} n={len(sub):<5} fill=silence {sil:5.1%}   "
                  f"fill=speech {spd:5.1%}   median dur {mdur:.2f}s")
        print()

    # LABELLED: never lose a known real drop.
    print("===== ear-verified lines (labelled, tiny — a guard, not a fitting set) =====")
    labels = ear_labels()
    epmap = {"poa-ep9": "EP09", "poa-ep10": "EP10", "poa-ep11": "EP11", "poa-ep12": "EP12",
             "poa-ep21": "EP21", "poa-ep23": "EP23", "poa-ep25": "EP25",
             "poa-ep11-stems": "EP11"}
    for lab in labels:
        ep = epmap.get(lab["case"])
        if not ep:
            continue
        cand = [f for f in poa if (f.get("episode") or "").upper() == ep
                and f.get("start") is not None
                and abs(float(f["start"]) - lab["start"]) <= 2.0]
        if not cand:
            print(f"  {ep} @{lab['start']:8.2f} {lab['label']:<8} -> NOT FLAGGED")
            continue
        f = cand[0]
        tiers = "  ".join(f"{k}={rule(f)}" for k, rule in RULES.items())
        print(f"  {ep} @{lab['start']:8.2f} {lab['label']:<8} -> {tiers}"
              f"   [fill={f.get('fill')} cov={f.get('coverage')} slot={f.get('slot_cov')}]")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "fetch":
        fetch()
    else:
        report()
