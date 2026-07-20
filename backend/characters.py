"""Character entities derived from a parsed script.

Deliverable 3 of the alignment service: collapse the many raw labels a script
uses for one performer (``Shoma``, ``Shoma[gavv]``, ``Shoma [narration]``) into a
single, editable entity with aliases, line counts and total scripted duration.
These entities become the speakers that voices get assigned to.

Also provides best-effort mapping of each character to one of the per-speaker
audio channels (channels mode), by name first and audio-content (VAD overlap) as
the tie-breaker / confirmation.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .script_parser import ScriptDoc

# Studio suffixes / generic role words that aren't part of a character's identity.
# NB: "granute" is deliberately NOT here — it's a franchise family name (Granute Man,
# Granute Woman are distinct characters); stripping it collapsed both tracks to
# "man"/"woman" and mis-mapped them (e.g. Granute Man -> generic MAN track).
_AFFIX = {"stomach", "inc", "kaijin", "monster", "voice", "vo", "dub",
          "stem", "track", "ch", "channel", "16k", "src", "onscreen", "screen"}
_ROLE_WORDS = {"actor", "actress", "acter", "man", "woman", "boy", "girl", "agent",
               "narrator", "narration", "guard", "soldier", "male", "female", "crowd"}


class CharacterEntity(BaseModel):
    id: str                       # canonical key, e.g. "shoma"
    name: str                     # display name
    aliases: list[str]            # raw labels that collapsed into this entity
    line_count: int
    total_speech_s: float         # summed scripted duration
    first_start_s: float
    channel: str | None = None    # mapped audio-channel name (channels mode)
    mapped_by: str | None = None  # "name" | "content" — how the channel was assigned
    grouped_in: str | None = None # bit-part delivered inside this group stem (walla/crowd);
                                  # set => "grouped/expected", NOT counted as "No audio"
    # Extra fuzzy-match targets lent by the studio character list (roster) — canonical
    # name, aliases and voice-name convention. NOT from the script; used only to help
    # match the delivered track. See backend/char_list.py.
    match_terms: list[str] = []
    roster_name: str | None = None        # canonical name from the character list
    roster_voice_name: str | None = None  # studio voice/track-name convention
    level_dbfs: float | None = None      # median speech loudness on the mapped track (dBFS)
    level_min_dbfs: float | None = None   # quietest delivered line (dBFS)
    level_max_dbfs: float | None = None   # loudest delivered line (dBFS)
    voice_id: str | None = None   # assigned bank voice (legacy single-voice field)
    # The voice-bank row this character fuzzily matched ('Shoma' -> 'SHOUMA'). Set by
    # voices.attach_voices; the QC workbook uses it to look up the character's picture,
    # since bank + pictures come from the same studio sheet and are spelled its way.
    voice_bank_name: str | None = None
    # ElevenLabs voices across dub languages, from the production voice bank:
    # [{lang: "hi"|"ta"|..., name, id, form: "normal"|"granute"}]
    voices: list[dict[str, Any]] | None = None


def build_characters(doc: ScriptDoc) -> list[CharacterEntity]:
    """Collapse script segments into one entity per canonical character key."""
    aliases: dict[str, set[str]] = defaultdict(set)
    lines: dict[str, int] = defaultdict(int)
    dur: dict[str, float] = defaultdict(float)
    first: dict[str, float] = {}
    display: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for seg in doc.segments:
        for key in seg.characters:
            aliases[key].add(seg.character_raw)
            lines[key] += 1
            dur[key] += seg.duration_s
            first[key] = min(first.get(key, seg.start_s), seg.start_s)
            # Track a clean display candidate (the raw label minus tags), most-common wins.
            clean = re.sub(r"\[.*?\]|\(.*?\)", "", seg.character_raw).strip()
            if "/" not in clean and clean:
                display[key][clean] += 1

    out: list[CharacterEntity] = []
    for key in sorted(aliases, key=lambda k: (-dur[k], k)):
        cand = display.get(key) or {}
        name = max(cand, key=cand.get) if cand else key.replace("_", " ").title()
        out.append(CharacterEntity(
            id=key, name=name, aliases=sorted(aliases[key]),
            line_count=lines[key], total_speech_s=round(dur[key], 2),
            first_start_s=round(first[key], 3),
        ))
    return out


# --------------------------------------------------------------------------- #
# Channel mapping
# --------------------------------------------------------------------------- #
def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _name_score(channel_name: str, entity: CharacterEntity) -> float:
    """Fuzzy name similarity between a channel filename and a character (+aliases),
    ignoring studio affixes/role words.

    Containment is judged on whole TOKENS, not raw substrings: a short name like 'Man'
    matches a standalone 'MAN' track (or 'Random Man') but NOT the middle of a different
    name ('Sachike AMANe' — the letters m-a-n sit inside 'Amane'). The old raw-substring
    rule gave that false 0.85 and let 'Man' steal the 'Sachike Amane' track. A genuine
    near-spelling (e.g. 'Sachika' vs 'Sachike') still scores via the fuzzy ratio below."""
    ch_tokens = [t for t in re.split(r"[^a-z0-9]+", channel_name.lower()) if t and t not in _AFFIX]
    ch_set = set(ch_tokens)
    ch = "".join(ch_tokens)
    best = 0.0
    # Include roster-lent match terms (canonical name / voice-name convention) so a
    # track named the studio's way still matches a tersely-scripted character.
    for label in [entity.name, entity.id, *entity.aliases, *getattr(entity, "match_terms", [])]:
        lab_set = {t for t in re.split(r"[^a-z0-9]+", label.lower()) if t}
        b = _squash(label)
        if not ch or not b:
            continue
        if ch == b:
            return 1.0
        # whole-token containment (one name's tokens are all tokens of the other)
        if lab_set and ch_set and (lab_set <= ch_set or ch_set <= lab_set):
            best = max(best, 0.85)
        # Contiguous whole-name containment as a fallback for GLUED names where the affix
        # words aren't separated ('ShomaStomachKaijin' -> no 'shoma' token). Length floor of
        # 4 on the shorter side keeps a short generic word ('man' inside 'amane') from
        # re-triggering the false match this whole change was made to stop.
        elif (ch in b or b in ch) and len(min(ch, b, key=len)) >= 4:
            best = max(best, 0.75)
        best = max(best, difflib.SequenceMatcher(None, ch, b).ratio())
    return best


def map_characters_to_channels(
    characters: list[CharacterEntity],
    channel_names: list[str],
    content_scores: dict[tuple[str, str], float] | None = None,
    name_threshold: float = 0.55,
) -> dict[str, str]:
    """Assign each character a channel. Name match first; if a content-overlap
    table is supplied (precision per (channel, character)), it confirms ambiguous
    name matches and rescues role-labelled stems (Actor->Shoma).

    Returns {character_id: channel_name}. Each channel used at most once.
    """
    pairs = []
    for ent in characters:
        for ch in channel_names:
            name = _name_score(ch, ent)
            content = (content_scores or {}).get((ch, ent.id), 0.0)
            # Content is authoritative when available; name carries the rest.
            score = max(content, 0.0) * 0.7 + name * 0.3 if content_scores else name
            pairs.append((score, name, content, ent.id, ch))

    pairs.sort(reverse=True)
    taken_ch: set[str] = set()
    taken_ent: set[str] = set()
    mapping: dict[str, str] = {}
    for score, name, content, ent_id, ch in pairs:
        if ent_id in taken_ent or ch in taken_ch:
            continue
        ok = (content >= 0.5) if content_scores else (name >= name_threshold)
        if not ok:
            continue
        mapping[ent_id] = ch
        taken_ent.add(ent_id)
        taken_ch.add(ch)
    return mapping
