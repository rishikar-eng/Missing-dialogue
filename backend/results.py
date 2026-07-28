"""Load a finished run's FULL per-language analysis results, wherever that run happened.

The chat agent's compact summary answers "how bad is it". Anything past that — *which* lines,
*which* character, *why* is this one missing in Tamil only — needs the complete result, and
that lives in three different places depending on how the run executed:

  * local (in-process)  -> the `EP{NN}_results.json` episode_runner already writes next to the
                           zip; its path is recorded in the run store
  * cloud, per-language -> one `xlang.json` per language in S3 (uploaded for the cross-language
                           Summary), merged back into one {language: result} map
  * cloud, single task  -> one `xlang.json` holding every language

Callers get the same shape from all three. Results are megabytes, so this is for background
work only — never call it on the Teams reply path (see the 5s webhook constraint).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from . import run_store

# Small LRU-ish cache: a user asking three follow-up questions about the same run shouldn't
# re-download megabytes each time. Bounded because these are big.
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_ORDER: list[str] = []
_CACHE_MAX = max(1, int(os.environ.get("DQC_RESULTS_CACHE", "3")))
_LOCK = threading.Lock()


def _cache_put(job_id: str, data: dict[str, Any]) -> None:
    with _LOCK:
        _CACHE[job_id] = data
        _CACHE_ORDER.append(job_id)
        while len(_CACHE_ORDER) > _CACHE_MAX:
            _CACHE.pop(_CACHE_ORDER.pop(0), None)


def load(job_id: str) -> dict[str, Any]:
    """{language: analysis result} for a finished run, or {} when it can't be recovered."""
    with _LOCK:
        if job_id in _CACHE:
            return _CACHE[job_id]
    rec = run_store.get(job_id) or {}
    data: dict[str, Any] = {}

    rp = rec.get("results_path")
    if rp and Path(rp).is_file():
        try:
            data = json.loads(Path(rp).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}

    if not data and rec.get("compute", "").startswith("fargate"):
        from . import fargate
        c = fargate._cfg()
        s3 = fargate._s3()
        job_ids = ([i["job_id"] for i in (rec.get("langs") or {}).values() if i.get("job_id")]
                   or [job_id])
        for jid in job_ids:
            try:
                obj = s3.get_object(Bucket=c["bucket"], Key=f"{c['prefix']}/{jid}/xlang.json")
                data.update(json.loads(obj["Body"].read()))
            except Exception:  # noqa: BLE001 — a skipped/failed language has no xlang.json
                continue

    if data:
        _cache_put(job_id, data)
    return data


# --------------------------------------------------------------------------- #
# Query helpers — the shapes the chat agent's tools hand back to the model.
# Each returns plain JSON-able data, trimmed to chat size.
# --------------------------------------------------------------------------- #
def _hhmmss(s: float | None) -> str:
    if s is None:
        return ""
    s = max(0.0, float(s))
    return f"{int(s // 3600):02d}:{int((s % 3600) // 60):02d}:{s % 60:04.1f}"


def findings(per_lang: dict[str, Any], language: str | None = None, kind: str | None = None,
             character: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
    """Individual findings with timecodes, filtered by language / type / character."""
    want = (kind or "").upper().strip() or None
    who = (character or "").lower().strip() or None
    out: list[dict[str, Any]] = []
    for lang, res in per_lang.items():
        if language and lang.lower() != language.lower():
            continue
        names = {c.get("id"): (c.get("name") or c.get("id"))
                 for c in (res.get("characters") or [])}
        for e in ((res.get("alignment") or {}).get("errors") or []):
            if want and e.get("type") != want:
                continue
            name = names.get(e.get("character"), e.get("character")) or ""
            if who and who not in str(name).lower():
                continue
            out.append({
                "language": lang, "type": e.get("type"), "character": name,
                "at": _hhmmss(e.get("script_start_s")),
                "text": (e.get("text") or "")[:120],
                "track": e.get("channel"),
                "detail": (e.get("message") or "")[:200],
            })
            if len(out) >= limit:
                return out
    return out


def character_report(per_lang: dict[str, Any], character: str) -> dict[str, Any]:
    """One character across every language: how they were mapped, how many lines are missing,
    and whether the mapping itself is in doubt — the usual follow-up to 'why is X missing?'."""
    who = character.lower().strip()
    per: dict[str, Any] = {}
    for lang, res in per_lang.items():
        for c in (res.get("characters") or []):
            name = str(c.get("name") or c.get("id") or "")
            if who not in name.lower() and who not in str(c.get("id") or "").lower():
                continue
            errs = [e for e in ((res.get("alignment") or {}).get("errors") or [])
                    if e.get("character") == c.get("id")]
            per[lang] = {
                "name": name,
                "scripted_lines": c.get("line_count"),
                "track": c.get("channel"),
                "mapped_by": c.get("mapped_by"),
                "extra_tracks": c.get("extra_channels") or [],
                "grouped_in": c.get("grouped_in"),
                "no_audio_delivered": not c.get("channel") and not c.get("grouped_in"),
                "missing": sum(1 for e in errs if e.get("type") == "MISSING"),
                "wrong_speaker": sum(1 for e in errs if e.get("type") == "MISMATCH"),
                "misaligned": sum(1 for e in errs if e.get("type") == "MISALIGNED"),
                "first_missing_at": next((_hhmmss(e.get("script_start_s")) for e in errs
                                          if e.get("type") == "MISSING"), None),
            }
            break
    return {"character": character, "by_language": per,
            "note": ("not found in this episode's script" if not per else None)}


def cross_language(per_lang: dict[str, Any], limit: int = 12) -> list[dict[str, Any]]:
    """The script-vs-dub root cause per affected character (same judgement as the workbook)."""
    from . import xlang
    return [{k: r[k] for k in ("character", "cause", "reading", "languages_affected",
                               "per_language")}
            for r in xlang.rows(per_lang)[:limit]]


def overview(per_lang: dict[str, Any]) -> dict[str, Any]:
    """Counts per language, plus the caveats that qualify them."""
    out: dict[str, Any] = {}
    for lang, res in per_lang.items():
        s = (res.get("alignment") or {}).get("summary") or {}
        chars = res.get("characters") or []
        out[lang] = {
            "missing": s.get("n_missing"), "wrong_speaker": s.get("n_mismatch"),
            "misaligned": s.get("n_misaligned"), "extra": s.get("n_extra"),
            "characters": len(chars), "tracks": len(res.get("channels") or []),
            "no_audio": [c.get("name") for c in chars
                         if not c.get("channel") and not c.get("grouped_in")
                         and (c.get("line_count") or 0) > 0],
            "sync_warnings": [w.get("message") for w in
                              ((res.get("alignment") or {}).get("sync_warnings") or [])],
            "script_rows_dropped": (res.get("parse_stats") or {}).get("dropped"),
            "mapping_issues": [str(i)[:160] for i in (res.get("naming_issues") or [])[:5]],
        }
    return out
