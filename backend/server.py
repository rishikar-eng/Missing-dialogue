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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from .alignment import align_script_to_channels
from .auth import login as rian_login, logout as rian_logout
from .characters import build_characters, map_characters_to_channels
from .content_map import verify_mapping
from .loudness import analyze_loudness, envelope
from .script_parser import parse_script
from .vad import detect_speech_regions, load_mono_native, resample_16k
from .voices import attach_voices

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
    # Optional: the ORIGINAL-language audio (one file — e.g. the source episode mix).
    # The script is source-timed, so slicing this at a flagged line's timecode plays
    # what the original sounded like there — reference for judging the dub.
    original_audio_path: str | None = None


class RealignRequest(BaseModel):
    tol_s: float = 1.0


class RemapRequest(BaseModel):
    character_id: str
    channel: str | None = None   # None = unassign (mark the character no-audio)
    tol_s: float = 1.0


class LoginRequest(BaseModel):
    em: str
    pw: str
    gotp: int = 0
    otp: str | None = None


class LogoutRequest(BaseModel):
    rt: str
    at: str | None = None


@app.post("/api/auth/login")
def auth_login(req: LoginRequest) -> dict[str, Any]:
    """Proxy the login to the Rian API (see backend/auth.py). Returns Rian's
    envelope {status, data, ...} so the frontend can branch on 200 / 3001 / errors."""
    try:
        return rian_login(req.em, req.pw, req.gotp, req.otp)
    except Exception as e:  # network / DNS / timeout reaching Rian
        raise HTTPException(status_code=502, detail=f"Could not reach the Rian login service: {e}")


@app.post("/api/auth/logout")
def auth_logout(req: LogoutRequest) -> dict[str, Any]:
    try:
        return rian_logout(req.rt, req.at)
    except Exception:
        return {"status": 200, "data": None}  # best-effort; local session is cleared regardless


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
        "sync_warnings": report.get("sync_warnings", []),
    }


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> dict[str, Any]:
    script_path = Path(req.script_path)
    audio_dir = Path(req.audio_dir)
    if not script_path.is_file():
        raise HTTPException(status_code=400, detail=f"Script not found: {script_path}")
    if not audio_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Audio folder not found: {audio_dir}")
    original_audio = Path(req.original_audio_path) if req.original_audio_path else None
    if original_audio is not None:
        if not original_audio.is_file():
            raise HTTPException(status_code=400, detail=f"Original audio file not found: {original_audio}")
        # Reject undecodable formats NOW with a clear message — otherwise the file
        # passes, the UI shows Original players, and playback 500s later.
        try:
            with sf.SoundFile(str(original_audio)):
                pass
        except Exception:
            raise HTTPException(
                status_code=400,
                detail=f"Could not decode original audio '{original_audio.name}'. "
                       f"Use WAV, FLAC, OGG, AIFF or MP3.",
            )

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

    # Script line intervals per character — feeds both content-mapping and alignment.
    spans_by_char: dict[str, list[tuple[float, float]]] = {}
    for seg in doc.segments:
        for key in seg.characters:
            spans_by_char.setdefault(key, []).append((seg.start_s, seg.end_s))

    def _prog(done: int, total: int, channel: str) -> None:
        PROGRESS.update(running=True, done=done, total=total, stage=channel)

    # VAD every track up front so the mapping can be verified by voice *timeline*
    # (content), not just filename. From the SAME loaded signal we also build a
    # loudness envelope (no second read). Cached regions are reused by alignment
    # below (so no track is VAD'd twice), which keeps /api/realign instant.
    #
    # Tracks are processed in PARALLEL: soundfile I/O, numpy, and onnxruntime all
    # release the GIL, and the shared Silero session is thread-safe for concurrent
    # runs. Workers are capped at 4 to bound peak memory (each worker holds one
    # track's native-rate signal, ~300 MB for a 25-min 48 kHz stem).
    PROGRESS.update(running=True, done=0, total=len(channel_wavs), stage="detecting speech")
    region_cache: dict[str, Any] = {}
    envelopes: dict[str, Any] = {}
    naming_issues: list[dict[str, Any]] = []
    loudness_flags: list[dict[str, Any]] = []

    def _process_track(item: tuple[str, Path]) -> tuple[str, list[tuple[float, float]], Any]:
        ch, wav = item
        # One read per track: native signal for loudness (true peaks -> real
        # clipping detection), resampled copy for VAD.
        native, native_sr = load_mono_native(wav)
        regs = detect_speech_regions(wav, audio=resample_16k(native, native_sr))
        return ch, [(r["start"], r["end"]) for r in regs], envelope(native, native_sr)

    try:
        workers = min(4, os.cpu_count() or 2, max(1, len(channel_wavs)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            done = 0
            for ch, regions, env in ex.map(_process_track, channel_wavs.items()):
                region_cache[ch] = regions
                envelopes[ch] = env
                done += 1
                PROGRESS.update(running=True, done=done, total=len(channel_wavs), stage=ch)
        PROGRESS.update(running=True, done=len(channel_wavs), total=len(channel_wavs), stage="mapping")

        # Name match first (authoritative where it fits), then content verification:
        # rescue characters the name step missed and flag name/voice disagreements.
        name_mapping = map_characters_to_channels(characters, list(channel_wavs))
        mapping, mapped_by, naming_issues = verify_mapping(
            characters, list(channel_wavs), name_mapping, spans_by_char, region_cache,
        )
        for c in characters:
            c.channel = mapping.get(c.id)
            c.mapped_by = mapped_by.get(c.id)
        # After mapping so the bank can also match by track name (informational).
        attach_voices(characters)

        report = align_script_to_channels(
            doc, characters, channel_wavs, tol_s=req.tol_s,
            on_progress=_prog, region_cache=region_cache,
        )

        # Per-line loudness on the (mapped) dub tracks: flag too-quiet / too-hot lines.
        lines_by_char: dict[str, list[tuple[int, float, float, str]]] = {}
        for seg in doc.segments:
            for key in seg.characters:
                lines_by_char.setdefault(key, []).append((seg.index, seg.start_s, seg.end_s, seg.text))
        loudness_flags, char_levels = analyze_loudness(characters, lines_by_char, envelopes, region_cache)
        for c in characters:
            lv = char_levels.get(c.id)
            if lv:
                c.level_dbfs = lv["median"]
                c.level_min_dbfs = lv["min"]
                c.level_max_dbfs = lv["max"]
    finally:
        PROGRESS.update(running=False)

    # Keep the VAD results so /api/realign (tolerance changes) is instant, and the
    # loudness envelopes so /api/remap can re-score a manual reassignment instantly.
    STATE.update(doc=doc, characters=characters, channel_wavs=channel_wavs,
                 region_cache=region_cache, envelopes=envelopes, naming_issues=naming_issues,
                 loudness_flags=loudness_flags,
                 original_audio_path=str(original_audio) if original_audio else None)
    return {
        "characters": [c.model_dump() for c in characters],
        "source_format": doc.source_format,
        "fps": doc.fps,
        "n_segments": len(doc.segments),
        "parse_stats": doc.parse_stats.model_dump() if doc.parse_stats else None,
        "channels": list(channel_wavs.keys()),
        "original_audio": original_audio is not None,
        "naming_issues": naming_issues,
        "loudness_flags": loudness_flags,
        "alignment": _alignment_payload(report),
    }


@app.post("/api/realign")
def realign(req: RealignRequest) -> dict[str, Any]:
    if STATE.get("doc") is None:
        raise HTTPException(status_code=400, detail="Run analyze first")
    report = align_script_to_channels(
        STATE["doc"], STATE["characters"], STATE["channel_wavs"], tol_s=req.tol_s,
        region_cache=STATE.get("region_cache"),  # reuse VAD -> instant
    )
    return _alignment_payload(report)


@app.post("/api/remap")
def remap(req: RemapRequest) -> dict[str, Any]:
    """Manually reassign a character to a different audio track — fixes mappings the
    automatics got wrong (mislabelled stems, possible_match candidates). Re-scores
    alignment (cached VAD) + loudness (cached envelopes), so it's instant."""
    doc = STATE.get("doc")
    if doc is None:
        raise HTTPException(status_code=400, detail="Run analyze first")
    characters = STATE["characters"]
    channel_wavs = STATE["channel_wavs"]
    ent = next((c for c in characters if c.id == req.character_id), None)
    if ent is None:
        raise HTTPException(status_code=404, detail=f"No character '{req.character_id}'")
    if req.channel is not None and req.channel not in channel_wavs:
        raise HTTPException(status_code=404, detail=f"No track '{req.channel}'")

    # Keep the mapping one-to-one: taking a track releases its previous owner.
    if req.channel is not None:
        for c in characters:
            if c.id != ent.id and c.channel == req.channel:
                c.channel = None
                c.mapped_by = None
    ent.channel = req.channel
    ent.mapped_by = "manual" if req.channel else None
    attach_voices(characters)  # channel changed -> the voice-bank match may too

    report = align_script_to_channels(
        doc, characters, channel_wavs, tol_s=req.tol_s,
        region_cache=STATE.get("region_cache"),
    )
    lines_by_char: dict[str, list[tuple[int, float, float, str]]] = {}
    for seg in doc.segments:
        for key in seg.characters:
            lines_by_char.setdefault(key, []).append((seg.index, seg.start_s, seg.end_s, seg.text))
    loudness_flags, char_levels = analyze_loudness(
        characters, lines_by_char, STATE.get("envelopes", {}), STATE.get("region_cache", {}),
    )
    for c in characters:
        lv = char_levels.get(c.id)
        # Reset first: a character that just LOST its track must not keep stale levels.
        c.level_dbfs = lv["median"] if lv else None
        c.level_min_dbfs = lv["min"] if lv else None
        c.level_max_dbfs = lv["max"] if lv else None
    STATE["loudness_flags"] = loudness_flags

    # Retire naming checks this manual action resolves: anything about this
    # character, and (when assigning) anything suggesting the now-taken track —
    # otherwise the panel/report keeps claiming "still counted as no-audio" about
    # a character the user just mapped.
    kept_issues = [
        it for it in (STATE.get("naming_issues") or [])
        if not (
            it.get("character") == ent.id
            or it.get("labelled_character") == ent.id
            or (req.channel is not None and it.get("channel") == req.channel)
        )
    ]
    STATE["naming_issues"] = kept_issues

    return {
        "characters": [c.model_dump() for c in characters],
        "loudness_flags": loudness_flags,
        "naming_issues": kept_issues,
        "alignment": _alignment_payload(report),
    }


@app.get("/api/audio-slice")
def audio_slice(channel: str | None = None, start_s: float = 0.0, end_s: float = 0.0,
                pad_s: float = 0.4, source: str = "dub"):
    if source == "original":
        # The single original-language reference file (source-timed, like the script).
        wav_path = STATE.get("original_audio_path")
        if not wav_path:
            raise HTTPException(status_code=404, detail="No original audio was provided for this analysis")
    else:
        wav_path = STATE["channel_wavs"].get(channel) if channel else None
        if not wav_path:
            raise HTTPException(status_code=404, detail=f"No track '{channel}'")
    try:
        with sf.SoundFile(str(wav_path)) as f:
            sr = f.samplerate
            total = len(f)
            i0 = max(0, int((start_s - pad_s) * sr))
            i1 = min(total, int((end_s + pad_s) * sr))
            if i1 <= i0:
                raise HTTPException(status_code=400, detail="Empty time range")
            f.seek(i0)
            data = f.read(i1 - i0, dtype="float32", always_2d=False)
    except HTTPException:
        raise
    except Exception:
        # Undecodable/corrupt file: a clean client error, not a 500.
        raise HTTPException(status_code=415, detail=f"Could not decode the {source} audio file")
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
