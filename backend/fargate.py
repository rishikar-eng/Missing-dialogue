"""Dispatch heavy QC runs to AWS Fargate (ECS RunTask) instead of running them in-process.

The always-on EC2 stays the brain: it mints a Box access token, launches a one-shot Fargate
task (`backend/job_entry.py`) that does the heavy compute and writes the result to S3, then
reads status/downloads back from S3. Enabled only when DQC_COMPUTE=fargate AND the ECS/S3
config is present; otherwise the caller falls back to the in-process jobs runner.

Env:
  DQC_COMPUTE=fargate            turn this path on
  DQC_ECS_CLUSTER               (default "dialogue-qc")
  DQC_ECS_TASKDEF               (default "dialogue-qc-job")
  DQC_ECS_SUBNETS               comma-separated subnet ids (public)
  DQC_ECS_SG                    security group id
  DQC_S3_BUCKET / DQC_S3_PREFIX output location (prefix default "output")
  AWS_REGION                    (default "ap-south-1")
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

_REGION = os.environ.get("AWS_REGION", "ap-south-1")


def _cfg() -> dict[str, str]:
    return {
        "cluster": os.environ.get("DQC_ECS_CLUSTER", "dialogue-qc"),
        "taskdef": os.environ.get("DQC_ECS_TASKDEF", "dialogue-qc-job"),
        "subnets": [s for s in os.environ.get("DQC_ECS_SUBNETS", "").split(",") if s],
        "sg": os.environ.get("DQC_ECS_SG", ""),
        "bucket": os.environ.get("DQC_S3_BUCKET", ""),
        "prefix": os.environ.get("DQC_S3_PREFIX", "output").strip("/"),
    }


def enabled() -> bool:
    c = _cfg()
    return (os.environ.get("DQC_COMPUTE") == "fargate"
            and bool(c["subnets"]) and bool(c["sg"]) and bool(c["bucket"]))


def _ecs():
    import boto3
    return boto3.client("ecs", region_name=_REGION)


def _s3():
    import boto3
    from botocore.config import Config
    # Force the REGIONAL virtual-hosted endpoint (bucket.s3.<region>.amazonaws.com) so a
    # presigned URL serves directly. The default global host (bucket.s3.amazonaws.com) 307s
    # to the region for a non-us-east-1 bucket, and the redirect breaks the host-signed URL.
    return boto3.client("s3", region_name=_REGION,
                        endpoint_url=f"https://s3.{_REGION}.amazonaws.com",
                        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))


def launch(series_key: str, episode: int, languages: list[str] | None = None) -> tuple[str, str]:
    """Start a Fargate QC task. Returns (job_id, task_arn). Raises on failure to launch."""
    from . import box_oauth
    c = _cfg()
    job_id = uuid.uuid4().hex[:12]
    env = [
        {"name": "DQC_JOB_SERIES", "value": series_key},
        {"name": "DQC_JOB_EPISODE", "value": str(int(episode))},
        {"name": "DQC_JOB_ID", "value": job_id},
        # a short-lived access token so the task never touches the rotating refresh token
        {"name": "BOX_ACCESS_TOKEN", "value": box_oauth.get_token()},
    ]
    if languages:
        env.append({"name": "DQC_JOB_LANGUAGES", "value": ",".join(languages)})
    resp = _ecs().run_task(
        cluster=c["cluster"], taskDefinition=c["taskdef"], launchType="FARGATE", count=1,
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": c["subnets"], "securityGroups": [c["sg"]], "assignPublicIp": "ENABLED"}},
        overrides={"containerOverrides": [{"name": "qc", "environment": env}]},
    )
    tasks = resp.get("tasks") or []
    if not tasks:
        raise RuntimeError(str(resp.get("failures") or "run_task returned no task"))
    return job_id, tasks[0]["taskArn"]


def launch_parallel(series_key: str, episode: int,
                    languages: list[str]) -> tuple[str, dict[str, dict[str, Any]]]:
    """Fan out ONE Fargate task per language (2-vCPU task def) so an episode's languages run
    concurrently — wall-clock becomes the slowest single language, not their sum. Returns
    (parent_id, {lang: {job_id, task_arn, error}}). run_task calls are issued in parallel so
    the dispatch itself stays within the Teams reply window."""
    from concurrent.futures import ThreadPoolExecutor

    from . import box_oauth
    c = _cfg()
    parent = uuid.uuid4().hex[:12]
    token = box_oauth.get_token()                       # one token shared by all tasks (read-only)
    taskdef = os.environ.get("DQC_ECS_TASKDEF_LANG", "dialogue-qc-lang")
    ecs = _ecs()

    def _one(lang: str) -> tuple[str, dict[str, Any]]:
        job_id = f"{parent}_{lang}"
        env = [
            {"name": "DQC_JOB_SERIES", "value": series_key},
            {"name": "DQC_JOB_EPISODE", "value": str(int(episode))},
            {"name": "DQC_JOB_ID", "value": job_id},
            {"name": "DQC_JOB_LANGUAGES", "value": lang},
            {"name": "BOX_ACCESS_TOKEN", "value": token},
        ]
        try:
            resp = ecs.run_task(
                cluster=c["cluster"], taskDefinition=taskdef, launchType="FARGATE", count=1,
                networkConfiguration={"awsvpcConfiguration": {
                    "subnets": c["subnets"], "securityGroups": [c["sg"]], "assignPublicIp": "ENABLED"}},
                overrides={"containerOverrides": [{"name": "qc", "environment": env}]})
            tasks = resp.get("tasks") or []
            arn = tasks[0]["taskArn"] if tasks else None
            return lang, {"job_id": job_id, "task_arn": arn,
                          "error": None if arn else str(resp.get("failures") or "no task")}
        except Exception as e:  # noqa: BLE001
            return lang, {"job_id": job_id, "task_arn": None, "error": str(e)[:120]}

    with ThreadPoolExecutor(max_workers=min(8, len(languages))) as ex:
        results = dict(ex.map(_one, languages))
    return parent, results


def _s3_status(job_id: str) -> dict[str, Any] | None:
    """The status record the task wrote to S3, or None if it hasn't written one yet."""
    c = _cfg()
    try:
        obj = _s3().get_object(Bucket=c["bucket"], Key=f"{c['prefix']}/{job_id}/status.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def status_parallel(langs_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-language {ecs_state, S3 status record} for a fanned-out run.

    Both lookups are batched/fanned out because this runs inside the Teams webhook's ~5s
    reply window: describe_tasks takes up to 100 ARNs in ONE call (it was one call per
    language), and the S3 status reads go out concurrently. Six languages was 12 serial
    AWS round-trips; it's now 1 + 6-in-parallel.
    """
    from concurrent.futures import ThreadPoolExecutor

    arns = [i["task_arn"] for i in langs_map.values() if i.get("task_arn")]
    ecs_by_arn: dict[str, str] = {}
    if arns:
        cluster = _cfg()["cluster"]
        ecs = _ecs()
        try:
            for i in range(0, len(arns), 100):          # describe_tasks caps at 100 per call
                d = ecs.describe_tasks(cluster=cluster, tasks=arns[i:i + 100])
                for tk in d.get("tasks") or []:
                    ecs_by_arn[tk["taskArn"]] = tk.get("lastStatus", "UNKNOWN")
        except Exception:
            pass                                        # fall through as UNKNOWN, S3 still decides

    jobs_to_read = {lang: i["job_id"] for lang, i in langs_map.items() if i.get("task_arn")}
    recs: dict[str, dict[str, Any] | None] = {}
    if jobs_to_read:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs_to_read))) as ex:
            recs = dict(zip(jobs_to_read, ex.map(_s3_status, jobs_to_read.values())))

    out: dict[str, dict[str, Any]] = {}
    for lang, info in langs_map.items():
        if not info.get("task_arn"):
            out[lang] = {"ecs": "FAILED", "rec": None, "error": info.get("error")}
            continue
        out[lang] = {"ecs": ecs_by_arn.get(info["task_arn"], "UNKNOWN"), "rec": recs.get(lang)}
    return out


def status(task_arn: str, job_id: str) -> tuple[str, dict[str, Any] | None]:
    """(ECS lastStatus, S3 status record or None). The S3 record — written by the task —
    is authoritative for the OUTCOME; the ECS state tells us if it's still running."""
    ecs_state = "UNKNOWN"
    try:
        d = _ecs().describe_tasks(cluster=_cfg()["cluster"], tasks=[task_arn])
        tk = d.get("tasks") or []
        if tk:
            ecs_state = tk[0].get("lastStatus", "UNKNOWN")
    except Exception:
        pass
    return ecs_state, _s3_status(job_id)


def ensure_summary(parent_id: str, episode: int,
                   langs_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Build (once, then cache in S3) the cross-language view for a fanned-out run from each
    language's uploaded `xlang.json`: the Summary workbook AND the chat-sized root-cause
    headline. Returns {"key": <xlsx s3 key>, "headline": [...]} or None if nothing to
    aggregate. Cheap (~1-2s, no audio) and idempotent — rebuilt only if the objects are gone.

    The headline is cached beside the workbook because the alternative — re-reading every
    language's full xlang.json on each 'status' — would blow the Teams reply window.
    """
    import tempfile

    from . import excel_report
    c = _cfg()
    key = f"{c['prefix']}/{parent_id}/EP{int(episode):02d}_Summary.xlsx"
    meta_key = f"{c['prefix']}/{parent_id}/EP{int(episode):02d}_Summary.json"
    s3 = _s3()
    try:
        s3.head_object(Bucket=c["bucket"], Key=key)
        headline: list[str] = []
        try:                                         # a few hundred bytes
            headline = json.loads(s3.get_object(Bucket=c["bucket"], Key=meta_key)["Body"].read())
        except Exception:
            pass                                     # pre-dating the cache, or not written
        return {"key": key, "headline": headline}    # already built
    except Exception:
        pass
    per_lang: dict[str, Any] = {}
    for info in langs_map.values():
        try:
            obj = s3.get_object(Bucket=c["bucket"], Key=f"{c['prefix']}/{info['job_id']}/xlang.json")
            per_lang.update(json.loads(obj["Body"].read()))   # {lang: res}
        except Exception:
            pass                                     # a skipped/failed language has no xlang.json
    if not per_lang:
        return None
    tmp = f"{tempfile.mkdtemp()}/EP{int(episode):02d}_Summary.xlsx"
    excel_report.build_summary_workbook(per_lang, tmp)
    s3.upload_file(tmp, c["bucket"], key)
    from . import xlang as _xlang
    try:
        headline = _xlang.headline(per_lang)
    except Exception:  # noqa: BLE001 — the workbook is the deliverable; the headline is extra
        headline = []
    try:
        s3.put_object(Bucket=c["bucket"], Key=meta_key,
                      Body=json.dumps(headline, ensure_ascii=False).encode("utf-8"),
                      ContentType="application/json")
    except Exception:  # noqa: BLE001
        pass
    return {"key": key, "headline": headline}


def download_url(zip_key: str, expires: int = 86400) -> str:
    c = _cfg()
    return _s3().generate_presigned_url(
        "get_object", Params={"Bucket": c["bucket"], "Key": zip_key}, ExpiresIn=expires)
