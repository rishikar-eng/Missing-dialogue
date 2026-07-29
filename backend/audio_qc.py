"""Audio-only QC — compare a dub to its source using WHAT WAS SAID, with no script.

The script-based mode is the easy case: a character-labelled script plus one clean track per
character. Most studios deliver neither — just a finished mix with music and effects baked in.
This module is for that case.

Why not the existing scriptless mode: `backend/scriptless.py` compares voice-activity
TIMELINES. It's cheap and offline, but content-blind, so it cannot tell a line that was
DROPPED from one that merely MOVED a second or two (translated lines run to different
lengths). It therefore flags both, and a reviewer clears the difference by hand.

    mixed audio ──1 SEPARATE──▶ dialogue only ──2 SEGMENT──▶ speech windows
                    (Demucs)                     (Silero VAD)
                                                      │
                                        3 EMBED  SONAR speech encoder
                                          (speech ➜ a shared cross-lingual meaning space,
                                           so Hindi and Tamil are directly comparable and
                                           no transcription — and no ASR error — is involved)
                                                      │
                                        4 ALIGN  backend/align.py  ──▶ findings

Output is deliberately the SAME shape `scriptless.compare_original_to_dub` returns, so the
Excel report, the UI and the Teams replies consume it unchanged.

HEAVY DEPENDENCIES (demucs, fairseq2, sonar) are imported lazily and are NOT installed in the
web process — a SONAR speech encoder alone is 8.6 GB and needs ~16 GB to load. This module is
meant to run inside the `dialogue-qc-sonar` Fargate task, not on the always-on box.
"""
from __future__ import annotations

import os
from typing import Any

import numpy as np

from . import align as _align

# SONAR speech encoders are per-language: ISO-639-3 codes.
LANG3 = {"hindi": "hin", "tamil": "tam", "telugu": "tel", "kannada": "kan",
         "bengali": "ben", "marathi": "mar", "malayalam": "mal", "english": "eng",
         "punjabi": "pan", "japanese": "jpn"}

# A window shorter than this is a breath or a click, not a line — embedding it produces a
# meaningless vector that matches everything, which is how false "missing" flags are born.
MIN_SEG_S = 0.6


def _lang3(name: str) -> str:
    n = (name or "").strip().lower()
    return LANG3.get(n, n[:3] if len(n) >= 3 else "eng")


def separate_dialogue(path: str, cache: bool = True) -> np.ndarray:
    """Demucs → the dialogue stem as 16 kHz mono float32.

    This step is what makes the whole thing possible on a finished mix: music reads as speech
    to a voice detector and wrecks any downstream comparison. Cached next to the input,
    because separation dominates the runtime and every re-run would otherwise repeat it.
    """
    import soundfile as sf
    import torch
    import torchaudio
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    npy = path + ".voc16.npy"
    if cache and os.path.exists(npy):
        return np.load(npy)
    model = getattr(separate_dialogue, "_model", None)
    if model is None:
        model = separate_dialogue._model = get_model("htdemucs")
        model.eval()
    data, sr = sf.read(path, dtype="float32")      # torch 2.13's loader needs torchcodec
    if data.ndim == 1:
        data = data[:, None]
    wav = torch.from_numpy(data.T).contiguous()
    if sr != model.samplerate:
        wav = torchaudio.functional.resample(wav, sr, model.samplerate)
    with torch.no_grad():
        srcs = apply_model(model, wav[None], device="cpu", progress=False, split=True)[0]
    vocals = srcs[model.sources.index("vocals")]
    out = torchaudio.functional.resample(
        vocals.mean(0, keepdim=True), model.samplerate, 16000).squeeze(0).numpy().astype("float32")
    if cache:
        try:
            np.save(npy, out)
        except OSError:
            pass
    return out


def segment(audio16: np.ndarray) -> list[tuple[float, float]]:
    """Silero VAD over the ISOLATED dialogue → (start, end) speech windows."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    vad = getattr(segment, "_vad", None) or load_silero_vad()
    segment._vad = vad
    ts = get_speech_timestamps(torch.from_numpy(audio16), vad,
                               sampling_rate=16000, return_seconds=True)
    return [(round(t["start"], 2), round(t["end"], 2)) for t in ts
            if (t["end"] - t["start"]) >= MIN_SEG_S]


def embed(audio16: np.ndarray, segs: list[tuple[float, float]], lang3: str):
    """SONAR speech embeddings, one vector per window, L2-normalised so a dot product is
    cosine similarity. Language-specific encoder; ~8.6 GB, so this is the memory-hungry step."""
    import tempfile

    import soundfile as sf
    import torch
    from sonar.inference_pipelines.speech import SpeechToEmbeddingModelPipeline
    # Cache per language: an encoder is ~8.6 GB and takes ~40 s to load, and the second
    # (rescue) pass embeds against the dub language again.
    cache = getattr(embed, "_pipes", None)
    if cache is None:
        cache = embed._pipes = {}
    pipe = cache.get(lang3)
    if pipe is None:
        pipe = cache[lang3] = SpeechToEmbeddingModelPipeline(
            encoder=f"sonar_speech_encoder_{lang3}")
    d = tempfile.mkdtemp()
    paths = []
    for k, (s, e) in enumerate(segs):
        p = os.path.join(d, f"{k:04d}.wav")
        sf.write(p, audio16[int(s * 16000):int(e * 16000)], 16000)
        paths.append(p)
    emb = pipe.predict(paths, batch_size=16)
    return torch.nn.functional.normalize(emb, dim=1)


def _load16(path: str) -> np.ndarray:
    """An audio file as 16 kHz mono float32, no separation — for inputs that are ALREADY
    clean dialogue (e.g. a sum of delivered per-character stems)."""
    import soundfile as sf
    import torch
    import torchaudio
    data, sr = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        data = torchaudio.functional.resample(
            torch.from_numpy(data)[None], sr, 16000).squeeze(0).numpy()
    return data.astype("float32")


# --- second pass: confirm an absence before reporting it ------------------------------
# Alignment is one-to-one, so when the two languages segment differently (measured: 13 windows
# vs 12 for identical content) a reference window can be left unmatched simply because no dub
# window was free — not because the line is absent. Scores alone don't separate the two cases:
# a false "missing" scored 0.77 while a real drop scored 0.75.
#
# So every candidate is re-checked WITHOUT segmentation: slide a window of the same duration
# across the dub audio around where the line should be, embed each position, and take the best.
# A genuinely dropped line has no match THERE; a segmentation artefact finds its counterpart
# as soon as it isn't forced onto a window boundary. Only the few flagged windows are scanned,
# so the cost is small.
#
# THE SPAN MUST BE TIGHT. At ±15 s the scan reached the NEIGHBOURING lines of the same scene,
# and same-scene speech scores high enough cross-lingually (>=0.55 measured) that a genuinely
# silenced line was "found" in its neighbour and cleared — a false negative. The rescue's whole
# premise is that a line which merely lost the window lottery sits at (nearly) its expected
# time; content found only seconds away is a DIFFERENT line. So we scan ±2 s around the
# expected position (after correcting the pair's overall sync offset) and no further, and a
# line shifted more than that is reported — "dropped or badly mistimed" is a fair flag.
SCAN_SPAN_S = 2.0     # how far either side of the (offset-corrected) expected position to look
SCAN_STEP_S = 0.4     # slide granularity
RESCUE_SIM = 0.60     # a match this good AT THE RIGHT TIME means the content is there


def _rescue(dub16: np.ndarray, ref_vec, window_s: float, at_s: float, lang3: str) -> tuple[float, float]:
    """Best (similarity, dub_start) for this reference window NEAR `at_s` in the dub.
    Returns (0.0, -1) when there is nothing to scan. Never widens to the whole file —
    a far-away match is a different line, and treating it as this one hides real drops."""
    import torch
    dur_total = len(dub16) / 16000.0
    w = max(MIN_SEG_S, window_s)
    lo = max(0.0, min(at_s - SCAN_SPAN_S, dur_total - w))
    hi = max(lo, min(dur_total - w, at_s + SCAN_SPAN_S))
    starts = [round(lo + k * SCAN_STEP_S, 2) for k in range(int((hi - lo) / SCAN_STEP_S) + 1)]
    starts = [s for s in starts if s + w <= dur_total + 1e-6] or ([lo] if lo + w <= dur_total else [])
    if not starts:
        return 0.0, -1.0
    cand = embed(dub16, [(s, s + w) for s in starts], lang3)
    sims = (cand @ ref_vec).squeeze(-1) if ref_vec.ndim > 1 else cand @ ref_vec
    best = int(torch.argmax(sims))
    return float(sims[best]), starts[best]


def compare(original_path: str, dub_path: str, *, original_lang: str, dub_lang: str,
            dub_label: str = "dub", tol_s: float = 1.0, dub_is_clean: bool = False,
            stage: Any = None) -> dict[str, Any]:
    """Audio-only QC of one dub against its source.

    `dub_is_clean=True` skips separation on the dub side — for a dub input that is already
    dialogue-only (e.g. the delivered per-character stems summed into one track), where
    running Demucs would only cost time.

    Returns the same report shape as `scriptless.compare_original_to_dub`:
    {errors, summary, channels, unmapped_characters, sync_warnings, tol_s}.
    """
    def _say(m: str) -> None:
        if stage:
            stage(m, 0, 0)
        print(f"[audio_qc] {m}", flush=True)

    _say("separating dialogue from the original mix")
    ov = separate_dialogue(original_path)
    if dub_is_clean:
        _say("dub is already clean dialogue — loading without separation")
        dv = _load16(dub_path)
    else:
        _say("separating dialogue from the dub mix")
        dv = separate_dialogue(dub_path)

    oseg, dseg = segment(ov), segment(dv)
    _say(f"speech windows: original={len(oseg)} dub={len(dseg)}")
    if not oseg:
        return _empty("no speech found in the original after separation", tol_s, dub_label)
    if not dseg:                                    # a totally silent dub: every line missing
        errors = [_missing(i, s, e, dub_label, 0.0) for i, (s, e) in enumerate(oseg)]
        return _report(errors, oseg, dub_label, tol_s)

    _say(f"embedding {len(oseg)} original windows ({_lang3(original_lang)})")
    R = embed(ov, oseg, _lang3(original_lang))
    _say(f"embedding {len(dseg)} dub windows ({_lang3(dub_lang)})")
    D = embed(dv, dseg, _lang3(dub_lang))
    sim = (R @ D.T).numpy()

    _say("aligning")
    pairs, missing, extra = _align.align(sim, oseg, dseg)

    best: dict[int, tuple[int, float]] = {}
    for i, j, sc in pairs:
        if sc > best.get(i, (-1, -1.0))[1]:
            best[i] = (j, sc)

    errors: list[dict[str, Any]] = []
    rescued = 0
    # The pair's overall sync offset (median drift of the matched pairs): the rescue scan is
    # deliberately narrow, so it must be centred where this dub actually puts its lines.
    drifts = sorted(dseg[j][0] - oseg[i][0] for i, j, _ in pairs)
    med_drift = drifts[len(drifts) // 2] if drifts else 0.0
    if missing:
        _say(f"second pass: re-checking {len(missing)} candidate(s) against the dub audio "
             f"(sync offset {med_drift:+.2f}s)")
    for i in missing:
        s, e = oseg[i]
        best_seg = float(sim[i].max()) if sim.size else 0.0
        found, at = _rescue(dv, R[i], e - s, s + med_drift, _lang3(dub_lang))
        # Log the score: RESCUE_SIM has to sit ABOVE the similarity that unrelated speech
        # reaches, or the scan clears everything (measured: 0.55 cleared a truly silenced
        # line). These numbers are how it gets calibrated.
        _say(f"  window @{s:.1f}-{e:.1f}s: aligned-best={best_seg:.2f} "
             f"scan-best={found:.2f} @{at:.1f}s -> {'cleared' if found >= RESCUE_SIM else 'MISSING'}")
        if found >= RESCUE_SIM:
            # The content IS in the dub, it just didn't get a window under one-to-one
            # alignment. Report it as retimed if it moved, otherwise say nothing at all.
            rescued += 1
            drift = at - s
            if abs(drift) > tol_s:
                errors.append({
                    "type": "MISALIGNED", "subtype": "shifted", "severity": "warn",
                    "character": None, "channel": dub_label, "script_index": i,
                    "script_start_s": round(s, 3), "script_end_s": round(e, 3),
                    "audio_start_s": round(at, 3), "audio_end_s": round(at + (e - s), 3),
                    "drift_s": round(drift, 2), "coverage": round(found, 3), "text": None,
                    "message": (f"The line at {s:.2f}s is in the dub, but {abs(drift):.1f}s "
                                f"{'late' if drift > 0 else 'early'} (match {found:.0%})."),
                })
            continue
        errors.append(_missing(i, s, e, dub_label, max(best_seg, found)))
    if missing:
        _say(f"second pass: {rescued}/{len(missing)} cleared as present, "
             f"{len(missing) - rescued} confirmed missing")
    # A matched line that lands well away from where it should be is present but retimed —
    # the distinction the timeline-only mode could never make.
    for i, (j, sc) in sorted(best.items()):
        drift = dseg[j][0] - oseg[i][0]
        if abs(drift) > tol_s:
            s, e = oseg[i]
            errors.append({
                "type": "MISALIGNED", "subtype": "shifted", "severity": "warn",
                "character": None, "channel": dub_label, "script_index": i,
                "script_start_s": round(s, 3), "script_end_s": round(e, 3),
                "audio_start_s": round(dseg[j][0], 3), "audio_end_s": round(dseg[j][1], 3),
                "drift_s": round(drift, 2), "coverage": round(sc, 3), "text": None,
                "message": (f"The line at {s:.2f}s is in the dub, but {abs(drift):.1f}s "
                            f"{'late' if drift > 0 else 'early'} (match {sc:.0%})."),
            })
    for j in extra:
        s, e = dseg[j]
        errors.append({
            "type": "EXTRA", "subtype": None, "severity": "info",
            "character": None, "channel": dub_label, "script_index": None,
            "script_start_s": None, "script_end_s": None,
            "audio_start_s": round(s, 3), "audio_end_s": round(e, 3),
            "drift_s": None, "coverage": None, "text": None,
            "message": (f"The dub speaks at {s:.2f}–{e:.2f}s with nothing matching it in "
                        f"the original — added or improvised."),
        })
    return _report(errors, oseg, dub_label, tol_s)


def _missing(i: int, s: float, e: float, ch: str, best: float) -> dict[str, Any]:
    return {
        "type": "MISSING", "subtype": None, "severity": "error",
        "character": None, "channel": ch, "script_index": i,
        "script_start_s": round(s, 3), "script_end_s": round(e, 3),
        "audio_start_s": None, "audio_end_s": None,
        "drift_s": None, "coverage": round(best, 3), "text": None,
        "message": (f"The original speaks at {s:.2f}–{e:.2f}s and the dub has nothing saying "
                    f"the same thing there (best match {best:.0%}) — dropped, or moved by "
                    f"more than a few seconds."),
    }


def _report(errors: list[dict[str, Any]], oseg: list, ch: str, tol_s: float) -> dict[str, Any]:
    n = {t: sum(1 for e in errors if e["type"] == t) for t in ("MISSING", "MISALIGNED", "EXTRA")}
    return {
        "mode": "audio_only",
        "tol_s": tol_s,
        "channels": [ch],
        "errors": errors,
        "unmapped_characters": [],
        "sync_warnings": [],
        "summary": {
            "n_characters_checked": 0,
            "n_missing": n["MISSING"], "n_misaligned": n["MISALIGNED"],
            "n_extra": n["EXTRA"], "n_mismatch": 0, "n_unmapped": 0,
            "n_sync_warnings": 0, "n_original_regions": len(oseg),
        },
    }


def _empty(why: str, tol_s: float, ch: str) -> dict[str, Any]:
    r = _report([], [], ch, tol_s)
    r["why"] = why
    return r
