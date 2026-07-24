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
    return boto3.client("s3", region_name=_REGION)


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
    rec = None
    c = _cfg()
    try:
        obj = _s3().get_object(Bucket=c["bucket"], Key=f"{c['prefix']}/{job_id}/status.json")
        rec = json.loads(obj["Body"].read())
    except Exception:
        pass
    return ecs_state, rec


def download_url(zip_key: str, expires: int = 86400) -> str:
    c = _cfg()
    return _s3().generate_presigned_url(
        "get_object", Params={"Bucket": c["bucket"], "Key": zip_key}, ExpiresIn=expires)
