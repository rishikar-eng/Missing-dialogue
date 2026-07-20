# Character-to-track mapping — how it works, what it broke, how we fixed it

Written 2026-07-20. Captures the accuracy work triggered by an Audacity spot-check of EP 40,
where lines that were clearly delivered showed up as "missing" / "no audio". Two layers were
involved — the cross-track delivery check and the character→track *mapping* — and the mapping
turned out to be the real weak spot. Files: `backend/characters.py`, `backend/content_map.py`,
`backend/alignment.py`, `backend/excel_report.py`.

---

## 1. What the mapper does & how it works

QC needs to know **which audio file belongs to which script character** before it can check a
character's lines. That match is made in **two steps**:

**Step 1 — NAME match (text only, never listens).** `backend/characters.py`.
- Character **names** come from the script (each speaker label → a slug: lowercased, brackets
  stripped, punctuation flattened). Two labels merge into one character **only if their slugs
  are exactly equal** — there's no nickname/family-name logic.
- Track **names** come from the **audio filenames** (`EP40_Sachike_Amane.wav` → track
  `Sachike Amane`).
- `_name_score(track, character)` scores each pair on text: exact = 1.0, containment = 0.85,
  else a fuzzy character-overlap ratio. Studio affix words (`stem`, `vo`, `stomach`, `kaijin`…)
  are dropped first. Assignment is **greedy and one-track-per-character**, accepted at ≥ 0.55.

**Step 2 — CONTENT check (uses the VAD timeline).** `backend/content_map.py::verify_mapping`.
- For every (track, character) it overlaps *when the track actually speaks* (VAD) with the
  character's scripted line times → **precision** (is this track that character?) and
  **recall** (does it cover their lines?).
- It then **rescues** unmapped characters onto free tracks, **flags** name/voice conflicts,
  **demotes** genuine crowd/walla stems, and marks tiny bit-parts **grouped**.

---

## 2. What it led to (the bugs, verified on EP 40 in Audacity)

The original design rule was *"the name match is authoritative; content only fills gaps"* —
Step 2 could **flag** a wrong match but never **overturn** it. Combined with a loose text rule,
that let a weak claimant win a track and never give it back:

**(a) Telugu — a generic word stole a lead's track.**
`_name_score` granted the 0.85 "containment" bonus on the **squashed blob**, ignoring word
boundaries. The generic `Man` (→ `man`) sat *inside* `Sachike Am**an**e` (→ `sachikeamane`), so
`Man` scored 0.85 and grabbed the `Sachike Amane` track. Cascade:
- `Man`'s real `MAN` track was left **orphaned** (nobody mapped to it),
- the 25-line lead `Amane` was left **"no audio"**,
- and `Man`'s line *"Eh?! Mr. Kisara vanished?!"* — actually delivered on the `MAN` track —
  was reported **MISSING**.

**(b) Marathi — a 1-line namesake kept the lead's track.**
`Amane`, `Sachika`, and `Sachika Amane` produce different slugs, so they were **split into
three characters**. The `Sachike` track name-matched the **1-line** `Sachika` and locked in;
the **25-line** `Amane` was left **"no audio"**. Content *knew* the truth (the track's voice is
overwhelmingly Amane's) but Step 2 only **flagged** it — it couldn't take the track back.

**The common thread:** a weak claimant (a generic word, or a 1-line namesake) wins a track in
Step 1, and Step 2 was built to *trust* Step 1. One bad match cascaded into a false MISSING, a
false "no audio", an orphaned track, and a misleading "delivered by" attribution.

---

## 3. How we fixed it

**Fix 1 — token-aware name match** (`characters.py::_name_score`).
Containment is now judged on **whole tokens**, not raw substrings: `Man` ⊄ {`sachike`,`amane`}
so it drops to ~0.2 (below 0.55) and can't steal the track, while `Amane` ⊆ {`sachike`,`amane`}
still scores 0.85 and `Man` still matches a standalone `MAN`/`Random Man` track. A guarded
contiguous-substring fallback (score 0.75, only when the shorter side is ≥ 4 chars) keeps
glued/no-separator names like `ShomaStomachKaijin` working without reviving `man`-in-`amane`.
Near-spellings (`Sachika`↔`Sachike`) still match via the fuzzy ratio.

**Fix 2 — content can REASSIGN, not just flag** (`content_map.py`, new step 0.5).
When a track name-matched to a weak/few-line claimant is **overwhelmingly owned** by a
different, still-unmapped character (high precision + recall, decisively beats the name-holder,
and isn't a smaller part), the track is **handed to that dominant speaker**. Runs *before*
rescue so the displaced claimant is re-handled. This is what rescues the 25-line `Amane` in
Marathi. Conservative guards; emits a `reassigned` note for the report.

**Fix 3 — merge fragmented aliases** (`Amane`/`Sachika`/`Sachika Amane` → one character):
**held.** It's the riskiest (over-merging genuinely distinct characters), and Fix 2 already
resolves the functional problem. The only residual it would clean up is a **1-line** namesake
still showing "no audio". Candidate approaches for later: the studio roster (`char_roster.json`)
or token-subset merging.

**Review follow-ups** (found by an adversarial review of Fixes 1 & 2):
- The FLAG pass iterated the *original* name mapping, so it re-emitted a stale, contradictory
  "check labelling" issue for every track REASSIGN/DEMOTE had already moved → now skips tracks
  whose name-holder no longer holds them.
- The frontend had no branch for the new `reassigned` kind and rendered it as "no audio" (its
  opposite) → added a `REASSIGNED` branch (Excel was already correct via the generic message).

---

## 4. Result (EP 40, validated)

| Case | Before | After |
|---|---|---|
| Telugu `Man` | → `Sachike Amane` (wrong) | → **`MAN`** ✅ |
| Telugu `Amane` (25 lines) | "no audio" | → **`Sachike Amane`** ✅ |
| Telugu line *"Eh?! Mr. Kisara vanished?!"* | MISSING (false) | **delivered** (no finding) ✅ |
| Marathi `Amane` (25 lines) | "no audio" | → **`Sachike`** (reassigned by content) ✅ |

A useful side effect: once `Amane` is properly mapped, its lines are actually *checked* — Telugu
now shows **2 genuine missing** Amane lines instead of one uncheckable "no audio" blob. That's
the accuracy improving, not regressing.

---

## 5. Related: the cross-track MISMATCH fix (same session)

The mapping bugs surfaced while fixing a separate, related accuracy issue: a line silent in a
character's **own** track but **delivered on another speaker's** track used to be double-reported
as MISSING (that character) + EXTRA (the other). It's now one finding — **MISMATCH**, naming the
delivering track (`alignment.py`). On EP 40 this reclassified the bulk of former "missing" lines
(e.g. 22 of 23 in Malayalam) as wrong-speaker deliveries. See the commit history for details.

---

## 6. Known residuals / future direction

- **1-line namesakes still "no audio"** (`Sachika` after Amane is reassigned) — needs Fix 3
  (alias merge), held for safety.
- **Continuous-neighbour MISMATCH** — a neighbour speaking through a genuinely dropped line can
  occasionally over-flag MISMATCH (it's a *warn*, and the line still enters the ref-audio, so
  nothing is lost); tighten the coverage threshold if reviews show noise.
- **Roster-driven matching** — feeding the studio character list (canonical name + aliases +
  voice-name convention) into both the alias merge and the name match would fix fragmentation
  and abbreviations at the source, more reliably than heuristics.
