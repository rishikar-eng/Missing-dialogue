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


def launch(series_key: str, cfg: dict[str, Any], episode: int, lang: str,
           original_stage: str = "mix") -> dict[str, Any]:
    """Discover the pair in Box and start the Fargate audio-QC task. Returns
    {job_id, original, dub, eta_min} or {error}."""
    groq = os.environ.get("GROQ_API_KEY", "")
    if not groq:
        return {"error": "GROQ_API_KEY is not configured on the server"}
    c = fargate._cfg()
    if not (c["subnets"] and c["sg"] and c["bucket"]):
        return {"error": "Fargate compute is not configured (subnets/sg/bucket)"}
    token = box_oauth.get_token()
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
    return {"job_id": job_id, "original": orig["name"], "original_stage": stage_used,
            "dub": dub["name"], "eta_min": 8,
            "note": "audio QC started; poll get_audio_result with this job_id"}


def status(job_id: str) -> dict[str, Any]:
    """S3-first: a published report.json means done regardless of task/server lifecycle."""
    import json
    c = fargate._cfg()
    prefix = (_JOBS.get(job_id) or {}).get("prefix") or f"{c['prefix']}/audioqc/{job_id}"
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
    rec = _JOBS.get(job_id)
    if not rec:
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
