"""Audio-only (scriptless) QC as a Teams-triggered Fargate job.

Original full mix vs dub full mix — no script, no stems, background music baked in. The
heavy task runs the sonar image (Demucs separation → VAD-gated Groq whisper → LaBSE →
evidence-gated flags) and publishes an AudioQC workbook + report.json to S3. This module
discovers the two Box files from the series registry, launches the task with a short-lived
Box access token (the task never sees the rotating refresh token), and reads results back
for the agent. Results are read S3-first so status survives a server restart.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from . import box_discovery, box_oauth, fargate

_JOBS: dict[str, dict[str, Any]] = {}          # job_id -> {task_arn, series, episode, lang}


def _sq(s: str) -> str:
    return box_discovery._sq(s)


def find_dub_mix(box: box_discovery._Box, cfg: dict[str, Any], lang: str,
                 n: int) -> dict[str, Any] | None:
    """The delivered full mix for one dub language: mixes_folder/EP NN/*_<LANG>_ST_MIX.wav.
    Prefers the final ST_MIX; falls back to the language PREMIX if that's all there is."""
    root = cfg["box"].get("mixes_folder")
    if not root:
        return None
    sub = next((d for d in box.listing(root)["folders"]
                if box_discovery._ep_num(d["name"]) == n), None)
    if not sub:
        return None
    files = [f for f in box.listing(sub["id"])["files"]
             if f["name"].lower().endswith(".wav") and lang.upper() in f["name"].upper()]
    mixes = [f for f in files if "stmix" in _sq(f["name"])]
    return (mixes or [f for f in files if "premix" in _sq(f["name"])] or [None])[0]


def find_original_mix(box: box_discovery._Box, cfg: dict[str, Any],
                      n: int) -> dict[str, Any] | None:
    """The original's FINAL mix (…_HINDI_ST_MIX.wav), which is the like-for-like reference
    for a delivered dub ST_MIX. The premix is an earlier stage: different SFX and levels, and
    for some episodes a different length outright (EP38/EP40's mixes run ~16 s longer than
    their premixes) — comparing across stages manufactures drift and false flags."""
    folder = cfg["box"].get("original_mix_folder")
    if not folder:
        return None
    cands = [f for f in box.listing(folder)["files"]
             if f["name"].lower().endswith(".wav")
             and box_discovery.ep_of(f["name"]) == n
             and "premix" not in _sq(f["name"]) and "stmix" in _sq(f["name"])]
    cands.sort(key=lambda f: len(f["name"]))
    return cands[0] if cands else None


def availability(series_key: str, cfg: dict[str, Any], episode: int) -> dict[str, Any]:
    """What audio-only QC needs for this episode: the original's mix and, per language, the
    delivered full mix. The script path has `check` before `run`; this is its counterpart, so
    the studio can see what is checkable before starting an 8-minute job."""
    token = box_oauth.get_token()
    box = box_discovery._Box(token)
    n = int(episode)
    orig = find_original_mix(box, cfg, n)
    stage = "ST_MIX"
    if not orig:
        orig = box_discovery.find_original(box, cfg, n)
        stage = "ST_PREMIX"
    ready, missing = {}, []
    for lang in cfg.get("languages", []):
        d = find_dub_mix(box, cfg, lang, n)
        if d:
            ready[lang] = d["name"]
        else:
            missing.append(lang)
    return {"series": cfg.get("display_name", series_key), "series_key": series_key,
            "episode": n, "original": orig["name"] if orig else None,
            "original_stage": stage if orig else None,
            "languages_ready": ready, "not_delivered": missing,
            "runnable": bool(orig and ready)}


def launch(series_key: str, cfg: dict[str, Any], episode: int, lang: str,
           original_stage: str = "mix", conv: str | None = None) -> dict[str, Any]:
    """Discover the pair in Box and start the Fargate audio-QC task. Returns
    {job_id, original, dub, eta_min} or {error}."""
    groq = os.environ.get("GROQ_API_KEY", "")
    if not groq:
        return {"error": "GROQ_API_KEY is not configured on the server"}
    c = fargate._cfg()
    if not (c["subnets"] and c["sg"] and c["bucket"]):
        return {"error": "Fargate compute is not configured (subnets/sg/bucket)"}
    # force_refresh: get_token() can hand out a nearly-expired cached token, which dies
    # mid-download inside the task (the task cannot refresh — it never sees the refresh
    # token). A task launch always deserves a fresh one.
    token = box_oauth.get_token(force_refresh=True)
    box = box_discovery._Box(token)
    orig = (find_original_mix(box, cfg, int(episode)) if original_stage == "mix" else None)
    stage_used = "ST_MIX"
    if not orig:                                   # fall back to the premix stage
        orig = box_discovery.find_original(box, cfg, int(episode))
        stage_used = "ST_PREMIX"
    if not orig:
        return {"error": f"no original mix or premix found in Box for EP{int(episode):02d}"}
    dub = find_dub_mix(box, cfg, lang, int(episode))
    if not dub:
        return {"error": f"no {lang} full mix (ST_MIX) delivered for EP{int(episode):02d}"}

    job_id = uuid.uuid4().hex[:12]
    prefix = f"{c['prefix']}/audioqc/{job_id}"
    ov = {"containerOverrides": [{
        "name": "sonar",
        "command": ["audio_qc_run.py", f"box://{orig['id']}", f"box://{dub['id']}",
                    cfg.get("original_language", "hindi"), lang.lower(),
                    "--series", cfg.get("display_name", series_key),
                    "--episode", str(int(episode)), "--out", prefix],
        "environment": [{"name": "GROQ_API_KEY", "value": groq},
                        {"name": "BOX_ACCESS_TOKEN", "value": token},
                        {"name": "DQC_S3_BUCKET", "value": c["bucket"]}],
    }]}
    r = fargate._ecs().run_task(
        cluster=c["cluster"],
        taskDefinition=os.environ.get("DQC_AUDIO_TASKDEF", "dialogue-qc-sonar:3"),
        launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": c["subnets"], "securityGroups": [c["sg"]], "assignPublicIp": "ENABLED"}},
        overrides=ov)
    if r.get("failures"):
        return {"error": f"could not start the audio-QC task: {r['failures']}"}
    arn = r["tasks"][0]["taskArn"]
    _JOBS[job_id] = {"task_arn": arn, "series": series_key, "episode": int(episode),
                     "lang": lang, "prefix": prefix}
    # Register under the Teams conversation exactly like a script run, so `status` and `runs`
    # find it. Without this an audio check was invisible to every other command.
    if conv:
        try:
            from . import run_store
            run_store.record(job_id, conv=conv, episode=int(episode),
                             series=cfg.get("display_name", series_key), status="running",
                             compute="audio", lang=lang, prefix=prefix, task_arn=arn)
        except Exception:  # noqa: BLE001 — bookkeeping must never break a launch
            pass
    return {"job_id": job_id, "original": orig["name"], "original_stage": stage_used,
            "dub": dub["name"], "eta_min": 8,
            "note": "audio QC started; poll get_audio_result with this job_id"}


def launch_all(series_key: str, cfg: dict[str, Any], episode: int,
               conv: str | None = None) -> dict[str, Any]:
    """Audio-QC every delivered language for an episode, one Fargate task each — the audio
    counterpart of fargate.launch_parallel, so `audio check ep 42` behaves like `run ep 42`.

    Returns {parent_id, langs: {lang: {...}}, launched, errors}. A language whose task fails to
    start (vCPU quota is the usual reason) is recorded with its error rather than dropped, so
    status can report it instead of silently showing fewer languages than were asked for.
    """
    parent_id = uuid.uuid4().hex[:12]
    langs: dict[str, dict[str, Any]] = {}
    for lang in cfg.get("languages", []):
        r = launch(series_key, cfg, int(episode), lang)      # no conv: parent owns the record
        if r.get("error"):
            if "no " + lang.lower() in r["error"].lower():
                langs[lang] = {"skipped": r["error"]}        # not delivered — expected
            else:
                langs[lang] = {"error": r["error"]}
        else:
            langs[lang] = {"job_id": r["job_id"], "original": r.get("original"),
                           "dub": r.get("dub"), "original_stage": r.get("original_stage")}
    launched = sum(1 for v in langs.values() if v.get("job_id"))
    if conv:
        try:
            from . import run_store
            run_store.record(parent_id, conv=conv, episode=int(episode),
                             series=cfg.get("display_name", series_key), status="running",
                             compute="audio-parallel", langs=langs)
        except Exception:  # noqa: BLE001
            pass
    return {"parent_id": parent_id, "langs": langs, "launched": launched,
            "errors": {k: v["error"] for k, v in langs.items() if v.get("error")}}


def status(job_id: str) -> dict[str, Any]:
    """S3-first: a published report.json means done regardless of task/server lifecycle."""
    import json
    c = fargate._cfg()
    rec = _JOBS.get(job_id) or {}
    if not rec:                                    # in-memory map is lost on restart
        try:
            from . import run_store
            rec = run_store.get(job_id) or {}
        except Exception:  # noqa: BLE001
            rec = {}
    prefix = rec.get("prefix") or f"{c['prefix']}/audioqc/{job_id}"
    s3 = fargate._s3()
    try:
        rep = json.loads(s3.get_object(Bucket=c["bucket"],
                                       Key=f"{prefix}/report.json")["Body"].read())
        s = rep.get("summary", {})
        out: dict[str, Any] = {
            "status": "done",
            "summary": {
                "missing": s.get("n_missing"),
                "missing_by_confidence": s.get("n_missing_by_confidence"),
                "extra": s.get("n_extra"), "misaligned": s.get("n_misaligned"),
                "original_lines": s.get("n_original_regions"),
                "coverage": s.get("coverage"), "unchecked": s.get("n_unchecked"),
            },
        }
        for obj in s3.list_objects_v2(Bucket=c["bucket"], Prefix=prefix).get("Contents", []):
            if obj["Key"].endswith(".xlsx"):
                out["download_url"] = fargate.download_url(obj["Key"])
                break
        return out
    except Exception:
        pass                                        # not published yet — fall through to ECS
    if not rec.get("task_arn"):
        return {"error": "unknown or expired job_id"}
    try:
        t = fargate._ecs().describe_tasks(cluster=c["cluster"],
                                          tasks=[rec["task_arn"]])["tasks"][0]
        st = t["lastStatus"]
        if st == "STOPPED":
            return {"status": "error",
                    "error": t.get("stoppedReason", "task stopped without publishing a report")}
        return {"status": "running", "stage": st.lower(),
                "note": "separation + transcription take ~8 min for a full episode"}
    except Exception as e:  # noqa: BLE001
        return {"status": "running", "note": f"task status unavailable ({e})"}
