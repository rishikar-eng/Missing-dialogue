# EP 43 (Marathi) — QC discrepancy report

**TL;DR:** QC flagged **28 missing lines** in the EP 43 Marathi delivery. On investigation, only **~9 are genuinely missing**. The rest trace back to **how the speaker stems are named and packaged**, not to undubbed dialogue. The audio itself is largely fine — the delivery format is what breaks speaker matching (for our tool *and* for any human sorting the files). Audio proof for every claim is in the attached clips (`01–08_*.wav`, timecodes in `cue_sheet.txt`).

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

## What is genuinely missing (~9 lines, all short reactions)

Absent from **every** delivered track:

| Timecode | Character | Line |
|---|---|---|
| 00:06:44 | Masaru | "Yes." |
| 00:06:49 | Masaru | "Thank you." |
| 00:08:56 | Masaru | "I figured." |
| 00:12:30 | Shoma | "Eh?" |
| 00:16:27 | Shoma | "Eh?" |
| 00:16:41 | Masaru | "I'm home!" |
| 00:17:37 | Masaru | "What's wrong?" |
| 00:18:52 | Shoma | "Yes?" |
| 00:23:42 | Shoma | "Yep." |

(Plus the 52 lines of the four characters with no stems, if those were never recorded.)

## Ask — delivery convention going forward

1. **One speaker per file, one file per speaker** (pickups appended to the same file, not delivered as `X 02`).
2. Filename carries **exactly one** speaker name — never a second character's name, nickname, or relationship ("Shoma's Uncle").
3. **Same spelling as the script**, consistent across episodes and languages.
4. Every speaking character in the script gets a stem (or is explicitly listed as not recorded).

---

*Confidence note: the detection itself is ear-verified — e.g. EP 41 Telugu was checked line-by-line in Audacity and **every** flagged missing line was genuinely missing. EP 43's inflated count is a packaging artifact, which is exactly why the delivery convention matters.*
