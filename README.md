# Dialogue QC

A **fully-offline desktop app** that checks a dub against its script. Point it at a
**script** (DOCX / SRT / CSV) and a **folder of per-speaker audio tracks**, and it:

- **builds the character list** from the script (collapsing alias spellings — e.g.
  `Shoma`, `Shoma[gavv]`, `Shoma [narration]` become one character), and
- flags **missing / misaligned / extra** dialogue with **timestamps**.

It uses **Silero VAD** (real speech detection — it ignores background music/SFX and
catches short lines). No transcription, no cloud, **no API keys, no internet**.
Audio never leaves the machine.

---

## What problem it solves
Checking a dubbed episode against its script is normally done by hand, line by line —
the biggest time sink in QC review. Dialogue QC does that pass automatically and gives
the reviewer a jump-list of exact problem timestamps (including whole characters whose
audio wasn't delivered).

## What it detects
- 🔴 **Missing** — the script has a line but the character's track is silent there.
- 🟠 **Misaligned** — the line is present but early/late or cut short.
- 🔵 **Extra** — speech in a track with no scripted line.
- ⚠️ **No audio** — a character with no matching track at all.
- Plus tolerant **name matching** (handles vendor prefixes/suffixes, case, spelling).

---

## Quick start

### Prerequisites
- **Python 3.11** (with `pip`)
- **Node.js 18+** (LTS) and `npm`
- **Git**
- Windows 10/11 (the packaged app targets Windows)

### Run from source
```bash
git clone <this-repo-url> dialogue-qc
cd dialogue-qc

# 1. Python backend deps  (a virtual env is recommended)
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 2. UI + Electron deps
npm install

# 3. Launch the desktop app (starts the backend + opens the window)
npm run dev
```
`npm run dev` opens the **Dialogue QC** window. It auto-detects the project's `.venv`
to run the backend; to force a specific interpreter set `DQC_PYTHON`:
```powershell
$env:DQC_PYTHON = "C:\path\to\python.exe"; npm run dev
```
> The backend alone can be run with `python run.py` (serves on `127.0.0.1:8765`).
> The UI also opens in a browser at `http://localhost:5173`, but **choosing files only
> works in the Electron window** — browsers can't read local file paths.

**Troubleshooting — the app window doesn't open / Electron crashes with
`Cannot read properties of undefined (reading 'isPackaged')`:** that means
`ELECTRON_RUN_AS_NODE` is set in your environment (it makes Electron run as plain Node).
The `dev:electron` script already clears it; if it persists, unset it in your shell
(`Remove-Item Env:ELECTRON_RUN_AS_NODE` in PowerShell) and re-run.

---

## Using the app
1. **Choose script + audio** — Browse… (or drag-and-drop) a script file and the folder
   of per-speaker tracks. Optionally set **Strip prefix** to remove a common vendor
   prefix from track names (e.g. `GAVV EPI 16 MAL - `). Click **Analyse**.
   A progress bar shows each track being processed.
2. **Characters** — the auto-built cast appears, with aliases and the mapped audio
   track (or a red **"no audio ✗"** for characters with no track).
3. **Detected errors** — counts (Missing / Misaligned / Extra / No-audio), a filter,
   and a list. **Click any row** to see the script line and **play that slice of audio**.
   Drag the **Tolerance** slider + **Re-run** to make "misaligned" stricter or looser.

A 25-minute episode (~12 tracks) takes roughly a minute to analyse.

---

## Build the Windows installer
```bash
# 1. Freeze the Python backend into a standalone exe (needs pyinstaller in the env)
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\python build_backend.py     # -> backend-dist/dqc-backend.exe

# 2. Build the UI + package the desktop app
npm run dist                               # -> release/  (installer + win-unpacked/)
```
The packaged app needs **no Python or Node** on the user's machine. `release/win-unpacked/`
is a portable folder you can zip and share; `Dialogue QC Setup *.exe` is the installer.

### Windows packaging notes (important)
- **Developer Mode must be ON** to package — electron-builder extracts a cache containing
  symlinks. Turn it on at *Settings → Privacy & security → For developers → Developer Mode*,
  then run `npm run dist`. (Otherwise: `Cannot create symbolic link: a required privilege
  is not held`.)
- **Code signing:** the app/backend are **unsigned**, so Windows **Smart App Control**
  may block or warn on them. For distribution, sign with an Authenticode certificate
  (set `win.certificateFile` / `certificatePassword` in the `build` block of `package.json`).
  Internally, users can allow it once via SmartScreen → *More info → Run anyway*.

---

## Supported inputs
- **Scripts:** DOCX (timecoded table: Sr | Start | End | Character | Dialogue), SRT, CSV/TSV.
- **Audio:** one file per speaker (**WAV / FLAC / OGG / AIFF**), named by character.

## Project layout
```
backend/             FastAPI server + VAD / script-parser / characters / alignment
backend/models/      silero_vad.onnx  (the speech-detection model, ~2 MB)
run.py               backend entry point (uvicorn)
src/                 React UI
electron/            Electron main + preload (launches backend, native pickers)
build_backend.py     PyInstaller freeze script
requirements.txt     Python deps   ·   package.json  UI + Electron deps
```

## Limitations (good to know)
- It detects whether someone **spoke at the right time**, not **what** they said — it
  can't catch a *wrong line* spoken on time (that would need transcription/ASR).
- It expects **per-speaker tracks** (one file per character), not a single mixed mixdown.
- Distribution to other machines needs **code signing** (see above).
