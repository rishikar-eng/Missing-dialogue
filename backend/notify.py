"""Proactive completion messages for Teams.

A QC run takes minutes and finishes in silence: Teams **Outgoing Webhooks are strictly
request/response**, so the bot cannot say anything unless it was just spoken to. The user has
to keep asking 'status'. That is the single biggest gap in the chat experience.

The fix that does NOT require becoming a full Bot Framework app is an **Incoming Webhook**: a
per-channel URL that accepts a POST and posts into the channel. It is configured per channel
by whoever owns it (Teams -> channel -> Connectors -> Incoming Webhook) and handed to us as:

    DQC_TEAMS_INCOMING       one default URL (single-channel deployments — the common case)
    DQC_TEAMS_INCOMING_MAP   {"<conversation id>": "<url>", ...} for several channels

The conversation id of an Outgoing Webhook message is not the same identifier a human sees, so
the map is easiest to fill by running `runs` in the target channel and copying the id from the
logs. With neither variable set the watcher simply never starts and everything behaves exactly
as before — polling still works.

One daemon thread polls the run store; when a run settles it posts the SAME payload the
'status' command would have produced (see server._run_status) and marks the record notified, so
a restart can't double-announce. Poll interval is deliberately lazy: a run takes minutes, and
each poll costs an ECS describe + a few S3 gets.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from . import run_store

POLL_S = max(15, int(os.environ.get("DQC_NOTIFY_POLL_S", "45")))
# A run that never settles (a task lost without writing status.json) must not be polled
# forever — after this we announce what we know and stop watching.
MAX_WATCH_S = max(600, int(os.environ.get("DQC_NOTIFY_MAX_S", "5400")))

_thread: threading.Thread | None = None
_lock = threading.Lock()


def target_for(conv: str) -> str | None:
    """The Incoming Webhook URL for a conversation, or None when push isn't configured."""
    raw = os.environ.get("DQC_TEAMS_INCOMING_MAP", "").strip()
    if raw:
        try:
            m = json.loads(raw)
            if isinstance(m, dict) and m.get(conv):
                return str(m[conv])
        except Exception:  # noqa: BLE001 — a malformed map must not break launching a run
            pass
    return os.environ.get("DQC_TEAMS_INCOMING", "").strip() or None


def enabled() -> bool:
    return bool(os.environ.get("DQC_TEAMS_INCOMING", "").strip()
                or os.environ.get("DQC_TEAMS_INCOMING_MAP", "").strip())


def post(url: str, text: str, card: dict[str, Any] | None = None) -> bool:
    """Post a message (optionally an Adaptive Card) to a Teams Incoming Webhook."""
    import httpx
    if card:
        body = {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
    else:
        body = {"text": text}
    try:
        r = httpx.post(url, json=body, timeout=10.0)
        return r.status_code < 300
    except Exception:  # noqa: BLE001 — a failed announcement must never affect the run
        return False


def watch(job_id: str, conv: str) -> bool:
    """Mark a freshly-launched run for announcement and make sure the watcher is running.
    Returns True when push is configured (so the caller can say 'I'll post when it's done')."""
    url = target_for(conv)
    if not url:
        return False
    run_store.record(job_id, notify_url=url, notified=False, watch_started=time.time())
    _ensure_thread()
    return True


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, daemon=True, name="dqc-notify")
        _thread.start()


def _announce(rec: dict[str, Any]) -> None:
    """Resolve one run's current state and, if it has settled, post it and mark it done."""
    from . import jobs, server                        # late: server imports this module
    jid = rec.get("job_id")
    st = server._run_status(jid, rec, jobs.get(jid) if jid else None)
    stale = (time.time() - float(rec.get("watch_started") or 0)) > MAX_WATCH_S
    if not st.get("settled") and not stale:
        return
    text = st.get("text") or "QC run finished."
    if stale and not st.get("settled"):
        text = ("⚠️ I've stopped watching this run — it hasn't reported in. Latest I have:\n\n"
                + text)
    ok = post(str(rec["notify_url"]), text, server._status_card(st))
    # Mark notified even when the POST failed: retrying a broken/rotated webhook URL every
    # 45s forever is worse than one lost announcement (the user can still ask 'status').
    run_store.record(jid, notified=True, notify_ok=ok)


def _loop() -> None:
    idle = 0
    while True:
        time.sleep(POLL_S)
        try:
            pending = run_store.unnotified()
        except Exception:  # noqa: BLE001
            pending = []
        if not pending:
            idle += 1
            if idle > 40:                             # ~30 min quiet -> let the thread exit
                return                                # (watch() restarts it on the next run)
            continue
        idle = 0
        for rec in pending:
            try:
                _announce(rec)
            except Exception:  # noqa: BLE001 — one bad record must not kill the watcher
                continue
