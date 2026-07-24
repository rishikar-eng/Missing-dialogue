"""Run QC for one series+episode end-to-end from Box and package the result — the engine
behind the agent's `run_qc` tool (`/api/agent/run`).

Given a resolved series config + episode, it downloads the shared English script and
original premix once, then per available language downloads the per-speaker stems, runs the
shared analysis pipeline, and writes ONE multi-language workbook plus the MISSING-only
stitched + timeline reference audio. Everything is zipped into EP{NN}.zip for delivery.

Promoted from the deploy-only batch runner so it lives in the backend and reads the series
registry (via box_discovery) instead of hardcoded folder ids. Runs inside a jobs.submit
worker (which already holds the heavy-analysis slot), so it calls _run_analysis directly.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

import numpy as np
import soundfile as sf

from . import box_discovery, box_fetch, box_oauth

AUDIO_EXT = box_discovery.AUDIO_EXT
_OUT_ROOT = Path(os.environ.get("DQC_DATA_ROOT", tempfile.gettempdir())) / "agent_out"
Stage = Callable[[str, int, int], None]


def _write_ref_audio(errors: list[dict], original_path: str, out_path: Path,
                     timeline_path: Path | None = None,
                     pad_s: float = 2.5, gap_s: float = 0.6) -> Path | None:
    """MISSING-only reference audio: the original-language audio of every genuinely MISSING
    line. `out_path` = stitched (clips back-to-back); `timeline_path` = same clips at their
    real episode timecodes (silent elsewhere), for lining up against the dub stems. MISMATCH
    is excluded (delivered, just by the wrong speaker). Returns the stitched path or None."""
    wins = sorted(
        (max(0.0, e["script_start_s"] - pad_s), (e.get("script_end_s") or e["script_start_s"]) + pad_s)
        for e in errors
        if e.get("type") == "MISSING" and e.get("script_start_s") is not None
    )
    merged: list[list[float]] = []
    for s, en in wins:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], en)
        else:
            merged.append([s, en])
    if not merged:
        return None
    with sf.SoundFile(str(original_path)) as f:
        sr, total = f.samplerate, len(f)
        gap = np.zeros(int(gap_s * sr), dtype=np.float32)
        chunks: list[np.ndarray] = []
        tl = np.zeros(total, dtype=np.float32) if timeline_path else None
        for s, en in merged:
            i0, i1 = max(0, int(s * sr)), min(total, int(en * sr))
            if i1 <= i0:
                continue
            f.seek(i0)
            d = f.read(i1 - i0, dtype="float32", always_2d=False)
            if getattr(d, "ndim", 1) > 1:
                d = d.mean(axis=1)
            chunks += [d, gap]
            if tl is not None:
                tl[i0:i0 + len(d)] = d
        out = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    if not len(out):
        return None
    sf.write(str(out_path), out, sr, format="FLAC")
    if tl is not None:
        sf.write(str(timeline_path), tl, sr, format="FLAC")
    return out_path


def _download_stems(token: str, box: box_discovery._Box, cfg: dict[str, Any],
                    lang: str, n: int, work: Path) -> Path | None:
    """One language's stems -> work/<lang>, with retry (Box intermittently drops a
    connection mid-transfer). None if that language isn't delivered for this episode."""
    st = box_discovery.find_stems(box, cfg, lang, n)
    if not st:
        return None
    ids = [f["id"] for f in box.listing(st["id"])["files"] if f["name"].lower().endswith(AUDIO_EXT)]
    if not ids:
        return None
    tracks = work / lang
    last: Exception | None = None
    for attempt in range(4):
        try:
            box_fetch.download_files(token, ids, tracks)
            return tracks
        except Exception as e:  # noqa: BLE001
            last = e
            shutil.rmtree(tracks, ignore_errors=True)
            time.sleep(4 * (attempt + 1))
    raise last if last else RuntimeError("stem download failed")


def run(key: str, cfg: dict[str, Any], n: int, *,
        languages: list[str] | None = None, ref_audio: bool = True,
        stage: Stage | None = None) -> dict[str, Any]:
    """Analyse an episode across its available languages and package EP{NN}.zip.

    Returns a result dict: status, per-language missing/extra summary, the workbook + audio
    filenames, and the absolute zip path (served by /api/agent/download)."""
    # Lazy import of the analysis pipeline to avoid a server<->runner import cycle.
    from .server import AnalyzeRequest, _check_analyze_inputs, _run_analysis  # noqa: PLC0415
    from .excel_report import build_workbook                                  # noqa: PLC0415

    def _stage(msg: str, done: int = 0, total: int = 0) -> None:
        if stage:
            stage(msg, done, total)

    token = box_oauth.get_token()

    # Refresh the ElevenLabs voice bank from the studio's live Box sheet (per series) so the
    # workbook's Voice-ID check reflects the CURRENT list, not a committed snapshot. Cheap
    # (etag-gated) and never fatal — a failure just keeps the last-known bank.
    vl = (cfg.get("box") or {}).get("voice_list") or {}
    if vl.get("file_id"):
        from . import voices as _voices  # noqa: PLC0415
        _stage("voice list: " + _voices.refresh_from_box(token, vl["file_id"], vl.get("name")))

    box = box_discovery._Box(token)
    sc = box_discovery.find_script(box, cfg, n)
    if not sc:
        return {"status": "error", "why": "no English script for this episode"}
    orig = box_discovery.find_original(box, cfg, n)
    want = languages or list(cfg.get("languages", []))

    out_dir = _OUT_ROOT / uuid.uuid4().hex[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"agent-ep{n}-"))
    per_lang: dict[str, dict] = {}
    notes: dict[str, str] = {}
    try:
        _stage("fetching script + original audio", 0, len(want))
        sp = box_fetch.download_file(token, sc["id"], work / "script")
        op = box_fetch.download_file(token, orig["id"], work / "orig") if orig else None

        for i, lang in enumerate(want):
            _stage(f"{lang}: downloading stems", i, len(want))
            try:
                tracks = _download_stems(token, box, cfg, lang, n, work)
            except Exception as e:  # noqa: BLE001
                notes[lang] = f"dl-ERR:{str(e)[:50]}"
                continue
            if not tracks:
                notes[lang] = "not delivered"
                continue
            _stage(f"{lang}: analysing", i, len(want))
            try:
                req = AnalyzeRequest(script_path=str(sp), audio_dir=str(tracks),
                                     original_audio_path=str(op) if op else None, tol_s=1.0)
                res = _run_analysis(req, *_check_analyze_inputs(req))
                res["_audio_dir"] = f"box:{lang}/EP{n}"
                res["characters"] = [c if isinstance(c, dict) else c.model_dump()
                                     for c in res["characters"]]
                per_lang[lang] = res
                s = res["alignment"]["summary"]
                notes[lang] = f"{s['n_missing']} missing / {s['n_extra']} extra"
                if ref_audio and op:
                    try:
                        _write_ref_audio(res["alignment"]["errors"], op,
                                         out_dir / f"EP{n:02d}_{lang}_MISSING_only.flac",
                                         out_dir / f"EP{n:02d}_{lang}_MISSING_timeline.flac")
                    except Exception as e:  # noqa: BLE001
                        notes[lang] += f" (ref-audio err: {str(e)[:40]})"
            except Exception as e:  # noqa: BLE001
                notes[lang] = f"ERR:{str(getattr(e, 'detail', None) or e)[:60]}"
            finally:
                shutil.rmtree(tracks, ignore_errors=True)

        if not per_lang:
            shutil.rmtree(out_dir, ignore_errors=True)
            return {"status": "skip", "why": "no language had usable stems", "languages": notes}

        _stage("building workbook", len(want), len(want))
        xlsx = out_dir / f"EP{n:02d}.xlsx"
        build_workbook(
            meta={"episode": f"EP{n:02d}", "series": cfg.get("display_name", key),
                  "generated_at": time.strftime("%Y-%m-%d %H:%M"),
                  "script_path": f"box:{sc['name']}",
                  "original_audio_path": f"box:{orig['name']}" if orig else "", "tol_s": 1.0},
            per_lang=per_lang, out_path=xlsx,
        )

        _stage("packaging", len(want), len(want))
        zip_path = out_dir / f"EP{n:02d}_QC.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(out_dir.iterdir()):
                if p.name != zip_path.name:
                    z.write(p, arcname=p.name)

        summary = {lang: {"missing": r["alignment"]["summary"]["n_missing"],
                          "mismatch": r["alignment"]["summary"].get("n_mismatch", 0),
                          "extra": r["alignment"]["summary"]["n_extra"]}
                   for lang, r in per_lang.items()}
        return {
            "status": "ok",
            "series": cfg.get("display_name", key),
            "episode": n,
            "languages": list(per_lang),
            "summary_by_language": summary,
            "notes": notes,
            "workbook": xlsx.name,
            "zip_name": zip_path.name,
            "zip_path": str(zip_path),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
