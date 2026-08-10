"""Build a BLIND, stratified ear-check pack for the confidence tiers.

Blind on purpose: the tier is not in the filename. A listener who can see that a clip is
rated HIGH will hear it as a drop, and the test stops measuring anything. The answer key is
written outside the pack.

Stratified on purpose: sampled ACROSS episodes and ACROSS tiers, so the answer says
something about POA generally rather than about the handful of lines already argued over.

Each case is one file: the ORIGINAL (Portuguese) at that moment, a beep, then the DUB
(English) at the SAME timecode. Matched gain throughout — per-clip normalisation once
amplified digital silence into audible static and cost a day of false conclusions.
"""
import collections
import json
import os
import random
import sys

sys.path.insert(0, "/home/ubuntu/app")
import boto3
import numpy as np
import soundfile as sf

from backend import box_discovery, box_oauth
from backend.audio_qc import _tier_from_features
from backend.audio_jobs import find_dub_mix

B = "dialogue-qc-output-848005667477"
s3 = boto3.client("s3")
OUT = "/home/ubuntu/earpack"
PAD = 2.5
PER_TIER = {"high": 8, "medium": 7, "low": 7}
os.makedirs(OUT, exist_ok=True)

rows = [json.loads(l) for l in
        open("/home/ubuntu/app/tools/_tier_data/flags.jsonl", encoding="utf-8")]
poa = [r for r in rows if "POA" in (r.get("series") or "")]

# Newest run per episode only: older runs used a dub source production no longer picks,
# and mixing them would score the tiers against a pipeline that no longer exists.
runs_by_ep = collections.defaultdict(set)
for r in poa:
    if r.get("episode"):
        runs_by_ep[r["episode"]].add(r["run"])
pick_run = {}
for ep, runs in runs_by_ep.items():
    stamped = []
    for run in runs:
        try:
            objs = s3.list_objects_v2(Bucket=B, Prefix="output/audioqc/%s/" % run)["Contents"]
            stamped.append((max(o["LastModified"] for o in objs), run))
        except Exception:  # noqa: BLE001
            pass
    if stamped:
        pick_run[ep] = sorted(stamped)[-1][1]

flags = [r for r in poa if pick_run.get(r.get("episode")) == r["run"]]
for r in flags:
    r["tier"] = _tier_from_features({
        "type": "MISSING", "text": r.get("text") or "", "coverage": r.get("coverage"),
        "slot_speech_cov": r.get("slot_cov"), "fill": r.get("fill"),
        "fragment": r.get("fragment"), "song_reprise": r.get("song"),
        "script_start_s": r.get("start"), "script_end_s": r.get("end"),
        "block": ({"n": r.get("block_n"), "first": r.get("block_first")}
                  if r.get("block_id") is not None else None)})

print("episodes in pool:", sorted({r["episode"] for r in flags}))
print("tier mix in pool:", dict(collections.Counter(r["tier"] for r in flags)))

rng = random.Random(20260810)
chosen = []
for tier, k in PER_TIER.items():
    by_ep = collections.defaultdict(list)
    for r in flags:
        if r["tier"] == tier:
            by_ep[r["episode"]].append(r)
    for v in by_ep.values():
        rng.shuffle(v)
    picked, depth = [], 0
    while len(picked) < k:
        added = False
        for ep in sorted(by_ep):
            if depth < len(by_ep[ep]) and len(picked) < k:
                picked.append(by_ep[ep][depth])
                added = True
        if not added:
            break
        depth += 1
    chosen += picked
print("chosen:", len(chosen), dict(collections.Counter(c["tier"] for c in chosen)))

_ocache = {}


def orig_stem(ep):
    """The original's cached 16 kHz dialogue stem — small, and already separated."""
    n = int(ep[2:])
    if n in _ocache:
        return _ocache[n]
    pg = s3.get_paginator("list_objects_v2")
    for page in pg.paginate(Bucket=B, Prefix="sepcache/"):
        for o in page.get("Contents", []):
            nm = o["Key"].split("/")[-1]
            if nm.upper().startswith("POA_EP%02d" % n) and nm.endswith(".voc16.npy"):
                dst = "/tmp/o%d.npy" % n
                if not os.path.exists(dst):
                    s3.download_file(B, o["Key"], dst)
                _ocache[n] = np.load(dst)
                return _ocache[n]
    _ocache[n] = None
    return None


_dcache = {}


def dub_audio(ep):
    n = int(ep[2:])
    if n in _dcache:
        return _dcache[n]
    reg = json.load(open("/home/ubuntu/app/backend/series_registry.json"))
    cfg = reg["poa-show"]
    tok = box_oauth.get_token(min_ttl_s=2700)
    d = find_dub_mix(box_discovery._Box(tok), cfg, "English", n)
    if not d:
        _dcache[n] = None
        return None
    raw = "/tmp/dub%d.wav" % n
    if not os.path.exists(raw):
        import httpx
        with httpx.stream("GET", "https://api.box.com/2.0/files/%s/content" % d["id"],
                          headers={"Authorization": "Bearer %s" % tok},
                          follow_redirects=True, timeout=900) as r:
            r.raise_for_status()
            with open(raw, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
    x, sr = sf.read(raw, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != 16000:
        import scipy.signal as ss
        x = ss.resample_poly(x, 16000, sr).astype("float32")
    os.remove(raw)                      # 400 MB each; the 16 kHz array is what we keep
    _dcache[n] = x
    return x


def cut(a, t0, t1):
    out = np.zeros(int((t1 - t0) * 16000), dtype="float32")
    if a is None:
        return out
    i0, i1 = max(0, int(t0 * 16000)), min(len(a), int(t1 * 16000))
    seg = a[i0:i1][:len(out)]          # rounding can make the slice one sample longer
    out[:len(seg)] = seg
    return out


beep = (0.06 * np.sin(2 * np.pi * 880 * np.arange(int(0.18 * 16000)) / 16000)).astype("float32")
gap = np.zeros(int(0.35 * 16000), dtype="float32")

key = []
for idx, r in enumerate(sorted(chosen, key=lambda x: (x["episode"], x["start"])), 1):
    ep, t = r["episode"], float(r["start"])
    O, D = orig_stem(ep), dub_audio(ep)
    if O is None or D is None:
        print("  skip %s @%.1f (no audio available)" % (ep, t))
        continue
    a = cut(O, t - PAD, float(r["end"]) + PAD)
    b = cut(D, t - PAD, float(r["end"]) + PAD)
    clip = np.concatenate([a, gap, beep, gap, b])          # MATCHED GAIN, never normalised
    name = "%02d_%s_%02dm%02ds.flac" % (idx, ep, int(t // 60), int(t % 60))
    sf.write(os.path.join(OUT, name), clip, 16000)
    key.append({"n": idx, "file": name, "episode": ep, "at_s": round(t, 2),
                "tier": r["tier"], "text": r.get("text"), "fill": r.get("fill"),
                "coverage": r.get("coverage"), "slot_cov": r.get("slot_cov")})

json.dump(key, open("/home/ubuntu/earpack_KEY.json", "w"), ensure_ascii=False, indent=1)
with open(os.path.join(OUT, "HOW-TO-LISTEN.txt"), "w", encoding="utf-8") as fh:
    fh.write(
        "POA CONFIDENCE-TIER EAR CHECK\n"
        "=============================\n"
        "%d clips. Each one is:\n\n"
        "    [PORTUGUESE original]  ...beep...  [ENGLISH dub, same timecode]\n\n"
        "Both sides are cut at the SAME timecode with matched gain. If the dub side sounds\n"
        "empty, it is empty - that is not a level trick.\n\n"
        "For each numbered clip, answer one of:\n"
        "    MISSING  - the original says a line and the English dub does not say it here\n"
        "    PRESENT  - the dub does say it (loose translation counts, slightly early/late counts)\n"
        "    UNSURE   - genuinely cannot tell\n\n"
        "Please do NOT try to work out which ones the tool rated highly. That is the point:\n"
        "the ratings are hidden from you and sit in a key file I hold, and I will only compare\n"
        "after your answers are in. Filenames carry the episode and timecode, nothing else.\n\n"
        "Easiest format for me:  \"1-missing, 2-present, 3-unsure, ...\"\n" % len(key))

print("PACK:", OUT, "cases:", len(key))
print("tier mix in pack:", dict(collections.Counter(k["tier"] for k in key)))
