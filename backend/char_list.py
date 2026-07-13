"""Apply the studio character list (backend/data/char_roster.json, built from
'GAV character list.docx' by backend/tools/build_char_roster.py) as a *mapping aid*.

The problem it solves: the SCRIPT names a speaker tersely ('Hanto', 'Glotta') while
the delivered TRACK is named verbosely or differently ('HANTO KARAKIDA.wav'). We
fuzzy-match the two, and when they diverge a character gets a false 'No audio' or
maps to the wrong stem. The roster bridges them — for each script character we find
its roster entry and lend the entity the roster's canonical name, aliases and the
studio voice-name convention as EXTRA match targets. The existing name mapper then
matches the track against those too, so correct tracks get claimed that terse-name
fuzzy matching alone would miss.

Additive and safe: it only ADDS candidate match terms. It never removes a mapping,
never changes VAD/timing/loudness, and a character with no roster hit is untouched.
Absent/corrupt roster -> feature quietly off.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .characters import _ROLE_WORDS, CharacterEntity, _name_score

_ROSTER_PATH = Path(__file__).resolve().parent / "data" / "char_roster.json"

# Real matches separate cleanly from noise: on the Gavv data, true matches score
# 0.85+ ('Shoma'~'Shouma' 0.91, 'Hanto'~'Hanto Karakida' 0.85) while shared-role-word
# false hits ('Rakia'~'Shouma', 'Granute man'~'Random man') top out at ~0.67 — so a
# 0.8 floor keeps the real ones and drops the noise. Generics need near-exact.
_MATCH_THRESHOLD = 0.8
_GENERIC_THRESHOLD = 0.92
# A short script name can be a substring of two DIFFERENT roster characters ('Hanto'
# is inside both 'Hanto Karakida' and 'Writer Hanto's mentor', both ~0.85). When the
# runner-up (a different underlying character) is this close to the best, the match is
# ambiguous — lending the wrong canonical name/voice-name would mislead. Skip it.
_AMBIGUOUS_MARGIN = 0.06

_roster: list[dict[str, Any]] | None = None


def _load_roster() -> list[dict[str, Any]]:
    global _roster
    if _roster is None:
        try:
            _roster = json.loads(_ROSTER_PATH.read_text(encoding="utf-8"))
        except Exception:
            _roster = []  # missing/corrupt -> feature off
    return _roster


def _is_generic(name: str) -> bool:
    """True when every word is a role word / tiny token — not a real identity."""
    import re
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return bool(tokens) and all(t in _ROLE_WORDS or len(t) <= 2 or t.isdigit() for t in tokens)


def _entry_score(entry: dict[str, Any], ent: CharacterEntity) -> float:
    """Best similarity between a roster entry (its aliases + voice-name) and a
    script character. Reuses the same fuzzy scorer the track mapper uses, so affix
    handling is identical."""
    cands = list(entry.get("aliases") or [])
    if entry.get("voice_name"):
        cands.append(entry["voice_name"])
    return max((_name_score(c, ent) for c in cands), default=0.0)


def _base(entry: dict[str, Any]) -> str:
    """The role-stripped identity of a roster entry (last alias) — so 'Shouma' and
    'Shouma as hero' count as the SAME character, but 'Hanto Karakida' and 'Writer
    Hanto's mentor' count as different ones when checking for ambiguity."""
    aliases = entry.get("aliases") or [entry.get("name", "")]
    return aliases[-1].strip().lower()


def apply_char_list(characters: list[CharacterEntity]) -> int:
    """For each script character, attach its best roster match: lend the roster's
    name/aliases/voice-name to entity.match_terms (extra track-match targets) and
    record roster_name/roster_voice_name for display. Returns how many matched.

    Call BEFORE map_characters_to_channels so the extra terms feed the mapping.
    """
    roster = _load_roster()
    if not roster:
        return 0
    matched = 0
    for ent in characters:
        threshold = _GENERIC_THRESHOLD if _is_generic(ent.name) else _MATCH_THRESHOLD
        scored = sorted(((_entry_score(e, ent), e) for e in roster),
                        key=lambda t: t[0], reverse=True)
        best_score, best = scored[0] if scored else (0.0, None)
        if best is None or best_score < threshold:
            continue
        # Ambiguity guard: reject when a DIFFERENT roster character scores nearly as
        # high — we can't tell which one this is, so lending either's name would lie.
        if any(s >= best_score - _AMBIGUOUS_MARGIN and _base(e) != _base(best)
               for s, e in scored[1:]):
            continue
        terms: list[str] = list(best.get("aliases") or [])
        if best.get("voice_name"):
            terms.append(best["voice_name"])
        # De-dupe against what the entity already carries.
        have = {t.lower() for t in [ent.name, ent.id, *ent.aliases]}
        ent.match_terms = [t for t in terms if t.lower() not in have]
        ent.roster_name = best.get("name")
        ent.roster_voice_name = best.get("voice_name")
        matched += 1
    return matched
