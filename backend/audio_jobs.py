"""Audio-only (scriptless) QC as a Teams-triggered Fargate job.

Original full mix vs dub full mix — no script, no stems, background music baked in. The
heavy task runs the sonar image (Demucs separation → VAD-gated Groq whisper → LaBSE →
evidence-gated flags) and publishes an AudioQC workbook + report.json to S3. This module
discovers the two Box files from the series registry, launches the task with a short-lived
Box access token (the task never sees the rotating refresh token), and reads results back
for the agent. Results are read S3-first so status survives a server restart.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from . import box_discovery, box_oauth, fargate

_JOBS: dict[str, dict[str, Any]] = {}          # job_id -> {task_arn, series, episode, lang}


def _sq(s: str) -> str:
    return box_discovery._sq(s)


def find_dub_mix(box: box_discovery._Box, cfg: dict[str, Any], lang: str,
                 n: int) -> dict[str, Any] | None:
    """The delivered full mix for one dub language: <language folder>/EP NN/*_ST_MIX.wav.
    Prefers the final ST_MIX; falls back to the language PREMIX if that's all there is.

    Folder resolution: per-language `mixes_folders` map first (the real Box layout is one
    folder PER LANGUAGE — the old single `mixes_folder` id was actually Tamil's folder, which
    is why every other language showed 'not delivered'), falling back to `mixes_folder`.
    Inside a per-language folder every file IS that language, so no language-name filter —
    filenames misspell languages anyway ('Malyalam_ST_MIX .wav', stray space included)."""
    per_lang = (cfg["box"].get("mixes_folders") or {}).get(lang)
    roots = per_lang or cfg["box"].get("mixes_folder")
    if not roots:
        return None
    # A value may be ONE folder id or a LIST of them — series delivered in monthly Box
    # folders (Mowgli/Chikoo get a fresh tree each month) list every month's language folder.
    for root in ([roots] if isinstance(roots, str) else roots):
        sub = next((d for d in box.listing(root)["folders"]
                    if box_discovery._ep_num(d["name"]) == n), None)
        if not sub:
            continue
        files = [f for f in box.listing(sub["id"])["files"]
                 if f["name"].lower().endswith(".wav")
                 and (per_lang or lang.upper() in f["name"].upper())]
        # Stage preference is per-series. `dub_prefer` lists substring keys in order (POA:
        # ["dialogue","premix","mix"] — a clean DIALOGUE track beats any mix for QC and
        # skips dub-side separation entirely); else dub_stage='premix' compares
        # premix-vs-premix when the original only exists as a premix (mixing stages
        # manufactures drift — EP38/40); default prefers the final ST_MIX.
        stages = (cfg["box"].get("dub_prefer")
                  or (["premix", "stmix"] if cfg["box"].get("dub_stage") == "premix"
                      else ["stmix", "premix"]))
        for stage_key in stages:
            if stage_key == "mix":     # plain 'mix' must not swallow 'premix'/'stmix' names
                hit = [f for f in files
                       if "mix" in _sq(f["name"]) and "premix" not in _sq(f["name"])]
            else:
                hit = [f for f in files if stage_key in _sq(f["name"])]
            if hit:
                # Revisions sort last lexically (…_R2 > …_R1 > base), so desc picks newest.
                hit.sort(key=lambda f: f["name"], reverse=True)
                pick = dict(hit[0])
                pick["_stage"] = stage_key
                return pick
    return None


def find_original_video(box: box_discovery._Box, cfg: dict[str, Any],
                        n: int) -> dict[str, Any] | None:
    """A VIDEO as the original (POA: no original audio deliverable exists, only low-res
    episode videos) — the runner extracts its audio track and separates dialogue as usual."""
    folder = cfg["box"].get("original_videos_folder")
    if not folder:
        return None
    vids = [f for f in box.listing(folder)["files"]
            if f["name"].lower().endswith((".mp4", ".mov", ".mkv", ".m4v"))
            and box_discovery.ep_of(f["name"]) == n]
    vids.sort(key=lambda f: f["name"], reverse=True)
    return vids[0] if vids else None


def find_original_mix(box: box_discovery._Box, cfg: dict[str, Any],
                      n: int) -> dict[str, Any] | None:
    """The original's FINAL mix (…_HINDI_ST_MIX.wav), which is the like-for-like reference
    for a delivered dub ST_MIX. The premix is an earlier stage: different SFX and levels, and
    for some episodes a different length outright (EP38/EP40's mixes run ~16 s longer than
    their premixes) — comparing across stages manufactures drift and false flags."""
    folder = cfg["box"].get("original_mix_folder")
    if not folder:
        return None
    cands = [f for f in box.listing(folder)["files"]
             if f["name"].lower().endswith(".wav")
             and box_discovery.ep_of(f["name"]) == n
             and "premix" not in _sq(f["name"]) and "stmix" in _sq(f["name"])]
    cands.sort(key=lambda f: len(f["name"]))
    return cands[0] if cands else None


def find_original_premix(box: box_discovery._Box, cfg: dict[str, Any],
                         n: int) -> dict[str, Any] | None:
    """The original's PREMIX from a list of `premix_folders` — for series whose original is
    only ever delivered as a premix (Mowgli, Chikoo), spread across monthly Box folders."""
    for folder in (cfg["box"].get("premix_folders") or []):
        cands = [f for f in box.listing(folder)["files"]
                 if f["name"].lower().endswith(".wav")
                 and box_discovery.ep_of(f["name"]) == n
                 and "premix" in _sq(f["name"])]
        if cands:
            cands.sort(key=lambda f: len(f["name"]))
            return cands[0]
    return None


def _file_name(token: str, fid: str) -> str:
    """Best-effort display name for a hand-picked Box file id — a wrong id should fail at
    launch with Box's own error, not here."""
    import httpx
    try:
        r = httpx.get(f"https://api.box.com/2.0/files/{fid}", params={"fields": "name"},
                      headers={"Authorization": f"Bearer {token}"}, timeout=20)
        if r.status_code == 200:
            return str(r.json().get("name") or f"box:{fid}")
    except Exception:  # noqa: BLE001
        pass
    return f"box:{fid}"


def candidates(series_key: str, cfg: dict[str, Any], episode: int) -> dict[str, Any]:
    """Every .wav the discovery COULD have picked for this episode, both sides — the menu
    behind the Teams card's 'Choose files' page. Each entry: {id, name, where}; the entry the
    automatic pick would use is marked default=True so the page pre-selects reality."""
    token = box_oauth.get_token()
    box = box_discovery._Box(token)
    n = int(episode)
    b = cfg["box"]
    out: dict[str, Any] = {"original": [], "dubs": {}}

    def _scan(folder: str, where: str) -> None:
        try:
            for f in box.listing(folder)["files"]:
                if f["name"].lower().endswith(".wav") and box_discovery.ep_of(f["name"]) == n:
                    out["original"].append({"id": f["id"], "name": f["name"], "where": where})
        except Exception:  # noqa: BLE001 — a dead folder id just contributes nothing
            pass

    if b.get("original_mix_folder"):
        _scan(b["original_mix_folder"], "original mixes")
    for folder in (b.get("premix_folders") or []):
        _scan(folder, "premixes")
    if b.get("premix_folder"):
        _scan(b["premix_folder"], "premixes (script QC)")
    if b.get("original_videos_folder"):
        try:
            for f in box.listing(b["original_videos_folder"])["files"]:
                if (f["name"].lower().endswith((".mp4", ".mov", ".mkv", ".m4v"))
                        and box_discovery.ep_of(f["name"]) == n):
                    out["original"].append({"id": f["id"], "name": f["name"],
                                            "where": "videos (audio extracted)"})
        except Exception:  # noqa: BLE001
            pass

    default = (find_original_mix(box, cfg, n) or find_original_premix(box, cfg, n))
    for c in out["original"]:
        c["default"] = bool(default and c["id"] == default["id"])

    for lang in cfg.get("languages", []):
        cands: list[dict[str, Any]] = []
        roots = (b.get("mixes_folders") or {}).get(lang) or b.get("mixes_folder")
        for root in ([roots] if isinstance(roots, str) else (roots or [])):
            try:
                sub = next((d for d in box.listing(root)["folders"]
                            if box_discovery._ep_num(d["name"]) == n), None)
                if not sub:
                    continue
                for f in box.listing(sub["id"])["files"]:
                    if f["name"].lower().endswith(".wav"):
                        cands.append({"id": f["id"], "name": f["name"], "where": sub["name"]})
            except Exception:  # noqa: BLE001
                continue
        d = find_dub_mix(box, cfg, lang, n)
        for c in cands:
            c["default"] = bool(d and c["id"] == d["id"])
        out["dubs"][lang] = cands
    return out


def launch_custom(series_key: str, cfg: dict[str, Any], episode: int,
                  picks: dict[str, str], original_file: str | None,
                  conv: str | None = None) -> dict[str, Any]:
    """Fan out with HAND-PICKED Box file ids: picks = {lang: dub_file_id}; original_file
    overrides the original for every language (None = automatic discovery). Same parent
    record shape as launch_all so status/runs/notify treat it identically."""
    parent_id = uuid.uuid4().hex[:12]
    token = box_oauth.get_token(min_ttl_s=2700)
    langs: dict[str, dict[str, Any]] = {}
    for lang, fid in picks.items():
        r = launch(series_key, cfg, int(episode), lang, token=token, parent=parent_id,
                   original_file=original_file, dub_file=fid)
        langs[lang] = ({"job_id": r["job_id"], "original": r.get("original"),
                        "dub": r.get("dub"), "original_stage": r.get("original_stage")}
                       if not r.get("error") else {"error": r["error"]})
    launched = sum(1 for v in langs.values() if v.get("job_id"))
    if conv:
        try:
            from . import run_store
            run_store.record(parent_id, conv=conv, episode=int(episode),
                             series=cfg.get("display_name", series_key), status="running",
                             compute="audio-parallel", langs=langs, custom_files=True)
        except Exception:  # noqa: BLE001
            pass
    return {"parent_id": parent_id, "langs": langs, "launched": launched,
            "errors": {k: v["error"] for k, v in langs.items() if v.get("error")}}


def availability(series_key: str, cfg: dict[str, Any], episode: int) -> dict[str, Any]:
    """What audio-only QC needs for this episode: the original's mix and, per language, the
    delivered full mix. The script path has `check` before `run`; this is its counterpart, so
    the studio can see what is checkable before starting an 8-minute job."""
    token = box_oauth.get_token()
    box = box_discovery._Box(token)
    n = int(episode)
    orig = find_original_mix(box, cfg, n)
    stage = "ST_MIX"
    if not orig:
        orig = find_original_premix(box, cfg, n)
        stage = "ST_PREMIX"
    if not orig:
        orig = find_original_video(box, cfg, n)
        stage = "VIDEO"
    if not orig:
        try:
            orig = box_discovery.find_original(box, cfg, n)
        except Exception:  # noqa: BLE001 — scriptless-only series lack script-QC folders
            orig = None
        stage = "ST_PREMIX"
    ready, missing = {}, []
    for lang in cfg.get("languages", []):
        d = find_dub_mix(box, cfg, lang, n)
        if d:
            ready[lang] = d["name"]
        else:
            missing.append(lang)
    return {"series": cfg.get("display_name", series_key), "series_key": series_key,
            "episode": n, "original": orig["name"] if orig else None,
            "original_stage": stage if orig else None,
            "languages_ready": ready, "not_delivered": missing,
            "runnable": bool(orig and ready)}


def launch(series_key: str, cfg: dict[str, Any], episode: int, lang: str,
           original_stage: str = "mix", conv: str | None = None,
           token: str | None = None, parent: str | None = None,
           original_file: str | None = None, dub_file: str | None = None) -> dict[str, Any]:
    """Discover the pair in Box and start the Fargate audio-QC task. Returns
    {job_id, original, dub, eta_min} or {error}.

    `token`: a Box access token minted by the caller — launch_all passes ONE token to every
    language. Never force-mint per launch: each mint REVOKES the previous access token, so a
    6-language fan-out revoked the tokens of the first 5 tasks and they all 401'd on their
    first Box call (EP32, 2026-08-04). min_ttl_s guarantees freshness without rotation."""
    groq = os.environ.get("GROQ_API_KEY", "")
    if not groq:
        return {"error": "GROQ_API_KEY is not configured on the server"}
    c = fargate._cfg()
    if not (c["subnets"] and c["sg"] and c["bucket"]):
        return {"error": "Fargate compute is not configured (subnets/sg/bucket)"}
    # ≥45 min of life covers the task's Box phase (inputs are downloaded in its first few
    # minutes) without rotating a token some already-running task is still holding.
    token = token or box_oauth.get_token(min_ttl_s=2700)
    box = box_discovery._Box(token)
    # Hand-picked overrides (the card's 'Choose files' page / chat original=/dub= links)
    # bypass discovery for that side — the id is used verbatim, the name is display-only.
    if original_file:
        orig: dict[str, Any] | None = {"id": str(original_file),
                                       "name": _file_name(token, str(original_file))}
        stage_used = "CUSTOM"
    else:
        orig = (find_original_mix(box, cfg, int(episode)) if original_stage == "mix" else None)
        stage_used = "ST_MIX"
        if not orig:                               # fall back to the premix stage
            orig = find_original_premix(box, cfg, int(episode))
            stage_used = "ST_PREMIX"
        if not orig:                               # then a video whose audio we extract
            orig = find_original_video(box, cfg, int(episode))
            stage_used = "VIDEO"
        if not orig:
            try:
                orig = box_discovery.find_original(box, cfg, int(episode))
            except Exception:  # noqa: BLE001 — scriptless-only series lack script-QC folders
                orig = None
            stage_used = "ST_PREMIX"
    if not orig:
        return {"error": f"no original mix or premix found in Box for EP{int(episode):02d}"}
    if dub_file:
        dub: dict[str, Any] | None = {"id": str(dub_file),
                                      "name": _file_name(token, str(dub_file))}
    else:
        dub = find_dub_mix(box, cfg, lang, int(episode))
    if not dub:
        return {"error": f"no {lang} full mix (ST_MIX) delivered for EP{int(episode):02d}"}

    job_id = uuid.uuid4().hex[:12]
    prefix = f"{c['prefix']}/audioqc/{job_id}"
    cmd = ["audio_qc_run.py", f"box://{orig['id']}", f"box://{dub['id']}",
           cfg.get("original_language", "hindi"), lang.lower(),
           "--series", cfg.get("display_name", series_key),
           "--episode", str(int(episode)), "--out", prefix]
    if dub.get("_stage") == "dialogue":
        cmd.append("--clean-dub")                  # already clean dialogue — skip Demucs
    ov = {"containerOverrides": [{
        "name": "sonar",
        "command": cmd,
        "environment": [{"name": "GROQ_API_KEY", "value": groq},
                        {"name": "BOX_ACCESS_TOKEN", "value": token},
                        {"name": "DQC_S3_BUCKET", "value": c["bucket"]},
                        # PRODUCTION CONFIG (2026-08-04, user go): Sarvam-only fast mode —
                        # one deterministic Saaras reading per side via the Batch API.
                        # Validated: perfect fixture scores on EP38+EP41, ~2 min detection,
                        # one-flag reports. If Sarvam is down/out of credits the task falls
                        # back to Whisper double-pass automatically (audio_qc fallback).
                        {"name": "AQC_SARVAM_ONLY", "value": "1"},
                        {"name": "AQC_SARVAM_BATCH", "value": "1"},
                        {"name": "SARVAM_API_KEY",
                         "value": os.environ.get("SARVAM_API_KEY", "")},
                        # Scribe: one reading per side, all languages, real confidences.
                        # AQC_SCRIBE_ONLY=1 on the server makes it the engine everywhere.
                        {"name": "AQC_SCRIBE_ONLY",
                         "value": os.environ.get("AQC_SCRIBE_ONLY", "")},
                        {"name": "ELEVENLABS_API_KEY",
                         "value": os.environ.get("ELEVENLABS_API_KEY", "")}],
    }]}
    r = fargate._ecs().run_task(
        cluster=c["cluster"],
        taskDefinition=os.environ.get("DQC_AUDIO_TASKDEF", "dialogue-qc-sonar:3"),
        launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": c["subnets"], "securityGroups": [c["sg"]], "assignPublicIp": "ENABLED"}},
        overrides=ov)
    if r.get("failures"):
        return {"error": f"could not start the audio-QC task: {r['failures']}"}
    arn = r["tasks"][0]["taskArn"]
    _JOBS[job_id] = {"task_arn": arn, "series": series_key, "episode": int(episode),
                     "lang": lang, "prefix": prefix}
    # ALWAYS persist the job (conv only when the launch came from a chat): status() falls
    # back to run_store after a server restart, and a fan-out child recorded only in the
    # in-memory map used to render as "running" forever once the process bounced.
    try:
        from . import run_store
        run_store.record(job_id, conv=conv, episode=int(episode),
                         series=cfg.get("display_name", series_key), status="running",
                         compute="audio", lang=lang, prefix=prefix, task_arn=arn,
                         parent=parent)
    except Exception:  # noqa: BLE001 — bookkeeping must never break a launch
        pass
    return {"job_id": job_id, "original": orig["name"], "original_stage": stage_used,
            "dub": dub["name"], "eta_min": 8,
            "note": "audio QC started; poll get_audio_result with this job_id"}


def _prewarm(series_key: str, cfg: dict[str, Any], episode: int, token: str,
             original_file: str | None, say: Any = None) -> None:
    """Prepare the ORIGINAL once before a fan-out: one task separates it and takes its single
    Sarvam reading, publishing both to the S3 cache; the language tasks then start warm.

    Without this every language re-separates AND re-transcribes the same original — six
    Sarvam readings of one Hindi mix per episode, the largest avoidable cost in the whole
    pipeline. Costs one extra task's wall-clock up front and gives most of it back, since
    each language task then skips the original's separation too. Best-effort throughout: any
    failure just means the languages do it themselves, exactly as before."""
    import time as _t
    c = fargate._cfg()
    box = box_discovery._Box(token)
    n = int(episode)
    if original_file:
        orig: dict[str, Any] | None = {"id": str(original_file)}
    else:
        orig = (find_original_mix(box, cfg, n) or find_original_premix(box, cfg, n)
                or find_original_video(box, cfg, n))
        if not orig:
            try:
                orig = box_discovery.find_original(box, cfg, n)
            except Exception:  # noqa: BLE001
                orig = None
    if not orig:
        return
    if say:
        say("🔥 Preparing the original once (shared by every language) — this saves "
            "re-doing it per language. Languages start in a few minutes.")
    r = fargate._ecs().run_task(
        cluster=c["cluster"],
        taskDefinition=os.environ.get("DQC_AUDIO_TASKDEF", "dialogue-qc-sonar:3"),
        launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": c["subnets"], "securityGroups": [c["sg"]], "assignPublicIp": "ENABLED"}},
        overrides={"containerOverrides": [{
            "name": "sonar",
            # the dub arg is a positional placeholder — warm mode never downloads it
            "command": ["audio_qc_run.py", f"box://{orig['id']}", f"box://{orig['id']}",
                        cfg.get("original_language", "hindi"), "warm", "--warm-only"],
            "environment": [{"name": "BOX_ACCESS_TOKEN", "value": token},
                            {"name": "DQC_S3_BUCKET", "value": c["bucket"]},
                            {"name": "AQC_SARVAM_ONLY", "value": "1"},
                            {"name": "AQC_SARVAM_BATCH", "value": "1"},
                            {"name": "GROQ_API_KEY", "value": os.environ.get("GROQ_API_KEY", "")},
                            {"name": "SARVAM_API_KEY",
                             "value": os.environ.get("SARVAM_API_KEY", "")},
                            {"name": "AQC_SCRIBE_ONLY",
                             "value": os.environ.get("AQC_SCRIBE_ONLY", "")},
                            {"name": "ELEVENLABS_API_KEY",
                             "value": os.environ.get("ELEVENLABS_API_KEY", "")}],
        }]})
    if r.get("failures") or not r.get("tasks"):
        return                                        # quota/etc — fan out unwarmed
    arn = r["tasks"][0]["taskArn"]
    deadline = _t.time() + 1500                       # 25 min ceiling, then go anyway
    while _t.time() < deadline:
        _t.sleep(30)
        try:
            t = fargate._ecs().describe_tasks(cluster=c["cluster"], tasks=[arn])["tasks"][0]
            if t["lastStatus"] == "STOPPED":
                return
        except Exception:  # noqa: BLE001
            return


def launch_all(series_key: str, cfg: dict[str, Any], episode: int,
               conv: str | None = None, original_file: str | None = None,
               say: Any = None) -> dict[str, Any]:
    """Audio-QC every delivered language for an episode, one Fargate task each — the audio
    counterpart of fargate.launch_parallel, so `audio check ep 42` behaves like `run ep 42`.

    Returns {parent_id, langs: {lang: {...}}, launched, errors}. A language whose task fails to
    start (vCPU quota is the usual reason) is recorded with its error rather than dropped, so
    status can report it instead of silently showing fewer languages than were asked for.
    """
    import time as _t
    parent_id = uuid.uuid4().hex[:12]
    langs: dict[str, dict[str, Any]] = {}
    pending = list(cfg.get("languages", []))
    # Quota-retry loop: at the 30-vCPU quota a 6-language fan-out CANNOT all start at once,
    # and the old code recorded the quota failures permanently — 1 language ran, 5 "failed".
    # Languages that bounce on the vCPU limit are retried as earlier tasks finish, up to
    # ~25 min, which covers two full task generations even cold.
    # Pre-warm the shared original when more than one language is going out (and Sarvam is
    # the engine — the Whisper path has nothing per-episode to share). DQC_AQC_PREWARM=0
    # disables it if the extra up-front wall-clock ever matters more than the API spend.
    if (len(pending) > 1 and os.environ.get("DQC_AQC_PREWARM", "1").strip() != "0"
            and (os.environ.get("AQC_SARVAM_ONLY", "1").strip() == "1"
                 or os.environ.get("AQC_SCRIBE_ONLY", "").strip() == "1")):
        try:
            _prewarm(series_key, cfg, int(episode),
                     box_oauth.get_token(min_ttl_s=2700), original_file, say=say)
        except Exception:  # noqa: BLE001 — warming is an optimisation, never a blocker
            pass
    deadline = _t.time() + 1500
    while pending:
        still: list[str] = []
        # ONE token per wave, shared by every language's task. Minting per-language revoked
        # the earlier tasks' tokens (each refresh kills the outstanding access token) — the
        # EP32 failure. A retry wave 90s+ later re-checks TTL; a refresh then is safe because
        # the earlier wave's tasks have finished their Box downloads by that point.
        wave_token = box_oauth.get_token(min_ttl_s=2700)
        for lang in pending:
            r = launch(series_key, cfg, int(episode), lang,   # no conv: parent owns the record
                       token=wave_token, parent=parent_id, original_file=original_file)
            err = r.get("error") or ""
            if not err:
                langs[lang] = {"job_id": r["job_id"], "original": r.get("original"),
                               "dub": r.get("dub"), "original_stage": r.get("original_stage")}
            elif "no " + lang.lower() in err.lower():
                langs[lang] = {"skipped": err}               # not delivered — expected
            elif "vCPU" in err or "vcpu" in err:
                still.append(lang)                           # quota — retry when tasks free up
            else:
                langs[lang] = {"error": err}
        if not still or _t.time() > deadline:
            for lang in still:
                langs[lang] = {"error": "vCPU quota stayed exhausted for 25 min — "
                                        "re-run this language once current tasks finish"}
            break
        pending = still
        _t.sleep(90)
    launched = sum(1 for v in langs.values() if v.get("job_id"))
    if conv:
        try:
            from . import run_store
            run_store.record(parent_id, conv=conv, episode=int(episode),
                             series=cfg.get("display_name", series_key), status="running",
                             compute="audio-parallel", langs=langs)
        except Exception:  # noqa: BLE001
            pass
    return {"parent_id": parent_id, "langs": langs, "launched": launched,
            "errors": {k: v["error"] for k, v in langs.items() if v.get("error")}}


def status(job_id: str) -> dict[str, Any]:
    """S3-first: a published report.json means done regardless of task/server lifecycle."""
    import json
    c = fargate._cfg()
    rec = _JOBS.get(job_id) or {}
    if not rec:                                    # in-memory map is lost on restart
        try:
            from . import run_store
            rec = run_store.get(job_id) or {}
        except Exception:  # noqa: BLE001
            rec = {}
    prefix = rec.get("prefix") or f"{c['prefix']}/audioqc/{job_id}"
    s3 = fargate._s3()
    try:
        rep = json.loads(s3.get_object(Bucket=c["bucket"],
                                       Key=f"{prefix}/report.json")["Body"].read())
        s = rep.get("summary", {})
        # Concrete flags for the Teams card's collapsed drill-down — same shape the script-QC
        # examples use ({at, character, text}), with the confidence tier standing in for the
        # character (scriptless mode has no speaker identity). Verify order: high → low → time.
        _rank = {"high": 0, "medium": 1, "low": 2}
        ex = [{"at": f"{int(e['script_start_s'] // 3600):02d}:"
                     f"{int((e['script_start_s'] % 3600) // 60):02d}:{e['script_start_s'] % 60:04.1f}",
               "character": (e.get("confidence") or "?")
               + (f" · {e['stability']}" if e.get("stability") else ""),
               "text": (e.get("text") or "").strip()[:70]}
              for e in sorted((x for x in rep.get("errors", [])
                               if x.get("type") == "MISSING" and x.get("script_start_s") is not None),
                              key=lambda x: (_rank.get(x.get("confidence"), 3),
                                             x["script_start_s"]))][:6]
        out: dict[str, Any] = {
            "status": "done",
            "summary": {
                "missing": s.get("n_missing"),
                "missing_by_confidence": s.get("n_missing_by_confidence"),
                "extra": s.get("n_extra"), "misaligned": s.get("n_misaligned"),
                "original_lines": s.get("n_original_regions"),
                "coverage": s.get("coverage"), "unchecked": s.get("n_unchecked"),
                "examples": ex,
            },
        }
        for obj in s3.list_objects_v2(Bucket=c["bucket"], Prefix=prefix).get("Contents", []):
            if obj["Key"].endswith(".xlsx") and "download_url" not in out:
                out["download_url"] = fargate.download_url(obj["Key"])
            elif obj["Key"].endswith(".mid"):
                out["protools_url"] = fargate.download_url(obj["Key"])
            elif obj["Key"].endswith(".flac"):
                # Missing-lines reference audio (same deliverable as script QC)
                which = "ref_timeline_url" if "Timeline" in obj["Key"] else "ref_audio_url"
                out[which] = fargate.download_url(obj["Key"])
        return out
    except Exception:
        pass                                        # not published yet — fall through to ECS
    # Live pipeline progress, written by the runner at stage boundaries. Read BEFORE the ECS
    # fallback so the Teams bar shows "separating 42%" instead of a bare ECS lifecycle state.
    prog: dict[str, Any] = {}
    try:
        prog = json.loads(s3.get_object(Bucket=c["bucket"],
                                        Key=f"{prefix}/progress.json")["Body"].read())
    except Exception:  # noqa: BLE001 — older images never write it
        pass
    if not rec.get("task_arn"):
        return {"error": "unknown or expired job_id"}
    try:
        t = fargate._ecs().describe_tasks(cluster=c["cluster"],
                                          tasks=[rec["task_arn"]])["tasks"][0]
        st = t["lastStatus"]
        if st == "STOPPED":
            return {"status": "error",
                    "error": t.get("stoppedReason", "task stopped without publishing a report")}
        out = {"status": "running", "stage": st.lower(),
               "note": "separation + transcription take ~8 min for a full episode"}
    except Exception as e:  # noqa: BLE001
        out = {"status": "running", "note": f"task status unavailable ({e})"}
    if prog.get("pct"):
        out["pct"] = int(prog["pct"])
        out["stage"] = str(prog.get("stage") or out.get("stage") or "running")
    return out
