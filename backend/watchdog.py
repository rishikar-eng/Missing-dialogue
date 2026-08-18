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


def episode_of(files: dict[str, dict[str, Any]]) -> int | None:
    """Which episode these files belong to, read from their names/paths ('Gavv_#42_…',
    'GAVV EPI 42 …', 'EP 42'). The most common number wins so one oddly-named file can't
    mislabel the batch. None when nothing looks like an episode."""
    counts: dict[int, int] = {}
    for f in files.values():
        hay = f"{f.get('path', '')} {f['name']}"
        m = (re.search(r"#\s*(\d{1,3})", hay)
             or re.search(r"(?i)\bEP(?:ISODE|I)?\s*[-_]?\s*(\d{1,3})\b", hay))
        if m:
            n = int(m.group(1))
            counts[n] = counts.get(n, 0) + 1
    return max(counts, key=counts.get) if counts else None


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
        "episode": episode_of(files),
        "ready": bool(scripts and originals and track_sets),
    }


def series_of_folder(fid: str) -> str | None:
    """Which registered series owns this watched folder, by id — or None if it is not one of
    theirs (the test folder, an ad-hoc drop).

    Before this existed `series_readiness` hardcoded "gavv", which was invisible while the
    only watched folders WERE Gavv's. The moment POA's delivery folder was watched
    (2026-08-17) every POA file produced a card headed "Kamen Rider Gavv" carrying Gavv's
    language readiness. Resolve the series from the folder that the file landed in."""
    from . import series_registry
    fid = str(fid)
    for key, cfg in ((s["key"], s) for s in series_registry.all_series()):
        b = cfg.get("box", {}) or {}
        ids: set[str] = set()
        for f in ("scripts_folder", "premix_folder", "original_mix_folder",
                  "mixes_folder", "me_stems_folder"):
            if b.get(f):
                ids.add(str(b[f]))
        for group in ("mixes_folders", "voiceover"):
            for v in (b.get(group) or {}).values():
                for one in (v if isinstance(v, list) else [v]):
                    ids.add(str(one))
        for v in (b.get("premix_folders") or []):
            ids.add(str(v))
        if fid in ids:
            return key
    return None


def series_readiness(token: str, episode: int, series_key: str | None = None) -> dict[str, Any] | None:
    """Authoritative readiness for a registered series' episode, via the same check the
    `@QC check` command uses. Far cheaper AND more complete than crawling the voiceover
    tree: it looks up just this episode's script/original/tracks across every language
    (~2s), instead of walking ~1000 files per language. Returns None if unavailable.

    `series_key` must name the series the files actually came from — see series_of_folder."""
    try:
        from . import box_discovery, series_registry
        # No silent default. Defaulting to "gavv" is precisely the bug this function had:
        # a caller that cannot name the series must get the generic card, not Gavv's.
        if not series_key:
            return None
        r = series_registry.resolve(series_key)
        if not r:
            return None
        key, cfg = r
        # A SCRIPTLESS-ONLY SERIES HAS NO SCRIPT TO WAIT FOR. check_episode is the script-QC
        # check; run it on POA and every card reads "Script ❌ / Original ❌ / not enough
        # delivered" for episodes we have actually QC'd. Same branch server.py already takes
        # for `check ep N`.
        if not cfg["box"].get("scripts_folder"):
            from . import audio_jobs
            av = audio_jobs.availability(key, cfg, int(episode))
            return {
                "series": av.get("series"), "episode": episode,
                "alias": (cfg.get("aliases") or [key])[0],
                "scriptless": True,
                "script_ok": None,                      # not applicable, not missing
                "original_ok": bool(av.get("original")),
                "original_name": av.get("original"),
                "ready": av.get("languages_ready") or {},
                "not_delivered": av.get("not_delivered") or [],
                "unusable": av.get("unusable") or {},
                "runnable": av.get("runnable"),
            }
        rep = box_discovery.check_episode(token, key, cfg, int(episode))
        langs = rep.get("languages", {})
        return {
            "series": rep.get("series"), "episode": episode,
            # the word the user actually types — "gavv", "poa" — not the display name
            "alias": (cfg.get("aliases") or [key])[0],
            "script_ok": rep["script"].get("present"),
            "original_ok": rep["original"].get("present"),
            "ready": {l: v.get("tracks") for l, v in langs.items() if v.get("present")},
            "not_delivered": [l for l, v in langs.items() if not v.get("present")],
            "runnable": rep["summary"]["runnable"],
        }
    except Exception as e:  # noqa: BLE001 — never break a scan over the enrichment step
        print("[watchdog] series check failed:", str(e)[:120])
        return None


def _fmt_series(new: list[dict[str, Any]], s: dict[str, Any]) -> str:
    """Card for a registered series: what landed + this episode's real cross-language state."""
    lines = [f"**📁 {s['series']} — EP {s['episode']} · {len(new)} new file"
             f"{'s' if len(new) != 1 else ''}**", ""]
    for f in sorted(new, key=lambda x: x["path"])[:12]:
        loc = f"  _(in {f['folder']})_" if f.get("folder") not in ("", "root") else ""
        lines.append(f"• **{f['name']}**{_size(f['size'])} — added by {f['who']}{loc}")
    if len(new) > 12:
        lines.append(f"• …and {len(new) - 12} more")
    scriptless = bool(s.get("scriptless"))
    lines.append("")
    # A scriptless series has no script and never will — printing "Script: ❌ not yet" reads
    # as a missing delivery the studio should chase, when in fact nothing is owed.
    if not scriptless:
        lines.append(f"Script: {'✅' if s['script_ok'] else '❌ not yet'}")
    lines.append(f"Original audio: {'✅' if s['original_ok'] else '❌ not yet'}")
    noun = "Dub mixes" if scriptless else "Dub speaker tracks"
    if s["ready"]:
        lines.append(f"{noun}: ✅ " + ", ".join(f"{l} ({n})" for l, n in s["ready"].items()))
    else:
        lines.append(f"{noun}: ❌ not yet")
    if s["not_delivered"]:
        lines.append(f"Not delivered yet: {', '.join(s['not_delivered'])}")
    for lang, why in (s.get("unusable") or {}).items():
        lines.append(f"⚠️ {lang}: {why}")
    lines.append("")
    alias = s.get("alias") or "<series>"
    # the command differs by mode: scriptless series are run with `run scriptless qc`
    cmd = (f"@QC check scriptless qc {alias} ep {s['episode']}" if scriptless
           else f"@QC check {alias} ep {s['episode']}")
    lines.append(f"**▶ Ready to QC.** Say `{cmd}` to kick it off."
                 if s["runnable"] else "_Not enough delivered to QC this episode yet._")
    return "\n".join(lines)


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
    # SAY WHAT THE CHANNEL ACTUALLY DID. This used to return False silently on any non-2xx:
    # a disabled Power Automate flow or an expired connection produced a completely clean log
    # — 27k lines, zero errors — that looked exactly like a healthy watchdog with nothing to
    # report. The one failure mode this endpoint is known to have (HTTP 202 accepted, message
    # never rendered) is invisible from the status code alone, so log which body shape won.
    for kind, body in (("adaptive-card", card), ("legacy-messagecard", legacy)):
        try:
            r = httpx.post(url, json=body, timeout=20)
            if r.status_code < 300:
                print(f"[watchdog] posted to Teams — {kind}, HTTP {r.status_code}")
                return True
            # never log `url`: it carries the flow's signature
            print(f"[watchdog] post REJECTED — {kind}, HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:  # never let a posting failure break the scan loop
            print(f"[watchdog] post failed — {kind}: {e}")
    print("[watchdog] POST FAILED — the channel was NOT notified")
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
    # Lead with the EPISODE when the filenames reveal one — that's what the team tracks; the
    # folder is just where it landed. This card is the fallback for a folder no registered
    # series claims, so the folder's own name is the only honest label: the old code printed
    # "Kamen Rider Gavv" unconditionally, on the since-falsified premise that Gavv was the
    # only registered series.
    ep = rep.get("episode")
    head = f"{label} — EP {ep}" if ep else label
    lines = [f"**📁 {head} — {len(new)} new file{'s' if len(new) != 1 else ''}**", ""]
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
        ep = rep.get("episode")
        # This card is the fallback for a folder NO registered series owns, so there is no
        # alias to offer — say so rather than guessing a series the files may not belong to.
        cmd = f"@QC check <series> ep {ep}" if ep else "@QC check <series> ep <n>"
        lines.append(f"**▶ Ready to QC.** Say `{cmd}` to kick it off.")
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
        posted = None
        if new and announce and not first_run:
            # THE EPISODE IS THE ONE THAT JUST ARRIVED, not the folder's most common number.
            # readiness() votes across every file under the watched root, which is right when
            # a root IS one episode's drop (the test folder) and nonsense once it is a whole
            # series tree: a file in "EP 50" announced itself as EP 2, because 2 was the
            # commonest number among 147 files spanning 50 episodes.
            ep = episode_of({str(i): f for i, f in enumerate(new)}) or rep.get("episode")
            # ...and the series is whichever one owns THIS folder, never a hardcoded default.
            skey = series_of_folder(fid)
            srep = series_readiness(token, ep, skey) if (ep and skey) else None
            # KEEP THE VERDICT. Discarding this was why nobody could tell a working watchdog
            # from one whose webhook had been silently rejecting every announcement.
            posted = post_teams(_fmt_series(new, srep) if srep else _fmt(new, rep, label))
        elif first_run:
            print(f"[watchdog] baseline for {fid}: {len(files)} files (no announcement)")
        state[fid] = {"files": {k: {"name": v["name"]} for k, v in files.items()},
                      "label": label, "last_scan": time.time(), "ready": rep["ready"]}
        summary[fid] = {"total": len(files), "new": len(new), "ready": rep["ready"]}
        if posted is not None:                 # only meaningful when we tried to announce
            summary[fid]["posted"] = posted
    _save_state(state)
    return summary


if __name__ == "__main__":
    print(json.dumps(run_once(), indent=1))
