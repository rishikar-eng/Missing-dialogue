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
import re
from pathlib import Path
from typing import Any

from .characters import _ROLE_WORDS, CharacterEntity, _name_score, _squash

_BANK_PATH = Path(__file__).resolve().parent / "data" / "voice_bank.json"
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


def _load_bank() -> list[dict[str, Any]]:
    global _bank
    if _bank is None:
        try:
            _bank = json.loads(_BANK_PATH.read_text(encoding="utf-8"))
        except Exception:
            _bank = []  # bank missing/corrupt -> feature quietly off
    return _bank


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
        best_score, best_entry = 0.0, None
        for entry in bank:
            s = max(_name_score(entry["character"], ent),
                    _channel_score(entry["character"], ent.channel))
            if s > best_score:
                best_score, best_entry = s, entry
        matched = best_entry if (best_entry is not None and best_score >= threshold) else None
        ent.voice_match_score = round(best_score, 3) if matched else None
        ent.voices = matched["voices"] if matched else None
        # Record WHICH bank row matched. The bank and the character PICTURES come from
        # the same studio sheet, so this fuzzy result ('Shoma' -> 'SHOUMA') is exactly
        # the key the workbook needs to find the picture — re-deriving it from the
        # script name with an exact match silently finds nothing.
        ent.voice_bank_name = matched["character"] if matched else None
