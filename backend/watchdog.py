"""Box watch-folder agent — proactively tells Teams what landed and when QC can start.

Runs on a timer (cron), NOT inside a request. Each pass:
  1. walks a watched Box folder tree,
  2. diffs against the previous pass (state on disk) to find genuinely NEW files,
  3. classifies what arrived — script / original audio / dubbed speaker tracks,
  4. posts to Teams: what was added and by whom, and — the useful part — whether the
     episode now has EVERYTHING needed to run QC.

WHY A SEPARATE POSTING CHANNEL: the QC bot answers Teams *Outgoing* Webhooks, which can only
reply to an @mention — they cannot start a conversation. Announcing something unprompted needs
an **Incoming Webhook** (a per-channel URL Teams gives you), set as DQC_TEAMS_INCOMING.
Without it this module still runs and logs; it just can't speak.

Env:
  DQC_WATCH_FOLDERS   comma-separated Box folder ids to watch  (required)
  DQC_TEAMS_INCOMING  Teams Incoming Webhook URL to post into  (required to post)
  DQC_WATCH_DEPTH     how deep to walk (default 3)
  DQC_DATA_ROOT       where state is kept
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import box_oauth

_API = "https://api.box.com/2.0"
_FIELDS = "id,type,name,size,created_at,modified_at,created_by,uploader_display_name"
_STATE = Path(os.environ.get("DQC_DATA_ROOT", "/tmp")) / "watchdog_state.json"

AUDIO_EXT = (".wav", ".flac", ".ogg", ".aif", ".aiff", ".mp3", ".m4a")
SCRIPT_EXT = (".docx", ".doc", ".pdf", ".txt", ".srt", ".xlsx", ".csv")
# An "original"/reference mix is named for what it is; a dub speaker track is named for a person.
_ORIGINAL_HINTS = ("premix", "pre mix", "original", "source", "m&e", "mne", "mix", "master", "ref")
# Track folders are usually named for the language or "for ai"/"track"
_TRACKS_HINTS = ("track", "for ai", "voiceover", "vo", "dub", "stems", "speaker")
_LANGS = ("malayalam", "tamil", "telugu", "kannada", "bengali", "marathi", "hindi", "punjabi",
          "mal", "tam", "tel", "kan", "ben", "mar", "hin")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _list(token: str, folder_id: str) -> list[dict[str, Any]]:
    """One level of a Box folder, with uploader metadata. Paginated."""
    out: list[dict[str, Any]] = []
    params = {"fields": _FIELDS, "limit": "1000", "usemarker": "true"}
    while True:
        r = httpx.get(f"{_API}/folders/{folder_id}/items", params=params,
                      headers=_headers(token), timeout=60)
        r.raise_for_status()
        d = r.json()
        out.extend(d.get("entries", []))
        marker = d.get("next_marker")
        if not marker:
            return out
        params["marker"] = marker


def _folder_name(token: str, folder_id: str) -> str:
    """The folder's real Box name — a raw id reads badly in a chat message."""
    try:
        r = httpx.get(f"{_API}/folders/{folder_id}", params={"fields": "name"},
                      headers=_headers(token), timeout=20)
        return r.json().get("name") or f"Box folder {folder_id}"
    except Exception:
        return f"Box folder {folder_id}"


def scan_tree(token: str, folder_id: str, max_depth: int = 3) -> dict[str, dict[str, Any]]:
    """Walk a folder tree -> {file_id: {name, path, size, who, when, folder, folder_id}}.
    Depth-limited: a watched root can be huge, and we only care about episode-level files."""
    files: dict[str, dict[str, Any]] = {}

    def walk(fid: str, path: str, depth: int) -> None:
        try:
            entries = _list(token, fid)
        except Exception:
            return                                  # unreadable folder shouldn't kill the pass
        for e in entries:
            name = e.get("name", "")
            if e.get("type") == "folder":
                if depth < max_depth:
                    walk(e["id"], f"{path}/{name}", depth + 1)
            else:
                who = (e.get("uploader_display_name")
                       or (e.get("created_by") or {}).get("name") or "someone")
                files[e["id"]] = {"name": name, "path": f"{path}/{name}", "size": e.get("size") or 0,
                                  "who": who, "when": e.get("created_at") or "",
                                  "folder": path.rsplit("/", 1)[-1] or "root", "folder_id": fid}
    walk(str(folder_id), "", 0)
    return files


def classify(f: dict[str, Any]) -> str:
    """script | original | track | other — from the filename + where it sits.

    'original' vs 'track' is the interesting call: both are audio. A reference/original mix is
    named for WHAT IT IS (premix, M&E, master); a dubbed speaker track is named for WHO speaks,
    and lives in a language/track folder alongside its siblings.
    """
    low = f["name"].lower()
    ext = os.path.splitext(low)[1]
    folder_low = (f.get("folder") or "").lower()
    if ext in SCRIPT_EXT:
        return "script"
    if ext in AUDIO_EXT:
        in_track_folder = (any(h in folder_low for h in _TRACKS_HINTS)
                           or any(re.search(rf"\b{l}\b", folder_low) for l in _LANGS))
        if any(h in low for h in _ORIGINAL_HINTS) and not in_track_folder:
            return "original"
        return "track" if in_track_folder else "original"
    return "other"


def readiness(files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Can we run QC yet? Needs a script, an original/reference audio, and dub speaker tracks.
    Tracks are grouped by their folder, since one folder = one language's speaker set."""
    scripts = [f for f in files.values() if classify(f) == "script"]
    originals = [f for f in files.values() if classify(f) == "original"]
    tracks: dict[str, list[dict[str, Any]]] = {}
    for f in files.values():
        if classify(f) == "track":
            tracks.setdefault(f["folder"], []).append(f)
    # a real speaker-track set is several files, not one stray audio file
    track_sets = {k: v for k, v in tracks.items() if len(v) >= 2}
    return {
        "script": scripts, "original": originals, "track_sets": track_sets,
        "ready": bool(scripts and originals and track_sets),
    }


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(d: dict[str, Any]) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    tmp.replace(_STATE)


def post_teams(text: str, webhook: str | None = None) -> bool:
    """Post an unprompted message into the Teams channel.

    The endpoint is a Power Automate 'Send webhook alerts to a channel' flow. Verified by
    experiment against the live channel: a bare {"text": ...} body is accepted with HTTP 202
    but **never appears in the channel** — it must be an Adaptive Card (or a legacy
    MessageCard, kept below as a fallback). An Adaptive Card TextBlock renders a useful subset
    of markdown (**bold**, bullets, links), so the message body is reused as-is.
    """
    url = webhook or os.environ.get("DQC_TEAMS_INCOMING", "")
    if not url:
        print("[watchdog] no DQC_TEAMS_INCOMING set — would have posted:\n" + text)
        return False
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}]}}]}
    legacy = {"@type": "MessageCard", "@context": "http://schema.org/extensions",
              "summary": "QC Watchdog", "text": text}
    for body in (card, legacy):
        try:
            if httpx.post(url, json=body, timeout=20).status_code < 300:
                return True
        except Exception as e:  # never let a posting failure break the scan loop
            print("[watchdog] post failed:", e)
    return False


def _size(n: float) -> str:
    """Human file size. Studio deliveries run to hundreds of MB, but a stray small file
    shouldn't read as '0 MB'."""
    if not n:
        return ""
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f" · {n/div:.1f} {unit}".replace(".0 ", " ")
    return f" · {int(n)} B"


def _fmt(new: list[dict[str, Any]], rep: dict[str, Any], label: str) -> str:
    """The Teams message: what arrived (name + who), then whether QC can be STARTED.

    Deliberately does NOT claim the delivery is complete. How many speaker tracks an episode
    should have varies by studio, series and even episode — there is no reliable expected
    count, and determining whether every character was delivered is exactly what QC itself
    does. So this reports that the three required INGREDIENTS are present and QC can run;
    it never asserts that nothing is missing.
    """
    lines = [f"**📁 {label} — {len(new)} new file{'s' if len(new) != 1 else ''}**", ""]
    for f in sorted(new, key=lambda x: x["path"])[:15]:
        loc = f"  _(in {f['folder']})_" if f.get("folder") not in ("", "root") else ""
        lines.append(f"• **{f['name']}**{_size(f['size'])} — added by {f['who']}{loc}")
    if len(new) > 15:
        lines.append(f"• …and {len(new) - 15} more")
    lines.append("")
    lines.append(f"Script: {'✅ ' + rep['script'][0]['name'] if rep['script'] else '❌ not yet'}")
    lines.append(f"Original audio: {'✅ ' + rep['original'][0]['name'] if rep['original'] else '❌ not yet'}")
    if rep["track_sets"]:
        sets = ", ".join(f"{k} — {len(v)} track{'s' if len(v) != 1 else ''}"
                         for k, v in list(rep["track_sets"].items())[:5])
        lines.append(f"Dub speaker tracks: ✅ {sets}")
    else:
        lines.append("Dub speaker tracks: ❌ not yet")
    lines.append("")
    if rep["ready"]:
        lines.append("**▶ Everything needed to start QC is here.** "
                     "Say `@QC check <series> ep <n>` to kick it off.")
        lines.append("_QC will confirm whether every character's track actually made it — "
                     "track counts vary by studio, so that's what the check is for._")
    else:
        want = [n for n, k in (("script", "script"), ("original audio", "original"),
                               ("dub speaker tracks", "track_sets")) if not rep[k]]
        lines.append(f"_Still waiting on: {', '.join(want)}._")
    return "\n".join(lines)


def run_once(folders: list[str] | None = None, announce: bool = True) -> dict[str, Any]:
    """One scan pass over every watched folder. Returns a per-folder summary."""
    token = box_oauth.get_token()
    folders = folders or [x.strip() for x in os.environ.get("DQC_WATCH_FOLDERS", "").split(",") if x.strip()]
    depth = int(os.environ.get("DQC_WATCH_DEPTH", "3"))
    state = _load_state()
    summary: dict[str, Any] = {}

    for fid in folders:
        files = scan_tree(token, fid, depth)
        prev = state.get(fid, {}).get("files", {})
        new = [f for k, f in files.items() if k not in prev]
        rep = readiness(files)
        first_run = not prev and not state.get(fid)
        label = state.get(fid, {}).get("label") or _folder_name(token, fid)
        if new and announce and not first_run:
            post_teams(_fmt(new, rep, label))
        elif first_run:
            print(f"[watchdog] baseline for {fid}: {len(files)} files (no announcement)")
        state[fid] = {"files": {k: {"name": v["name"]} for k, v in files.items()},
                      "label": label, "last_scan": time.time(), "ready": rep["ready"]}
        summary[fid] = {"total": len(files), "new": len(new), "ready": rep["ready"]}
    _save_state(state)
    return summary


if __name__ == "__main__":
    print(json.dumps(run_once(), indent=1))
