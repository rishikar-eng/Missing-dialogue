# Scriptless / Audio-Only QC — Findings & Current State

**Status:** POC complete, route chosen (SONAR), **not yet integrated into the product.**
**Last updated:** 2026-07-27

---

## 1. Why this exists

The script-based QC (Kamen Rider) is the *easy* case: the studio hands us a character-labelled
script **and** one clean audio track per character. **Most studios don't.** They ship a single
**mixed audio file** — dialogue, music and effects baked together — with no script, no
per-character stems, no premix. Audio-only QC is the real target of the tool.

The question audio-only QC has to answer: **"is any dialogue missing from this dub?"**

## 2. Why the old approach plateaued

The original scriptless mode compared **voice-activity timelines** (Silero VAD) between the
original and the dub. It's fast, free and offline — a good candidate generator — but it is
**content-blind**: it knows *someone spoke here*, never *what was said*.

That creates one unavoidable ambiguity. Original has speech at 00:12, dub is silent at 00:12:

* the line was **dropped** (a real QC failure), or
* the line **moved** — a translated line is a different length, so timing drifts a second or two.

VAD cannot tell these apart, so it flagged both, and reports filled with timing noise that a
human had to clear by hand in Audacity. Background music made it worse (music reads as speech).

## 3. What we built and proved

Pipeline (all of it CPU, no GPU anywhere):

```
mixed audio ──1 SEPARATE──> dialogue only ──2 SEGMENT──> speech windows
                (Demucs)                      (Silero VAD)
                                                   │
                        ┌──────────────────────────┴───────────────────────┐
                   3a ROUTE A: transcribe (Whisper)     3b ROUTE B: embed the SPEECH
                      then match text (LaBSE)              directly (Meta SONAR)
                        └──────────────────────────┬───────────────────────┘
                                                   ▼
                                    4 ALIGN + CLASSIFY (align.py)
                          OK · MISALIGNED (present, retimed) · MISSING · EXTRA
```

**Separation is non-negotiable and it works.** Demucs pulled clean dialogue out of a
music-heavy mix; everything downstream depends on it.

## 4. The bake-off — SONAR won

Tested on real material: **SUITS S9 Ep1**, Hindi vs Tamil full mixes (60 s clips), plus a
**synthetic drop** (one line deliberately silenced) as the recall test.

| Route | Drop test — *must* catch it | Complete pair — *should* say nothing |
|---|---|---|
| **SONAR** (speech → meaning, no transcription) | **catches it** ✅ | **0 false flags** ✅ |
| Groq Whisper-large-v3 + LaBSE | catches it ✅ | 3 false flags ❌ |

**Why SONAR wins:** it compares the *sound* against a shared cross-lingual meaning space, so it
never inherits transcription errors. Whisper garbles Indian languages on short or ambiguous
utterances — and *every* remaining Groq false positive traced to exactly that. SONAR also needs
**no API key** and the **audio never leaves our infrastructure**.

Groq is still worth keeping as a cheap second opinion (and it's fast — 2–4 s per clip).

## 5. The bug that mattered most (do not regress this)

The detector twice reported **"all clear" on audio with a line deliberately removed** — a false
negative, the worst possible error in QC. Neither model was at fault; the *matching logic* was.

* **Cause 1 — many-to-one matching.** Matching each reference line to its best-scoring dub line
  independently let several reference lines claim the *same* dub line, so a dropped line simply
  borrowed its neighbour's match. Fixed by **global sequence alignment** (Needleman–Wunsch):
  dialogue order is preserved in a dub, so each dub segment is consumed at most once and a
  reference line left with no partner *is* a MISSING.
* **Cause 2 — the 2-ref→1-dub merge.** Collapsing two reference lines onto one dub segment is
  *literally the shape a dropped line makes*. A duration guard was not enough (same-language
  embeddings score ~1.0 broadly, so both halves cleared any threshold). It is now **disabled by
  default** (`DQC_ALLOW_2TO1=1` re-enables). In QC a false positive costs a reviewer 30 seconds;
  a missed drop ships broken audio.
* **Also required — adaptive thresholds.** Same-language matches score ~1.0 while cross-language
  ones sit at 0.4–0.7, so one fixed cutoff either hides drops or flags good lines. The threshold
  now calibrates off the observed score distribution.

**Lesson for whoever continues this:** always test with a *deliberately damaged* file. Testing
only on complete pairs makes a broken detector look perfect.

## 6. Infrastructure

* **SONAR runs on AWS Fargate** — task def `dialogue-qc-sonar` (8 vCPU / **48 GB RAM**),
  image `dialogue-qc-sonar` in ECR. On-demand, scales to zero, pennies per run.
* **Why 48 GB:** each SONAR speech encoder checkpoint is **8.6 GB**, and fairseq2 loads it by
  reading the whole file into memory and copying it into a buffer — a transient ~17–26 GB spike,
  per language. This is why it OOM'd on the 4 GB EC2 box and at 16 GB. Not a code bug; model size.
* Gotchas that cost time: torch/torchaudio builds must match exactly; fairseq2 needs its **CPU**
  variant (`--extra-index-url https://fair.pkg.atmeta.com/fairseq2/whl/pt2.9.1/cpu`), otherwise
  its native lib demands CUDA libraries that don't exist on a CPU box.

## 7. Where the code is

POC (not yet in the product): `C:\Users\Rishi\pocenv\`
* `audio_qc_poc.py` — Route A (Demucs → Whisper/Groq → LaBSE → align)
* `sonar_qc_poc.py` — Route B (Demucs → VAD → SONAR → align) *(also on EC2 `/home/ubuntu/sonar_build/`)*
* `align.py` — the alignment + classification (shared by both routes)

## 8. What's left before this ships

1. **Bake the model weights into the container** — each run currently re-downloads 8.6 GB.
   Biggest single speed win.
2. **Run a full episode** (validated so far on 60-second clips).
3. **Test source-vs-its-own-dub** — the real use case. Dub-vs-dub (two independent translations)
   is the hardest possible case, and we already pass it.
4. **Calibrate thresholds** on a hand-labelled episode; report a confidence per flag.
5. **Wire it into Teams** as an "audio-only" mode alongside the script-based one.

## 9. Honest limitations

* It detects **missing/moved dialogue**, not *wrong* dialogue — a correctly-timed but incorrectly
  translated line still passes. That needs a semantic check on the text, not just presence.
* Separation quality on Indian-language cinema mixes is validated on one clip, not broadly.
* It reduces human review, it doesn't remove it: review shifts from "sift 30 timing-noise flags"
  to "confirm 3 genuine drops". A human still signs off for broadcast.
