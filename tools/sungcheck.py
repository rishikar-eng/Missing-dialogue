"""Safety check on the sung detector, now that it suppresses ~21 lines per episode.

It earned the wider remit on 35 clips, but at that point it was removing half a line per
episode. It is now the single biggest filter in the pipeline, so the question worth asking is
narrow and specific: of the lines it takes OFF the timeline, is any of them spoken dialogue?

CONTROLS ARE MIXED IN on purpose. A pack made only of suppressed lines can be answered
"singing" throughout without listening, and would confirm itself. Roughly a quarter of these
are ordinary flags the detector did NOT suppress, so the answers have to discriminate.

One file per case, both sides at the same timecode, matched gain — same format as before.
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
OUT = "/home/ubuntu/sungcheck"
PAD = 4.0
N_SUNG, N_CONTROL = 16, 6
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


EPS = [4, 5, 8, 11, 13, 15, 17, 19, 21, 25, 27]
sung_pool, ctrl_pool = [], []
for ep in EPS:
    k = newest(ep)
    if not k:
        continue
    rep = json.loads(s3.get_object(Bucket=B, Key=k)["Body"].read())
    for e in rep["errors"]:
        if e["type"] != "MISSING" or e.get("script_start_s") is None:
            continue
        row = {"ep": ep, "at": float(e["script_start_s"]),
               "end": float(e.get("script_end_s") or e["script_start_s"] + 1.0),
               "text": e.get("text") or "", "tier": e.get("confidence"),
               "sung": bool(e.get("sung")), "margin": e.get("sung_margin_db"),
               "fill": e.get("fill")}
        (sung_pool if row["sung"] else ctrl_pool).append(row)

print("pool: %d suppressed (sung), %d not suppressed" % (len(sung_pool), len(ctrl_pool)))


def spread(pool, k):
    """Round-robin across episodes so no single episode dominates."""
    by = collections.defaultdict(list)
    for r in pool:
        by[r["ep"]].append(r)
    rng = random.Random(20260811)
    for v in by.values():
        rng.shuffle(v)
    out, depth = [], 0
    while len(out) < k:
        added = False
        for ep in sorted(by):
            if depth < len(by[ep]) and len(out) < k:
                out.append(by[ep][depth])
                added = True
        if not added:
            break
        depth += 1
    return out


chosen = spread(sung_pool, N_SUNG) + spread(ctrl_pool, N_CONTROL)
random.Random(7).shuffle(chosen)
for i, c in enumerate(chosen, start=1):
    c["n"] = i
print("chosen: %d suppressed + %d controls"
      % (sum(1 for c in chosen if c["sung"]), sum(1 for c in chosen if not c["sung"])))

_oc = {}


def orig(ep):
    if ep in _oc:
        return _oc[ep]
    pg = s3.get_paginator("list_objects_v2")
    _oc[ep] = None
    for page in pg.paginate(Bucket=B, Prefix="sepcache/"):
        for o in page.get("Contents", []):
            nm = o["Key"].split("/")[-1]
            if nm.upper().startswith("POA_EP%02d" % ep) and nm.endswith(".voc16.npy"):
                p = "/tmp/sc_o%d.npy" % ep
                if not os.path.exists(p):
                    s3.download_file(B, o["Key"], p)
                _oc[ep] = np.load(p)
    return _oc[ep]


def dub(ep, tok):
    """A dub file, or the cached speaker-sum for episodes delivered as per-character tracks."""
    d = audio_jobs.find_dub_mix(box_discovery._Box(tok), cfg, "English", ep)
    if d:
        p = "/tmp/sc_d%d.wav" % ep
        if not os.path.exists(p):
            import httpx
            with httpx.stream("GET", "https://api.box.com/2.0/files/%s/content" % d["id"],
                              headers={"Authorization": "Bearer %s" % tok},
                              follow_redirects=True, timeout=900) as r:
                r.raise_for_status()
                with open(p, "wb") as fh:
                    for c in r.iter_bytes(1 << 20):
                        fh.write(c)
        return p
    sp = audio_jobs.find_dub_speakers(box_discovery._Box(tok), cfg, "English", ep)
    if not sp:
        return None
    for o in s3.list_objects_v2(Bucket=B, Prefix="spksum/%s." % sp["id"]).get("Contents", []):
        p = "/tmp/sc_d%d.wav" % ep
        if not os.path.exists(p):
            s3.download_file(B, o["Key"], p)
        return p
    return None


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
by_ep = collections.defaultdict(list)
for c in chosen:
    by_ep[c["ep"]].append(c)

key = []
for ep in sorted(by_ep):
    O, dp = orig(ep), dub(ep, tok)
    if O is None or not dp:
        print("  skip EP%02d (orig=%s dub=%s)" % (ep, O is not None, bool(dp)), flush=True)
        continue
    for c in by_ep[ep]:
        a, b = max(0.0, c["at"] - PAD), c["end"] + PAD
        m = int((b - a) * 16000)
        i0, i1 = int(a * 16000), int(b * 16000)
        base = "%02d_EP%02d_%02dm%02ds" % (c["n"], ep, int(c["at"] // 60), int(c["at"] % 60))
        sf.write(os.path.join(OUT, base + "_1-ORIGINAL-pt.flac"), fit(O[i0:i1], m), 16000)
        sf.write(os.path.join(OUT, base + "_2-DUB-en.flac"), fit(region(dp, a, b), m), 16000)
        key.append({"n": c["n"], "episode": "EP%02d" % ep, "at_s": round(c["at"], 2),
                    "suppressed_as_sung": c["sung"], "margin_db": c["margin"],
                    "tier": c["tier"], "fill": c["fill"], "text": c["text"]})
        print("  %s  %s" % (base, "SUPPRESSED" if c["sung"] else "control"), flush=True)
    for f in ("/tmp/sc_o%d.npy" % ep, "/tmp/sc_d%d.wav" % ep):
        if os.path.exists(f):
            os.remove(f)
    _oc.pop(ep, None)

json.dump(key, open("/home/ubuntu/sungcheck_KEY.json", "w"), ensure_ascii=False, indent=1)
print("\nPACK: %s  (%d cases)" % (OUT, len(key)))
