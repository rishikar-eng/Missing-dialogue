"""Score an audio-QC report against human-verified ground truth.

    python -m tests.score_ear_truth <report.json> <case-id>
    python -m tests.score_ear_truth --list

Exists because accuracy work on this pipeline kept being judged on flag COUNTS, which are
noise: identical inputs have produced 28-48 transcript lines, and two identical EP41 runs once
shared 1 of 8 flags. Counting alone hid a real regression (local drift, reverted in 982485c)
and nearly hid another (the relative-only music gate silently killed EP42's verified 147.6
drop). The only trustworthy question is: did we keep the lines a human confirmed are missing,
without adding flags on lines a human confirmed are present?

Exit code is non-zero when recall is incomplete, so this can gate a change.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "ear_truth.json"


def _overlap(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
    return not (a[1] + tol < b[0] or b[1] + tol < a[0])


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def score(report_path: str, case_id: str) -> int:
    truth = json.loads(FIXTURE.read_text(encoding="utf-8"))
    tol = float(truth.get("tolerance_s", 2.5))
    case = truth["cases"].get(case_id)
    if not case:
        print(f"unknown case '{case_id}'. Known: {', '.join(truth['cases'])}")
        return 2
    rep = json.loads(Path(report_path).read_text(encoding="utf-8"))
    flags = [(e["script_start_s"], e["script_end_s"], e.get("confidence") or "?")
             for e in rep.get("errors", []) if e.get("type") == "MISSING"]

    print(f"=== {case_id} ===")
    print(case["description"][:160])
    print(f"\nreport: {len(flags)} MISSING flags")

    missing = case.get("verified_missing") or []
    hits, lost = [], []
    for m in missing:
        got = [f for f in flags if _overlap((m["start"], m["end"]), (f[0], f[1]), tol)]
        (hits if got else lost).append(m)
    if missing:
        print(f"\nRECALL {len(hits)}/{len(missing)} of human-verified missing lines")
        for m in lost:
            print(f"   LOST  {_mmss(m['start'])}  {m['note'][:90]}")

    present = case.get("verified_present") or []
    false_flags = []
    for p in present:
        got = [f for f in flags if _overlap((p["start"], p["end"]), (f[0], f[1]), tol)]
        if got:
            false_flags.append((p, got[0]))
    if present:
        print(f"\nFALSE FLAGS on verified-present lines: {len(false_flags)}/{len(present)}")
        for p, f in false_flags:
            print(f"   FALSE {_mmss(p['start'])} (conf={f[2]})  {p['note'][:80]}")

    # Everything not matched to a verified line is unclassified — usually the noise floor.
    classified = [m for m in missing] + [p for p in present]
    unclassified = [f for f in flags
                    if not any(_overlap((c["start"], c["end"]), (f[0], f[1]), tol)
                               for c in classified)]
    print(f"\nunclassified flags (need an ear): {len(unclassified)}")
    for f in unclassified[:12]:
        print(f"   ?     {_mmss(f[0])} conf={f[2]}")

    for c in case.get("contested") or []:
        print(f"\nCONTESTED {_mmss(c['start'])}: {c['note'][:150]}")

    ok = (not missing or len(hits) == len(missing)) and not false_flags
    print(f"\n{'PASS' if ok else 'FAIL'} — "
          f"recall {len(hits)}/{len(missing)}, false flags {len(false_flags)}")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "--list":
        truth = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for k, v in truth["cases"].items():
            n_m = len(v.get("verified_missing") or [])
            n_p = len(v.get("verified_present") or [])
            print(f"{k:22} {n_m} verified-missing, {n_p} verified-present")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    return score(argv[0], argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
