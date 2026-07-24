"""One-shot Fargate task: run QC for one episode and publish the result to S3.

This is the container's command for the heavy-compute role (`python -m backend.job_entry`).
The always-on dispatcher (EC2) launches an ECS task with these env vars, mints a Box access
token for it (BOX_ACCESS_TOKEN — so the task never touches the rotating refresh token), and
later reads the result back from S3.

Env in:
  DQC_JOB_SERIES     series key/alias (e.g. "gavv")           [required]
  DQC_JOB_EPISODE    episode number                            [required]
  DQC_JOB_LANGUAGES  comma-separated subset (optional; all if empty)
  DQC_JOB_ID         id used in the S3 key + status record     [required]
  DQC_S3_BUCKET      output bucket                             [required]
  DQC_S3_PREFIX      key prefix (default "output")
  BOX_ACCESS_TOKEN   pre-minted Box access token (from the dispatcher)

Writes to s3://$BUCKET/$PREFIX/$JOB_ID/:
  EP{NN}_QC.zip   the report bundle (workbook + missing-audio FLACs)
  status.json     {status, episode, series, summary, zip_key, error}
Exit code is non-zero on failure so ECS marks the task FAILED.
"""
from __future__ import annotations

import json
import os
import sys
import traceback


def _s3():
    import boto3  # lazy: only the Fargate role needs it
    return boto3.client("s3")


def _put_status(bucket: str, prefix: str, job_id: str, rec: dict) -> None:
    try:
        _s3().put_object(Bucket=bucket, Key=f"{prefix}/{job_id}/status.json",
                         Body=json.dumps(rec, default=str).encode("utf-8"),
                         ContentType="application/json")
    except Exception as e:  # never mask the real error with a reporting error
        print("WARN: could not write status.json:", e, file=sys.stderr)


def main() -> int:
    from . import episode_runner, series_registry

    series = os.environ["DQC_JOB_SERIES"]
    episode = int(os.environ["DQC_JOB_EPISODE"])
    langs = [x.strip() for x in os.environ.get("DQC_JOB_LANGUAGES", "").split(",") if x.strip()] or None
    job_id = os.environ["DQC_JOB_ID"]
    bucket = os.environ["DQC_S3_BUCKET"]
    prefix = os.environ.get("DQC_S3_PREFIX", "output").strip("/")

    _put_status(bucket, prefix, job_id, {"status": "running", "episode": episode, "series": series})
    try:
        key, cfg = series_registry.resolve(series)
    except Exception as e:
        _put_status(bucket, prefix, job_id, {"status": "error", "episode": episode, "why": str(e)})
        print("resolve failed:", e, file=sys.stderr)
        return 2

    try:
        r = episode_runner.run(key, cfg, episode, languages=langs, ref_audio=True)
    except Exception as e:
        traceback.print_exc()
        _put_status(bucket, prefix, job_id,
                    {"status": "error", "episode": episode, "series": cfg.get("display_name"),
                     "why": str(e)[:300]})
        return 1

    zip_key = None
    zp = r.get("zip_path")
    if r.get("status") == "ok" and zp and os.path.isfile(zp):
        zip_key = f"{prefix}/{job_id}/{os.path.basename(zp)}"
        _s3().upload_file(zp, bucket, zip_key)
        # per-language result JSON -> for the cross-language Summary aggregation
        rp = r.get("results_path")
        if rp and os.path.isfile(rp):
            try:
                _s3().upload_file(rp, bucket, f"{prefix}/{job_id}/xlang.json")
            except Exception as e:  # noqa: BLE001
                print("WARN: could not upload xlang.json:", e, file=sys.stderr)

    _put_status(bucket, prefix, job_id, {
        "status": r.get("status"), "episode": r.get("episode", episode),
        "series": r.get("series") or cfg.get("display_name"),
        "summary": r.get("summary_by_language"), "notes": r.get("notes"),
        "why": r.get("why"), "zip_key": zip_key,
    })
    print(json.dumps({"status": r.get("status"), "zip_key": zip_key,
                      "summary": r.get("summary_by_language")}, default=str))
    return 0 if r.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
