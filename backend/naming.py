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


def markers_mid(series: str | None, episode: int | str, lang: str) -> str:
    return _join(slug(series), _ep(episode), lang.title(), "ProTools-Markers") + ".mid"


def markers_ptx_from_csv(csv_name: str) -> str:
    """The .ptx that a marker CSV converts to, named from the CSV so the pair is obviously
    one deliverable.

    The dated `AudioQC-Report_<date>` middle is dropped, which is not cosmetic: a .ptx stores
    its own name INSIDE the file in a fixed-width field (58 bytes in the studio's sessions),
    and the workbook stem alone is 74. Dropping it lands on exactly the name shape the studio
    already has from the first POA batch — POA-Paulo-O-Apostolo_EP11_English_QC-Markers.ptx.
    Anything still too long is trimmed by backend.ptx.session_name_for.
    """
    from . import ptx
    stem = re.sub(r"_AudioQC-Report_\d{4}-\d{2}-\d{2}", "",
                  csv_name.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    return ptx.session_name_for(stem)
