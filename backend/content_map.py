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
                     kind="verified_absent" char with no track AND no voice match anywhere
    """
    ent_by_id = {e.id: e for e in characters}
    scores = pair_scores(spans_by_char, track_regions)

    mapping = dict(name_mapping)
    mapped_by = {cid: "name" for cid in mapping}
    issues: list[dict[str, Any]] = []

    # Characters that actually have scripted lines (others are irrelevant to QC).
    speaking = [e for e in characters if spans_by_char.get(e.id)]

    # 1) RESCUE — unmapped speaking characters, best free track by recall.
    taken = set(mapping.values())
    unmapped = [e for e in speaking if e.id not in mapping]
    # Greedy by best available recall so the strongest matches claim tracks first.
    def best_free(cid: str) -> tuple[str | None, float, float]:
        best = (None, 0.0, 0.0)
        for ch in channel_names:
            if ch in taken:
                continue
            s = scores.get((ch, cid))
            if s and s["recall"] > best[1]:
                best = (ch, s["recall"], s["precision"])
        return best

    # How many characters does each track substantially cover? Crowd/walla stems
    # overlap many characters' lines, so their "matches" are weak evidence.
    def n_covered(ch: str) -> int:
        return sum(
            1 for e in speaking
            if scores.get((ch, e.id), {}).get("recall", 0.0) >= PROMISCUOUS_RECALL
        )

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
    for e in speaking:
        if e.id in mapping:
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
