"""Disk-persisted registry of agent QC runs.

The in-process jobs registry (backend/jobs.py) is forgotten on every server restart, so a
Teams user who asks 'status' after a deploy sees "No QC run here yet" even though their run
finished and its zip is still on disk. This tiny store records each agent run to one JSON
file so 'status' and the download link survive restarts.

Deliberately a whole-file rewrite under a lock: a handful of runs a day, so simplicity beats
a database. Keyed by job_id; also carries `conv` (Teams conversation id) so a status check
can recover the latest run for a conversation after the in-process session is gone too.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_PATH = Path(os.environ.get("DQC_DATA_ROOT", "/tmp")) / "agent_runs.json"
_LOCK = threading.Lock()
_MAX = 200   # keep the newest N runs; older records are reaped on write


def _load() -> dict[str, Any]:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_PATH)   # atomic on POSIX — never a half-written file


def record(job_id: str, **fields: Any) -> None:
    """Upsert one run's record (status, episode, series, zip_path, summary, why, conv…)."""
    with _LOCK:
        d = _load()
        rec = d.get(job_id, {})
        now = time.time()
        rec.update(job_id=job_id, updated_at=now, **fields)
        rec.setdefault("created_at", now)
        d[job_id] = rec
        if len(d) > _MAX:
            for k in sorted(d, key=lambda k: d[k].get("updated_at", 0))[: len(d) - _MAX]:
                del d[k]
        _save(d)


def get(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        return _load().get(job_id)


def latest_for(conv: str) -> dict[str, Any] | None:
    """Most recently updated run for a Teams conversation (used when the in-process
    session was lost to a restart, so we no longer hold the job id)."""
    with _LOCK:
        recs = [r for r in _load().values() if r.get("conv") == conv]
    return max(recs, key=lambda r: r.get("updated_at", 0)) if recs else None


def recent_for(conv: str, limit: int = 5) -> list[dict[str, Any]]:
    """Newest-first runs for a conversation — so a user can find an earlier episode's report
    without scrolling the channel."""
    with _LOCK:
        recs = [r for r in _load().values() if r.get("conv") == conv]
    return sorted(recs, key=lambda r: r.get("updated_at", 0), reverse=True)[:limit]


def unnotified() -> list[dict[str, Any]]:
    """Runs with a push target that haven't been announced yet — the proactive notifier's work
    queue (see backend/notify.py).

    Deliberately NOT filtered to status == 'running': a short local run can finish before the
    watcher's first poll, and filtering on 'running' would drop it from the queue before it was
    ever announced. The notifier decides whether a record has settled; this only decides whether
    it still needs looking at."""
    with _LOCK:
        return [r for r in _load().values() if r.get("notify_url") and not r.get("notified")]
