"""The series registry — the single source of truth for WHERE each show's assets live in
Box, kept as editable DATA (series_registry.json), not hardcoded in code.

A series entry maps a show to its Box folder ids (scripts, premix/original, per-language
voiceover, character list) plus the aliases people use for it in chat. Adding a show is a
JSON entry; no code change. Resolution is by name or alias, case-insensitive, so the router
can turn "Gavv" / "KRG" / "Kamen Rider Gavv" into the same series.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PATH = Path(__file__).with_name("series_registry.json")
_cache: dict[str, dict[str, Any]] | None = None


def _load() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is None:
        _cache = json.loads(_PATH.read_text(encoding="utf-8"))
    return _cache


def reload() -> None:
    """Drop the in-memory cache so a live edit to series_registry.json takes effect."""
    global _cache
    _cache = None


def all_series() -> list[dict[str, Any]]:
    """Every registered series as {key, display_name, aliases, languages, ...}."""
    return [{"key": k, **v} for k, v in _load().items()]


def series_names() -> list[str]:
    """Display names of every registered series (for 'which series?' prompts)."""
    return [v.get("display_name", k) for k, v in _load().items()]


def resolve(name: str) -> tuple[str, dict[str, Any]] | None:
    """(key, config) for a series named/aliased `name`, case-insensitive; None if no match.

    Tries, in order: exact key, exact display-name/alias, then a loose contains match so
    'gavv' finds 'Kamen Rider Gavv'. Loose matching only runs when there's no exact hit,
    so it can't shadow a precise name once more series are added."""
    if not name or not name.strip():
        return None
    q = name.strip().lower()
    reg = _load()
    if q in reg:
        return q, reg[q]

    def names_of(key: str, cfg: dict[str, Any]) -> list[str]:
        return [n.lower() for n in [key, cfg.get("display_name", ""), *cfg.get("aliases", [])] if n]

    for key, cfg in reg.items():
        if q in names_of(key, cfg):
            return key, cfg
    # loose: query is contained in an alias or vice-versa (>=3 chars to avoid silly hits)
    if len(q) >= 3:
        for key, cfg in reg.items():
            for n in names_of(key, cfg):
                if len(n) >= 3 and (q in n or n in q):
                    return key, cfg
    return None
