# Scriptless (audio-only) QC — how the pipeline actually works

Traced from the code on 2026-08-10 and verified claim-by-claim against it: 108 agents read
the pipeline, and **26 of their statements were wrong on first pass and corrected** before
landing here. Where something is still uncertain this file says so rather than guessing.

The question this document exists to answer precisely: **for each combination of original
and dub source, which side gets Demucs separation?**

---

## 1. The separation matrix (the short answer)

Two decisions, made in different places, and only one of them is about the original.

```python
# backend/audio_qc.py, compare()
if AQC_NO_SEP == "1":  ov = _load16(original_path)          # experiment only
else:                  ov = separate_dialogue(original_path)   # ORIGINAL: Demucs

if dub_is_clean:       dv = _load16(dub_path)               # NO separation
else:                  dv = separate_dialogue(dub_path)     # DUB: Demucs
```

| dub source | `_stage` | `--clean-dub` | ORIGINAL | DUB |
|---|---|---|---|---|
| full mix (ST_MIX) | `stmix` / `mix` | no | **Demucs** | **Demucs** |
| premix | `premix` | no | **Demucs** | **Demucs** |
| clean DIALOGUE track | `dialogue` | **yes** | **Demucs** | raw, no separation |
| per-character speaker tracks | `speakers` | **yes** | **Demucs** | raw, no separation |

So **both sides are separated only when the dub is a mix or a premix.**

* The original's source form is irrelevant — a full mix, a premix and a wav extracted from
  an episode video are all normalised to a local path first, then separated identically.
* `separate_dialogue()` returns **only the htdemucs `vocals` stem**, mono, resampled to
  exactly 16 kHz. The accompaniment (the sum of the non-vocal stems) is computed and cached
  but discarded by this function.
* Speaker tracks are never separated at any point: the runner sums them with `_load16()`
  into one dialogue-only wav *before* `compare()`, and `--clean-dub` then skips Demucs too.
* `--clean-dub` is appended in exactly one place, `backend/audio_jobs.py:370`, and only for
  `_stage in ("dialogue", "speakers")`.

### The consequence that matters most

When the dub is a DIALOGUE track or speaker tracks, the two sides are **different kinds of
signal**:

* original = a Demucs *vocals* stem, which by definition contains **singing**
* dub = a dialogue-only delivery, which by construction **cannot** contain the song vocal

Every sung line therefore has no possible counterpart and is flagged MISSING, on every
episode, regardless of whether the song was dubbed. Measured on POA EP11: the original's
vocals sit at **−31.5 dBFS** through the opening theme, the delivered dialogue stem at
**−180 dBFS** (absolute digital black), while both are healthy during dialogue.

This is a structural property of the source pairing, not a tuning problem.

### `AQC_NO_SEP`

Read in three places, **set by none**, and not included in the Fargate task's environment
list. Whether it is set in production depends on the ECS task definition
(`dialogue-qc-sonar:3`), which lives outside this repo — **not determinable from the code.**
The dub side has no equivalent flag; `AQC_NO_SEP` is not consulted there.

---

## 2. Choosing the original

`launch()` takes a hand-picked `original_file` if given (stage `CUSTOM`); otherwise it walks
a fixed chain, and the first hit wins:

1. `find_original_mix` → `ST_MIX` — reads `original_mix_folder`, wants `stmix` and not `premix`
2. `find_original_premix` → `ST_PREMIX` — reads `premix_folders` (**plural**)
3. `find_original_video` → `VIDEO` — reads `original_videos_folder`, `.mp4/.mov/.mkv/.m4v`
4. `box_discovery.find_original` → also labelled `ST_PREMIX` — reads `premix_folder` (**singular**)

> **Wart.** Steps 2 and 4 read *differently named keys* and report the *same* stage label, so
> `original_stage` cannot tell you which folder the file came from. Kamen Rider Gavv is the
> only series with `premix_folder`, so its premix is only ever reachable through step 4.

> **Wart.** `availability()` re-implements this same chain by hand instead of sharing a
> helper, so the card and the launcher can drift apart.

A video original is converted by ffmpeg to mono 44.1 kHz wav (`-vn -ac 1 -ar 44100`) and it
is that wav — not the video — that everything downstream sees.

---

## 3. Choosing the dub

The stage preference list is built exactly three ways:

* `dub_prefer` verbatim if present (**overrides `dub_stage` entirely**)
* else `["premix", "stmix"]` if `dub_stage == "premix"`
* else the default `["stmix", "premix"]`

Each key is matched as a **substring of the squashed filename** (lowercased, non-alphanumerics
stripped). `mix` is special-cased to also require that `premix` is absent.

> **Wart.** The comment says the `mix` key must not swallow `stmix`, but the code only
> excludes `premix` — `ST_MIX` squashes to a string containing `mix`, so it *is* swallowed.

> **Wart.** Chikoo's non-premix dubs are named `*_Tam_MIX.wav` (no "ST"), and its stage list
> is `["premix","stmix"]`. An episode delivered with only a MIX and no PREMIX is reported as
> **not delivered**, because `stmix` is not a substring of `…tammixwav`.

**Speaker tracks** are consulted only when `find_dub_mix` returns nothing. A folder qualifies
with ≥3 audio files, and is refused unless the file sizes are within 10% of each other — the
guard added after EP22's malformed 148 s–3376 s set fabricated a dub.

**Hand-picked dubs** get their stage from *one substring test on the Box display name*:
`"dialogue" in squashed_name` → `_stage="dialogue"`, otherwise no `_stage` at all.

> **Wart.** That name comes from a best-effort HTTP call whose failure path returns the
> placeholder `box:<id>`, which contains no "dialogue". A network blip or stale token
> silently downgrades a clean DIALOGUE pick into a full Demucs run — re-introducing exactly
> the 8–11 wasted minutes the classification was written to prevent, with no log line.

---

## 4. Per-series configuration

| | Mowgli | Chikoo | POA | Kamen Rider Gavv |
|---|---|---|---|---|
| original | premix only (`premix_folders`, 2 monthly ids) | premix only (3 folders) | **episode video** only | **ST_MIX** (`original_mix_folder`) |
| original lang | hindi | hindi | portuguese | hindi |
| dub stage | `dub_stage: premix` | `dub_stage: premix` | `dub_prefer: [dialogue, premix, mix]` | *neither* → default `[stmix, premix]` |
| dub separated? | yes | yes | **no** (dialogue/speakers) | yes |
| speaker tracks | — | — | `dub_speaker_root` | — |
| engine | *(inherits server)* | *(inherits server)* | `scribe` | `scribe` |
| languages | Tamil, Telugu | Tamil, Telugu | English | 6 |

* `mixes_folders` values are **lists** for Mowgli/Chikoo/POA but a bare **string** for Gavv;
  both consumers normalise inline rather than the schema being fixed.
* `dub_speaker_folders` (a per-language speaker map) is supported by code and used by **no**
  series — dead configuration surface.
* `me_stems_folder` (Gavv) is read by **no code anywhere**. Its own note describes it as "a
  candidate route to exact dialogue by subtraction" — aspiration stored as config.

---

## 5. What a finished run produces

At most **five** objects under `s3://$DQC_S3_BUCKET/<prefix>/`, all gated on `--out`:

| file | conditional? |
|---|---|
| `report.json` | always |
| `<Series>_EP<NN>_<Lang>_AudioQC-Report_<date>.xlsx` | always |
| `…_AudioQC-Report_<date>_ProTools-Markers.mid` | always |
| `<Series>_EP<NN>_<Lang>_Missing-Lines-Only.flac` | only if ≥1 MISSING flag |
| `…_Missing-Lines-On-Timeline.flac` | only if ≥1 MISSING flag |

Delivered to the studio purely as **presigned S3 URLs** matched by file extension (and the
substring `Timeline` to split the two FLACs). Nothing is written back to Box.

> **Wart.** The workbook header always states the mode as *"original full mix vs dub full
> mix"*. For POA — video original, dialogue-only or speaker-summed dub — every word of that
> line is false.

> **Wart.** The reference FLACs are cut from the original file, but on a fully cached run
> (where the source was never downloaded) they are cut from the cached 16 kHz dialogue stem.
> Identical filename, materially different audio and sample rate, decided by an invisible
> cache hit.

> **Wart.** A clean episode still ships a MIDI file containing only a track name and an EOT,
> and Teams still shows a "Pro Tools markers" link to it.

> **Wart.** `build_marker_csv` — the REAPER-format CSV documented as the studio's route to a
> real `.ptx` — is called by nothing. No run produces a CSV.

> **Wart.** The workbook build is *not* wrapped in try/except while the MIDI and FLAC exports
> are, so a workbook failure aborts the run before markers or reference audio are attempted.

---

## 6. Caches

| cache | key | note |
|---|---|---|
| separation | `<path>.voc16.npy` + `<path>.acc16.npy` | read only when **both** exist |
| separation (S3) | `sepcache/{name}.{sha1 or etag}` | content-addressed one level up |
| speaker sum | `spksum/{folder}.{n}f.{bytes}` | folder id + file count + total bytes |
| Scribe transcript | `<path>.scribe.json`, or `.raw.scribe.json` when `AQC_NO_SEP=1` | **original side only** |
| Sarvam transcript | `<path>.sarvam.json` | no raw/separated distinction at all |
| song bank | `songbank/<series>.json` + `.candidates.json` | per series |

> **Wart, and a live one.** The *dub-side* Scribe key does **not** encode `dub_is_clean`. Two
> runs over the same dub file — one `--clean-dub` (raw), one separated — write and read the
> same cache object, so the second silently gets the first's differently-prepared transcript.
> This is precisely the bug the comment three lines above it was written to fix, left unfixed
> on the dub side.

> **Wart.** The Sarvam key encodes nothing either, on either side.

> **Wart.** `audio_qc.py`'s own separation cache is keyed on the **local file path string** —
> no hash, size or mtime. Correctness depends entirely on the runner naming `/tmp` files
> consistently.

Empty transcripts are never written and never trusted on read (added 2026-08-10 after
Sarvam's exhausted quota cached `[]` and poisoned EP41).

---

## 7. The gate cascade (text engine)

All candidate-suppressing gates live in one loop over the alignment's `missing` list, and
apply **only on the text-engine path**. In order:

1. **song-like text** — repetition ratio; drops the candidate
2. **long span** — a >12 s window is not a single dialogue line → UNCHECKED
3. **reference unreliable** (`_reliable` on nsp / alp / cr, engine-aware) → UNCHECKED
4. **dub-side unreliable** — dub speech near the slot exists but is unreadable → UNCHECKED
5. **acoustic slot gate** — dub speech covers ≥40% of the slot → UNCHECKED
6. **LLM judge** — `garble` → UNCHECKED, `present` → **dropped silently**, `missing` → flagged

Then, after the flag exists: `_dub_fill` (silence / ambience / speech-like), the fragment
tag, `_group_missing` (blocks), `_tag_song_reprise` (+ song bank), and finally `_retier`.

**The confidence tier** (`_tier_from_features`, applied by `_retier` at the very end, so it
can see fill, block and song context):

```
song reprise / fragment / ≤1 word            → low
block of ≥4 lines                            → medium if first line, else low
dub speaks under the line (fill speech-like) → low
acoustic hole + ≥3 words + ≥0.35 s           → high
acoustic hole, or ambience                   → medium
best semantic match < 0.35                   → medium
otherwise                                    → low
```

`_dub_fill`'s verdict **overrules** `slot_speech_cov` when present: they measure different
things, and the slot gate upstream deliberately tolerates 40% neighbour bleed.

> **Wart.** `_confidence()` still exists and still assigns a tier at detection time on the
> old `best < 0.35` rule. `_retier` overwrites it. Two definitions of "high" in one file.

> **Note.** A judge `present` verdict drops the candidate without counting it as unchecked —
> it is treated as verified, not unverifiable. The judge has a per-episode wall-clock budget
> and stands down after 3 consecutive API failures (fail open, flags kept).

---

## 8. The Teams workflow

Single entry point: `POST /api/agent/teams` (`backend/server.py:942`), raw `Request` because
it must HMAC the exact body bytes. Key-exempt — the Bot Framework token is the auth.

**Command routing.** Scriptless mode is selected by a *topic* regex
(`audio` / `mix(es)` / `scriptless` / `no-script`); the **verb** then decides preview vs launch
(`run` / `runn…` / `start` / `go` / `launch` / `kick` / `compare`). The scriptless launch branch
is tested *before* the generic run/check branches, so `run scriptless qc ep 42` starts the
mix-vs-mix fan-out rather than the script pipeline.

```
check scriptless qc <series> ep N   → availability card
run scriptless qc <series> ep N     → fan-out across every delivered language
status                              → progress bars, then the result card
runs                                → last 5 runs in this channel
check ep N / run ep N               → the script-QC path
help
```

`status` matches status/done/ready/result/finished/progress/"is it" **unless** the message
also contains run, check or start.

**The 5-second webhook timeout** is handled by launching off-thread and replying immediately;
the real outcome is posted later through the Incoming Webhook.

> **Wart.** Because the launch is off-thread, *every* outcome — success, "nothing to check",
> and any exception — is reported **only** via the Incoming Webhook. With no
> `DQC_TEAMS_INCOMING` configured, a failed launch is completely silent.

**Availability card** shows Original (with the stage it came from), "Ready to check", "Not
delivered yet", and a per-language "cannot check" row with the reason. The Run button is
withheld when nothing is runnable; "Choose files" is always offered.

**The picker** (`GET /api/agent/pick` → `/api/agent/pick-go`) lists every candidate `.wav` on
both sides, pre-selects what the automatic run would use, offers "paste a Box link" per side
and an escape option per radio group. Authenticated by the HMAC link itself (1 h expiry);
Box names are HTML-escaped. Submits by GET; a pasted link beats the radio; at least one dub
is required.

> **Wart.** The pick-go confirmation page unconditionally says "the confirmation and results
> will post in the channel" even when no webhook target exists and nothing can ever post.

**Status rendering** dispatches on the record's `compute` field into four renderers
(audio / audio-parallel, fargate-parallel, fargate, in-process). A single-language scriptless
run is normalised into the fan-out shape so one renderer covers both. `status` and the
proactive notifier share `_run_status` / `_status_card`, so they cannot drift. A terminal
state is persisted back to `run_store`, because a cloud fan-out's outcome lives in ECS/S3 and
the record would otherwise sit at "running" forever.

The notifier gives up after `MAX_WATCH_S` (default 5400 s) and posts what it has with an
"I've stopped watching this run" preamble.

---

## 9. Open questions

* **Is `AQC_NO_SEP` set in the production task definition?** Not answerable from this repo.
  The measured verdict on real episodes was that separation wins, so it should be unset.
* **Are POA's songs dubbed?** Comparing EP11 against the full stereo mix instead of the
  dialogue stem removes 28 of 31 song-region flags while keeping the ear-verified real drop
  at `high`. That is consistent with *either* the song being dubbed into the music stem *or*
  the original Portuguese song being retained untranslated — the two are indistinguishable to
  the tool. Pending an ear check.
* **The `(accompaniment − vocals)` discriminator.** Measured over 8 episodes / 327 flags:
  sung lines median **+1.7 dB**, spoken lines median **−12.3 dB**. At a 0 dB threshold it
  catches 72% of song lines and mislabels 9.8% of dialogue, with all four ear-verified real
  drops safely below (closest: −2.0 dB). Proposed but **not implemented**.
