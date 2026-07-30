"""Fargate entry for audio-only QC runs + diagnostics.

Usage: audio_qc_run.py ORIGINAL DUB ORIG_LANG DUB_LANG [options]

ORIGINAL / DUB may be local paths (baked test clips) or s3://bucket/key — s3 inputs are
downloaded to /tmp first, which is how full episodes reach the task (Box → EC2 → S3 → here).

--clean-dub       the dub input is already dialogue-only (summed stems): skip separation.
--make-dub-mix    construct a realistic dub mix: original accompaniment + clean dub dialogue.
--dump KEY        upload the embedding dump for offline calibration (needs AQC_DUMP=1).
--series NAME     series display name for the workbook + file naming.
--episode N       episode number for the workbook + file naming.
--out S3PREFIX    upload report.json + the AudioQC workbook to s3://<bucket>/<S3PREFIX>/.
"""
import json
import os
import sys
import time

import numpy as np
import soundfile as sf

from backend import audio_qc

BUCKET = os.environ.get("DQC_S3_BUCKET", "dialogue-qc-output-848005667477")


# Demucs separation is the wall-time cost, and one original serves every dub language of the
# episode — so separations computed here are pushed to S3 keyed by source ETag, and later
# runs of the same file skip Demucs for it entirely.
_SEP_CACHE: dict[str, tuple[str, str, list[str]]] = {}   # local -> (bucket, base, hit sufs)


def _cache_attach(dst: str, base: str) -> None:
    """Pull any cached separation for `base` next to `dst` and remember what to push back."""
    import boto3
    s3 = boto3.client("s3")
    hits = []
    for suf in (".voc16.npy", ".acc16.npy"):
        try:
            s3.download_file(BUCKET, base + suf, dst + suf)
            hits.append(suf)
            print(f"[run] separation cache HIT {base + suf}", flush=True)
        except Exception:
            pass
    _SEP_CACHE[dst] = (BUCKET, base, hits)


def _local(p: str) -> str:
    if p.startswith("box://"):
        # Teams-triggered runs pass Box file ids + a short-lived access token in the env.
        import httpx

        from backend import box_fetch
        fid = p[6:]
        tok = os.environ["BOX_ACCESS_TOKEN"]
        info = httpx.get(f"https://api.box.com/2.0/files/{fid}?fields=name,sha1",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=60).json()
        name = info.get("name") or f"box_{fid}.wav"
        print(f"[run] downloading box:{fid} ({name})", flush=True)
        dst = str(box_fetch.download_file(tok, fid, "/tmp", name=name))
        try:
            _cache_attach(dst, f"sepcache/{name}.{info.get('sha1') or fid}")
        except Exception as e:  # cache is an optimization, never a blocker
            print(f"[run] separation cache unavailable: {e}", flush=True)
        return dst
    if not p.startswith("s3://"):
        return p
    import boto3
    s3 = boto3.client("s3")
    bucket, key = p[5:].split("/", 1)
    dst = "/tmp/" + os.path.basename(key)
    print(f"[run] downloading {p}", flush=True)
    s3.download_file(bucket, key, dst)
    try:
        etag = s3.head_object(Bucket=bucket, Key=key)["ETag"].strip('"').replace("-", "_")
        _cache_attach(dst, f"sepcache/{os.path.basename(key)}.{etag}")
    except Exception as e:  # cache is an optimization, never a blocker
        print(f"[run] separation cache unavailable: {e}", flush=True)
    return dst


def _push_sep_cache() -> None:
    import boto3
    s3 = boto3.client("s3")
    for local, (bucket, base, hits) in _SEP_CACHE.items():
        for suf in (".voc16.npy", ".acc16.npy"):
            if suf not in hits and os.path.exists(local + suf):
                try:
                    s3.upload_file(local + suf, bucket, base + suf)
                    print(f"[run] separation cached -> {base + suf}", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[run] cache upload failed: {e}", flush=True)


args = sys.argv[1:]


def _opt(name: str) -> str | None:
    if name in args:
        v = args[args.index(name) + 1]
        args.remove(name)
        args.remove(v)
        return v
    return None


dump_key = _opt("--dump")
series = _opt("--series") or ""
episode = _opt("--episode") or ""
out_prefix = _opt("--out")
clean = "--clean-dub" in args
mkmix = "--make-dub-mix" in args
args = [a for a in args if not a.startswith("--")]
o, d, ol, dl = args[:4]
o, d = _local(o), _local(d)

if mkmix:
    print("[run] constructing dub mix = original accompaniment + clean dub dialogue", flush=True)
    acc = audio_qc._separate(o)[1]
    tam = audio_qc._load16(d)
    n = min(len(acc), len(tam))
    mix = acc[:n] + tam[:n]
    peak = float(np.abs(mix).max())
    if peak > 0.95:
        mix = mix / peak * 0.9
    sf.write("/tmp/dubmix.wav", mix.astype("float32"), 16000)
    d, clean = "/tmp/dubmix.wav", False

t = time.time()
rep = audio_qc.compare(o, d, original_lang=ol, dub_lang=dl, dub_label="dub", dub_is_clean=clean)
s = rep["summary"]
conf = s.get("n_missing_by_confidence", {}) or {}
print(f"ELAPSED {time.time()-t:.0f}s")
print(f"RESULT missing={s['n_missing']} (high={conf.get('high', 0)} med={conf.get('medium', 0)} "
      f"low={conf.get('low', 0)}) misaligned={s['n_misaligned']} "
      f"extra={s['n_extra']} of {s['n_original_regions']} original windows")
for e in rep["errors"]:
    if e["type"] == "MISSING":
        print(f"   MISSING    @{e['script_start_s']:7.1f}-{e['script_end_s']:7.1f}s "
              f"best={e['coverage']:.2f} conf={e.get('confidence', '?')}")
for e in rep["errors"]:
    if e["type"] == "MISALIGNED":
        print(f"   MISALIGNED @{e['script_start_s']:7.1f}s drift={e['drift_s']:+.1f}s match={e['coverage']:.2f}")
json.dump(rep, open("/tmp/report.json", "w"), default=str)
_push_sep_cache()

if out_prefix:
    from backend import audio_report, naming
    fname = naming.audio_qc_xlsx(series or None, episode or 0, dl)
    audio_report.build_audio_workbook(
        rep,
        meta={"series": series, "episode": f"EP{int(episode):02d}" if episode.isdigit() else episode,
              "original": f"{os.path.basename(o)} ({ol})", "dub": f"{os.path.basename(d)} ({dl})",
              "generated_at": time.strftime("%Y-%m-%d %H:%M")},
        out_path=f"/tmp/{fname}")
    import boto3
    s3 = boto3.client("s3")
    pre = out_prefix.strip("/")
    s3.upload_file(f"/tmp/{fname}", BUCKET, f"{pre}/{fname}")
    s3.upload_file("/tmp/report.json", BUCKET, f"{pre}/report.json")
    print(f"WORKBOOK s3://{BUCKET}/{pre}/{fname}", flush=True)

if dump_key:
    print("[run] embedding probe windows (silence + random) for calibration", flush=True)
    dv = audio_qc._load16(d) if clean else audio_qc.separate_dialogue(d)
    dur = len(dv) / 16000.0
    sil = np.zeros(int(1.2 * 16000), dtype="float32")
    dv_probe = np.concatenate([dv, sil])
    probes = [(dur + 0.05, dur + 1.15)]                       # one pure-silence window
    rng = np.random.RandomState(7)
    probes += [(t0, t0 + 1.2) for t0 in sorted(rng.uniform(0, dur - 1.3, 6))]
    lang3 = audio_qc._lang3(dl)
    P = audio_qc.embed(dv_probe, probes, lang3).numpy()
    base = np.load("/tmp/aqc_dump.npz")
    np.savez("/tmp/aqc_full.npz", P=P, probes=np.array(probes, dtype="float32"),
             **{k: base[k] for k in base.files})
    import boto3
    boto3.client("s3").upload_file("/tmp/aqc_full.npz", BUCKET, dump_key)
    print(f"[run] dump uploaded to s3://{BUCKET}/{dump_key}")
