"""L3 — the router. Identifies WHICH series a request is about, then the caller hands off
to that series' worker agent. Runs once per session (to bind the series); after that the
worker carries the conversation.

Two-tier for cost: a cheap rule pass matches a series name/alias in the message directly
(covers "QC ep 42 of Gavv"); only genuinely ambiguous messages fall through to the bigger
model (Claude Sonnet 5), which reads the registered-series list and returns the key or asks.
"""
from __future__ import annotations

import json
import re
from typing import Any

import anthropic

from . import series_registry

ROUTER_MODEL = "claude-sonnet-5"


def _rule_match(message: str) -> set[str]:
    """Series whose name/alias appears as a whole word in the message."""
    m = " " + message.lower() + " "
    hits: set[str] = set()
    for s in series_registry.all_series():
        for name in [s["key"], s.get("display_name", ""), *s.get("aliases", [])]:
            nl = (name or "").lower()
            if len(nl) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(nl)}(?![a-z0-9])", m):
                hits.add(s["key"])
    return hits


def route(message: str) -> dict[str, Any]:
    """Return {"series_key": key} once the series is identified, or {"ask": question} to
    prompt the user when it's unknown or ambiguous."""
    all_s = series_registry.all_series()
    hits = _rule_match(message)
    if len(hits) == 1:
        return {"series_key": next(iter(hits))}

    # Ambiguous or unnamed -> let Sonnet 5 decide from the registered-series list.
    listing = "\n".join(
        f"- key: {s['key']} | name: {s.get('display_name')} | aliases: {', '.join(s.get('aliases', []))}"
        for s in all_s)
    system = (
        "You route dubbing-QC requests to the correct series. Registered series:\n" + listing + "\n\n"
        "Given the user's message, decide which series it's about and return its `key`.\n"
        "Rules:\n"
        "- If the message names one of the series (by name or alias), return that key.\n"
        "- If exactly one series is registered and the message is a QC request that does NOT "
        "clearly refer to a DIFFERENT show, return that one series' key.\n"
        "- If it could be several, or names a show that isn't registered, return null for series_key."
    )
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=ROUTER_MODEL, max_tokens=200,
        thinking={"type": "disabled"},          # fast classification, no thinking tokens
        system=system,
        messages=[{"role": "user", "content": message}],
        output_config={"format": {"type": "json_schema", "schema": {
            "type": "object",
            "properties": {"series_key": {"type": ["string", "null"]},
                           "reason": {"type": "string"}},
            "required": ["series_key", "reason"],
            "additionalProperties": False,
        }}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    try:
        data = json.loads(text)
    except Exception:
        data = {}
    key = data.get("series_key")
    if key:
        r = series_registry.resolve(str(key))
        if r:
            return {"series_key": r[0]}
    names = ", ".join(series_registry.series_names())
    return {"ask": f"Which series is this for? I currently handle: {names}."}
