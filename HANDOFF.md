# Dialogue QC — Agent Handoff

Context primer for the next agent picking up this project. Read this fully before changing anything.

> **Detailed, always-current context lives in the agent memory** at
> `C:\Users\Rishi\.claude\projects\c--Users-Rishi-Desktop-dialogue-qc\memory\` (index: `MEMORY.md`).
> This file is the architectural primer; the memory files carry the blow-by-blow of each subsystem.

---

## 0. CURRENT STATE (2026-07-24) — this project is now BOTH a desktop app AND a hosted service

The original desktop app (sections 1–8 below) still stands. On top of it, a **hosted pipeline** was built:

- **Live on AWS EC2** — `ap-south-1`, instance `i-02a2c3ef2e468ab05` (**t3.medium**; resize to `c7i.4xlarge` for batches, back after), Elastic IP `13.205.42.228`, HTTPS at **https://13-205-42-228.sslip.io** (Caddy + Let's Encrypt), `systemd` unit `dialogue-qc`, env in `/home/ubuntu/app/.env`. Deploy = `git pull` + `sudo systemctl restart dialogue-qc`. See memory `ec2-deployment`.
- **Box integration** — the pipeline fetches the English script, original premix, and per-language dub stems **live from Box** by naming convention. Which folders belong to which show is **data, not code**: `backend/series_registry.json` (per-series Box folder ids) + `backend/box_discovery.py` (find script/premix/stems). Adding a show = a JSON entry, no code change. `backend/box_fetch.py` = server-to-server download; `backend/box_oauth.py` = token. Batch runner: `box_batch.py`. Per-episode agent runner: `backend/episode_runner.py`.
- **QC chat agent (Teams)** — 3 layers: **L1** engine API (`/api/agent/*`), **L2** per-series worker (Claude Haiku 4.5, `backend/agent.py`), **L3** router (Claude Sonnet 5, `backend/router.py`). Teams Outgoing-Webhook receiver `/api/agent/teams` uses a **fast LLM-free path** (`_teams_fast` in server.py) to fit the 5 s reply window (check/run/status → availability card + Run button). Runs are **persisted to disk** (`backend/run_store.py`) so status + download survive restarts (in-process `backend/jobs.py` does not). See memory `teams-qc-agent`.
- **Voice-ID check** — the Excel report validates each delivered audio's **ElevenLabs voice id** against the studio's master sheet (`KAMEN RIDER CHARACTER LIST & VOICES.xlsx`), **auto-fetched live from Box** (etag-cached, `backend/voices.py::refresh_from_box`; parsed by `backend/tools/build_voice_bank.py`; committed `voice_bank.json` = fallback). New "Voice ID check" column: **OK / no voice id / not in list / duplicate id / verify match / — (generic)**. Pure metadata, no cloning. See memory `voice-id-check`.
- **Mapping accuracy** — `backend/content_map.py` (twin-merge, swap-repair, reassign-by-voice, grouped bit-parts) + `backend/char_list.py` roster aid. See memory `mapping-accuracy-fixes`, `content-based-mapping`, `group-stem-recognition`.
- **Other shows (Engaged / Suits S9 / Motu Patlu)** were assessed 2026-07-24: similar top-level Box layout but **not QC-able like Gavv** — they ship `.srt` subtitles with **no speaker names** (per-character QC needs character-labelled scripts), and dub tracks are organised differently (Suits = Male/Female bundles; Motu = per-episode single-language). Scriptless timeline QC is the only option without scripts. Held for later.

**Compute note:** QC analysis is CPU/RAM-heavy (Silero VAD holds native-rate stems in RAM; ~3–4 GB per run) and **bursty** — idle most of the time, then a heavy multi-language batch. This is the motivation for the ongoing **Fargate / serverless** discussion (`docs/lambda-serverless-plan.md`): run heavy jobs on-demand instead of paying for a big always-on box.

---

## 1. What this tool is
**Dialogue QC** is a **fully-offline Windows desktop app** that checks a dubbed episode against its script and flags problems. Given a **script** (DOCX/SRT/CSV) and a **folder of per-speaker audio tracks** (one file per character), it:

1. **Builds the character list** from the script (collapsing alias spellings — e.g. `Shoma`, `Shoma[gavv]`, `Shoma [narration]` → one character).
2. Uses **Silero VAD** (real speech detection — ignores background music/SFX) to find when each track actually contains speech.
3. Compares that to the script's timecodes and flags **Missing / Misaligned / Extra** dialogue, plus characters with **No audio** at all.

No transcription, no cloud, **no API keys, no internet.** Audio is read from local disk and never leaves the machine.

## 2. What it's used for (the QC problem)
Checking a dub against its script is normally done by hand, line by line — the biggest time sink in QC review. This automates that pass and hands the reviewer a **jump-list of timestamped issues** (and catches whole characters whose audio wasn't delivered — including, on the test episode, the protagonist). The team downloads a **CSV report** and acts on it: re-record missing lines, fix timing, flag the vendor for missing tracks.

**Error types:** `Missing` (script line, silent track) · `Misaligned` (present but early/late/cut-short) · `Extra` (speech with no scripted line) · `No audio` (character with no matching track).

## 3. Where it lives
- **Repo:** https://github.com/rishikar-eng/Missing-dialogue (branch `main`)
- **Local:** `C:\Users\Rishi\Desktop\dialogue-qc`
- **Sibling project (S2ST):** `C:\Users\Rishi\Desktop\S2ST` — where this originated, and where the **content-based mapping scripts** live (`scripts/verify_channel_speakers.py`, `scripts/detect_naming_inconsistencies.py`) and the `md_to_pdf.py` used for report PDFs.

## 4. Architecture
```
Electron shell (electron/main.cjs) ──spawns──▶ Python backend (FastAPI, run.py :8765)
        │ loads                                   backend/: parser · Silero VAD · alignment
        ▼
React UI (src/, Vite :5173 in dev)  ──HTTP──▶ 127.0.0.1:8765
```
- **backend/** — `server.py` (FastAPI), `script_parser/` (DOCX/SRT/CSV → timecoded segments), `characters.py` (entities + alias-collapsing + name→track mapping), `vad.py` (Silero ONNX), `alignment.py` (script-vs-speech scoring), `models/silero_vad.onnx` (2.3 MB, bundled).
- **src/** — `App.tsx` (the whole UI), `api.ts` (backend client). Tailwind theme in `index.css`.
- **electron/** — `main.cjs` (launches backend, native file pickers, waits for healthz), `preload.cjs` (exposes `pickFile`/`pickFolder`/`getPathForFile`).
- **run.py** — backend entry (uvicorn on `DQC_PORT`, default 8765).
- **build_backend.py** — PyInstaller freeze. **package.json** — electron-builder config.

### Backend API
`POST /api/analyze` {script_path, audio_dir, fps?, strip_prefix?, tol_s} → characters + alignment. `POST /api/realign` {tol_s} → re-score at new tolerance (reuses cached VAD → instant). `GET /api/audio-slice?channel&start_s&end_s` → WAV slice for the player. `GET /api/progress` → live per-track progress. `GET /api/healthz`. State is a single in-memory session (desktop = one user); VAD regions are cached in `STATE["region_cache"]`.

## 5. How the pipeline works (the important bit)
1. **Parse** the script → `ScriptDoc` of `ScriptSegment{start_s, end_s, characters[], text}`. DOCX timecodes are `HH:MM:SS:FF`; **fps is auto-detected** (Gavv = 25).
2. **Build characters** → collapse alias spellings into one `CharacterEntity`; **map each character to an audio track by NAME** (fuzzy, strips vendor prefixes via the `strip_prefix` field / affixes like "Stomach").
3. **VAD each mapped track** with Silero → speech regions (seconds). Tuned to keep short lines (`min_speech_ms≈90`).
4. **Align** per character: for each script line, check speech coverage in the track (offset-corrected). No/low coverage → Missing; present but drifted/short → Misaligned (onset/offset/truncated); track speech with no line → Extra. A per-track **capture offset is auto-estimated** (median) so a constant script-vs-audio shift doesn't false-flag everything.

**Key limitation:** it detects *presence of speech at the right time*, **not the words**. It can't catch a *wrong line* spoken on time (that needs ASR). And mapping is **name-based** — it can be fooled by generic/mislabelled track names (e.g. a lead's audio inside an `Actor.wav`). "No audio" means "no track *named* for this character," NOT "verified no voice."

## 6. Run / build / test
Prereqs: **Python 3.11**, **Node 18+**, Git.
```powershell
# from source (dev)
python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
npm install
npm run dev            # opens the Electron window (drag-drop / click-to-browse work here)

# backend alone
.venv\Scripts\python run.py      # serves 127.0.0.1:8765

# build the Windows installer
.venv\Scripts\pip install pyinstaller
.venv\Scripts\python build_backend.py    # -> backend-dist/dqc-backend.exe (49 MB, standalone)
npm run dist                             # -> release/ (installer + win-unpacked portable app)
```
Verified on **Kamen Rider Gavv E16 (Malayalam)**: 14 characters built, ~10 missing / 1 misaligned / 10 extra at tol=1.0, and correctly caught **3 leads (Rojoe/Shoma/Amane) with no audio delivered**.

## 7. GOTCHAS (hard-won — do not re-learn these)
- **`ELECTRON_RUN_AS_NODE=1`** in the environment makes Electron run as plain Node and crash (`app` undefined) — the window never opens. The `dev:electron` script clears it via `cross-env`. If Electron won't launch, check this env var.
- **PyInstaller + conda Python:** the conda interpreter keeps C-extension DLLs (`libffi` for `_ctypes`, `libexpat` for `pyexpat`, ssl, lzma…) in `<env>/Library/bin`, which PyInstaller misses → frozen exe fails with "DLL load failed". `build_backend.py` bundles them. Building from a clean python.org Python avoids this entirely.
- **Silero v5 ONNX** requires each input to be **64 context samples + 512 new = 576**, with context carried between windows. Feeding a bare 512 makes it output ~0 for *everything* (looks broken). See `vad.py::_speech_probs`.
- **Windows Smart App Control / Application Control** blocks freshly-built **unsigned** exes — the backend exe and the app may be blocked/warned. Distribution needs an **Authenticode code-signing certificate**. `electron-builder` also needs **Developer Mode ON** (symlink privilege) to package; if `winCodeSign` extraction fails, pre-extract it excluding the `darwin` folder.
- **Browser vs Electron:** a plain browser **cannot read local file paths** (security). File selection only works in the Electron window. The UI shows a "use the desktop app" hint in a browser.
- **Backend launch from tooling:** launching the frozen `.exe` via git-bash/`Start-Process` can hit permission/AppControl issues; `cmd //c "start /b …"` works. The `.venv` python backend runs fine normally.

## 8. Current UI features
Drag-drop / click-to-browse file pickers · character table (aliases, mapped track, red "no audio") · error list with **expandable rows** (script line + audio player for that slice) · **tolerance slider** (+ instant Re-run via VAD cache) · **live progress bar** (per track) · **"New analysis"** reset button · **"Download report"** CSV (all issues with script timecodes + lines + detail + track, episode-ordered, opens in Excel).

## 9. Roadmap / next steps
- **Content-based (timeline) track mapping — DONE (2026-07-01).** Ported natively into
  [backend/content_map.py](backend/content_map.py) (`verify_mapping`), wired into `server.py::analyze`.
  Now VADs **every** track up front, then: keeps name matches (authoritative), **rescues**
  name-unmapped characters via voice-timeline coverage (precision/recall), **flags** name↔voice
  disagreements, and **content-verifies** "no audio". Surfaced in the UI (a "Track ↔ character
  checks" panel + "via voice" badge) and the payload (`naming_issues`, `characters[].mapped_by`).
  Tuning constants at the top of `content_map.py`. Validated on E29 (3 rescues incl. `Agent→LADY BIT 02`
  at precision 1.0 — a mislabelled stem — + 1 name-mismatch flag) and E30 (clean names → 0 rescues,
  no regressions). Trade-off: analysis now VADs all tracks, so it's slower (E29 ~200s) → makes the
  next item more valuable.
- **False-negative hardening — DONE (2026-07-06).** Four silent-miss fixes, adversarially reviewed:
  (1) **whole-track sync warnings** — a uniformly late/early track (auto-offset ≥0.75s,
  `SYNC_WARN_OFFSET_S` in alignment.py) is surfaced instead of silently corrected;
  (2) **rescue guard** — content rescues auto-map only at precision ≥0.5 (`RESCUE_STRONG_PREC`);
  weaker candidates become a `possible_match` issue and the character STAYS no-audio;
  (3) **parse stats** — `ParseStats{candidates,parsed,dropped}` from every parser; dropped
  dialogue-looking rows (= lines never checked) trigger a red UI banner + report warning;
  (4) **native-rate clipping** — loudness envelope built from the native signal (true peaks);
  `envelope(audio, sr)` returns `(rms, peak, sec_per_frame)` and `measure()` indexes with the
  true frame duration. Login: Rian auth works against `api.rian.io/v1/Auth/LoginUser` with
  AES-256-CBC-encrypted payloads (backend/auth.py); bad creds → status 50004/50169.
- **Parallelize VAD across tracks** — analysis is sequential; threads/async would cut it a lot
  (onnxruntime releases the GIL). More impactful now that we VAD every track for content mapping.
- **Cloud/serverless hosting** for the Rian pipeline — see `docs/cloud-hosting-plan.md` (Lambda + Step Functions + S3 + API Gateway; fan-out one Lambda per track; auto-triggered off Box import). Lambda suffices; SageMaker only if ASR/voiceprints are added.
- **ASR (Whisper)** for wrong-line detection, and **speaker embeddings (ECAPA-TDNN)** for true voiceprint identity — both are "phase 2 if needed", not required now.
- **Code signing** before distributing the installer to QC staff. No code change needed:
  electron-builder signs automatically when these env vars are set at `npm run dist` time —
  `WIN_CSC_LINK` (path to the `.pfx` certificate) and `WIN_CSC_KEY_PASSWORD`. Also sign the
  frozen backend separately (`signtool sign /f cert.pfx /p <pw> /tr http://timestamp.digicert.com
  /td sha256 /fd sha256 backend-dist\dqc-backend.exe`) BEFORE `npm run dist` bundles it.
  What to buy: an **OV code-signing certificate** (~$100–300/yr, e.g. Sectigo/Certum) clears the
  SmartScreen "Run anyway" prompt after some reputation builds; an **EV certificate** (~$300–500/yr,
  hardware token) is trusted immediately and passes Smart App Control. Until then, testers click
  through SmartScreen once; SAC-enforced machines need SAC off.

## 10. Conventions
Match the existing style. Backend: FastAPI + pydantic, single in-memory session, keep `detect_speech_regions(wav_path) -> [{start,end}]` stable so alignment/characters don't change. Frontend: React + react-query, Tailwind `ink-*` theme, one screen (`App.tsx`). Commit messages end with the Co-Authored-By trailer; push to `main`.
