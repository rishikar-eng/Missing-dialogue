"""Tiny in-process async-job registry for the HOSTED deployment.

Why jobs at all: an analysis run takes 1-5 minutes, and tunnels/proxies in front of a
hosted instance (ngrok, Cloudflare, Railway, API gateways) cut long-silent HTTP
requests — ngrok idles out around 5 minutes, Cloudflare hard-524s at 120 s. So the
hosted UI submits work, gets a job id back immediately, and polls. The desktop app
keeps its original synchronous /api/analyze and is unaffected.

Deliberately in-process and dict-backed (like STATE): one uvicorn worker serves a
handful of colleagues. Restarting the server forgets jobs — acceptable, the UI just
shows the error and the user re-runs.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

# One analysis at a time by default: each run holds several native-rate stems in RAM,
# and two concurrent runs can OOM a small host. Extra submissions queue.
MAX_CONCURRENT = max(1, int(os.environ.get("DQC_MAX_CONCURRENT_JOBS", "1")))
# Queued-but-not-started cap: refuse (429) rather than let a stuck queue grow forever.
MAX_QUEUED = max(1, int(os.environ.get("DQC_MAX_QUEUED_JOBS", "10")))
# Finished jobs are kept this long so the client can fetch the result, then reaped.
JOB_TTL_S = max(60, int(os.environ.get("DQC_JOB_TTL_S", "3600")))


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued | running | done | error
    progress: dict[str, Any] = field(default_factory=lambda: {"stage": "queued", "done": 0, "total": 0})
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "result": self.result if self.status == "done" else None,
            "error": self.error,
        }


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()
# One "heavy slot": each analysis (job OR a synchronous /api/analyze,/api/compare,/api/qc
# call) holds it while running, so at most MAX_CONCURRENT run at once regardless of which
# entry point they came through — two 4 GB runs at once would OOM a small host. On the
# single-user desktop it is always uncontended (one request in flight at a time).
_SEM = threading.Semaphore(MAX_CONCURRENT)


@contextmanager
def heavy_slot() -> Iterator[None]:
    """Bound concurrent heavy analyses across every entry point (used by the synchronous
    pipeline endpoints; the job runner acquires the same semaphore directly)."""
    _SEM.acquire()
    try:
        yield
    finally:
        _SEM.release()


def _reap_locked() -> None:
    now = time.time()
    dead = [jid for jid, j in _JOBS.items()
            if j.finished_at is not None and now - j.finished_at > JOB_TTL_S]
    for jid in dead:
        del _JOBS[jid]


def submit(kind: str, runner: Callable[[Callable[[str, int, int], None]], dict[str, Any]],
           on_done: Callable[[Job], None] | None = None) -> Job:
    """Queue `runner` on a worker thread. `runner` receives a stage(msg, done, total)
    callback for progress and returns the result dict; raising marks the job failed.
    `on_done`, if given, is called with the finished Job (done OR error) — used to persist
    the result to disk so it survives a restart. Raises RuntimeError when the queue is full
    (the endpoint maps it to HTTP 429)."""
    with _LOCK:
        _reap_locked()
        queued = sum(1 for j in _JOBS.values() if j.status == "queued")
        if queued >= MAX_QUEUED:
            raise RuntimeError(f"Too many queued analyses ({queued}) — try again in a few minutes")
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        _JOBS[job.id] = job

    def stage(msg: str, done: int = 0, total: int = 0) -> None:
        job.progress = {"stage": msg, "done": done, "total": total}

    def work() -> None:
        with _SEM:
            job.status = "running"
            job.progress = {"stage": "starting", "done": 0, "total": 0}
            try:
                job.result = runner(stage)
                job.status = "done"
            except Exception as e:  # HTTPException carries .detail; anything else str()
                job.error = str(getattr(e, "detail", None) or e) or "Analysis failed"
                job.status = "error"
            finally:
                job.finished_at = time.time()
                if on_done:
                    try:
                        on_done(job)
                    except Exception:  # persistence must never fail the job
                        pass

    try:
        threading.Thread(target=work, daemon=True, name=f"dqc-job-{job.id}").start()
    except Exception as e:  # OS thread-limit etc.: don't leave an immortal 'queued' job
        job.status, job.error, job.finished_at = "error", f"Could not start worker: {e}", time.time()
    return job


def get(job_id: str) -> Job | None:
    with _LOCK:
        _reap_locked()
        return _JOBS.get(job_id)
