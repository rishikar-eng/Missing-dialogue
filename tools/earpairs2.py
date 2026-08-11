"""Blind ear-check batch, drawn from the newly-run episodes.

Numbered from 1: the first batch has been scored and its clips deleted, so there is nothing
to collide with and no reason to make the listener track an offset.

Deliberately different from batch one in two ways:

  * it EXCLUDES anything already written off as the recurring theme, so the listener spends
    their time on lines that are actually in question rather than re-confirming that songs
    are songs. Batch one lost 8 of 16 clips to that.
  * five of these six episodes ship only a Print Premix, so the dub side carries music and
    effects. That is the harder regime to judge and the one we have least evidence about —
    it also produced almost no high-confidence flags, which is itself worth testing.

Both sides are cut at the same timecode with matched gain, as separate files, so they stack
on one Audacity timeline.
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

from backend import audio_jobs, box_discovery, box_oauth

B = "dialogue-qc-output-848005667477"
s3 = boto3.client("s3")
OUT = "/home/ubuntu/earpairs2"
PAD = 4.0
START_AT = 1
PER_TIER = {"high": 4, "medium": 8, "low": 8}
EPS = [8, 16, 17, 18, 19, 27]
os.makedirs(OUT, exist_ok=True)
reg = json.load(open("/home/ubuntu/app/backend/series_registry.json"))
cfg = reg["poa-show"]


def newest(ep):
    pg = s3.get_paginator("list_objects_v2")
    best = None
    for page in pg.paginate(Bucket=B, Prefix="output/audioqc/"):
        for o in page.get("Contents", []):
            if o["Key"].endswith(".xlsx") and "POA" in o["Key"] and "_EP%02d_" % ep in o["Key"]:
                if best is None or o["LastModified"] > best[1]:
                    best = (o["Key"].rsplit("/", 1)[0] + "/report.json", o["LastModified"])
    return best[0] if best else None


pool = []
for ep in EPS:
    k = newest(ep)
    if not k:
        continue
    rep = json.loads(s3.get_object(Bucket=B, Key=k)["Body"].read())
    for e in rep["errors"]:
        if e["type"] != "MISSING" or e.get("script_start_s") is None:
            continue
        if e.get("sung") and e.get("song_reprise"):
            continue                     # already written off as the theme
        pool.append({"ep": ep, "at": float(e["script_start_s"]),
                     "end": float(e.get("script_end_s") or e["script_start_s"] + 1.0),
                     "tier": e.get("confidence"), "text": e.get("text") or "",
                     "fill": e.get("fill"), "sung": bool(e.get("sung")),
                     "coverage": e.get("coverage")})

print("pool: %d flags across %d episodes  %s"
      % (len(pool), len({p["ep"] for p in pool}),
         dict(collections.Counter(p["tier"] for p in pool))))

rng = random.Random(20260811)
chosen = []
for tier, want in PER_TIER.items():
    by_ep = collections.defaultdict(list)
    for p in pool:
        if p["tier"] == tier:
            by_ep[p["ep"]].append(p)
    for v in by_ep.values():
        rng.shuffle(v)
    picked, depth = [], 0
    while len(picked) < want:
        added = False
        for ep in sorted(by_ep):
            if depth < len(by_ep[ep]) and len(picked) < want:
                picked.append(by_ep[ep][depth])
                added = True
        if not added:
            break
        depth += 1
    chosen += picked
print("chosen: %d  %s" % (len(chosen), dict(collections.Counter(c["tier"] for c in chosen))))


def orig_stem(n):
    pg = s3.get_paginator("list_objects_v2")
    for page in pg.paginate(Bucket=B, Prefix="sepcache/"):
        for o in page.get("Contents", []):
            nm = o["Key"].split("/")[-1]
            if nm.upper().startswith("POA_EP%02d" % n) and nm.endswith(".voc16.npy"):
                p = "/tmp/e2_%d.npy" % n
                if not os.path.exists(p):
                    s3.download_file(B, o["Key"], p)
                return np.load(p)
    return None


def dub_file(n, tok):
    d = audio_jobs.find_dub_mix(box_discovery._Box(tok), cfg, "English", n)
    if not d:
        return None, None
    p = "/tmp/e2dub_%d.wav" % n
    if not os.path.exists(p):
        import httpx
        with httpx.stream("GET", "https://api.box.com/2.0/files/%s/content" % d["id"],
                          headers={"Authorization": "Bearer %s" % tok},
                          follow_redirects=True, timeout=900) as r:
            r.raise_for_status()
            with open(p, "wb") as fh:
                for c in r.iter_bytes(1 << 20):
                    fh.write(c)
    return p, d["name"]


def region(path, t0, t1):
    info = sf.info(path)
    sr = info.samplerate
    start = max(0, int(t0 * sr))
    frames = max(0, min(int((t1 - t0) * sr), info.frames - start))
    if frames <= 0:
        return None
    x, _ = sf.read(path, start=start, frames=frames, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != 16000:
        import scipy.signal as ss
        x = ss.resample_poly(x, 16000, sr).astype("float32")
    return x


def fit(x, n):
    out = np.zeros(n, dtype="float32")
    if x is not None:
        out[:min(len(x), n)] = x[:n]
    return out


tok = box_oauth.get_token(min_ttl_s=2700)
chosen.sort(key=lambda c: (c["ep"], c["at"]))
by_ep = collections.defaultdict(list)
for i, c in enumerate(chosen, start=START_AT):
    c["n"] = i
    by_ep[c["ep"]].append(c)

key, written = [], 0
for ep in sorted(by_ep):
    O = orig_stem(ep)
    dp, dname = dub_file(ep, tok)
    if O is None or not dp:
        print("  skip EP%02d (orig=%s dub=%s)" % (ep, O is not None, bool(dp)), flush=True)
        continue
    for c in by_ep[ep]:
        a, b = max(0.0, c["at"] - PAD), c["end"] + PAD
        want = int((b - a) * 16000)
        i0, i1 = int(a * 16000), int(b * 16000)
        base = "%02d_EP%02d_%02dm%02ds" % (c["n"], ep, int(c["at"] // 60), int(c["at"] % 60))
        sf.write(os.path.join(OUT, base + "_1-ORIGINAL-pt.flac"), fit(O[i0:i1], want), 16000)
        sf.write(os.path.join(OUT, base + "_2-DUB-en.flac"), fit(region(dp, a, b), want), 16000)
        key.append({"n": c["n"], "episode": "EP%02d" % ep, "at_s": round(c["at"], 2),
                    "tier": c["tier"], "text": c["text"], "fill": c["fill"],
                    "sung": c["sung"], "coverage": c["coverage"], "dub": dname})
        written += 1
        print("  %s  (line at 0:%04.1f)" % (base, PAD), flush=True)
    for f in ("/tmp/e2_%d.npy" % ep, dp):
        if f and os.path.exists(f):
            os.remove(f)

json.dump(key, open("/home/ubuntu/earpairs2_KEY.json", "w"), ensure_ascii=False, indent=1)
with open(os.path.join(OUT, "READ-ME.txt"), "w", encoding="utf-8") as fh:
    fh.write(
        "POA EAR CHECK (numbers %d-%d)\n"
        "==============================\n"
        "%d cases from 6 episodes never QC-ed before. Two files each, SAME number:\n\n"
        "    NN_EPxx_MMmSSs_1-ORIGINAL-pt.flac\n"
        "    NN_EPxx_MMmSSs_2-DUB-en.flac\n\n"
        "Import both into one Audacity window - same length, same start, so they stack.\n"
        "The line sits at 0:%04.1f in both. Do NOT normalise either track.\n\n"
        "  MISSING  - the original says the line, the English does not say it here\n"
        "  PRESENT  - the English does say it (loose translation / slightly early or late counts)\n"
        "  UNSURE   - genuinely cannot tell\n\n"
        "  Format:  \"1-missing, 2-present, ...\"\n\n"
        "WHAT IS DIFFERENT FROM BATCH 1\n"
        "  Anything already written off as the opening/closing THEME has been excluded, so you\n"
        "  are not spending time re-confirming that songs are songs - that cost 8 of the 16\n"
        "  clips last time. Mid-episode songs ARE still in, deliberately.\n\n"
        "  Five of these six episodes ship only a PRINT PREMIX, so the English side carries\n"
        "  music and effects rather than being a clean dialogue track. Silence is harder to\n"
        "  judge there: you are listening for the absence of a VOICE under the music. Those\n"
        "  episodes also produced almost no high-confidence flags, and finding out whether\n"
        "  that is correct is half the point of this batch.\n\n"
        "THE LINES\n" % (START_AT, START_AT + written - 1, written, PAD))
    for k in sorted(key, key=lambda x: x["n"]):
        fh.write("  %-3d %-6s %2d:%05.2f  %-42s [dub: %s]\n"
                 % (k["n"], k["episode"], int(k["at_s"] // 60), k["at_s"] % 60,
                    (k["text"] or "")[:42], (k["dub"] or "")[:30]))

print("\nPACK: %s  (%d pairs, numbers %d-%d)" % (OUT, written, START_AT, START_AT + written - 1))
