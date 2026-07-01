# Dialogue QC — Agent Handoff

Context primer for the next agent picking up this project. Read this fully before changing anything.

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

## 9. Roadmap / next steps (not yet done)
- **Content-based (timeline) track mapping** — match a track to a character by whether its *speech timing* correlates with that character's *script timing*. Would catch "generic-labelled `Actor` is really Shoma" and make "no audio" content-verified instead of name-only. **Logic already exists** in the S2ST sibling scripts (`verify_channel_speakers.py` / `detect_naming_inconsistencies.py`) — port it into `characters.map_characters_to_channels` (pass `content_scores`).
- **Parallelize VAD across tracks** — analysis is ~75 s sequential; threads/async would cut it to ~15 s (onnxruntime releases the GIL).
- **Cloud/serverless hosting** for the Rian pipeline — see `docs/cloud-hosting-plan.md` (Lambda + Step Functions + S3 + API Gateway; fan-out one Lambda per track; auto-triggered off Box import). Lambda suffices; SageMaker only if ASR/voiceprints are added.
- **ASR (Whisper)** for wrong-line detection, and **speaker embeddings (ECAPA-TDNN)** for true voiceprint identity — both are "phase 2 if needed", not required now.
- **Code signing** before distributing the installer to QC staff.

## 10. Conventions
Match the existing style. Backend: FastAPI + pydantic, single in-memory session, keep `detect_speech_regions(wav_path) -> [{start,end}]` stable so alignment/characters don't change. Frontend: React + react-query, Tailwind `ink-*` theme, one screen (`App.tsx`). Commit messages end with the Co-Authored-By trailer; push to `main`.
