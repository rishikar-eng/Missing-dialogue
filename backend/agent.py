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
        "name": "audio_check",
        "description": ("Start AUDIO-ONLY QC for one episode + one dub language (asynchronous, "
                        "~8 min). No script or stems needed: compares the original full mix "
                        "against that language's delivered final mix straight from Box and flags "
                        "missing dialogue with confidence tiers. Use when the user says audio "
                        "check / mix check / scriptless QC, or when no script is delivered. Only "
                        "call after the user has confirmed. Returns a job_id."),
        "input_schema": {
            "type": "object",
            "properties": {
                "episode": {"type": "integer"},
                "language": {"type": "string", "description": "One dub language, e.g. Tamil"},
            },
            "required": ["episode", "language"],
        },
    },
    {
        "name": "get_audio_result",
        "description": ("Check an audio-only QC run by its job_id. When done, returns missing "
                        "counts by confidence, coverage, and the AudioQC workbook download link."),
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
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
        "series": rep.get("series"), "series_key": rep.get("series_key"),
        "episode": rep.get("episode"),
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

    if name == "audio_check":
        from . import audio_jobs
        return audio_jobs.launch(series_key, cfg, int(inp["episode"]), str(inp["language"]))

    if name == "get_audio_result":
        from . import audio_jobs
        return audio_jobs.status(str(inp["job_id"]))

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
        "- AUDIO-ONLY QC: when the user asks for an audio check / mix check / scriptless QC, or "
        "wants QC but no script is delivered, call audio_check with the episode and ONE language "
        "(confirm first, same as run_qc). It compares the original full mix to that language's "
        "delivered final mix (~8 min). Poll get_audio_result; when done, report missing counts "
        "BY CONFIDENCE (high = verify first), coverage, and the workbook link. Over-flagging is "
        "expected — the sound team verifies flags in Pro Tools, confidence gives the order.\n"
        "- Keep replies short and Teams-friendly. Episode numbers are integers. Never invent data — "
        "report only what the tools return. If an episode has no script or no delivered languages, "
        "say so plainly."
    )


# --------------------------------------------------------------------------- #
# Asking questions ABOUT a finished run (the async path)
# --------------------------------------------------------------------------- #
# The Teams webhook can't wait for an LLM (5s reply window), so anything the regex intents
# don't cover is answered on a background thread and pushed into the channel. That removes the
# latency ceiling, which is what makes a read-only tool loop over the FULL results affordable:
# the model can look up individual lines, one character across languages, or the cross-language
# root cause, instead of paraphrasing a summary it was handed.
_ASK_TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_overview",
        "description": ("Counts per language for this run plus the caveats that qualify them "
                        "(characters with no audio delivered, out-of-sync tracks, unparsed "
                        "script rows, mapping issues). Start here."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_findings",
        "description": ("Individual findings with episode timecodes and the scripted line. "
                        "Filter by language, type (MISSING / MISMATCH / MISALIGNED / EXTRA) "
                        "and/or character name."),
        "input_schema": {
            "type": "object",
            "properties": {
                "language": {"type": "string"},
                "type": {"type": "string", "enum": ["MISSING", "MISMATCH", "MISALIGNED", "EXTRA"]},
                "character": {"type": "string"},
                "limit": {"type": "integer", "description": "default 25, max 50"},
            },
        },
    },
    {
        "name": "character_report",
        "description": ("One character across every language: which track they were mapped to "
                        "and how, whether any audio was delivered at all, and their missing / "
                        "wrong-speaker / misaligned counts. Use for 'why is X missing?'."),
        "input_schema": {"type": "object",
                         "properties": {"character": {"type": "string"}},
                         "required": ["character"]},
    },
    {
        "name": "cross_language_check",
        "description": ("Per affected character, whether a gap repeats across ALL languages "
                        "(points at the script or the character mapping) or shows up in one "
                        "language only (points at that dub vendor). The root-cause view."),
        "input_schema": {"type": "object", "properties": {}},
    },
]

_ASK_SYSTEM = (
    "You answer questions about a COMPLETED dialogue-QC run for a dubbed episode, for a studio "
    "QC team in a Teams channel.\n\n"
    "Dialogue QC compares each dubbed line against the script using speech detection. It knows "
    "WHETHER someone spoke in the right place, never WHAT they said — so never claim a line was "
    "mis-spoken, mistranslated or badly acted; that needs a human listen.\n"
    "Finding types: MISSING (scripted line, silent track) · MISMATCH (delivered, but on another "
    "character's track) · MISALIGNED (present but early/late/clipped) · EXTRA (speech with no "
    "scripted line — often breaths or un-scripted reactions, usually harmless).\n"
    "'No audio delivered' means no track was matched to that character — either it wasn't "
    "delivered or it was labelled unrecognisably.\n\n"
    "How to answer:\n"
    "- Call tools for every fact. Never guess a number, a timecode or a character name.\n"
    "- Lead with the answer. Then the evidence: timecodes, counts, language names.\n"
    "- Say what to DO when it's clear (re-record, check the delivery folder, look at the "
    "script) and, where the cross-language check supports it, say whether it's a script-side "
    "or a dub-side problem.\n"
    "- Keep it under ~150 words, Teams-friendly markdown, no headings.\n"
    "- If the tools don't cover the question, say so plainly rather than speculating."
)
_ASK_MAX_TURNS = 6


def answer_about_run(question: str, per_lang: dict[str, Any], context: str = "") -> str:
    """Answer a free-form question against one finished run's full results. Runs OFF the Teams
    reply path (see backend/notify.py) — a tool loop takes ~10-20s."""
    from . import results as _results
    if not per_lang:
        return ("I don't have the detailed results for that run any more — ask `status` for the "
                "summary, or re-run the episode.")

    def _dispatch(name: str, inp: dict[str, Any]) -> Any:
        if name == "run_overview":
            return _results.overview(per_lang)
        if name == "list_findings":
            return _results.findings(per_lang, language=inp.get("language"),
                                     kind=inp.get("type"), character=inp.get("character"),
                                     limit=min(int(inp.get("limit") or 25), 50))
        if name == "character_report":
            return _results.character_report(per_lang, str(inp.get("character") or ""))
        if name == "cross_language_check":
            return _results.cross_language(per_lang)
        return {"error": f"unknown tool {name}"}

    client = anthropic.Anthropic()
    convo: list[dict[str, Any]] = [{"role": "user", "content": question}]
    system = _ASK_SYSTEM + (f"\n\nContext for this run: {context}" if context else "")
    reply = ""
    for _ in range(_ASK_MAX_TURNS):
        resp = client.messages.create(model=WORKER_MODEL, max_tokens=1024, system=system,
                                      tools=_ASK_TOOLS, messages=convo)
        reply = "".join(b.text for b in resp.content if b.type == "text")
        convo.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        out_blocks = []
        for b in resp.content:
            if b.type == "tool_use":
                try:
                    out = _dispatch(b.name, b.input or {})
                except Exception as e:  # noqa: BLE001 — let the model see and recover
                    out = {"error": str(e)[:200]}
                out_blocks.append({"type": "tool_result", "tool_use_id": b.id,
                                   "content": json.dumps(out, default=str)[:60000]})
        convo.append({"role": "user", "content": out_blocks})
    return reply or "I couldn't work that out — try asking about a specific character or language."


def worker_reply(series_key: str, cfg: dict[str, Any],
                 convo: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the Haiku tool-use loop over `convo` (a message list ending in the new user turn).
    Returns {reply, convo} — convo is the full history incl. tool turns, for the session store
    (kept server-side; never serialised to the client)."""
    client = anthropic.Anthropic()
    system = _system(cfg)
    reply = ""
    last_availability: dict[str, Any] | None = None   # most recent check, for the Run card
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
                if b.name == "check_availability" and isinstance(out, dict) and "runnable" in out:
                    last_availability = out
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out)})
        convo.append({"role": "user", "content": results})
    else:
        reply = reply or "Sorry - I got stuck taking too many steps. Please try rephrasing."
    return {"reply": reply, "convo": convo, "last_availability": last_availability}
