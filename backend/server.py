"""Dialogue-QC backend — a slim, fully-offline FastAPI server for the desktop app.

Unlike the original cloud version, this reads audio **straight from local disk by
path** (no uploads, no storage, no external APIs). The Electron shell starts this
server on 127.0.0.1 and the UI talks to it over localhost.

Flow:
  POST /api/analyze   {script_path, audio_dir, fps?, strip_prefix?, tol_s}
        -> parse script, build characters, map them to the audio tracks in the
           folder, run VAD-vs-script alignment, return characters + errors.
  POST /api/realign   {tol_s}        -> re-score the last analysis at a new tolerance.
  GET  /api/audio-slice?channel&start_s&end_s  -> a WAV slice of a track (for the player).
  GET  /api/healthz
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from .alignment import align_script_to_channels
from .characters import build_characters, map_characters_to_channels
from .script_parser import parse_script

app = FastAPI(title="Dialogue QC", version="0.1.0")
# Local desktop app: the Electron renderer (file:// or vite dev) calls us cross-origin.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# soundfile/libsndfile-readable formats. (Dub stems are almost always WAV.)
AUDIO_EXTS = {".wav", ".flac", ".ogg", ".aiff", ".aif"}

# Single in-memory session — a desktop app serves exactly one user.
STATE: dict[str, Any] = {"doc": None, "characters": None, "channel_wavs": {}}

# Live progress for the UI to poll while /api/analyze runs.
PROGRESS: dict[str, Any] = {"running": False, "done": 0, "total": 0, "stage": ""}


class AnalyzeRequest(BaseModel):
    script_path: str
    audio_dir: str
    fps: float | None = None
    strip_prefix: str = ""
    tol_s: float = 1.0


class RealignRequest(BaseModel):
    tol_s: float = 1.0


def _auto_prefix(stems: list[str]) -> str:
    """Detect a common vendor prefix shared by all track names (e.g.
    'GAVV EPI 16 MAL - '), trimmed back to a natural separator so we never cut
    into a character's name. Returns '' when there's nothing safe to strip."""
    if len(stems) < 2:
        return ""
    lcp = os.path.commonprefix(stems)
    for sep in (" - ", " – ", " — ", "_", " "):
        i = lcp.rfind(sep)
        if i != -1:
            return lcp[: i + len(sep)]
    return ""


def _discover_channels(audio_dir: Path, strip_prefix: str) -> dict[str, Path]:
    files = [
        p for p in sorted(audio_dir.iterdir())
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    ]
    # If the user didn't give a prefix, auto-detect the common one.
    prefix = strip_prefix or _auto_prefix([p.stem for p in files])
    out: dict[str, Path] = {}
    for p in files:
        name = p.stem
        if prefix and name.startswith(prefix):
            name = name[len(prefix):]
        out[name.strip()] = p
    return out


def _alignment_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "tol_s": report["tol_s"],
        "summary": report["summary"],
        "errors": report["errors"],
        "unmapped_characters": report["unmapped_characters"],
    }


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> dict[str, Any]:
    script_path = Path(req.script_path)
    audio_dir = Path(req.audio_dir)
    if not script_path.is_file():
        raise HTTPException(status_code=400, detail=f"Script not found: {script_path}")
    if not audio_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Audio folder not found: {audio_dir}")

    try:
        doc = parse_script(script_path, fps=req.fps)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse script: {e}")

    channel_wavs = _discover_channels(audio_dir, req.strip_prefix)
    if not channel_wavs:
        raise HTTPException(
            status_code=400,
            detail=f"No audio tracks ({', '.join(sorted(AUDIO_EXTS))}) found in {audio_dir}",
        )

    characters = build_characters(doc)
    mapping = map_characters_to_channels(characters, list(channel_wavs))
    for c in characters:
        c.channel = mapping.get(c.id)

    PROGRESS.update(running=True, done=0, total=0, stage="starting")

    def _prog(done: int, total: int, channel: str) -> None:
        PROGRESS.update(running=True, done=done, total=total, stage=channel)

    try:
        report = align_script_to_channels(
            doc, characters, channel_wavs, tol_s=req.tol_s, on_progress=_prog
        )
    finally:
        PROGRESS.update(running=False)

    STATE.update(doc=doc, characters=characters, channel_wavs=channel_wavs)
    return {
        "characters": [c.model_dump() for c in characters],
        "source_format": doc.source_format,
        "fps": doc.fps,
        "n_segments": len(doc.segments),
        "channels": list(channel_wavs.keys()),
        "alignment": _alignment_payload(report),
    }


@app.post("/api/realign")
def realign(req: RealignRequest) -> dict[str, Any]:
    if STATE.get("doc") is None:
        raise HTTPException(status_code=400, detail="Run analyze first")
    report = align_script_to_channels(
        STATE["doc"], STATE["characters"], STATE["channel_wavs"], tol_s=req.tol_s
    )
    return _alignment_payload(report)


@app.get("/api/audio-slice")
def audio_slice(channel: str, start_s: float, end_s: float, pad_s: float = 0.4):
    wav_path = STATE["channel_wavs"].get(channel)
    if not wav_path:
        raise HTTPException(status_code=404, detail=f"No track '{channel}'")
    with sf.SoundFile(str(wav_path)) as f:
        sr = f.samplerate
        total = len(f)
        i0 = max(0, int((start_s - pad_s) * sr))
        i1 = min(total, int((end_s + pad_s) * sr))
        if i1 <= i0:
            raise HTTPException(status_code=400, detail="Empty time range")
        f.seek(i0)
        data = f.read(i1 - i0, dtype="float32", always_2d=False)
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    buf = io.BytesIO()
    sf.write(buf, np.asarray(data, dtype="float32"), sr, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")


@app.get("/api/progress")
def get_progress() -> dict[str, Any]:
    return PROGRESS


@app.get("/api/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
