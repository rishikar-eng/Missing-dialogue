"""Attach ElevenLabs voice IDs (per language) to script characters.

The voice bank (backend/data/voice_bank.json, generated from the production
'KAMEN RIDER CHARACTER LIST & VOICES.xlsx' by backend/tools/build_voice_bank.py)
maps each show character to their ElevenLabs voices across dub languages
(hi/ta/te/ml/mr/bn/kn) plus separate Granute-form voices.

Bank names are spelled the studio's way ('SHOUMA', 'HANTO  KARAKEDE'); script
names ours ('Shoma', 'Hanto') — so matching reuses the same fuzzy name scorer the
track mapping uses. Purely informational: no effect on detection.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Any

from .characters import _ROLE_WORDS, CharacterEntity, _name_score, _squash

# Committed snapshot (offline fallback) + on-disk cache of the last live Box fetch.
_BANK_PATH = Path(__file__).resolve().parent / "data" / "voice_bank.json"
_CACHE_PATH = Path(os.environ.get("DQC_DATA_ROOT", os.environ.get("TMPDIR") or "/tmp")) / "voice_bank_cache.json"
# Bump when build_voice_bank.parse's OUTPUT shape changes, so an etag-unchanged sheet is
# still re-parsed (the cache is keyed by file etag, which can't see a parser change).
_PARSER_VERSION = 2
# Fuzzy floor for real names ('Shoma'~'SHOUMA'). GENERIC script names ('Man',
# 'Girl', 'Team guy B') would substring-match into unrelated bank rows
# ('HOUND MAN'), so they only attach on an essentially exact match.
_MATCH_THRESHOLD = 0.8
_EXACT_THRESHOLD = 0.98
_GENERIC_WORDS = _ROLE_WORDS | {"customer", "lady", "kid", "child", "team", "guy",
                                "random", "granute", "waitress", "store", "shopkeeper"}


def _is_generic(name: str) -> bool:
    """True when every word is a role word / tiny token — not a real identity."""
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return bool(tokens) and all(
        t in _GENERIC_WORDS or len(t) <= 2 or t.isdigit() for t in tokens
    )

_bank: list[dict[str, Any]] | None = None
_bank_etag: str | None = None      # Box etag of the loaded sheet (skips re-download)
_bank_fid: str | None = None       # Box file id of the loaded sheet


def _read_cache() -> tuple[list | None, str | None, str | None]:
    try:
        d = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if d.get("pv") != _PARSER_VERSION:   # parser changed -> cache is stale
            return None, None, None
        return d.get("bank"), d.get("etag"), d.get("file_id")
    except Exception:
        return None, None, None


def _load_bank() -> list[dict[str, Any]]:
    """The active voice bank: a live Box fetch (refresh_from_box) if one ran this process,
    else the on-disk cache from a previous fetch, else the committed snapshot."""
    global _bank, _bank_etag, _bank_fid
    if _bank is None:
        cached, etag, fid = _read_cache()
        if cached is not None:
            _bank, _bank_etag, _bank_fid = cached, etag, fid
        else:
            try:
                _bank = json.loads(_BANK_PATH.read_text(encoding="utf-8"))
            except Exception:
                _bank = []  # bank missing/corrupt -> feature quietly off
    return _bank


def refresh_from_box(token: str, file_id: str, name: str | None = None) -> str:
    """Refresh the voice bank from the studio's Box sheet so the report reflects the CURRENT
    list, not a committed snapshot. Cheap: an etag metadata check skips the (large, ~90 MB)
    download whenever the sheet is unchanged. NEVER raises — on any failure the existing bank
    stays in use. Returns a short human status (surfaced in the run's progress)."""
    global _bank, _bank_etag, _bank_fid
    import shutil
    import tempfile

    import httpx

    from . import box_fetch
    file_id = str(file_id)
    try:
        r = httpx.get(f"https://api.box.com/2.0/files/{file_id}", params={"fields": "etag,name"},
                      headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        etag = str(r.json().get("etag"))
    except Exception as e:  # metadata unreachable -> keep whatever bank we have
        _load_bank()
        return f"kept cached voice list ({str(e)[:40]})"

    if _bank is not None and _bank_fid == file_id and _bank_etag == etag:
        return "voice list current"
    cached, cetag, cfid = _read_cache()
    if cached is not None and cfid == file_id and cetag == etag:
        _bank, _bank_etag, _bank_fid = cached, etag, file_id
        return f"voice list from cache ({len(cached)} characters)"

    d = tempfile.mkdtemp()
    try:
        from .tools.build_voice_bank import parse as _parse   # lazy: pulls openpyxl
        p = box_fetch.download_file(token, file_id, d, name=name or "voices.xlsx")
        bank = _parse(str(p))
    except Exception as e:  # download/parse failed -> keep existing bank
        _load_bank()
        return f"voice list fetch failed, kept cached ({str(e)[:40]})"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if not bank:
        _load_bank()
        return "voice list parsed empty, kept cached"

    _bank, _bank_etag, _bank_fid = bank, etag, file_id
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"file_id": file_id, "etag": etag, "pv": _PARSER_VERSION,
                                   "bank": bank}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception:
        pass
    return f"voice list refreshed from Box ({len(bank)} characters)"


def duplicate_voice_ids() -> dict[str, list[str]]:
    """ElevenLabs voice IDs assigned to MORE THAN ONE character in the bank -> the list
    of those characters. A shared id means two characters would speak in the same voice —
    a copy-paste error in the studio sheet (e.g. Glotta & Nyelv share a Tamil id). The
    workbook's voice-ID check flags any delivered audio whose id is in here. Language and
    normal/Granute form are ignored: one id must belong to exactly one character."""
    id_to_chars: dict[str, set[str]] = {}
    for entry in _load_bank():
        for v in entry.get("voices", []):
            if v.get("id"):
                id_to_chars.setdefault(v["id"], set()).add(entry["character"])
    return {i: sorted(cs) for i, cs in id_to_chars.items() if len(cs) > 1}


def _channel_score(bank_name: str, channel: str | None) -> float:
    """The bank often spells characters the way the AUDIO TRACKS do ('Jilip
    Stomach') rather than the script ('Jiib') — so the mapped channel name is a
    second identity signal. Call attach_voices AFTER track mapping to use it."""
    if not channel:
        return 0.0
    a, b = _squash(bank_name), _squash(channel)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return difflib.SequenceMatcher(None, a, b).ratio()


def attach_voices(characters: list[CharacterEntity]) -> None:
    """Set entity.voices = [{lang, name, id, form}] for the best-matching bank
    entry (None when nothing matches). Bit-part bank rows ('Random lady - ep 10')
    rarely match script names — that's fine; the section shows what we're sure of."""
    bank = _load_bank()
    if not bank:
        return
    for ent in characters:
        threshold = _EXACT_THRESHOLD if _is_generic(ent.name) else _MATCH_THRESHOLD
        best_score, best_entry, best_has_ids = 0.0, None, False
        for entry in bank:
            s = max(_name_score(entry["character"], ent),
                    _channel_score(entry["character"], ent.channel))
            has_ids = any(v.get("id") for v in entry["voices"])
            # Highest score wins; on a tie prefer the row that actually carries a voice id
            # (the bank now also holds listed-but-unvoiced rows, e.g. 'Yoshida ma'am').
            if s > best_score + 1e-9 or (abs(s - best_score) <= 1e-9 and has_ids and not best_has_ids):
                best_score, best_entry, best_has_ids = s, entry, has_ids
        matched = best_entry if (best_entry is not None and best_score >= threshold) else None
        ent.voice_match_score = round(best_score, 3) if matched else None
        ent.voices = matched["voices"] if matched else None
        # Record WHICH bank row matched. The bank and the character PICTURES come from
        # the same studio sheet, so this fuzzy result ('Shoma' -> 'SHOUMA') is exactly
        # the key the workbook needs to find the picture — re-deriving it from the
        # script name with an exact match silently finds nothing.
        ent.voice_bank_name = matched["character"] if matched else None
