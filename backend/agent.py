"""L2 — per-series worker agent (Claude Haiku 4.5).

Understands a natural-language request about ONE series and drives the QC engine through
three tools — check availability, run QC, get result — which call the in-process engine
functions directly (same server as the HTTP /api/agent/* surface external callers use).
The L3 router picks which series' worker handles a message; a worker is series-scoped via
the config bound into its tool dispatch.

Haiku 4.5 note: this tier uses plain messages.create with tools — no `effort`/adaptive
thinking params (those are Opus/Sonnet-5 only and 400 on Haiku).
"""
from __future__ import annotations

import json
from typing import Any

import anthropic

from . import box_discovery, box_oauth, episode_runner, jobs

WORKER_MODEL = "claude-haiku-4-5"
_MAX_TURNS = 6   # tool-use round-trips before we force a reply

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "check_availability",
        "description": ("Check Box for what's available to QC for an episode of this series: "
                        "the English script, the original audio, the character list, and each "
                        "dub language (present + track count). Call this before running QC."),
        "input_schema": {
            "type": "object",
            "properties": {"episode": {"type": "integer", "description": "Episode number"}},
            "required": ["episode"],
        },
    },
    {
        "name": "run_qc",
        "description": ("Start dialogue QC for an episode from Box (asynchronous — takes minutes). "
                        "Returns a job_id. Only call after the user has confirmed. Optionally limit "
                        "to specific languages; omit to run every available language."),
        "input_schema": {
            "type": "object",
            "properties": {
                "episode": {"type": "integer"},
                "languages": {"type": "array", "items": {"type": "string"},
                              "description": "Optional subset of dub languages"},
            },
            "required": ["episode"],
        },
    },
    {
        "name": "get_result",
        "description": ("Check the status of a QC run by its job_id. When done, returns the "
                        "per-language missing/extra summary and a download link for the report zip."),
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
]


def _availability_brief(rep: dict[str, Any]) -> dict[str, Any]:
    """Compact the full availability report to what the model needs to decide + summarise."""
    langs = rep.get("languages", {})
    return {
        "series": rep.get("series"), "episode": rep.get("episode"),
        "script": rep["script"].get("present"),
        "original_audio": rep["original"].get("present"),
        "character_list": rep["char_list"].get("present"),
        "languages_ready": {l: v.get("tracks") for l, v in langs.items() if v.get("present")},
        "not_delivered": [l for l, v in langs.items() if not v.get("present")],
        "runnable": rep["summary"]["runnable"],
    }


def _dispatch(series_key: str, cfg: dict[str, Any], name: str, inp: dict[str, Any]) -> dict[str, Any]:
    """Execute one tool call against the engine (in-process)."""
    if name == "check_availability":
        token = box_oauth.get_token()
        rep = box_discovery.check_episode(token, series_key, cfg, int(inp["episode"]))
        return _availability_brief(rep)

    if name == "run_qc":
        n = int(inp["episode"])
        langs = inp.get("languages") or None
        try:
            job = jobs.submit("agent-run", lambda stage: episode_runner.run(
                series_key, cfg, n, languages=langs, ref_audio=True, stage=stage))
        except RuntimeError as e:
            return {"error": str(e)}
        return {"job_id": job.id, "status": job.status,
                "note": "QC started; poll get_result with this job_id."}

    if name == "get_result":
        job = jobs.get(str(inp["job_id"]))
        if not job:
            return {"error": "unknown or expired job_id"}
        out: dict[str, Any] = {"status": job.status, "stage": job.progress.get("stage")}
        if job.status == "done" and job.result:
            r = job.result
            out["result"] = {
                "status": r.get("status"),
                "summary_by_language": r.get("summary_by_language"),
                "notes": r.get("notes"),
                "download_url": (f"/api/agent/download?job_id={job.id}"
                                 if r.get("status") == "ok" else None),
            }
        if job.status == "error":
            out["error"] = job.error
        return out

    return {"error": f"unknown tool {name}"}


def _system(cfg: dict[str, Any]) -> str:
    langs = ", ".join(cfg.get("languages", []))
    return (
        f"You are the dubbing-QC assistant for {cfg.get('display_name')}. You help a studio "
        f"check and run dialogue QC on episodes.\n\n"
        f"Dub languages for this series: {langs}.\n\n"
        "How to work:\n"
        "- When the user asks to QC or check an episode, FIRST call check_availability for it.\n"
        "- Then give a SHORT summary: whether the script / original audio / character list are "
        "present, which languages are ready (with track counts), and which are not delivered. "
        "If it's runnable, ask the user to confirm before running — UNLESS they already clearly "
        "said run/go/yes.\n"
        "- On confirmation, call run_qc (pass a language subset only if the user asked for specific "
        "languages). Tell them it's running and they can ask for the result.\n"
        "- When the user asks for status/result, or you already hold a job_id, call get_result. "
        "When it's done, give the per-language missing/extra counts and the download link.\n"
        "- Keep replies short and Teams-friendly. Episode numbers are integers. Never invent data — "
        "report only what the tools return. If an episode has no script or no delivered languages, "
        "say so plainly."
    )


def worker_reply(series_key: str, cfg: dict[str, Any],
                 convo: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the Haiku tool-use loop over `convo` (a message list ending in the new user turn).
    Returns {reply, convo} — convo is the full history incl. tool turns, for the session store
    (kept server-side; never serialised to the client)."""
    client = anthropic.Anthropic()
    system = _system(cfg)
    reply = ""
    for _ in range(_MAX_TURNS):
        resp = client.messages.create(
            model=WORKER_MODEL, max_tokens=1024, system=system, tools=_TOOLS, messages=convo)
        reply = "".join(b.text for b in resp.content if b.type == "text")
        convo.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                try:
                    out = _dispatch(series_key, cfg, b.name, b.input)
                except Exception as e:  # noqa: BLE001 — surface tool errors to the model
                    out = {"error": str(e)[:200]}
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out)})
        convo.append({"role": "user", "content": results})
    else:
        reply = reply or "Sorry — I got stuck taking too many steps. Please try rephrasing."
    return {"reply": reply, "convo": convo}
