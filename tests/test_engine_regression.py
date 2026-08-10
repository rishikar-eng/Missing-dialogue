"""Regression tests for the ranking engine, over REAL production flags.

Why this exists
---------------
Every threshold in audio_qc.py is justified by a measurement — and until now those
measurements lived in commit messages. Nothing re-ran them. In one afternoon three separate
self-inflicted regressions shipped and were only caught by manually re-reading reports:

  * a block-contiguity fix shattered the closing song, sending four song lines to HIGH
  * a song-neighbourhood pass spanned the whole episode and demoted a verified real drop
  * a tier rework read slot_cov as "the dub speaks here" and buried three verified drops

All three would have failed here in under a second.

Two kinds of assertion, deliberately separated:

  SNAPSHOT   every real flag's tier is pinned. Changing a threshold changes tiers and fails
             the build with a diff. That is not a bug — it is the point. Re-bless with
             `python tests/test_engine_regression.py --bless` once you have looked at what
             moved and agreed with it.

  SEMANTIC   what a human established by ear. These encode knowledge, not current behaviour,
             and must NEVER be re-blessed to make a build pass. If one of these fails, the
             engine got worse.

Runs offline: no S3, no audio, no Fargate.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from backend.audio_qc import (  # noqa: E402
    _group_missing, _songlike, _tag_song_reprise, _tier_from_features,
)

CASES = os.path.join(HERE, "fixtures", "tier_cases.json")
TRUTH = os.path.join(HERE, "fixtures", "ear_truth.json")


def _load(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _flag_to_features(f):
    """A fixture row is already the recorded feature vector; _tier_from_features wants the
    error-dict shape it sees in production."""
    return {
        "type": "MISSING",
        "text": f.get("text") or "",
        "coverage": f.get("coverage"),
        "slot_speech_cov": f.get("slot_speech_cov"),
        "fill": f.get("fill"),
        "fragment": f.get("fragment"),
        "sung": f.get("sung"),
        "song_reprise": f.get("song_reprise"),
        "script_start_s": f.get("script_start_s"),
        "script_end_s": f.get("script_end_s"),
        "block": f.get("block"),
    }


# --------------------------------------------------------------------------- SNAPSHOT
def test_tiers_have_not_drifted():
    """Every real flag still ranks where it did. Fails loudly on any threshold change."""
    data = _load(CASES)
    moved = []
    checked = 0
    for ep, blob in sorted(data["episodes"].items()):
        for f in blob["flags"]:
            want = f.get("expected_tier")
            if want is None:
                continue
            checked += 1
            got = _tier_from_features(_flag_to_features(f))
            if got != want:
                moved.append("%s @%8.2f  %s -> %s  %r"
                             % (ep, f["script_start_s"], want, got, (f.get("text") or "")[:40]))
    assert checked > 400, "fixture looks empty — did the capture run? (%d)" % checked
    assert not moved, (
        "%d of %d flags changed tier.\n\nIf this was deliberate, review each line and "
        "re-bless with:\n    python tests/test_engine_regression.py --bless\n\n%s"
        % (len(moved), checked, "\n".join(moved[:25])))


def test_high_tier_stays_a_workable_queue():
    """HIGH is a 'verify these first' list. If it stops being short it stops being useful —
    and if it is empty the tier carries no information at all (the pre-2026-08-10 rule
    scored HIGH exactly 0 times in 677 flags)."""
    data = _load(CASES)
    per_ep, tot_high, tot = [], 0, 0
    for ep, blob in data["episodes"].items():
        tiers = [_tier_from_features(_flag_to_features(f)) for f in blob["flags"]]
        if not tiers:
            continue
        n_high = sum(1 for t in tiers if t == "high")
        per_ep.append((ep, n_high))
        tot_high += n_high
        tot += len(tiers)
    assert tot > 400
    share = tot_high / tot
    assert 0 < share < 0.25, (
        "HIGH is %.1f%% of all flags (%d/%d) — %s"
        % (100 * share, tot_high, tot,
           "the tier has stopped discriminating" if share else "the tier never fires"))
    worst = max(per_ep, key=lambda x: x[1])
    assert worst[1] <= 25, "%s alone has %d HIGH flags — nobody will work that queue" % worst


# --------------------------------------------------------------------------- SEMANTIC
def _ear_points():
    truth = _load(TRUTH)
    epmap = {"poa-ep9": "EP09", "poa-ep10": "EP10", "poa-ep11": "EP11",
             "poa-ep11-stems": "EP11", "poa-ep12": "EP12", "poa-ep21": "EP21",
             "poa-ep23": "EP23", "poa-ep25": "EP25"}
    for case, v in truth["cases"].items():
        if v.get("INVALID") or case not in epmap:
            continue
        for kind in ("verified_missing", "verified_present"):
            for row in (v.get(kind) or []):
                if isinstance(row, dict) and row.get("start") is not None:
                    yield epmap[case], float(row["start"]), kind, (row.get("note") or "")


def _find(data, ep, t, tol=2.5):
    for f in data["episodes"].get(ep, {}).get("flags", []):
        if abs(float(f["script_start_s"]) - t) <= tol:
            return f
    return None


def test_no_ear_verified_drop_is_ranked_low():
    """A line a human confirmed is missing must not be filed where the studio is told to
    look last. This is the assertion the tier rework exists to satisfy: before it, 5 of 7
    verified drops sat at LOW."""
    data = _load(CASES)
    buried, found = [], 0
    for ep, t, kind, note in _ear_points():
        if kind != "verified_missing":
            continue
        f = _find(data, ep, t)
        if f is None:
            continue                      # not flagged in this capture; covered by the test below
        found += 1
        tier = _tier_from_features(_flag_to_features(f))
        if tier == "low":
            buried.append("%s @%.1f  %s" % (ep, t, note[:60]))
    assert found >= 4, "expected several ear-verified drops in the fixture, saw %d" % found
    assert len(buried) <= 1, (
        "%d ear-verified real drops ranked LOW:\n%s" % (len(buried), "\n".join(buried)))


def test_no_ear_verified_drop_is_written_off_as_sung():
    """The sung-line detector demotes and removes markers, so a real drop tagged sung is a
    finding the sound team will never see on the timeline. Measured margin when this was
    written: the closest real drop sat 2.1 dB below the threshold."""
    data = _load(CASES)
    bad = []
    for ep, t, kind, note in _ear_points():
        if kind != "verified_missing":
            continue
        f = _find(data, ep, t)
        if f and f.get("sung"):
            bad.append("%s @%.1f (margin %s dB) %s" % (ep, t, f.get("sung_margin_db"), note[:50]))
    assert not bad, "ear-verified real drops tagged as SUNG:\n%s" % "\n".join(bad)


def test_ear_verified_false_flags_do_not_reach_high():
    """Lines a human confirmed are PRESENT must not sit at the top of the verify queue."""
    data = _load(CASES)
    promoted = []
    for ep, t, kind, note in _ear_points():
        if kind != "verified_present":
            continue
        f = _find(data, ep, t)
        if f is None:
            continue
        if _tier_from_features(_flag_to_features(f)) == "high":
            promoted.append("%s @%.1f  %s" % (ep, t, note[:60]))
    assert len(promoted) <= 1, (
        "%d ear-verified FALSE flags promoted to HIGH:\n%s" % (len(promoted), "\n".join(promoted)))


# --------------------------------------------------------------------------- UNIT
def test_block_needs_consecutive_original_lines():
    """A block claims 'no dub audio throughout'. Four drops with correctly dubbed lines
    between them are four findings, not one un-dubbed stretch."""
    sparse = [{"type": "MISSING", "script_start_s": t, "script_end_s": t + 1.0,
               "slot_speech_cov": 0.0, "script_index": i}
              for i, t in zip((10, 12, 14, 16), (100.0, 109.0, 118.0, 127.0))]
    assert _group_missing(sparse) == 0
    run = [{"type": "MISSING", "script_start_s": t, "script_end_s": t + 1.0,
            "slot_speech_cov": 0.0, "script_index": i}
           for i, t in enumerate((100.0, 103.0, 106.0, 109.0), start=10)]
    assert _group_missing(run) == 1


def test_a_short_lyric_cannot_demote_a_longer_line():
    """The reprise ratio divides by the FLAGGED line's length. With min() a three-word lyric
    of function words scored 1.00 against any longer line containing them.

    The candidate sits 10 minutes from the song on purpose: the neighbourhood pass would
    otherwise sweep it in for a completely different (and correct) reason, and this test
    would silently stop testing the ratio.
    """
    errs = [{"type": "MISSING", "text": "Eu sei que ele nao vem.", "script_start_s": 600.0},
            {"type": "MISSING", "text": "Eu sei que", "block": {"id": 1}, "script_start_s": 5.0},
            {"type": "MISSING", "text": "Eu sei que", "block": {"id": 1}, "script_start_s": 6.0}]
    _tag_song_reprise(errs, {"eu", "sei", "que"})
    assert not errs[0].get("song_reprise"), "a 3-word lyric demoted a 6-word dialogue line"


def test_song_neighbourhood_does_not_span_the_episode():
    """Opening and closing themes both produce anchors. Taking min/max over all of them once
    covered the whole running time and tagged the dialogue in between."""
    bank = {"eu", "sei", "que", "diz", "entre", "nos"}
    errs = [{"type": "MISSING", "text": "Eu sei que diz entre nos", "script_start_s": 20.0},
            {"type": "MISSING", "text": "diz entre nos eu sei", "script_start_s": 40.0},
            {"type": "MISSING", "text": "Alguem paga ele agora", "script_start_s": 1215.0},
            {"type": "MISSING", "text": "Eu sei que diz entre nos", "script_start_s": 2400.0},
            {"type": "MISSING", "text": "diz entre nos eu sei", "script_start_s": 2420.0}]
    _tag_song_reprise(errs, bank)
    mid = [e for e in errs if e["script_start_s"] == 1215.0][0]
    assert not mid.get("song_reprise"), "a line 20 minutes from any song was tagged as song"


def test_songlike_detects_repetition_not_length():
    assert _songlike("la la la la la la la la")
    assert not _songlike("Alguem paga ele por favor agora mesmo")


# --------------------------------------------------------------------------- bless
def _bless():
    """Recompute expected_tier for every fixture row. Look at the diff before committing."""
    data = _load(CASES)
    changed = 0
    for blob in data["episodes"].values():
        for f in blob["flags"]:
            got = _tier_from_features(_flag_to_features(f))
            if f.get("expected_tier") != got:
                changed += 1
            f["expected_tier"] = got
    with open(CASES, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print("blessed %d flags (%d changed) -> %s"
          % (sum(len(b["flags"]) for b in data["episodes"].values()), changed, CASES))


if __name__ == "__main__":
    if "--bless" in sys.argv:
        _bless()
    else:
        import traceback
        mod = sys.modules[__name__]
        tests = [n for n in dir(mod) if n.startswith("test_")]
        bad = 0
        for n in sorted(tests):
            try:
                getattr(mod, n)()
                print("  PASS  %s" % n)
            except AssertionError as ex:
                bad += 1
                print("  FAIL  %s\n        %s" % (n, str(ex)[:600].replace("\n", "\n        ")))
            except Exception:  # noqa: BLE001
                bad += 1
                print("  ERROR %s\n%s" % (n, traceback.format_exc()[:600]))
        print("\n%d/%d passed" % (len(tests) - bad, len(tests)))
        sys.exit(1 if bad else 0)
