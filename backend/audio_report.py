"""Workbook for an audio-only (scriptless) QC run — one sheet, triage-ordered.

The sound team verifies flags in Pro Tools, so rows are ordered the way they should be
checked: MISSING by confidence (high → low) then time, then EXTRA and MISALIGNED. Times are
given both in seconds and min:sec (they scrub in Audacity/Pro Tools, not in seconds).
"""
from __future__ import annotations

from typing import Any


def _ms(t: float | None) -> str:
    if t is None:
        return ""
    return f"{int(t // 60)}:{t % 60:04.1f}"


def build_audio_workbook(report: dict[str, Any], meta: dict[str, Any], out_path: str) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Audio QC"

    s = report.get("summary", {})
    conf = s.get("n_missing_by_confidence", {}) or {}
    head = [
        ("Series", meta.get("series", "")),
        ("Episode", meta.get("episode", "")),
        ("Original (reference)", meta.get("original", "")),
        ("Dub checked", meta.get("dub", "")),
        ("Generated", meta.get("generated_at", "")),
        ("Mode", "audio-only (no script) — original full mix vs dub full mix"),
        ("Original lines found", s.get("n_original_regions", 0)),
        ("Verifiable coverage", f"{s.get('coverage', 0.0):.0%} "
                                f"({s.get('n_unchecked', 0)} lines unreadable on one side)"),
        ("MISSING flags", f"{s.get('n_missing', 0)}  "
                          f"(high {conf.get('high', 0)} / medium {conf.get('medium', 0)}"
                          f" / low {conf.get('low', 0)})"),
        ("EXTRA flags", s.get("n_extra", 0)),
        ("MISALIGNED flags", s.get("n_misaligned", 0)),
    ]
    bold = Font(bold=True)
    for k, v in head:
        ws.append([k, v])
        ws.cell(row=ws.max_row, column=1).font = bold
    ws.append([])

    cols = ["Verify order", "Type", "Confidence", "Start", "End", "Start (s)", "End (s)",
            "Original line (transcribed)", "Best dub match", "What to check"]
    ws.append(cols)
    hdr_row = ws.max_row
    fill = PatternFill("solid", fgColor="1F4E79")
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=hdr_row, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill

    order = {"high": 0, "medium": 1, "low": 2}
    missing = sorted((e for e in report.get("errors", []) if e["type"] == "MISSING"),
                     key=lambda e: (order.get(e.get("confidence"), 3),
                                    e.get("script_start_s") or 0.0))
    others = sorted((e for e in report.get("errors", []) if e["type"] != "MISSING"),
                    key=lambda e: (e["type"], e.get("script_start_s") or 0.0))
    tint = {"high": "FCE4E4", "medium": "FFF2CC", "low": "EDEDED"}
    for i, e in enumerate(missing + others, start=1):
        st, en = e.get("script_start_s"), e.get("script_end_s")
        if e["type"] == "MISSING":
            note = "Listen to the dub here — the original line appears to have no dub."
        elif e["type"] == "EXTRA":
            note = "The dub speaks here but the original does not — added line?"
        else:
            note = f"Line present but shifted by {e.get('drift_s', 0):+.1f}s."
        best = e.get("coverage")
        ws.append([i, e["type"], e.get("confidence", ""), _ms(st), _ms(en),
                   st, en, e.get("text") or "", f"{best:.0%}" if best is not None else "", note])
        color = tint.get(e.get("confidence")) if e["type"] == "MISSING" else None
        if color:
            row_fill = PatternFill("solid", fgColor=color)
            for c in range(1, len(cols) + 1):
                ws.cell(row=ws.max_row, column=c).fill = row_fill

    widths = [11, 12, 11, 8, 8, 9, 9, 60, 13, 52]
    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=hdr_row, column=c).column_letter].width = w
        for r in range(hdr_row, ws.max_row + 1):
            ws.cell(row=r, column=c).alignment = Alignment(vertical="top",
                                                           wrap_text=(c in (8, 10)))
    ws.freeze_panes = ws.cell(row=hdr_row + 1, column=1)
    wb.save(out_path)
    return out_path
