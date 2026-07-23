"""Locate a series+episode's assets in Box from the series registry — the QC engine's
availability layer. Given a series config (from series_registry) and an episode number, it
finds the English script, the original-language audio, the character list, and each dub
language's per-speaker stems, and reports what's present vs not delivered.

This is the reusable core behind the agent's `check_availability` tool and the
`/api/agent/availability` endpoint. Discovery is by naming convention over the registry's
folder ids — no per-episode paths are hardcoded, so a new episode (or a new series, once
its folders are in the registry) needs no code change. Ported and generalised from the
batch runner's find_* helpers.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from . import box_fetch

AUDIO_EXT = (".wav", ".flac", ".ogg", ".aif", ".aiff")

# A dub against a non-English script is meaningless, so an English-folder file carrying a
# language tag (e.g. 'Gavv_#07_..._HINDI.DOCX') is NOT treated as the QC script.
_DEFAULT_NON_EN = ("HINDI", "MALAYALAM", "TAMIL", "TELUGU", "KANNADA", "BENGALI", "MARATHI")


def _sq(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def ep_of(name: str) -> int | None:
    """Episode number from Box's naming styles: 'Gavv_#01_SCLA', 'S1_E40'. '#NN' wins,
    else an uppercase-E-number (so 'Rider' won't match)."""
    m = re.search(r"#(\d+)", name)
    if m:
        return int(m.group(1))
    m = re.search(r"(?<![A-Za-z0-9])E(\d+)", name)
    return int(m.group(1)) if m else None


def _ep_num(name: str) -> int | None:
    """Episode number embedded in a folder name: 'EP 40', 'MAL_GAVV_EPI 40_FOR AI'."""
    m = re.search(r"EP(?:ISODE|I)?\s*[-_]?\s*0*(\d+)", name, re.I)
    return int(m.group(1)) if m else None


def _is_range(name: str) -> bool:
    return bool(re.search(r"\d+\s*(?:to|[-–—])\s*\d+", name))


def _is_helper(name: str) -> bool:
    low = name.lower()
    return ("missing" in low or "renamed" in low or "client" in low
            or re.search(r"out\b", low) is not None)


class _Box:
    """One-Box-listing-per-folder cache for a single availability check (folders get
    listed more than once — e.g. a voiceover root and its range subfolder)."""

    def __init__(self, token: str) -> None:
        self.token = token
        self._c: dict[str, dict[str, Any]] = {}

    def listing(self, folder_id: str) -> dict[str, Any]:
        if folder_id not in self._c:
            self._c[folder_id] = box_fetch.list_folder(self.token, folder_id)
        return self._c[folder_id]


# --------------------------------------------------------------------------- #
# per-asset discovery
# --------------------------------------------------------------------------- #
def find_script(box: _Box, cfg: dict[str, Any], n: int) -> dict[str, Any] | None:
    folder = cfg["box"].get("scripts_folder")
    if not folder:
        return None
    non_en = tuple(t.upper() for t in cfg.get("script_non_en_tags", _DEFAULT_NON_EN))
    cands = [f for f in box.listing(folder)["files"]
             if f["name"].lower().endswith(".docx") and ep_of(f["name"]) == n
             and not any(tag in f["name"].upper() for tag in non_en)]
    cands.sort(key=lambda f: len(f["name"]))     # simplest name wins on ties
    return cands[0] if cands else None


def find_original(box: _Box, cfg: dict[str, Any], n: int) -> dict[str, Any] | None:
    folder = cfg["box"].get("premix_folder")
    if not folder:
        return None
    # 'premix' after squashing separators — later eps ship '..._PRE MIX.wav' (a space).
    cands = [f for f in box.listing(folder)["files"]
             if f["name"].lower().endswith(".wav") and "premix" in _sq(f["name"])
             and ep_of(f["name"]) == n]
    cands.sort(key=lambda f: len(f["name"]))
    return cands[0] if cands else None


def find_char_list(box: _Box, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The series' character-list file. Either a direct file_id, or a name search over
    one or more folders. Not episode-specific (one list per series)."""
    spec = cfg["box"].get("char_list") or {}
    if spec.get("file_id"):
        try:
            fid = str(spec["file_id"])
            # trust the id; surface a name if we can, but don't fail availability if not
            return {"id": fid, "name": spec.get("name", "character list")}
        except Exception:
            return None
    terms = [t.lower() for t in spec.get("name_contains", [])]
    if not terms:
        return None
    for folder in spec.get("search_folders", []):
        try:
            files = box.listing(str(folder))["files"]
        except Exception:
            continue
        for f in files:
            low = f["name"].lower()
            if any(t in low for t in terms):
                return f
    return None


def find_stems(box: _Box, cfg: dict[str, Any], lang: str, n: int) -> dict[str, Any] | None:
    """The per-speaker stem folder for one language+episode. Handles both direct
    per-episode folders and range folders ('EPI 41 to 45' -> 'GAVV EPI 42 ...')."""
    root = (cfg["box"].get("voiceover") or {}).get(lang)
    if not root:
        return None
    folders = box.listing(root)["folders"]
    direct = [f for f in folders
              if not _is_range(f["name"]) and not _is_helper(f["name"]) and _ep_num(f["name"]) == n]
    if direct:
        direct.sort(key=lambda f: len(f["name"]))
        return direct[0]
    for f in folders:
        rng = re.search(r"(\d+)\s*(?:to|[-–—])\s*(\d+)", f["name"])
        if rng and int(rng.group(1)) <= n <= int(rng.group(2)):
            subs = [g for g in box.listing(f["id"])["folders"]
                    if not _is_helper(g["name"]) and _ep_num(g["name"]) == n]
            if subs:
                subs.sort(key=lambda g: len(g["name"]))
                return subs[0]
    return None


def _track_count(box: _Box, folder_id: str) -> int:
    return sum(1 for f in box.listing(folder_id)["files"]
               if f["name"].lower().endswith(AUDIO_EXT))


# --------------------------------------------------------------------------- #
# the availability report
# --------------------------------------------------------------------------- #
def check_episode(token: str, key: str, cfg: dict[str, Any], n: int) -> dict[str, Any]:
    """Structured presence report for one series+episode: script, original audio, character
    list, and each dub language (present + track count). `token` is a Box access token."""
    box = _Box(token)

    def asset(x: dict[str, Any] | None) -> dict[str, Any]:
        return {"present": True, "name": x["name"], "id": x["id"]} if x else {"present": False}

    script = find_script(box, cfg, n)
    original = find_original(box, cfg, n)
    char_list = find_char_list(box, cfg)

    languages: dict[str, dict[str, Any]] = {}
    ready: list[str] = []
    missing: list[str] = []
    for lang in cfg.get("languages", []):
        st = find_stems(box, cfg, lang, n)
        if st:
            cnt = _track_count(box, st["id"])
            languages[lang] = {"present": cnt > 0, "tracks": cnt, "folder": st["name"]}
            (ready if cnt > 0 else missing).append(lang)
        else:
            languages[lang] = {"present": False}
            missing.append(lang)

    return {
        "series": cfg.get("display_name", key),
        "series_key": key,
        "episode": n,
        "script": asset(script),
        "original": asset(original),
        "char_list": asset(char_list),
        "languages": languages,
        "summary": {
            "languages_ready": len(ready),
            "languages_total": len(cfg.get("languages", [])),
            "ready": ready,
            "not_delivered": missing,
            "script_ok": bool(script),
            "original_ok": bool(original),
            "runnable": bool(script) and len(ready) > 0,
        },
    }
