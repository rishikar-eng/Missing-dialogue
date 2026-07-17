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
import re
import secrets
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import box_fetch, excel_report, jobs, scriptless
from .alignment import align_script_to_channels
from .auth import login as rian_login, logout as rian_logout
from .char_list import apply_char_list
from .characters import build_characters, map_characters_to_channels
from .content_map import verify_mapping
from .loudness import analyze_loudness, envelope
from .script_parser import parse_script
from .vad import detect_speech_regions, load_mono_native, resample_16k
from .voices import attach_voices

# ---- API-key gate (hosted deployments only) ----
# When DQC_API_KEY is set, every /api/* call (and the FastAPI doc routes) must carry
# the key. The browser sends it three ways: an X-API-Key header (fetch), a `dqc_key`
# cookie (set once from the share link — so <audio>/<a download> requests, which can't
# add headers, authenticate WITHOUT the key ever appearing in a URL/log), or ?key=
# (the initial share link + curl). Unset (the desktop app) = no auth, behaviour unchanged.
API_KEY = os.environ.get("DQC_API_KEY", "")
# /api/healthz stays open so the UI (and a tunnel healthcheck) can probe the server.
_KEY_EXEMPT = {"/api/healthz"}
# FastAPI's auto-docs sit OUTSIDE /api/* and would otherwise be public through the
# tunnel, leaking the whole schema (routes + models, incl. the Box token fields) — so
# gate them too, and disable them outright when a key is set (belt and braces).
_GATED_DOCS = {"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}

app = FastAPI(
    title="Dialogue QC", version="0.1.0",
    **({"openapi_url": None, "docs_url": None, "redoc_url": None} if API_KEY else {}),
)


def _key_ok(request: Request) -> bool:
    supplied = (request.headers.get("x-api-key")
                or request.cookies.get("dqc_key")
                or request.query_params.get("key") or "")
    # Compare as BYTES: str compare_digest raises TypeError on non-ASCII input, which
    # would be a public 500-on-demand; encoding makes a malformed key just a wrong key.
    return secrets.compare_digest(supplied.encode("utf-8"), API_KEY.encode("utf-8"))


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    path = request.url.path
    gated = path.startswith("/api/") or path in _GATED_DOCS
    if API_KEY and gated and path not in _KEY_EXEMPT:
        if not _key_ok(request):
            return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
    return await call_next(request)

# Local desktop app: the Electron renderer (file:// or vite dev) calls us cross-origin.
# Added AFTER the key middleware so CORS is outermost and 401 responses carry CORS
# headers too (a dev-origin browser then sees the JSON error, not an opaque failure).
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# soundfile/libsndfile-readable formats. (Dub stems are almost always WAV.)
AUDIO_EXTS = {".wav", ".flac", ".ogg", ".aiff", ".aif"}

# Single in-memory session. The desktop app serves exactly one user; the HOSTED
# deployment serves the LATEST finished analysis to everyone (analyses are serialized —
# see jobs.MAX_CONCURRENT — so "latest wins", documented as a known limitation). The
# lock guards against TORN READS: a reviewer's realign/audio-slice must never see a
# half-swapped STATE (new doc + old channel_wavs). Uncontended on the desktop.
STATE: dict[str, Any] = {"doc": None, "characters": None, "channel_wavs": {}}
_STATE_LOCK = threading.RLock()


def _set_state(**kw: Any) -> None:
    with _STATE_LOCK:
        STATE.update(kw)


def _state_snapshot() -> dict[str, Any]:
    """A consistent copy of STATE — read multiple related keys from THIS, never from the
    live dict, so a concurrent analysis-completion can't tear the read."""
    with _STATE_LOCK:
        return dict(STATE)

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


class QCRequest(BaseModel):
    """One-shot QC for the VOX web integration. Inputs come from EITHER local paths
    (dev/testing) OR Box (the file ids VOX's 'Import from Box' popup already produces,
    plus its OAuth bearer token). Returns the same report shape as /api/analyze.

    Script: exactly one of `script_path` | `box_script_file_id`.
    Tracks: exactly one of `audio_dir` | `box_track_file_ids`.
    """
    # --- local sources ---
    script_path: str | None = None
    audio_dir: str | None = None
    # --- Box sources (server-to-server fetch) ---
    box_script_file_id: str | None = None
    box_track_file_ids: list[str] | None = None
    box_token: str | None = None          # bearer token from VOX's Box OAuth popup (or a CCG token)
    box_shared_link: str | None = None    # optional: scope access to a shared folder link
    # --- params ---
    strip_prefix: str = ""
    tol_s: float = 1.0
    fps: float | None = None


class CompareRequest(BaseModel):
    """Scriptless QC: compare the ORIGINAL episode audio against the dub when no
    timecoded script exists. Dub side is EITHER a folder of per-speaker tracks
    (combined by union of speech) OR one full-episode dub file."""
    original_audio_path: str
    audio_dir: str | None = None       # folder of dub speaker tracks…
    dub_audio_path: str | None = None  # …or a single full-episode dub file
    strip_prefix: str = ""
    tol_s: float = 1.0


class EpisodeRequest(BaseModel):
    """One episode, every dub language, one workbook.

    `languages` maps the sheet name -> that language's tracks folder, e.g.
        {"Malayalam": "D:/eps/mala/30", "Tamil": "D:/eps/tamil/30", ...}
    Each language is analysed against the SAME source-timed script (that's what makes
    them comparable), then written to one .xlsx with a sheet per language.
    """
    script_path: str
    languages: dict[str, str]
    original_audio_path: str | None = None
    episode: str = ""
    strip_prefix: str = ""
    tol_s: float = 1.0
    fps: float | None = None


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


def _resolve_tracks_dir(d: Path, max_depth: int = 3) -> Path:
    """Descend through redundant wrapper folders to the one that holds the stems.

    The studio's zips routinely extract with a duplicated level —
    'tamil/30/GAVV EPI 30 TAMIL TRACK FOR AI/GAVV EPI 30 TAMIL TRACK FOR AI/*.wav' —
    so picking the obvious folder used to fail with "No audio tracks found". Only
    descends when the current folder has NO audio and exactly ONE subfolder, i.e. when
    there is nothing to choose; anything ambiguous is left exactly as the user picked it.
    """
    for _ in range(max_depth):
        try:
            entries = list(d.iterdir())
        except OSError:
            return d
        if any(p.is_file() and p.suffix.lower() in AUDIO_EXTS for p in entries):
            return d                                   # stems are right here
        subs = [p for p in entries if p.is_dir()]
        if len(subs) != 1:
            return d                                   # 0 or many -> don't guess
        d = subs[0]
    return d


def _discover_channels(audio_dir: Path, strip_prefix: str) -> dict[str, Path]:
    audio_dir = _resolve_tracks_dir(audio_dir)
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


def _analyze_pipeline(
    doc: Any,
    channel_wavs: dict[str, Path],
    tol_s: float,
    *,
    on_stage: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """The shared analysis core used by both /api/analyze (desktop) and /api/qc (VOX).

    build characters -> roster aid -> parallel VAD + loudness envelopes -> name+content
    mapping (with group-stem 'grouped' handling) -> alignment -> per-line loudness.
    Returns the pieces both the response payload and the desktop STATE need. No globals.
    """
    def stage(msg: str, done: int = 0, total: int = 0) -> None:
        if on_stage:
            on_stage(msg, done, total)

    characters = build_characters(doc)
    apply_char_list(characters)

    spans_by_char: dict[str, list[tuple[float, float]]] = {}
    for seg in doc.segments:
        for key in seg.characters:
            spans_by_char.setdefault(key, []).append((seg.start_s, seg.end_s))

    # Parallel VAD (GIL-releasing) — 4 workers is the measured sweet spot on the desktop;
    # each holds one native-rate stem (~150-300 MB), so the count bounds peak memory.
    # Hosted small boxes set DQC_VAD_WORKERS=1/2 to fit their RAM.
    total = len(channel_wavs)
    env_workers = int(os.environ.get("DQC_VAD_WORKERS", "4"))
    n_workers = max(1, min(env_workers, os.cpu_count() or 2, total))
    stage(f"analysing audio — {n_workers} tracks in parallel", 0, total)
    region_cache: dict[str, Any] = {}
    envelopes: dict[str, Any] = {}

    def _process_track(item: tuple[str, Path]) -> tuple[str, list[tuple[float, float]], Any]:
        ch, wav = item
        native, native_sr = load_mono_native(wav)
        regs = detect_speech_regions(wav, audio=resample_16k(native, native_sr))
        return ch, [(r["start"], r["end"]) for r in regs], envelope(native, native_sr)

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = {ex.submit(_process_track, item): item[0] for item in channel_wavs.items()}
        done = 0
        for fut in as_completed(futures):
            ch, regions, env = fut.result()
            region_cache[ch] = regions
            envelopes[ch] = env
            done += 1
            stage(f"{done}/{total} tracks analysed ({n_workers} in parallel)", done, total)
    stage("mapping speakers", total, total)

    # Name match first, then content verification (rescue + name/voice flags + grouped).
    name_mapping = map_characters_to_channels(characters, list(channel_wavs))
    mapping, mapped_by, naming_issues = verify_mapping(
        characters, list(channel_wavs), name_mapping, spans_by_char, region_cache,
    )
    grouped_in = {it["character"]: it["channel"] for it in naming_issues
                  if it.get("kind") == "grouped" and it.get("character")}
    for c in characters:
        c.channel = mapping.get(c.id)
        c.mapped_by = mapped_by.get(c.id)
        c.grouped_in = grouped_in.get(c.id)
    attach_voices(characters)

    report = align_script_to_channels(
        doc, characters, channel_wavs, tol_s=tol_s, region_cache=region_cache,
    )

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

    return {
        "characters": characters,
        "region_cache": region_cache,
        "envelopes": envelopes,
        "naming_issues": naming_issues,
        "loudness_flags": loudness_flags,
        "report": report,
    }


def _check_analyze_inputs(req: AnalyzeRequest) -> tuple[Path, Path, Path | None]:
    """Fast input validation shared by the sync endpoint and the job submitter — bad
    paths 400 immediately in both, never as a delayed job failure."""
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
    return script_path, audio_dir, original_audio


def _run_analysis(
    req: AnalyzeRequest,
    script_path: Path,
    audio_dir: Path,
    original_audio: Path | None,
    on_stage: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Parse → discover tracks → run the pipeline → update STATE → response payload.
    The whole body of a script-mode analysis, shared by /api/analyze (desktop, sync)
    and /api/jobs/analyze (hosted, background thread)."""
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

    res = _analyze_pipeline(doc, channel_wavs, req.tol_s, on_stage=on_stage)

    characters = res["characters"]
    naming_issues = res["naming_issues"]
    loudness_flags = res["loudness_flags"]
    report = res["report"]

    # Keep the VAD results so /api/realign (tolerance changes) is instant, and the
    # loudness envelopes so /api/remap can re-score a manual reassignment instantly.
    # Hosted note: STATE is process-global, so the audio players/compilation serve the
    # LATEST finished analysis (one analysis runs at a time — see jobs.MAX_CONCURRENT).
    # Atomic swap so a concurrent reviewer never sees a half-updated STATE.
    _set_state(doc=doc, characters=characters, channel_wavs=channel_wavs,
               region_cache=res["region_cache"], envelopes=res["envelopes"],
               naming_issues=naming_issues, loudness_flags=loudness_flags,
               original_audio_path=str(original_audio) if original_audio else None,
               scriptless_errors=None)
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


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> dict[str, Any]:
    paths = _check_analyze_inputs(req)

    # The desktop UI polls /api/progress; wire the pipeline's stage callback to it.
    def _on_stage(msg: str, done: int, total: int) -> None:
        PROGRESS.update(running=True, done=done, total=total, stage=msg)

    # heavy_slot bounds concurrent analyses across every entry point (uncontended on the
    # single-user desktop; on a hosted box it stops two runs OOM-ing the container).
    try:
        with jobs.heavy_slot():
            return _run_analysis(req, *paths, on_stage=_on_stage)
    finally:
        PROGRESS.update(running=False)


@app.post("/api/jobs/analyze", status_code=202)
def analyze_job(req: AnalyzeRequest) -> dict[str, Any]:
    """Async flavour of /api/analyze for HOSTED use: returns 202 + a job id right away
    (tunnels cut long-silent requests), progress + result come from GET /api/jobs/{id}."""
    paths = _check_analyze_inputs(req)  # obvious mistakes still fail fast with a 400
    try:
        job = jobs.submit("analyze", lambda stage: _run_analysis(req, *paths, on_stage=stage))
    except RuntimeError as e:
        raise HTTPException(status_code=429, detail=str(e))
    return job.public()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job id")
    return job.public()


def _run_episode(req: EpisodeRequest, on_stage: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
    """Analyse every language of one episode against the same script, then build the
    workbook. Sequential on purpose: each language holds several native-rate stems, and
    running them in parallel is what OOMs a small host."""
    script_path = Path(req.script_path)
    if not script_path.is_file():
        raise HTTPException(status_code=400, detail=f"Script not found: {script_path}")
    if not req.languages:
        raise HTTPException(status_code=400, detail="Provide at least one language -> tracks folder")
    original = Path(req.original_audio_path) if req.original_audio_path else None
    if original is not None and not original.is_file():
        raise HTTPException(status_code=400, detail=f"Original audio not found: {original}")
    for lang, d in req.languages.items():
        if not Path(d).is_dir():
            raise HTTPException(status_code=400, detail=f"Tracks folder for {lang} not found: {d}")

    total = len(req.languages)
    per_lang: dict[str, dict[str, Any]] = {}
    failed: dict[str, str] = {}
    for i, (lang, audio_dir) in enumerate(req.languages.items()):
        if on_stage:
            on_stage(f"{lang} ({i + 1}/{total})", i, total)
        one = AnalyzeRequest(
            script_path=req.script_path, audio_dir=audio_dir, fps=req.fps,
            strip_prefix=req.strip_prefix, tol_s=req.tol_s,
            original_audio_path=req.original_audio_path,
        )
        try:
            with jobs.heavy_slot():
                res = _run_analysis(one, *_check_analyze_inputs(one),
                                    on_stage=(lambda m, d, t, _l=lang, _i=i: on_stage(f"{_l}: {m}", _i, total))
                                    if on_stage else None)
            res["_audio_dir"] = audio_dir
            per_lang[lang] = res
        except HTTPException as e:
            # One bad language must not lose the other five — record and carry on.
            failed[lang] = str(e.detail)
        except Exception as e:
            failed[lang] = str(e) or "analysis failed"
    if not per_lang:
        raise HTTPException(status_code=400,
                            detail="Every language failed: " + "; ".join(f"{k}: {v}" for k, v in failed.items()))

    if on_stage:
        on_stage("building workbook", total, total)
    from datetime import datetime, timezone
    ep = req.episode or script_path.stem
    out = Path(tempfile.gettempdir()) / f"dialogue-qc_{re.sub(r'[^A-Za-z0-9_.-]+', '_', ep)}.xlsx"
    excel_report.build_workbook(
        meta={
            "episode": ep,
            "generated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
            "script_path": str(script_path),
            "original_audio_path": str(original) if original else "",
            "tol_s": req.tol_s,
        },
        per_lang=per_lang,
        out_path=out,
    )
    STATE["report_xlsx"] = str(out)   # served by GET /api/report.xlsx
    return {
        "episode": ep,
        "languages": list(per_lang),
        "failed": failed,
        "report_ready": True,
        "summary": {lang: (r.get("alignment") or {}).get("summary") for lang, r in per_lang.items()},
    }


@app.post("/api/jobs/episode", status_code=202)
def episode_job(req: EpisodeRequest) -> dict[str, Any]:
    """One episode x N languages -> one workbook. Always a job: 6 languages x ~20 stems
    is 10-20 minutes, far past any proxy/tunnel request timeout."""
    try:
        job = jobs.submit("episode", lambda stage: _run_episode(req, on_stage=stage))
    except RuntimeError as e:
        raise HTTPException(status_code=429, detail=str(e))
    return job.public()


@app.get("/api/report.xlsx")
def report_xlsx():
    """Download the workbook built by the last /api/jobs/episode run."""
    p = STATE.get("report_xlsx")
    if not p or not Path(p).is_file():
        raise HTTPException(status_code=404, detail="No workbook yet — run an episode analysis first")
    return FileResponse(p, filename=Path(p).name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/qc")
def qc(req: QCRequest) -> dict[str, Any]:
    """Stateless one-shot QC for the VOX web app. Resolves the script + dub tracks from
    local paths OR from Box (via VOX's OAuth token + picked file ids), runs the shared
    analysis pipeline, and returns the Missing/Misaligned/Extra + loudness +
    no-audio/grouped report. Cleans up any fetched files afterwards."""
    tmp_dir: str | None = None
    try:
        # --- resolve the script (local path or Box file) ---
        if req.box_script_file_id:
            if not req.box_token:
                raise HTTPException(status_code=400, detail="box_token is required to fetch a Box script")
            tmp_dir = tmp_dir or tempfile.mkdtemp(prefix="dqc-qc-")
            try:
                script_path = box_fetch.download_file(
                    req.box_token, req.box_script_file_id, Path(tmp_dir) / "script",
                    shared_link=req.box_shared_link,
                )
            except box_fetch.BoxFetchError as e:
                raise HTTPException(status_code=502, detail=f"Could not fetch the script from Box: {e}")
            except Exception:
                # Never echo the raw error — it can carry a signed dl.boxcloud URL / token.
                raise HTTPException(status_code=502, detail="Could not fetch the script from Box")
        elif req.script_path:
            script_path = Path(req.script_path)
            if not script_path.is_file():
                raise HTTPException(status_code=400, detail=f"Script not found: {script_path}")
        else:
            raise HTTPException(status_code=400, detail="Provide either script_path or box_script_file_id")

        # --- resolve the dub tracks into a directory (local dir or Box files) ---
        if req.box_track_file_ids:
            if not req.box_token:
                raise HTTPException(status_code=400, detail="box_token is required to fetch Box tracks")
            tmp_dir = tmp_dir or tempfile.mkdtemp(prefix="dqc-qc-")
            audio_dir = Path(tmp_dir) / "tracks"
            try:
                box_fetch.download_files(
                    req.box_token, req.box_track_file_ids, audio_dir, shared_link=req.box_shared_link,
                )
            except box_fetch.BoxFetchError as e:
                raise HTTPException(status_code=502, detail=f"Could not fetch tracks from Box: {e}")
            except Exception:
                raise HTTPException(status_code=502, detail="Could not fetch tracks from Box")
        elif req.audio_dir:
            audio_dir = Path(req.audio_dir)
            if not audio_dir.is_dir():
                raise HTTPException(status_code=400, detail=f"Audio folder not found: {audio_dir}")
        else:
            raise HTTPException(status_code=400, detail="Provide either audio_dir or box_track_file_ids")

        # --- parse + discover + run the shared analysis ---
        try:
            doc = parse_script(script_path, fps=req.fps)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Could not parse script: {e}")
        channel_wavs = _discover_channels(audio_dir, req.strip_prefix)
        if not channel_wavs:
            raise HTTPException(
                status_code=400,
                detail=f"No audio tracks ({', '.join(sorted(AUDIO_EXTS))}) found in the tracks source",
            )

        with jobs.heavy_slot():
            res = _analyze_pipeline(doc, channel_wavs, req.tol_s)
        characters = res["characters"]
        return {
            "characters": [c.model_dump() for c in characters],
            "source_format": doc.source_format,
            "fps": doc.fps,
            "n_segments": len(doc.segments),
            "parse_stats": doc.parse_stats.model_dump() if doc.parse_stats else None,
            "channels": list(channel_wavs.keys()),
            "naming_issues": res["naming_issues"],
            "loudness_flags": res["loudness_flags"],
            "alignment": _alignment_payload(res["report"]),
        }
    finally:
        # Never leave fetched Box files (multi-hundred-MB stems) on disk.
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/compare")
def compare(req: CompareRequest) -> dict[str, Any]:
    """Scriptless QC — original-vs-dub timeline comparison (see backend/scriptless.py).
    Fills STATE like /api/analyze does, so the audio players (dub + original slices)
    and the missing-lines compilation work in this mode too. VAD results are cached by
    file path, so re-running at a new tolerance is instant."""
    original = Path(req.original_audio_path)
    if not original.is_file():
        raise HTTPException(status_code=400, detail=f"Original audio not found: {original}")
    try:
        with sf.SoundFile(str(original)):
            pass
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"Could not decode original audio '{original.name}'. Use WAV, FLAC, OGG, "
                   f"AIFF or MP3 (for video files, extract/export the audio first).",
        )

    if bool(req.audio_dir) == bool(req.dub_audio_path):
        raise HTTPException(status_code=400,
                            detail="Provide exactly one of audio_dir (speaker tracks) or dub_audio_path (full dub)")
    if req.audio_dir:
        audio_dir = Path(req.audio_dir)
        if not audio_dir.is_dir():
            raise HTTPException(status_code=400, detail=f"Audio folder not found: {audio_dir}")
        channel_wavs = _discover_channels(audio_dir, req.strip_prefix)
        if not channel_wavs:
            raise HTTPException(
                status_code=400,
                detail=f"No audio tracks ({', '.join(sorted(AUDIO_EXTS))}) found in {audio_dir}",
            )
    else:
        dub = Path(req.dub_audio_path)  # type: ignore[arg-type]
        if not dub.is_file():
            raise HTTPException(status_code=400, detail=f"Dub audio not found: {dub}")
        try:
            with sf.SoundFile(str(dub)):
                pass
        except Exception:
            raise HTTPException(status_code=400,
                                detail=f"Could not decode dub audio '{dub.name}'. Use WAV, FLAC, OGG, AIFF or MP3.")
        channel_wavs = {dub.stem: dub}

    # VAD original + all dub sources in parallel, reusing cached regions. The cache key
    # includes mtime+size so a file that CHANGED on disk between runs is re-analysed,
    # never scored with stale regions; the cache is pruned to the current inputs after
    # each run so it serves tolerance re-runs without growing for the whole session.
    def _cmp_key(p: Path) -> str:
        st = p.stat()
        return f"{p.resolve()}|{st.st_mtime_ns}|{st.st_size}"

    cmp_cache: dict[str, list[tuple[float, float]]] = STATE.get("cmp_regions") or {}
    all_files = [("__original__", original), *channel_wavs.items()]
    keys = {label: _cmp_key(p) for label, p in all_files}
    to_vad = {label: p for label, p in all_files if keys[label] not in cmp_cache}
    total = len(to_vad)
    n_workers = max(1, min(4, os.cpu_count() or 2, total or 1))
    PROGRESS.update(running=True, done=0, total=total,
                    stage=f"analysing audio — {n_workers} files in parallel" if total else "using cached analysis")

    def _vad_one(p: Path) -> list[tuple[float, float]]:
        native, native_sr = load_mono_native(p)
        regs = detect_speech_regions(p, audio=resample_16k(native, native_sr))
        return [(r["start"], r["end"]) for r in regs]

    # Hold a heavy slot for the VAD/compare compute (bounds concurrent RAM on a host);
    # tied to the existing try/finally so it's always released. Uncontended on desktop.
    _slot = jobs.heavy_slot()
    _slot.__enter__()
    try:
        if to_vad:
            with ThreadPoolExecutor(max_workers=n_workers) as ex:
                futures = {ex.submit(_vad_one, p): label for label, p in to_vad.items()}
                done = 0
                for fut in as_completed(futures):
                    label = futures[fut]
                    try:
                        cmp_cache[keys[label]] = fut.result()
                    except Exception:
                        # One unreadable file in the folder -> a clear 4xx naming it,
                        # not a bare 500 (the original/full-dub are pre-validated above).
                        bad = to_vad[label].name
                        raise HTTPException(
                            status_code=400,
                            detail=f"Could not decode audio file '{bad}'. Use WAV, FLAC, OGG, AIFF or MP3.",
                        )
                    done += 1
                    PROGRESS.update(running=True, done=done, total=total,
                                    stage=f"{done}/{total} files analysed ({n_workers} in parallel)")
        PROGRESS.update(running=True, done=total, total=total, stage="comparing timelines")

        original_regions = cmp_cache[keys["__original__"]]
        dub_regions = {ch: cmp_cache[keys[ch]] for ch in channel_wavs}
        report = scriptless.compare_original_to_dub(original_regions, dub_regions, tol_s=req.tol_s)
    finally:
        PROGRESS.update(running=False)
        _slot.__exit__(None, None, None)

    # Prune the cache to this run's inputs (bounded memory; still instant re-runs).
    cmp_cache = {k: v for k, v in cmp_cache.items() if k in keys.values()}

    # STATE so audio-slice (dub channels + original) and missing-compilation work.
    # doc=None → script-only endpoints (realign/remap) correctly refuse in this mode.
    _set_state(doc=None, characters=[], channel_wavs=channel_wavs,
               region_cache={ch: dub_regions[ch] for ch in channel_wavs},
               envelopes={}, naming_issues=[], loudness_flags=[],
               original_audio_path=str(original),
               cmp_regions=cmp_cache,
               scriptless_errors=report["errors"])
    return {
        "mode": "compare",
        "characters": [],
        "source_format": "original-audio",
        "fps": None,
        "n_segments": report["summary"]["n_original_regions"],
        "parse_stats": None,
        "channels": list(channel_wavs.keys()),
        "original_audio": True,
        "naming_issues": [],
        "loudness_flags": [],
        "alignment": _alignment_payload(report),
    }


@app.post("/api/realign")
def realign(req: RealignRequest) -> dict[str, Any]:
    st = _state_snapshot()  # consistent view — never a half-swapped analysis
    if st.get("doc") is None:
        raise HTTPException(status_code=400, detail="Run analyze first")
    report = align_script_to_channels(
        st["doc"], st["characters"], st["channel_wavs"], tol_s=req.tol_s,
        region_cache=st.get("region_cache"),  # reuse VAD -> instant
    )
    return _alignment_payload(report)


@app.post("/api/remap")
def remap(req: RemapRequest) -> dict[str, Any]:
    """Manually reassign a character to a different audio track — fixes mappings the
    automatics got wrong (mislabelled stems, possible_match candidates). Re-scores
    alignment (cached VAD) + loudness (cached envelopes), so it's instant."""
    st = _state_snapshot()  # consistent view of the analysis being edited
    doc = st.get("doc")
    if doc is None:
        raise HTTPException(status_code=400, detail="Run analyze first")
    characters = st["characters"]
    channel_wavs = st["channel_wavs"]
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
    ent.grouped_in = None  # a manual assignment/unassignment supersedes the auto 'grouped' label
    attach_voices(characters)  # channel changed -> the voice-bank match may too

    report = align_script_to_channels(
        doc, characters, channel_wavs, tol_s=req.tol_s,
        region_cache=st.get("region_cache"),
    )
    lines_by_char: dict[str, list[tuple[int, float, float, str]]] = {}
    for seg in doc.segments:
        for key in seg.characters:
            lines_by_char.setdefault(key, []).append((seg.index, seg.start_s, seg.end_s, seg.text))
    loudness_flags, char_levels = analyze_loudness(
        characters, lines_by_char, st.get("envelopes", {}), st.get("region_cache", {}),
    )
    for c in characters:
        lv = char_levels.get(c.id)
        # Reset first: a character that just LOST its track must not keep stale levels.
        c.level_dbfs = lv["median"] if lv else None
        c.level_min_dbfs = lv["min"] if lv else None
        c.level_max_dbfs = lv["max"] if lv else None

    # Retire naming checks this manual action resolves: anything about this
    # character, and (when assigning) anything suggesting the now-taken track —
    # otherwise the panel/report keeps claiming "still counted as no-audio" about
    # a character the user just mapped. EXCEPTION: a 'grouped' issue for ANOTHER
    # character on that track must survive (it explains a different bit-part's
    # bundling) — pruning it would strand that character between grouped and no-audio.
    kept_issues = [
        it for it in (st.get("naming_issues") or [])
        if not (
            it.get("character") == ent.id
            or it.get("labelled_character") == ent.id
            or (req.channel is not None and it.get("channel") == req.channel
                and it.get("kind") != "grouped")
        )
    ]
    # Keep each character's grouped_in in sync with the surviving 'grouped' issues, so a
    # char is never left with grouped_in set but no issue (limbo: neither list shows it).
    still_grouped = {it["character"]: it["channel"] for it in kept_issues
                     if it.get("kind") == "grouped" and it.get("character")}
    for c in characters:
        if c.id != ent.id and c.channel is None:
            c.grouped_in = still_grouped.get(c.id)

    # Persist the edited analysis (loudness + surviving naming checks) atomically.
    _set_state(loudness_flags=loudness_flags, naming_issues=kept_issues)

    return {
        "characters": [c.model_dump() for c in characters],
        "loudness_flags": loudness_flags,
        "naming_issues": kept_issues,
        "alignment": _alignment_payload(report),
    }


@app.get("/api/audio-slice")
def audio_slice(channel: str | None = None, start_s: float = 0.0, end_s: float = 0.0,
                pad_s: float = 0.4, source: str = "dub"):
    st = _state_snapshot()
    if source == "original":
        # The single original-language reference file (source-timed, like the script).
        wav_path = st.get("original_audio_path")
        if not wav_path:
            raise HTTPException(status_code=404, detail="No original audio was provided for this analysis")
    else:
        wav_path = (st.get("channel_wavs") or {}).get(channel) if channel else None
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


def _missing_windows(st: dict[str, Any], tol_s: float, pad_s: float) -> list[tuple[float, float]]:
    """[start,end] windows (script/original timecodes) of every MISSING line, padded
    and merged where they overlap. Script mode re-scores from the cached VAD; scriptless
    (compare) mode uses the stored original-vs-dub findings — both timelines are the
    original's, which is exactly the file the compilation slices. `st` is a STATE snapshot
    passed by the caller so the whole compilation sees one consistent analysis."""
    if st.get("doc") is not None:
        report = align_script_to_channels(
            st["doc"], st["characters"], st["channel_wavs"],
            tol_s=tol_s, region_cache=st.get("region_cache"),
        )
        errors = report["errors"]
    else:
        errors = st.get("scriptless_errors") or []
    wins = sorted(
        (max(0.0, e["script_start_s"] - pad_s), (e["script_end_s"] or e["script_start_s"]) + pad_s)
        for e in errors
        if e["type"] == "MISSING" and e.get("script_start_s") is not None
    )
    merged: list[tuple[float, float]] = []
    for s, en in wins:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], en))
        else:
            merged.append((s, en))
    return merged


@app.get("/api/missing-compilation")
def missing_compilation(pad_s: float = 2.5, gap_s: float = 0.6, tol_s: float = 1.0, mode: str = "stitch"):
    """A WAV of every MISSING line cut from the ORIGINAL audio (±pad_s context).
    Requires the original-language file. Two modes:
      mode="stitch"   — the clips back-to-back with a short silence between them
                        (a short re-record worklist to play straight through).
      mode="timeline" — a full episode-length track, silent everywhere EXCEPT at the
                        missing lines, where the original plays at its real timecode
                        (drop it onto the episode/dub timeline in an editor).

    pad_s defaults to 2.5 s each side (5 s of context per gap) so the cuts land in
    silence/room-tone rather than chopping a word mid-syllable — abrupt cuts made the
    clips hard to judge. Overlapping padded windows are merged (see _missing_windows),
    so neighbouring gaps become one continuous passage instead of stuttering."""
    st = _state_snapshot()
    # scriptless_errors is a LIST after a compare run (possibly empty — that's a valid
    # "no findings" state, which falls through to the 404 below), None otherwise.
    if st.get("doc") is None and st.get("scriptless_errors") is None:
        raise HTTPException(status_code=400, detail="Run analyze first")
    orig = st.get("original_audio_path")
    if not orig:
        raise HTTPException(status_code=400,
                            detail="No original audio — add the original-language file and re-analyse")
    windows = _missing_windows(st, tol_s, pad_s)
    if not windows:
        raise HTTPException(status_code=404, detail="No missing lines to compile")
    try:
        with sf.SoundFile(str(orig)) as f:
            sr = f.samplerate
            total = len(f)
            if mode == "timeline":
                # Silent buffer the full length of the original; paste each missing
                # span at its true position so it lines up with the episode timeline.
                out = np.zeros(total, dtype=np.float32)
                for s, en in windows:
                    i0, i1 = max(0, int(s * sr)), min(total, int(en * sr))
                    if i1 <= i0:
                        continue
                    f.seek(i0)
                    d = f.read(i1 - i0, dtype="float32", always_2d=False)
                    if getattr(d, "ndim", 1) > 1:
                        d = d.mean(axis=1)
                    out[i0:i0 + len(d)] = d
                fname = f"missing-lines-timeline-{len(windows)}.wav"
            else:
                gap = np.zeros(int(gap_s * sr), dtype=np.float32)
                parts: list[np.ndarray] = []
                for s, en in windows:
                    i0, i1 = max(0, int(s * sr)), min(total, int(en * sr))
                    if i1 <= i0:
                        continue
                    f.seek(i0)
                    d = f.read(i1 - i0, dtype="float32", always_2d=False)
                    if getattr(d, "ndim", 1) > 1:
                        d = d.mean(axis=1)
                    parts.append(np.asarray(d, dtype=np.float32))
                    parts.append(gap)
                if not parts:
                    raise HTTPException(status_code=404, detail="No audio extracted for the missing lines")
                out = np.concatenate(parts[:-1])  # drop trailing gap
                fname = f"missing-lines-original-{len(windows)}clips.wav"
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=415, detail="Could not decode the original audio file")
    buf = io.BytesIO()
    sf.write(buf, out, sr, format="WAV", subtype="PCM_16")
    headers = {"Content-Disposition": f'attachment; filename="{fname}"'}
    return Response(content=buf.getvalue(), media_type="audio/wav", headers=headers)


@app.get("/api/dub-mixdown")
def dub_mixdown():
    """All dub tracks from the current session summed into ONE full-episode-length WAV —
    for laying next to the original in an editor (Audacity) to compare by eye/ear.
    Block-streamed (never holds a full stem in memory) and written as float32 so
    summing many stems can't clip. Works after /api/analyze or /api/compare."""
    channel_wavs: dict[str, Path] = _state_snapshot().get("channel_wavs") or {}
    if not channel_wavs:
        raise HTTPException(status_code=400, detail="Run analyze or compare first")

    files = []
    try:
        for p in channel_wavs.values():
            try:
                files.append(sf.SoundFile(str(p)))
            except Exception:
                raise HTTPException(status_code=415, detail=f"Could not decode track '{Path(p).name}'")
        sr = files[0].samplerate
        if any(f.samplerate != sr for f in files):
            raise HTTPException(status_code=400,
                                detail="Tracks have different sample rates — can't mix into one file")
        total = max(len(f) for f in files)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        block = 1 << 20  # ~1M frames per pass keeps peak memory at a few MB per track
        try:
            with sf.SoundFile(tmp.name, "w", samplerate=sr, channels=1, subtype="FLOAT") as out:
                pos = 0
                while pos < total:
                    n = min(block, total - pos)
                    acc = np.zeros(n, dtype=np.float64)
                    for f in files:
                        if pos < len(f):
                            f.seek(pos)
                            d = f.read(min(n, len(f) - pos), dtype="float64", always_2d=False)
                            if getattr(d, "ndim", 1) > 1:
                                d = d.mean(axis=1)
                            acc[: len(d)] += d
                    out.write(acc.astype(np.float32))
                    pos += n
        except Exception:
            os.remove(tmp.name)
            raise
    finally:
        for f in files:
            f.close()
    fname = f"dub-combined-{len(channel_wavs)}tracks.wav"
    return FileResponse(tmp.name, media_type="audio/wav", filename=fname,
                        background=BackgroundTask(os.remove, tmp.name))


@app.get("/api/progress")
def get_progress() -> dict[str, Any]:
    return PROGRESS


# Script formats the parser accepts + audio the analyzer/original player can read.
_SCRIPT_EXTS = {".docx", ".srt", ".csv", ".tsv"}
_BROWSE_AUDIO_EXTS = AUDIO_EXTS | {".mp3", ".m4a"}


_BROWSE_CAP = 4000  # max entries returned per folder; more sets truncated=true


@app.get("/api/browse")
def browse(path: str = "") -> dict[str, Any]:
    """Server-side file browser for the HOSTED UI (a browser can't open the server's
    file dialogs). Locked to the folder in DQC_DATA_ROOT — requests outside it are
    rejected, so the tunnel never exposes the wider filesystem. Disabled (404) unless
    the env var is set, so the desktop app is unaffected. Also refuses to run without
    DQC_API_KEY — a filesystem browser must never be reachable unauthenticated."""
    root = os.environ.get("DQC_DATA_ROOT", "")
    if not root:
        raise HTTPException(status_code=404, detail="Server-side browsing is not enabled (set DQC_DATA_ROOT)")
    if not API_KEY:
        raise HTTPException(status_code=404, detail="Server-side browsing requires DQC_API_KEY to be set")
    # Reject NUL / control characters before they reach the filesystem (embedded-NUL
    # would otherwise raise ValueError -> a public 500).
    if any(ord(c) < 32 for c in path):
        raise HTTPException(status_code=400, detail="Invalid path")
    rootp = Path(root).resolve()
    if not rootp.is_dir():
        raise HTTPException(status_code=500, detail="DQC_DATA_ROOT does not exist on the server")
    rel = path.replace("\\", "/").strip().strip("/")
    try:
        target = (rootp / rel).resolve() if rel else rootp
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid path")
    # Containment: the resolved target must be the root or inside it (blocks ../, absolute
    # paths, and junctions/symlinks that resolve outside).
    if not (target == rootp or target.is_relative_to(rootp)):
        raise HTTPException(status_code=400, detail="Path escapes the shared folder")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")

    show = _BROWSE_AUDIO_EXTS | _SCRIPT_EXTS
    dirs: list[str] = []
    files: list[dict[str, Any]] = []
    truncated = False
    try:
        # Classify FIRST, then cap the shown results — never cap raw entries before
        # filtering (that would silently hide real audio behind unrelated files).
        for p in target.iterdir():
            if p.name.startswith("."):
                continue
            try:
                if p.is_dir():
                    dirs.append(p.name)
                elif p.suffix.lower() in show:
                    files.append({"name": p.name, "size": p.stat().st_size})
            except OSError:
                continue  # unreadable entry (permissions/junction) — skip, don't 500
            if len(dirs) + len(files) >= _BROWSE_CAP:
                truncated = True
                break
    except OSError:
        raise HTTPException(status_code=400, detail="Could not read that folder")
    dirs.sort(key=str.lower)
    files.sort(key=lambda f: str(f["name"]).lower())
    return {
        "path": str(target.relative_to(rootp)).replace("\\", "/").strip("."),
        "abs": str(target),
        "dirs": dirs,
        "files": files,
        "truncated": truncated,
    }


@app.get("/api/healthz")
def healthz() -> dict[str, Any]:
    # auth_required/browse_enabled tell the hosted UI what to show; this endpoint is
    # deliberately outside the API-key gate (see _require_api_key).
    return {
        "status": "ok",
        "auth_required": bool(API_KEY),
        # browse needs BOTH a data root and a key (server.py browse() enforces the key)
        "browse_enabled": bool(os.environ.get("DQC_DATA_ROOT") and API_KEY),
    }


# ---- hosted UI ----
# Serve the built React app (dist/) from this same server, so one ngrok tunnel exposes
# both UI and API on one origin. Registered last: every /api route above wins first.
# No dist/ (desktop dev, frozen exe) -> nothing is mounted and the API behaves as before.
_DIST_DIR = Path(__file__).resolve().parent.parent / "dist"
if _DIST_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST_DIR), html=True), name="ui")
