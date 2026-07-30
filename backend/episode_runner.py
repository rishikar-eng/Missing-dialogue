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

import json
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

from . import box_discovery, box_fetch, box_oauth, naming, xlang

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


# How many names to carry in the compact summary before it's just "+N more". These land in
# a Teams message, so the cap is about readability, not size.
_TOP_N = 4


def _hhmmss(s: float | None) -> str:
    """Episode timecode for a chat message — the same HH:MM:SS.s the workbook uses."""
    if s is None:
        return ""
    s = max(0.0, float(s))
    return f"{int(s // 3600):02d}:{int((s % 3600) // 60):02d}:{s % 60:04.1f}"


def _lang_summary(res: dict[str, Any]) -> dict[str, Any]:
    """Compact, JSON-safe digest of ONE language's analysis — the record every downstream
    consumer sees (S3 status.json for cloud runs, run_store for local ones, and from there
    the Teams reply).

    It used to carry only missing/mismatch/extra, so findings the pipeline had already
    computed — an entire character never delivered, a track out of sync, script rows that
    failed to parse and were therefore never checked — reached the workbook but never the
    chat. Those are exactly the findings that change what someone does next, so they're
    summarised here. The full detail still lives in the workbook; this is the headline.

    Keys `missing`/`mismatch`/`extra` keep their original names and meaning — older records
    already written to S3 lack the rest, so every reader must treat the new keys as optional.
    """
    align = res.get("alignment") or {}
    s = align.get("summary") or {}
    chars = res.get("characters") or []

    # Characters with scripted lines but no track at all (and not bundled into a group stem):
    # nobody delivered them. The single most actionable finding in the whole report.
    undelivered = [c.get("name") or c.get("id") for c in chars
                   if not c.get("channel") and not c.get("grouped_in")
                   and (c.get("line_count") or 0) > 0]

    # Who to chase first: characters ranked by how many of their lines are missing.
    missed: dict[str, int] = {}
    for e in align.get("errors") or []:
        if e.get("type") == "MISSING" and e.get("character"):
            missed[e["character"]] = missed.get(e["character"], 0) + 1
    by_name = {c.get("id"): (c.get("name") or c.get("id")) for c in chars}
    top = sorted(missed.items(), key=lambda kv: -kv[1])[:_TOP_N]

    # A uniformly late/early track is a whole-delivery problem, not a per-line one.
    sync = [{"channel": w.get("channel"), "offset_s": w.get("offset_s")}
            for w in (align.get("sync_warnings") or [])][:_TOP_N]

    # Dropped script rows are lines that were never checked — a "0 missing" that omits this
    # is misleading, so it travels with the counts rather than living only in the workbook.
    ps = res.get("parse_stats") or {}

    # A few concrete missing lines (timecode + who + what was said). Enough for a reviewer to
    # jump straight to the tape from chat without opening the workbook; the workbook still
    # holds all of them. Capped because this rides inside a chat message.
    samples = []
    for e in align.get("errors") or []:
        if e.get("type") != "MISSING" or e.get("script_start_s") is None:
            continue
        samples.append({
            "at": _hhmmss(e.get("script_start_s")),
            "character": by_name.get(e.get("character"), e.get("character")),
            "text": (e.get("text") or "").strip()[:70],
        })
        if len(samples) >= 6:
            break

    return {
        "missing": s.get("n_missing", 0),
        "mismatch": s.get("n_mismatch", 0),
        "extra": s.get("n_extra", 0),
        "misaligned": s.get("n_misaligned", 0),
        "characters_checked": s.get("n_characters_checked", 0),
        "tracks": len(res.get("channels") or []),
        "no_audio": len(undelivered),
        "undelivered": undelivered[:_TOP_N],
        "undelivered_more": max(0, len(undelivered) - _TOP_N),
        "top_missing": [{"character": by_name.get(cid, cid), "lines": n} for cid, n in top],
        "sync_warnings": sync,
        "loudness_flags": len(res.get("loudness_flags") or []),
        "naming_issues": len(res.get("naming_issues") or []),
        "script_lines": ps.get("parsed"),
        "script_dropped": ps.get("dropped") or 0,
        "examples": samples,
    }


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
                        _series = cfg.get("display_name", key)
                        _write_ref_audio(res["alignment"]["errors"], op,
                                         out_dir / naming.missing_flac(_series, n, lang, False),
                                         out_dir / naming.missing_flac(_series, n, lang, True))
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
        xlsx = out_dir / naming.report_xlsx(cfg.get("display_name", key), n)
        build_workbook(
            meta={"episode": f"EP{n:02d}", "series": cfg.get("display_name", key),
                  "generated_at": time.strftime("%Y-%m-%d %H:%M"),
                  "script_path": f"box:{sc['name']}",
                  "original_audio_path": f"box:{orig['name']}" if orig else "", "tol_s": 1.0},
            per_lang=per_lang, out_path=xlsx,
        )

        _stage("packaging", len(want), len(want))
        zip_path = out_dir / naming.bundle_zip(cfg.get("display_name", key), n)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(out_dir.iterdir()):
                if p.name != zip_path.name:
                    z.write(p, arcname=p.name)

        # Per-language analysis result as JSON (written AFTER the zip, so it stays out of the
        # studio's bundle). The Fargate fan-out uploads this so the cross-language Summary can
        # be aggregated from all languages without re-running any audio analysis.
        results_path: Path | None = out_dir / f"EP{n:02d}_results.json"
        try:
            results_path.write_text(json.dumps(per_lang, default=str, ensure_ascii=False),
                                    encoding="utf-8")
        except Exception:  # noqa: BLE001 — never fail a run over the companion JSON
            results_path = None

        summary = {lang: _lang_summary(r) for lang, r in per_lang.items()}
        # The cross-language reading (script problem vs one vendor's gap) — computed here so a
        # single-task run carries it without anyone re-reading the per-language results. The
        # per-LANGUAGE fan-out can't: each task sees one language, so its aggregate is rebuilt
        # from the uploaded xlang.json files (fargate.ensure_summary).
        try:
            cross = xlang.headline(per_lang)
        except Exception:  # noqa: BLE001 — a reporting extra must never fail the run
            cross = []
        return {
            "status": "ok",
            "cross_language": cross,
            "series": cfg.get("display_name", key),
            "episode": n,
            "languages": list(per_lang),
            "summary_by_language": summary,
            "notes": notes,
            "workbook": xlsx.name,
            "zip_name": zip_path.name,
            "zip_path": str(zip_path),
            "results_path": str(results_path) if results_path else None,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
