"""One place for user-facing output file names.

These files land in Box folders and Teams chats in front of studio staff, so a name must say
what the file is without opening it: series, episode, language, artifact kind, date. Internal
S3 keys and JSON side-files (status.json, xlang.json) are NOT named here — only deliverables.
"""
from __future__ import annotations

import re
import time


def slug(name: str | None) -> str:
    """'KAMEN RIDER (2023)' -> 'Kamen-Rider-2023'; empty/None -> ''."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", (name or "").strip()) if w]
    return "-".join(w if w.isupper() and len(w) <= 4 else w.capitalize() for w in words)


def _stamp() -> str:
    return time.strftime("%Y-%m-%d")


def _ep(episode: int | str) -> str:
    return f"EP{int(episode):02d}" if str(episode).isdigit() else slug(str(episode))


def _join(*parts: str) -> str:
    return "_".join(p for p in parts if p)


def report_xlsx(series: str | None, episode: int | str) -> str:
    return _join(slug(series), _ep(episode), "QC-Report", _stamp()) + ".xlsx"


def summary_xlsx(series: str | None, episode: int | str) -> str:
    return _join(slug(series), _ep(episode), "QC-Summary-All-Languages", _stamp()) + ".xlsx"


def bundle_zip(series: str | None, episode: int | str) -> str:
    return _join(slug(series), _ep(episode), "QC-Bundle", _stamp()) + ".zip"


def missing_flac(series: str | None, episode: int | str, lang: str, timeline: bool) -> str:
    kind = "Missing-Lines-On-Timeline" if timeline else "Missing-Lines-Only"
    return _join(slug(series), _ep(episode), lang.title(), kind) + ".flac"


def audio_qc_xlsx(series: str | None, episode: int | str, dub_lang: str) -> str:
    return _join(slug(series), _ep(episode), dub_lang.title(), "AudioQC-Report", _stamp()) + ".xlsx"
