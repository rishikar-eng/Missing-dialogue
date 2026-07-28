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


# Two VAD passes over the same dialogue in different languages do NOT produce the same number
# of windows — a natural pause inside one line gets split on one side and not the other. With
# one-to-one alignment that asymmetry alone FORCES a false "missing" (measured: 13 Hindi
# windows vs 12 Tamil for identical content, and the flagged window scored 0.77 — a good match
# existed but no window was left to pair it with). Gluing windows separated by less than a
# breath makes the two sides comparably granular, which fixes the cause rather than asking the
# aligner to paper over it afterwards.
GLUE_GAP_S = 0.35


def _merge_close(segs: list[tuple[float, float]], gap: float = GLUE_GAP_S) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for s, e in segs:
        if out and s - out[-1][1] <= gap:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [(round(a, 2), round(b, 2)) for a, b in out]


def segment(audio16: np.ndarray) -> list[tuple[float, float]]:
    """Silero VAD over the ISOLATED dialogue → (start, end) speech windows, with
    near-adjacent windows glued so both languages segment at a comparable granularity."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    vad = getattr(segment, "_vad", None) or load_silero_vad()
    segment._vad = vad
    ts = get_speech_timestamps(torch.from_numpy(audio16), vad,
                               sampling_rate=16000, return_seconds=True)
    merged = _merge_close([(t["start"], t["end"]) for t in ts])
    return [(s, e) for s, e in merged if (e - s) >= MIN_SEG_S]


def embed(audio16: np.ndarray, segs: list[tuple[float, float]], lang3: str):
    """SONAR speech embeddings, one vector per window, L2-normalised so a dot product is
    cosine similarity. Language-specific encoder; ~8.6 GB, so this is the memory-hungry step."""
    import tempfile

    import soundfile as sf
    import torch
    from sonar.inference_pipelines.speech import SpeechToEmbeddingModelPipeline
    pipe = SpeechToEmbeddingModelPipeline(encoder=f"sonar_speech_encoder_{lang3}")
    d = tempfile.mkdtemp()
    paths = []
    for k, (s, e) in enumerate(segs):
        p = os.path.join(d, f"{k:04d}.wav")
        sf.write(p, audio16[int(s * 16000):int(e * 16000)], 16000)
        paths.append(p)
    emb = pipe.predict(paths, batch_size=4)
    return torch.nn.functional.normalize(emb, dim=1)


def compare(original_path: str, dub_path: str, *, original_lang: str, dub_lang: str,
            dub_label: str = "dub", tol_s: float = 1.0,
            stage: Any = None) -> dict[str, Any]:
    """Audio-only QC of one dub against its source.

    Returns the same report shape as `scriptless.compare_original_to_dub`:
    {errors, summary, channels, unmapped_characters, sync_warnings, tol_s}.
    """
    def _say(m: str) -> None:
        if stage:
            stage(m, 0, 0)
        print(f"[audio_qc] {m}", flush=True)

    _say("separating dialogue from the original mix")
    ov = separate_dialogue(original_path)
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
    for i in missing:
        s, e = oseg[i]
        errors.append(_missing(i, s, e, dub_label, float(sim[i].max()) if sim.size else 0.0))
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
        "message": (f"The original speaks at {s:.2f}–{e:.2f}s and nothing in the dub says "
                    f"the same thing (best match {best:.0%}) — the line looks dropped."),
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
