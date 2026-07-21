# EP 43 (Marathi) — QC discrepancy report

**TL;DR:** QC initially flagged **28 missing lines** in the EP 43 Marathi delivery. On investigation, **every one of them traces back to how the speaker stems are named and packaged — zero lines are actually missing** from the tracks that were delivered. After teaching the QC tool to see through the packaging (re-matching the ambiguous filenames and joining the split Bocha tracks), the episode re-scores at **0 missing**. The real, outstanding gap is the **four characters with no stem at all** (52 scripted lines, table below). Audio proof for every packaging claim is in the attached clips (`01–08_*.wav`, timecodes in `cue_sheet.txt`).

---

## What the delivery looks like

10 stem files for an episode whose script has 10+ speaking characters:

```
Bocca Human form_01.wav        Jaldak minion 1_01.wav     Rakia_Vraam_Laage9_01.wav
Bocca Jaldak_01.wav            Jaldak minion 2_01.wav     Shoma_01.wav
Hanto_01.wav                   Male Agent_01.wav          Sweets Shopkeeper_Inoi Masaru_Shoma's Uncle_01.wav
Nyelv Stomach_01.wav
```

## The four discrepancies

| # | Issue | Evidence | Impact |
|---|-------|----------|--------|
| 1 | **Ambiguous multi-name filename.** Masaru's track is named `Sweets Shopkeeper_Inoi Masaru_Shoma's Uncle_01` — it contains **another character's name** ("Shoma"). The recording itself is correct (clips 01–03 = Masaru's voice; clip 04 = Shoma correctly on `Shoma_01`). | clips 01–04 | Speaker matching latches onto "Shoma" in the wrong file → the two leads' line-checks cross-wire → dozens of false flags |
| 2 | **One character split across two tracks, spelled differently.** Bocha is delivered as `Bocca Human form_01` **and** `Bocca Jaldak_01` (script spells him *Bocha*). His monster-form scenes live only in the second file. | clips 05–07 | **19 of the 28 "missing" lines** — they exist, in the second track |
| 3 | **Bundle track.** `Rakia_Vraam_Laage9_01` is one file named for three parts. | clip 08 | Only one character can be verified against it; the others' lines can't be attributed |
| 4 | **Four characters have no stem at all:** Lizel (**28 lines**), Amane (15), Michiru (5), Jiib (4). | no clip — nothing to play | 52 scripted lines cannot be QC'd or delivered; largest single gap in the episode |

## What is genuinely missing

**From the delivered tracks: nothing.** Once the packaging issues above are seen through
(the ambiguous Masaru/Shoma filenames re-matched, Bocha's two form tracks joined), all
28 originally-flagged lines are found, delivered, on the correct voices — the episode
re-scores at **0 missing lines**. (An earlier draft listed ~9 short reaction lines as
missing; those were an artifact of the mis-matching itself — with the tracks correctly
paired they are all present.)

**The real gap:** the **52 scripted lines of the four characters with no stem**
(issue 4 above). If those were recorded, the files were never delivered; if not,
they need a session:

| Character | Scripted lines |
|---|---|
| Lizel | 28 |
| Amane | 15 |
| Michiru | 5 |
| Jiib | 4 |

## Ask — delivery convention going forward

1. **One speaker per file, one file per speaker** (pickups appended to the same file, not delivered as `X 02`).
2. Filename carries **exactly one** speaker name — never a second character's name, nickname, or relationship ("Shoma's Uncle").
3. **Same spelling as the script**, consistent across episodes and languages.
4. Every speaking character in the script gets a stem (or is explicitly listed as not recorded).

---

*Confidence note: the detection itself is ear-verified — e.g. EP 41 Telugu was checked line-by-line in Audacity and **every** flagged missing line was genuinely missing. EP 43's inflated count is a packaging artifact, which is exactly why the delivery convention matters.*
