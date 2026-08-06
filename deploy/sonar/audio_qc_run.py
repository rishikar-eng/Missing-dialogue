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


# Cached artefacts, keyed by SOURCE file (sha1/etag): the two separation stems and the
# Sarvam reading. The reading is per-FILE and language-independent, so caching it stops a
# 6-language fan-out from paying Sarvam six times for the same original — the single
# biggest source of avoidable spend (see docs/scriptless-qc-cost.md).
_CACHE_SUF = (".voc16.npy", ".acc16.npy", ".sarvam.json", ".scribe.json")


def _cache_attach(dst: str, base: str) -> None:
    """Pull any cached artefacts for `base` next to `dst` and remember what to push back."""
    import boto3
    s3 = boto3.client("s3")
    hits = []
    for suf in _CACHE_SUF:
        try:
            s3.download_file(BUCKET, base + suf, dst + suf)
            hits.append(suf)
            print(f"[run] cache HIT {base + suf}", flush=True)
        except Exception:
            pass
    _SEP_CACHE[dst] = (BUCKET, base, hits)


_VIDEO_EXT = (".mp4", ".mov", ".mkv", ".m4v")


def _extract_audio(src: str, out: str) -> str:
    """Video original (POA ships only episode videos) -> mono wav via ffmpeg. The wav, not
    the video, is what the pipeline (and the separation cache) sees."""
    import subprocess
    if not os.path.exists(out):
        print(f"[run] extracting audio track from {os.path.basename(src)}", flush=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                        "-vn", "-ac", "1", "-ar", "44100", out], check=True)
    return out


def _local(p: str) -> str:
    if p.startswith("box://"):
        # Teams-triggered runs pass Box file ids + a short-lived access token in the env.
        import httpx

        from backend import box_fetch
        fid = p[6:]
        tok = os.environ["BOX_ACCESS_TOKEN"]
        _r = httpx.get(f"https://api.box.com/2.0/files/{fid}?fields=name,sha1",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        if _r.status_code != 200:
            # Box rotates access tokens: launching two tasks back-to-back can invalidate the
            # first one's token, and the old code crashed on the non-JSON 401 body with a
            # baffling JSONDecodeError instead of saying what actually happened.
            raise RuntimeError(f"Box file-info for {fid} failed: HTTP {_r.status_code} "
                               f"(token expired/rotated?)")
        info = _r.json()
        name = info.get("name") or f"box_{fid}.wav"
        dst = "/tmp/" + name
        # A video original is represented by its EXTRACTED wav everywhere downstream —
        # including the separation cache key, so a cache hit skips the video download too.
        video = name.lower().endswith(_VIDEO_EXT)
        target = dst + ".wav" if video else dst
        try:
            # Cache FIRST: when both separation stems are cached, the source is never
            # read (Demucs loads the .npy pair), so skipping its download is free.
            _cache_attach(target, f"sepcache/{name}.{info.get('sha1') or fid}")
            _hit = _SEP_CACHE.get(target, (None, None, []))[2]
            if ".voc16.npy" in _hit and ".acc16.npy" in _hit:
                print(f"[run] both stems cached — skipping download of {name}", flush=True)
                return target
        except Exception as e:  # cache is an optimization, never a blocker
            print(f"[run] separation cache unavailable: {e}", flush=True)
        print(f"[run] downloading box:{fid} ({name})", flush=True)
        got = str(box_fetch.download_file(tok, fid, "/tmp", name=name))
        return _extract_audio(got, target) if video else got
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
        for suf in _CACHE_SUF:
            if suf not in hits and os.path.exists(local + suf):
                try:
                    s3.upload_file(local + suf, bucket, base + suf)
                    print(f"[run] cached -> {base + suf}", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[run] cache upload failed: {e}", flush=True)


def _main():
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
    warm_only = "--warm-only" in args
    t0 = time.time()
    args = [a for a in args if not a.startswith("--")]
    o, d, ol, dl = args[:4]
    # the two ~450MB inputs download CONCURRENTLY — pure network wait
    import concurrent.futures as _cfdl
    if warm_only:
        o = _local(o)
    else:
        with _cfdl.ThreadPoolExecutor(max_workers=2) as _dl:
            _fo, _fd = _dl.submit(_local, o), _dl.submit(_local, d)
            o, d = _fo.result(), _fd.result()

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

    # Live progress for the Teams bar: known stage messages map to percentages and land in
    # S3 as <prefix>/progress.json. Monotonic (a bar must never move backwards) and
    # best-effort — progress must never break a run. Old images simply never write it.
    _prog = {"pct": 0}
    _STAGE_PCT = [
        ("separating original and dub concurrently", 15, "separating audio (both sides)"),
        ("separating dialogue from the original mix", 20, "separating the original"),
        ("separating dialogue from the dub mix", 42, "separating the dub"),
        ("SARVAM-ONLY", 55, "transcribing (Sarvam batch)"),
        ("SCRIBE-ONLY", 55, "transcribing (Scribe)"),
        ("transcribing original + dub via Groq", 55, "transcribing"),
        ("lines: original=", 75, "matching lines across languages"),
        ("second pass", 85, "verifying candidate flags"),
        ("coverage:", 92, "finalizing the report"),
    ]

    def _progress(pct, label):
        if not out_prefix or pct <= _prog["pct"]:
            return
        _prog["pct"] = pct
        try:
            import boto3
            boto3.client("s3").put_object(
                Bucket=BUCKET, Key=f"{out_prefix.strip('/')}/progress.json",
                Body=json.dumps({"pct": pct, "stage": label,
                                 "updated_at": time.time()}).encode())
            print(f"[run] progress {pct}% — {label}", flush=True)
        except Exception:  # noqa: BLE001
            pass

    def _stage_cb(m, _a, _b):
        for prefix, pct, label in _STAGE_PCT:
            if m.startswith(prefix):
                _progress(pct, label)
                return

    _progress(8, "inputs downloaded")

    if warm_only:
        # Pre-warm for a fan-out: do the per-EPISODE work (separate the original, read it
        # once with Sarvam) so the N language tasks all start from cache. Without this each
        # language re-separates AND re-transcribes the same original — 6x the Sarvam spend.
        print("[run] WARM-ONLY: preparing the original for every language", flush=True)
        _progress(20, "separating the original")
        ov = audio_qc.separate_dialogue(o)
        if os.environ.get("AQC_SCRIBE_ONLY", "").strip() == "1":
            _progress(55, "transcribing the original (shared)")
            segs = audio_qc.transcribe_scribe(ov, o + ".scribe.json",
                                              audio_qc.LANG1.get(ol.lower()))
            print(f"[run] warm: original transcript = {len(segs or [])} lines", flush=True)
        elif (os.environ.get("AQC_SARVAM_ONLY", "").strip() == "1"
                and ol.lower() in ("hindi", "tamil", "telugu", "kannada", "bengali",
                                   "marathi", "malayalam", "punjabi")):
            _progress(55, "transcribing the original (shared)")
            segs = audio_qc.transcribe_sarvam(ov, o + ".sarvam.json")
            print(f"[run] warm: original transcript = {len(segs or [])} lines", flush=True)
        _push_sep_cache()
        _progress(100, "original ready — languages starting")
        print(f"[run] WARM done in {time.time() - t0:.0f}s", flush=True)
        return

    t = time.time()
    # AQC_DRAWS>1: run detection N times and UNION the MISSING flags. Groq's transcription is
    # nondeterministic and every gate downstream inherits that lottery — measured on the ear-truth
    # window: single draws score 2-5/6 recall, the union of 5 draws scores 6/6 (the only full-recall
    # configuration ever observed). Separation is cached after draw 1, so extra draws cost only the
    # transcribe+detect stage. Each union flag reports its stability (seen in k/N draws) — a free,
    # honest confidence signal. Default (unset/1) = exactly the old single-draw behaviour.
    N = max(1, int(os.environ.get("AQC_DRAWS", "1")))
    reps = []
    # Draw 1 runs alone: it warms the Demucs caches so later draws can never race the separator.
    print(f"[run] draw 1/{N}", flush=True)
    reps.append(audio_qc.compare(o, d, original_lang=ol, dub_lang=dl, dub_label="dub",
                                 dub_is_clean=clean, stage=_stage_cb))
    if N > 1:
        # Draws 2..N run CONCURRENTLY in spawned processes: they are pure network wait
        # (separation cache-hits from disk; transcription is API calls), so the union costs
        # roughly one draw's wall-clock instead of N. Spawn, not fork — draw 1 leaves torch
        # state and finished threads behind, and forking across OpenMP locks can deadlock.
        # A failed draw is dropped with a warning: a union of N-1 beats a dead task.
        import multiprocessing as _mp
        ctx = _mp.get_context("spawn")
        import concurrent.futures as _cf
        with _cf.ProcessPoolExecutor(max_workers=min(N - 1, 4), mp_context=ctx) as _ex:
            _model = ("whisper-large-v3-turbo"
                      if os.environ.get("AQC_TURBO", "").strip() == "1" else None)
            _futs = [_ex.submit(audio_qc._spawn_draw, o, d, ol, dl, clean, _model)
                     for _ in range(N - 1)]
            for _f in _cf.as_completed(_futs):
                try:
                    reps.append(_f.result())
                except Exception as _e:  # noqa: BLE001
                    print(f"[run] concurrent draw failed ({_e}); union proceeds with fewer",
                          flush=True)
        print(f"[run] {len(reps)}/{N} draws completed", flush=True)
    rep = max(reps, key=lambda r: r["summary"].get("coverage") or 0)
    if N > 1:
        TOL = 2.5
        def _ov(a, b):
            return not (a["script_end_s"] + TOL < b["script_start_s"]
                        or b["script_end_s"] + TOL < a["script_start_s"])
        clusters = []                     # [(representative_flag, seen_count)]
        for r in reps:
            for e in r["errors"]:
                if e["type"] != "MISSING":
                    continue
                for k, (c0, n0) in enumerate(clusters):
                    if _ov(c0, e):
                        order = {"high": 2, "medium": 1, "low": 0}
                        best = e if order.get(e.get("confidence"), 0) > order.get(c0.get("confidence"), 0) else c0
                        clusters[k] = (best, n0 + 1)
                        break
                else:
                    clusters.append((dict(e), 1))
        union = []
        for c0, n0 in clusters:
            c0["stability"] = f"{min(n0, N)}/{N}"
            c0["message"] = (c0.get("message") or "") + f" [seen in {min(n0, N)}/{N} independent reads]"
            union.append(c0)
        union.sort(key=lambda e: e["script_start_s"])
        rep["errors"] = union + [e for e in rep["errors"] if e["type"] != "MISSING"]
        n_conf = {}
        for c in ("high", "medium", "low"):
            n_conf[c] = sum(1 for e in union if e.get("confidence") == c)
        rep["summary"]["n_missing"] = len(union)
        rep["summary"]["n_missing_by_confidence"] = n_conf
        rep["summary"]["n_draws"] = N
        print(f"[run] union of {N} draws: {len(union)} missing "
              f"(per-draw: {[r['summary']['n_missing'] for r in reps]})", flush=True)
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
        s3.upload_file("/tmp/report.json", BUCKET, f"{pre}/report.json")
        s3.upload_file(f"/tmp/{fname}", BUCKET, f"{pre}/{fname}")
        print(f"WORKBOOK s3://{BUCKET}/{pre}/{fname}", flush=True)
        try:
            mname = fname.rsplit(".", 1)[0] + "_ProTools-Markers.mid"
            audio_report.build_marker_midi(rep, f"/tmp/{mname}")
            s3.upload_file(f"/tmp/{mname}", BUCKET, f"{pre}/{mname}")
            print(f"MARKERS s3://{BUCKET}/{pre}/{mname}", flush=True)
        except Exception as _me:  # noqa: BLE001 — markers are extra, never fatal
            print(f"[run] marker export failed: {_me}", flush=True)
        try:
            # Missing-lines reference audio, same deliverable script QC ships: the original's
            # audio for every flag — stitched, and on-timeline (silent between flags).
            rname = naming.missing_flac(series or None, episode or 0, dl, False)
            tname = naming.missing_flac(series or None, episode or 0, dl, True)
            if audio_report.build_ref_audio(rep.get("errors", []), o,
                                            f"/tmp/{rname}", f"/tmp/{tname}"):
                s3.upload_file(f"/tmp/{rname}", BUCKET, f"{pre}/{rname}")
                s3.upload_file(f"/tmp/{tname}", BUCKET, f"{pre}/{tname}")
                print(f"REFAUDIO s3://{BUCKET}/{pre}/{rname}", flush=True)
        except Exception as _re:  # noqa: BLE001 — ref audio is extra, never fatal
            print(f"[run] ref-audio export failed: {_re}", flush=True)
    _push_sep_cache()                      # cache push LAST: results first, always

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



if __name__ == "__main__":
    _main()
