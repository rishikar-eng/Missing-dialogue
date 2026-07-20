"""Content-based (timeline) verification of the name→track mapping.

Names alone can be wrong: mislabelled stems (a lead delivered as ``Actor.wav``),
ambiguous generics (``Agent`` vs ``AGENT SANGA`` vs ``Agent B``), spelling/case
variants (``JJilip`` vs ``Jilip``). Because each track is ONE isolated voice, the
moments it actually contains speech must line up with the timecodes the script
assigns to that character. We cross-tabulate VAD speech against each character's
scripted line intervals:

    precision = overlap / track_speech_time    -> "is this track that character?"
    recall    = overlap / character_line_time  -> "does this track cover them?"

The name-based mapping stays authoritative for tracks it matches (it's right for
most, and content precision is noisy on this material because the stems carry a lot
of un-scripted vocalisation). Content is used *additively*:

  * RESCUE  — a character the name step left unmapped is given a still-free track
              whose voice timeline covers their lines (fixes "no audio" caused by a
              naming error, e.g. a mislabelled stem).
  * FLAG    — where a track's voice timeline best matches a *different* character
              than its filename suggests, we surface it for the QC team instead of
              silently trusting or silently overriding the name.

Nothing here removes a name-based mapping; it only fills gaps and reports conflicts.
"""

from __future__ import annotations

import re
from typing import Any

from .characters import CharacterEntity, _name_score

Interval = tuple[float, float]

# --- tuning (validated against Gavv E29/E30; see HANDOFF) --------------------- #
RESCUE_RECALL = 0.30   # a free track must cover >=30% of an unmapped char's lines to claim them
RESCUE_PREC = 0.25     # ...and be specific enough (a chunk of its speech must be their lines,
                       #    which rules out promiscuous crowd/walla tracks that cover many chars)
# Auto-map a rescue ONLY above this precision. Below it a busy crowd/walla track can
# cover a small character's few lines by coincidence — auto-mapping would silently
# suppress a real "no audio" finding. Weaker candidates are surfaced as a
# possible_match for a human to confirm by ear, and the character stays unmapped.
RESCUE_STRONG_PREC = 0.50
PROMISCUOUS_RECALL = 0.30   # a track "covers" a character at this recall...
PROMISCUOUS_MIN_CHARS = 3   # ...and covering this many characters marks it walla-like
FLAG_PREC = 0.35       # a track whose best-matching character reaches this precision...
FLAG_MARGIN = 0.15     # ...and beats the name-mapped character by this margin is flagged
VERIFIED_ABSENT_RECALL = 0.15  # below this best-recall, a character's audio is "verified absent"

# Group stems (WALLA / GIRL CROWD / LADY BIT …) can bundle several small parts into one
# track. A bit-part with no dedicated track whose lines fall inside a genuinely SHARED
# stem is DELIVERED (bundled), not missing — flagging it "No audio" is a false alarm. We
# label it 'grouped' instead.
#
# Whether a group-NAMED track is really a shared bundle is decided by CONTENT, not the
# name: if one character dominates the track's speech (high owner precision) it's really
# THAT character's dedicated stem — even when generically named 'LADY BIT 02' (which on
# Gavv E29 is 100% one bit-part) — so its owner is mapped normally. Only a track no
# single character dominates is a "real group stem". This is what keeps a solo track
# named 'MARIA VOX'/'BG SINGER'/'BORTAN (CROWD SCENE)' with its true owner instead of
# tearing it off (false "No audio") or letting it absorb others (false "grouped").
#
# Still heuristic (VAD can't PROVE a voice is in a shared stem, only that the part's line
# windows fall on its speech), so 'grouped' also requires a genuinely small part with
# strong coverage and stays a reviewable bucket (with a listen sample), never silent.
GROUP_COVER_RECALL = 0.50     # a shared stem must cover >=50% of the part's line-time
GROUP_OWNER_PREC = 0.50       # a group-NAMED track one char dominates above this is really their solo stem
BITPART_MAX_LINES = 6         # only small parts qualify for 'grouped' (protects real leads)...
BITPART_MAX_SPEECH_S = 30.0   # ...by BOTH line count and total speech (a 6-line monologue is not a bit-part)
# Group-stem name tokens. Includes plurals/synonyms/abbreviations seen in delivery
# folders. Safe to be liberal: a solo track that merely carries one of these tokens is
# rejected downstream because it's the confident name-match of a real-named character.
_GROUP_TOKENS = {"walla", "wallas", "crowd", "crowds", "mob", "mobs", "group", "groups",
                 "bit", "bits", "ensemble", "background", "bg", "ambience", "ambient",
                 "chatter", "gang", "gangs", "kids", "villager", "villagers", "students",
                 "guests", "passengers", "patrons", "reactions", "vox", "grp"}
# Unambiguous stem words to also catch when the name has no separators ('WALLA01').
_GROUP_SUBSTR = ("walla", "crowd", "chatter", "ensemble", "ambience")


def _is_group_stem(channel: str) -> bool:
    """True for tracks that bundle many small parts (WALLA, GIRL CROWD, LADY BIT…).
    Token-based (so 'Rabbit'/'Bituin' don't match on 'bit'), plus a substring pass for
    a few unambiguous stem words so separator-less names ('WALLA01') still match.
    NOTE: this is a NAME test only — callers additionally require the track NOT to be the
    confident dedicated track of a real-named character before treating it as a bundle."""
    low = channel.lower()
    if any(t in _GROUP_TOKENS for t in re.split(r"[^a-z0-9]+", low)):
        return True
    squashed = re.sub(r"[^a-z0-9]+", "", low)
    return any(w in squashed for w in _GROUP_SUBSTR)


def _looks_like_bitpart(ent: CharacterEntity) -> bool:
    """A genuinely SMALL part — safe to treat as 'grouped' when a group stem covers it.
    Gated on BOTH line count and total speech so a role-named lead ('Narrator' with
    many/long lines) is never mistaken for a bit-part and silenced. (An earlier version
    also short-circuited to True for any all-role-word name regardless of size — that
    let a role-named lead be grouped away; removed.)"""
    return ent.line_count <= BITPART_MAX_LINES and ent.total_speech_s <= BITPART_MAX_SPEECH_S




def _total(iv: list[Interval]) -> float:
    return sum(b - a for a, b in iv)


def _overlap_seconds(a: list[Interval], b: list[Interval]) -> float:
    """Total overlapping seconds between two lists of intervals (merge scan)."""
    if not a or not b:
        return 0.0
    a = sorted(a)
    b = sorted(b)
    i = j = 0
    ov = 0.0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            ov += hi - lo
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return ov


def _sample_window(spans: list[Interval], regions: list[Interval]) -> Interval | None:
    """Pick one representative [start, end] to play as evidence: the character's line
    that most overlaps the track's speech (so it's audible). If nothing overlaps —
    e.g. a track labelled for a character it doesn't actually contain — return their
    longest line so the reviewer hears the *absence* at that cue."""
    if not spans:
        return None
    best_ov, best_span = -1.0, None
    for s in spans:
        ov = _overlap_seconds([s], regions)
        if ov > best_ov:
            best_ov, best_span = ov, s
    if best_ov <= 0:  # track is silent at all of this character's cues
        return max(spans, key=lambda s: s[1] - s[0])
    return best_span


def pair_scores(
    spans_by_char: dict[str, list[Interval]],
    track_regions: dict[str, list[Interval]],
) -> dict[tuple[str, str], dict[str, float]]:
    """precision/recall/overlap for every (channel, character_id) pair."""
    char_total = {c: _total(iv) for c, iv in spans_by_char.items()}
    out: dict[tuple[str, str], dict[str, float]] = {}
    for ch, regions in track_regions.items():
        speech = _total(regions)
        for char, iv in spans_by_char.items():
            ov = _overlap_seconds(regions, iv)
            out[(ch, char)] = {
                "overlap": round(ov, 2),
                "precision": round(ov / speech, 3) if speech else 0.0,
                "recall": round(ov / char_total[char], 3) if char_total[char] else 0.0,
            }
    return out


def verify_mapping(
    characters: list[CharacterEntity],
    channel_names: list[str],
    name_mapping: dict[str, str],
    spans_by_char: dict[str, list[Interval]],
    track_regions: dict[str, list[Interval]],
) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    """Augment `name_mapping` with content evidence.

    Returns (mapping, mapped_by, issues):
      mapping    — {char_id: channel} (name matches + content rescues)
      mapped_by  — {char_id: "name" | "content"} for mapped characters
      issues     — list of {kind, ...} diagnostics for the UI/report:
                     kind="name_mismatch"   track labelled X but voice matches Y
                     kind="rescued"         unmapped char recovered by voice timeline
                     kind="possible_match"  weak candidate — NOT mapped, human confirms
                     kind="grouped"         bit-part with no own track, delivered inside a
                                            group stem (walla/crowd) — expected, not missing
                     kind="verified_absent" char with no track AND no voice match anywhere
    """
    ent_by_id = {e.id: e for e in characters}
    scores = pair_scores(spans_by_char, track_regions)

    mapping = dict(name_mapping)
    mapped_by = {cid: "name" for cid in mapping}
    issues: list[dict[str, Any]] = []

    # Characters that actually have scripted lines (others are irrelevant to QC).
    speaking = [e for e in characters if spans_by_char.get(e.id)]

    # How many characters does each track substantially cover? (Used only for the
    # rescue crowd/walla note.)
    def n_covered(ch: str) -> int:
        return sum(
            1 for e in speaking
            if scores.get((ch, e.id), {}).get("recall", 0.0) >= PROMISCUOUS_RECALL
        )

    # Owner precision: the largest fraction of a track's speech that belongs to any ONE
    # character. A dedicated stem (even a generically-named one like 'LADY BIT 02') is
    # dominated by its owner (high); a genuinely shared crowd/walla bundle is not (low).
    # This CONTENT signal — not the track name — decides whether a group-NAMED track is
    # really a shared bundle, so a solo track named 'MARIA VOX'/'BG SINGER' stays its
    # owner's and a thin bit-stem 'LADY BIT 02' gets mapped to its one bit-part.
    _owner_prec: dict[str, float] = {}
    def owner_prec(ch: str) -> float:
        if ch not in _owner_prec:
            _owner_prec[ch] = max(
                (scores.get((ch, e.id), {}).get("precision", 0.0) for e in speaking),
                default=0.0,
            )
        return _owner_prec[ch]

    def _is_real_group_stem(ch: str) -> bool:
        """Named like a bundle AND dominated by no single character — a genuinely shared
        stem. A group-named track one char owns (high owner precision) is that char's
        dedicated stem, NOT a bundle, so it is mapped to them instead."""
        return _is_group_stem(ch) and owner_prec(ch) < GROUP_OWNER_PREC

    # 0) DEMOTE — a character name-matched to a genuinely shared group stem they do NOT
    # dominate ('Girl' -> 'GIRL CROWD 03') was absorbed by name only, not delivered a
    # dedicated track. Unmap so content rescue / the GROUPED pass re-handle them. A real
    # solo track (its owner dominates it) is never a real group stem, so it is KEPT —
    # this is what prevents tearing 'MARIA VOX'/'BORTAN (CROWD SCENE)' off its true owner.
    for cid, ch in list(mapping.items()):
        if _is_real_group_stem(ch):
            del mapping[cid]
            mapped_by.pop(cid, None)

    # 0.5) REASSIGN — a track name-matched to a WEAK claimant whose voice overwhelmingly
    # belongs to a DIFFERENT, still-unmapped character is handed to that dominant speaker.
    # The name match can't see who actually speaks, so a 1-line namesake ('Sachika') or a
    # generic word can win the track of the real 25-line lead ('Amane'), leaving the lead
    # falsely "No audio". Content proves ownership, so here we let it TAKE THE TRACK BACK
    # (the old code only FLAGGED this and trusted the name). Conservative: only when the
    # other character clearly OWNS the track (high precision), COVERS their lines (recall),
    # decisively beats the current holder, and isn't a smaller part than them. Runs BEFORE
    # rescue so the displaced claimant is re-handled (rescued elsewhere or reported absent).
    for cid, ch in list(mapping.items()):
        mapped_prec = scores.get((ch, cid), {}).get("precision", 0.0)
        best_cid, best_prec = None, 0.0
        for e in speaking:
            p = scores.get((ch, e.id), {}).get("precision", 0.0)
            if p > best_prec:
                best_cid, best_prec = e.id, p
        if not best_cid or best_cid == cid or best_cid in mapping:
            continue
        best_rec = scores.get((ch, best_cid), {}).get("recall", 0.0)
        dominant, claimant = ent_by_id.get(best_cid), ent_by_id.get(cid)
        if not (dominant and claimant):
            continue
        if (
            best_prec >= RESCUE_STRONG_PREC                    # the track is really theirs
            and best_rec >= RESCUE_RECALL                      # ...and carries their lines
            and best_prec - mapped_prec >= FLAG_MARGIN         # clearly beats the name-holder
            and dominant.line_count >= claimant.line_count     # and isn't a smaller part
        ):
            del mapping[cid]
            mapped_by.pop(cid, None)
            mapping[best_cid] = ch
            mapped_by[best_cid] = "content"
            issues.append({
                "kind": "reassigned", "channel": ch,
                "character": best_cid, "character_name": dominant.name,
                "from_character": cid, "from_character_name": claimant.name,
                "precision": round(best_prec, 3), "recall": round(best_rec, 3),
                "message": f"Track '{ch}' was name-matched to '{claimant.name}' "
                           f"({claimant.line_count} line{'s' if claimant.line_count != 1 else ''}), "
                           f"but its voice is '{dominant.name}'s — {best_prec:.0%} of the track is "
                           f"their speech, covering {best_rec:.0%} of their {dominant.line_count} "
                           f"lines. Reassigned to '{dominant.name}'.",
            })

    # 1) RESCUE — unmapped speaking characters, best free track by recall. A group-NAMED
    # track a bit-part actually dominates ('LADY BIT 02' = only Agent) IS a valid rescue
    # target; only genuinely shared bundles are skipped (the GROUPED pass handles those).
    taken = set(mapping.values())
    unmapped = [e for e in speaking if e.id not in mapping]
    possible_ids: set[str] = set()   # chars surfaced as possible_match (kept out of 'grouped')
    # Greedy by best available recall so the strongest matches claim tracks first.
    def best_free(cid: str) -> tuple[str | None, float, float]:
        best = (None, 0.0, 0.0)
        for ch in channel_names:
            if ch in taken or _is_real_group_stem(ch):
                continue
            s = scores.get((ch, cid))
            if s and s["recall"] > best[1]:
                best = (ch, s["recall"], s["precision"])
        return best

    # Sort rescues by their best recall, strongest first.
    ranked = sorted(unmapped, key=lambda e: best_free(e.id)[1], reverse=True)
    for e in ranked:
        ch, rec, prec = best_free(e.id)
        if not (ch and rec >= RESCUE_RECALL and prec >= RESCUE_PREC):
            continue
        win = _sample_window(spans_by_char.get(e.id, []), track_regions.get(ch, []))
        samples = []
        if win:
            samples.append({
                "label": f"'{ch}' at {e.name}'s line", "channel": ch,
                "start_s": round(win[0], 3), "end_s": round(win[1], 3),
            })
        promiscuous = n_covered(ch) >= PROMISCUOUS_MIN_CHARS
        if prec >= RESCUE_STRONG_PREC:
            # Strong: most of the track's speech IS this character's lines.
            mapping[e.id] = ch
            mapped_by[e.id] = "content"
            taken.add(ch)
            issues.append({
                "kind": "rescued", "character": e.id, "character_name": e.name,
                "channel": ch, "recall": rec, "precision": prec, "samples": samples,
                "message": f"'{e.name}' had no name-matched track; recovered '{ch}' "
                           f"by voice timeline (covers {rec:.0%} of their lines).",
            })
        else:
            # Weak: plausible but not proof — a busy track can cover a small part
            # by chance. Do NOT map (the character stays in the no-audio list);
            # surface the candidate for a human to confirm by ear.
            possible_ids.add(e.id)
            crowd = (f" '{ch}' also covers {n_covered(ch) - 1} other characters' lines "
                     f"(likely a crowd/walla track)." if promiscuous else "")
            issues.append({
                "kind": "possible_match", "character": e.id, "character_name": e.name,
                "channel": ch, "recall": rec, "precision": prec, "samples": samples,
                "message": f"'{e.name}' has no track, but '{ch}' MIGHT contain their "
                           f"lines (covers {rec:.0%}, match confidence {prec:.0%} — too "
                           f"low to auto-assign).{crowd} Listen to confirm; still "
                           f"counted as no-audio below.",
            })

    # 1.5) GROUPED — a genuinely SMALL part with no dedicated track whose lines fall
    # inside a real group stem (walla/crowd/bit) is DELIVERED (bundled), not missing.
    # Requirements (all must hold, so we don't silence real missing dialogue):
    #   * bit-part (few lines AND little speech) — a role-named LEAD never qualifies;
    #   * the stem is a genuinely shared bundle — NO single character dominates it
    #     (owner precision < GROUP_OWNER_PREC); a stem one char owns was mapped above;
    #   * STRONG coverage (>=GROUP_COVER_RECALL of the part's line-time), not a weak
    #     coincidental overlap with a continuously-chattering track;
    #   * not already surfaced as a possible_match (a plausible dedicated track wins).
    # Heuristic by nature (VAD can't PROVE the voice is in the bundle), so it stays a
    # reviewable bucket with a listen sample — never a silent "clean".
    group_channels = [ch for ch in channel_names if _is_real_group_stem(ch)]
    grouped_ids: set[str] = set()
    grouped_channel: dict[str, str] = {}
    for e in speaking:
        if e.id in mapping or e.id in possible_ids or not _looks_like_bitpart(e):
            continue
        best_ch, best_rec, best_prec = None, 0.0, 0.0
        for ch in group_channels:
            s = scores.get((ch, e.id))
            if s and s["recall"] > best_rec:
                best_ch, best_rec, best_prec = ch, s["recall"], s["precision"]
        if not best_ch or best_rec < GROUP_COVER_RECALL:
            continue
        grouped_ids.add(e.id)
        grouped_channel[e.id] = best_ch
        win = _sample_window(spans_by_char.get(e.id, []), track_regions.get(best_ch, []))
        samples = []
        if win:
            samples.append({
                "label": f"'{best_ch}' at {e.name}'s line", "channel": best_ch,
                "start_s": round(win[0], 3), "end_s": round(win[1], 3),
            })
        plural = "" if e.line_count == 1 else "s"
        issues.append({
            "kind": "grouped", "character": e.id, "character_name": e.name,
            "channel": best_ch, "recall": best_rec, "precision": best_prec, "samples": samples,
            "message": f"'{e.name}' ({e.line_count} line{plural}) has no dedicated track; their "
                       f"lines fall inside the group stem '{best_ch}' (walla/crowd/bit). Normal "
                       f"for small parts — treated as delivered, not missing. Listen to confirm.",
        })

    # 2) FLAG — a name-mapped track whose voice best matches a different character.
    for cid, ch in list(name_mapping.items()):
        mapped_prec = scores.get((ch, cid), {}).get("precision", 0.0)
        # Who does this track's voice look most like?
        best_cid, best_prec = None, 0.0
        for e in speaking:
            p = scores.get((ch, e.id), {}).get("precision", 0.0)
            if p > best_prec:
                best_cid, best_prec = e.id, p
        if (
            best_cid and best_cid != cid
            and best_prec >= FLAG_PREC
            and best_prec - mapped_prec >= FLAG_MARGIN
        ):
            other = ent_by_id.get(best_cid)
            other_name = other.name if other else best_cid
            name_sim = _name_score(ch, ent_by_id[cid]) if cid in ent_by_id else 0.0
            # Two evidence clips on the SAME track: at the voice-matched speaker's cue
            # (should have the voice) and at the labelled speaker's cue (likely silent).
            voice_win = _sample_window(spans_by_char.get(best_cid, []), track_regions.get(ch, []))
            labelled_win = _sample_window(spans_by_char.get(cid, []), track_regions.get(ch, []))
            samples = []
            if voice_win:
                samples.append({
                    "label": f"'{ch}' at {other_name}'s line (voice match)", "channel": ch,
                    "start_s": round(voice_win[0], 3), "end_s": round(voice_win[1], 3),
                })
            if labelled_win:
                samples.append({
                    "label": f"'{ch}' at {ent_by_id[cid].name}'s line (as labelled)", "channel": ch,
                    "start_s": round(labelled_win[0], 3), "end_s": round(labelled_win[1], 3),
                })
            issues.append({
                "kind": "name_mismatch", "channel": ch,
                "labelled_character": cid, "labelled_character_name": ent_by_id[cid].name,
                "voice_character": best_cid, "voice_character_name": other_name,
                "labelled_precision": round(mapped_prec, 3), "voice_precision": round(best_prec, 3),
                "name_similarity": round(name_sim, 2), "samples": samples,
                "message": f"Track '{ch}' is mapped to '{ent_by_id[cid].name}' by name, but its "
                           f"voice timeline matches '{other_name}' more "
                           f"closely ({best_prec:.0%} vs {mapped_prec:.0%}). Check the labelling.",
            })

    # 3) VERIFIED ABSENT — still-unmapped speaking chars: is their voice anywhere?
    # (grouped bit-parts are accounted for above — don't also call them absent.)
    for e in speaking:
        if e.id in mapping or e.id in grouped_ids:
            continue
        best_rec = max((scores.get((ch, e.id), {}).get("recall", 0.0) for ch in channel_names), default=0.0)
        if best_rec < VERIFIED_ABSENT_RECALL:
            issues.append({
                "kind": "verified_absent", "character": e.id, "character_name": e.name,
                "best_recall": round(best_rec, 3),
                "message": f"'{e.name}' has no matching track and no other track's voice covers "
                           f"their lines (best {best_rec:.0%}) — audio genuinely not delivered.",
            })

    return mapping, mapped_by, issues
