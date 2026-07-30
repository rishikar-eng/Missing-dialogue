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
# Whisper wants ISO-639-1.
LANG1 = {"hindi": "hi", "tamil": "ta", "telugu": "te", "kannada": "kn", "bengali": "bn",
         "marathi": "mr", "malayalam": "ml", "english": "en", "japanese": "ja",
         "punjabi": "pa"}

# A window shorter than this is a breath or a click, not a line — embedding it produces a
# meaningless vector that matches everything, which is how false "missing" flags are born.
MIN_SEG_S = 0.6


def _lang3(name: str) -> str:
    n = (name or "").strip().lower()
    return LANG3.get(n, n[:3] if len(n) >= 3 else "eng")


def _separate(path: str, cache: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Demucs → (dialogue, accompaniment) as 16 kHz mono float32.

    Separation is what makes any of this possible on a finished mix: music reads as speech to
    a voice detector and wrecks any downstream comparison. The accompaniment (everything BUT
    the dialogue) is kept too — it's how a realistic dub mix is constructed when a delivery
    has stems but no mix (original M&E under the dubbed dialogue, which is how real dub mixes
    are made). It comes from Demucs' non-vocal stems rather than mix-minus-vocals, because
    subtraction leaks the original dialogue back in — and bleed at a dropped line's slot would
    mask exactly the finding we're after. Both outputs are cached next to the input, since
    separation dominates the runtime.
    """
    import soundfile as sf
    import torch
    import torchaudio
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    vnpy, anpy = path + ".voc16.npy", path + ".acc16.npy"
    if cache and os.path.exists(vnpy) and os.path.exists(anpy):
        return np.load(vnpy), np.load(anpy)
    model = getattr(_separate, "_model", None)
    if model is None:
        model = _separate._model = get_model("htdemucs")
        model.eval()
    data, sr = sf.read(path, dtype="float32")      # torch 2.13's loader needs torchcodec
    if data.ndim == 1:
        data = data[:, None]
    wav = torch.from_numpy(data.T).contiguous()
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)                     # htdemucs is stereo-only; duplicate mono
    if sr != model.samplerate:
        wav = torchaudio.functional.resample(wav, sr, model.samplerate)
    with torch.no_grad():
        srcs = apply_model(model, wav[None], device="cpu", progress=False, split=True)[0]
    vi = model.sources.index("vocals")
    vocals = srcs[vi]
    accomp = sum(srcs[k] for k in range(len(model.sources)) if k != vi)

    def _to16(x) -> np.ndarray:
        return torchaudio.functional.resample(
            x.mean(0, keepdim=True), model.samplerate, 16000).squeeze(0).numpy().astype("float32")

    v16, a16 = _to16(vocals), _to16(accomp)
    if cache:
        try:
            np.save(vnpy, v16)
            np.save(anpy, a16)
        except OSError:
            pass
    return v16, a16


def separate_dialogue(path: str, cache: bool = True) -> np.ndarray:
    """The dialogue stem of a mix, as 16 kHz mono float32 (see `_separate`)."""
    return _separate(path, cache)[0]


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


# --- text engine: transcribe (Groq Whisper) + match meaning (LaBSE) --------------------
# Chosen over SONAR speech embeddings from measurement, not preference. On identical
# material, SONAR similarities barely separate (time-matched pairs med 0.68 vs RANDOM
# windows med 0.72 raw; still overlapping after mean-centering) — 1-3 s VAD fragments of
# separated audio just don't carry sentence-level meaning. Transcribing first and matching
# TEXT gives true pairs ~0.85-1.0 vs unrelated ~0.4-0.6: a margin thresholds can live on.
# The trade: audio (the separated dialogue only) goes to the Groq API.

def transcribe_groq(audio16: np.ndarray, lang1: str) -> list[tuple[float, float, str]]:
    """Groq-hosted whisper-large-v3 → [(start, end, text)] segments, VAD-GATED.

    Whisper hallucinates over music and near-silence — measured on the EP42 source: a
    47-second segment reading "ご視聴ありがとうございました" ("thanks for watching", a training
    artefact), swallowing the exact region where the known-missing lines live. So only the
    audio Silero actually calls speech is sent: windows are concatenated with short gaps and
    a piecewise map converts Whisper's times back to the real timeline. Whisper never sees
    the material it hallucinates on, and segment times stay honest.
    """
    wins = segment(audio16)
    if not wins:
        return []
    GAP = 0.5
    PAD = 0.2                                      # a little context helps word boundaries
    gap = np.zeros(int(GAP * 16000), dtype="float32")
    pieces: list[np.ndarray] = []
    cmap: list[tuple[float, float, float]] = []    # (concat_start, orig_start, dur)
    t = 0.0
    prev_end = 0.0
    for (s, e) in wins:
        ps = max(prev_end, s - PAD)
        pe = min(len(audio16) / 16000.0, e + PAD)
        prev_end = pe
        d = audio16[int(ps * 16000):int(pe * 16000)]
        pieces += [d, gap]
        cmap.append((t, ps, pe - ps))
        t += (pe - ps) + GAP

    def back(ct: float) -> float:
        k = 0
        for k in range(len(cmap)):
            cs, _, dur = cmap[k]
            if ct < cs + dur + GAP:
                break
        cs, os_, dur = cmap[k]
        return os_ + min(max(ct - cs, 0.0), dur)   # inside the gap -> clamp to window edge

    segs = _groq_call(np.concatenate(pieces), lang1)
    out = []
    for cs, ce, txt, q in segs:
        s2, e2 = back(cs), back(ce)
        if e2 - s2 >= 0.2 and txt:
            out.append((round(s2, 2), round(e2, 2), txt, q))
    return out


def _groq_call(audio16: np.ndarray, lang1: str) -> list[tuple[float, float, str]]:
    """One transcription request against the Groq API (with rate-limit retries)."""
    import io as _io

    import httpx
    import soundfile as sf
    buf = _io.BytesIO()
    sf.write(buf, audio16, 16000, format="WAV")
    buf.seek(0)
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set (needed for the text engine)")
    for attempt in range(4):
        r = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions",
                       headers={"Authorization": f"Bearer {key}"},
                       files={"file": ("a.wav", buf.getvalue())},
                       data={"model": "whisper-large-v3", "language": lang1,
                             "response_format": "verbose_json"},
                       timeout=300)
        if r.status_code == 429:                    # free-tier rate limit: back off and retry
            import time as _t
            _t.sleep(15 * (attempt + 1))
            continue
        r.raise_for_status()
        segs = []
        for s in r.json().get("segments") or []:
            t = (s.get("text") or "").strip()
            if t:
                segs.append((round(float(s["start"]), 2), round(float(s["end"]), 2), t,
                             {"nsp": float(s.get("no_speech_prob") or 0.0),
                              "alp": float(s.get("avg_logprob") or 0.0),
                              "cr": float(s.get("compression_ratio") or 1.0)}))
        return segs
    raise RuntimeError("Groq transcription kept rate-limiting")


def _reliable(q: dict | None, text: str | None = None) -> bool:
    """Whisper's own confidence in a segment. A flag built on a garbled transcript is noise —
    EP43's fight scene produced ~20 false 'missing' from exactly that.

    avg_logprob is shared by every segment of one ~30 s decode window, so one noisy window
    poisons its good neighbours (this gated out a REAL missing line on EP42). A full sentence
    is its own evidence of real speech, so bad alp only disqualifies SHORT segments."""
    if not q or q.get("nsp", 1.0) >= 0.5:
        return False
    # compression_ratio is PER SEGMENT (unlike alp) — the standard whisper garble signal.
    # Repetitive junk compresses well (>2.4); that's what multi-word fight-scene garble is.
    if q.get("cr", 1.0) > 2.4:
        return False
    words = len((text or "").split())
    return q.get("alp", -9.0) > -1.2 or words >= 4


def _judge(ref_text: str, ref_lang: str, dub_lines: list[str], dub_lang: str) -> str:
    """Last gate before a MISSING flag: a cheap LLM look at the actual text, doing exactly
    what a human reviewer does — 'is this original line even coherent dialogue, and does any
    nearby dub line say roughly the same thing?' Embedding scores can't reject fluent-looking
    ASR garble; a language model can. Fails OPEN (keeps the flag) — over-flagging is the
    acceptable error here. Returns 'missing' | 'present' | 'garble'."""
    import json as _json
    import time as _t

    import httpx
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return "missing"
    listing = "\n".join(f"{k + 1}. {t}" for k, t in enumerate(dub_lines)) or "(none)"
    prompt = (f"You are QC-checking a dubbed TV episode.\n"
              f"Original line ({ref_lang}): {ref_text}\n"
              f"Dub lines spoken near that same moment ({dub_lang}):\n{listing}\n\n"
              'Reply with STRICT JSON only: {"coherent": true|false, "conveyed": true|false}. '
              '"coherent" = the original line reads as a real piece of dialogue, not '
              'speech-recognition gibberish. "conveyed" = at least one dub line expresses '
              "roughly the same meaning (translations are loose; judge meaning, not words).")
    for attempt in range(3):
        try:
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": "llama-3.3-70b-versatile", "temperature": 0,
                                 "response_format": {"type": "json_object"},
                                 "messages": [{"role": "user", "content": prompt}]},
                           timeout=60)
            if r.status_code == 429:
                _t.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            v = _json.loads(r.json()["choices"][0]["message"]["content"])
            if not v.get("coherent"):
                return "garble"
            return "present" if v.get("conveyed") else "missing"
        except Exception:  # noqa: BLE001 — judge trouble must never suppress a real flag
            return "missing"
    return "missing"


def _songlike(text: str) -> bool:
    """Lyrics repeat; dialogue doesn't. Catches ED/insert songs that survive VAD."""
    w = (text or "").split()
    return len(w) >= 6 and len(set(w)) / len(w) < 0.5


def embed_text(texts: list[str]):
    """LaBSE sentence embeddings, L2-normalised (cross-lingual: a line and its translation
    land close). Model cached per process (~1.9 GB first download)."""
    from sentence_transformers import SentenceTransformer
    m = getattr(embed_text, "_m", None)
    if m is None:
        m = embed_text._m = SentenceTransformer("sentence-transformers/LaBSE")
    import torch
    return torch.from_numpy(m.encode(texts, normalize_embeddings=True))


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


def _rescue(dub16: np.ndarray, ref_vec, window_s: float, at_s: float, lang3: str,
            mu=None) -> tuple[float, float]:
    """Best (similarity, dub_start) for this reference window NEAR `at_s` in the dub.
    Returns (0.0, -1) when there is nothing to scan. Never widens to the whole file —
    a far-away match is a different line, and treating it as this one hides real drops.
    `mu` is the run's mean embedding: candidates are centred with it so their scores live
    on the same scale as the (already-centred) `ref_vec`."""
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
    if mu is not None:
        cand = torch.nn.functional.normalize(cand - mu, dim=1)
    sims = (cand @ ref_vec).squeeze(-1) if ref_vec.ndim > 1 else cand @ ref_vec
    best = int(torch.argmax(sims))
    return float(sims[best]), starts[best]


def compare(original_path: str, dub_path: str, *, original_lang: str, dub_lang: str,
            dub_label: str = "dub", tol_s: float = 1.0, dub_is_clean: bool = False,
            engine: str = "text", stage: Any = None) -> dict[str, Any]:
    """Audio-only QC of one dub against its source.

    `engine="text"` (default): transcribe both sides (Groq whisper-large-v3) and match the
    TEXT cross-lingually with LaBSE. Chosen from measurement — see the text-engine note above.
    Needs GROQ_API_KEY; the separated dialogue audio is sent to the Groq API.
    `engine="sonar"`: match the SPEECH directly with SONAR encoders — fully in-house, but its
    similarities did not separate matches from noise on our material; kept for experiments.

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

    otext: list[str] | None = None
    dtext: list[str] | None = None
    oqual: list[dict] | None = None
    dqual: list[dict] | None = None
    dwin: list[tuple[float, float]] | None = None
    mu = None
    if engine == "text":
        _say(f"transcribing original ({LANG1.get(original_lang.lower(), 'auto')}) via Groq")
        osegs = transcribe_groq(ov, LANG1.get(original_lang.lower(), original_lang))
        _say(f"transcribing dub ({LANG1.get(dub_lang.lower(), 'auto')}) via Groq")
        dsegs = transcribe_groq(dv, LANG1.get(dub_lang.lower(), dub_lang))
        oseg = [(s, e) for s, e, _, _ in osegs]
        dseg = [(s, e) for s, e, _, _ in dsegs]
        otext = [t for _, _, t, _ in osegs]
        dtext = [t for _, _, t, _ in dsegs]
        oqual = [q for _, _, _, q in osegs]
        dqual = [q for _, _, _, q in dsegs]
        _say(f"lines: original={len(oseg)} dub={len(dseg)}")
        if not oseg:
            return _empty("no speech transcribed in the original", tol_s, dub_label)
        if not dseg:
            errors = [_missing(i, s, e, dub_label, 0.0, (otext or [None] * len(oseg))[i])
                      for i, (s, e) in enumerate(oseg)]
            return _report(errors, oseg, dub_label, tol_s)
        _say("matching meaning (LaBSE)")
        # Raw speech map of the dub — kept for the flag gate below. Text alone cannot tell
        # "dub audio absent" from "dub audio present but Whisper couldn't render it under
        # SFX" (EP43: lines verified present by ear produced no transcript). VAD can.
        dwin = segment(dv)
        R = embed_text(otext)
        D = embed_text(dtext)
        sim = (R @ D.T).numpy()
    else:
        oseg, dseg = segment(ov), segment(dv)
        _say(f"speech windows: original={len(oseg)} dub={len(dseg)}")
        if not oseg:
            return _empty("no speech found in the original after separation", tol_s, dub_label)
        if not dseg:                                # a totally silent dub: every line missing
            errors = [_missing(i, s, e, dub_label, 0.0) for i, (s, e) in enumerate(oseg)]
            return _report(errors, oseg, dub_label, tol_s)
        _say(f"embedding {len(oseg)} original windows ({_lang3(original_lang)})")
        R = embed(ov, oseg, _lang3(original_lang))
        _say(f"embedding {len(dseg)} dub windows ({_lang3(dub_lang)})")
        D = embed(dv, dseg, _lang3(dub_lang))

        if os.environ.get("AQC_DUMP"):
            # RAW embeddings for offline analysis — measuring similarity floors locally
            # beats a 10-minute container cycle per hypothesis.
            try:
                np.savez("/tmp/aqc_dump.npz", R=R.numpy(), D=D.numpy(),
                         oseg=np.array(oseg, dtype="float32"),
                         dseg=np.array(dseg, dtype="float32"))
                _say("dumped embeddings to /tmp/aqc_dump.npz")
            except Exception as ex:  # noqa: BLE001
                _say(f"dump failed: {ex}")

        # ANISOTROPY CORRECTION for SONAR: a PURE-SILENCE window scored 0.79 raw cosine
        # against speech, so the signal lives in a thin band at the top. Mean-centering +
        # renormalising pushes unrelated content toward 0. (Even centred, separation on our
        # material was weak — which is why "text" is the default engine.)
        import torch as _torch
        mu = _torch.cat([R, D]).mean(dim=0)
        R = _torch.nn.functional.normalize(R - mu, dim=1)
        D = _torch.nn.functional.normalize(D - mu, dim=1)
        sim = (R @ D.T).numpy()

    _say("aligning")
    pairs, missing, extra = _align.align(sim, oseg, dseg)

    best: dict[int, tuple[int, float]] = {}
    for i, j, sc in pairs:
        if sc > best.get(i, (-1, -1.0))[1]:
            best[i] = (j, sc)

    errors: list[dict[str, Any]] = []
    n_unchecked = 0
    if engine == "text" or not missing:
        # EVIDENCE-GATED flagging: "no match found" is NOT the same claim as "this dialogue
        # line is absent". EP43's fight scene produced ~20 false MISSING (all disproved by
        # ear) because garbled shouts on both sides can never match. A flag now requires:
        #   A. the reference segment is a confidently-read dialogue line (Whisper quality,
        #      not song-like);
        #   B. the dub transcription near the expected slot is itself trustworthy — if the
        #      dub is unreadable there, the region is UNCHECKED, never MISSING;
        #   C. the semantic no-match (the alignment gap that got us here).
        drifts0 = sorted(dseg[j][0] - oseg[i][0] for i, j, _ in pairs)
        drift0 = drifts0[len(drifts0) // 2] if drifts0 else 0.0
        for i in missing:
            s, e = oseg[i]
            txt = otext[i] if otext else None
            q = oqual[i] if oqual else None
            best_seg = float(sim[i].max()) if sim.size else 0.0
            if txt and _songlike(txt):
                _say(f"  song-like @{s:.1f}-{e:.1f}s — not dialogue, not flagged  {txt!r}")
                continue
            if (e - s) > 12.0:
                # A single "line" spanning tens of seconds is a decode artefact or a song
                # section, never one piece of dialogue (EP43: 28-40 s blobs passed as lines).
                n_unchecked += 1
                _say(f"  UNCHECKED @{s:.1f}-{e:.1f}s — {e - s:.0f}s span is not a single "
                     f"dialogue line  {txt!r}")
                continue
            if q is not None and not _reliable(q, txt):
                n_unchecked += 1
                _say(f"  UNCHECKED @{s:.1f}-{e:.1f}s — reference transcript unreliable "
                     f"(nsp={q['nsp']:.2f} alp={q['alp']:.2f})  {txt!r}")
                continue
            if dqual is not None:
                # SILENCE and GARBLE are opposite evidence. No dub speech near the slot at
                # all = a hole = SUPPORTS missing (consecutive dropped lines look exactly
                # like this — gating on it cost 3 real catches on EP42). Speech that's
                # present but unreadable = we can't certify anything = UNCHECKED.
                nearby = [j for j in range(len(dseg))
                          if abs(dseg[j][0] - (s + drift0)) <= 10.0]
                if nearby and not any(_reliable(dqual[j], dtext[j] if dtext else None)
                                      for j in nearby):
                    n_unchecked += 1
                    _say(f"  UNCHECKED @{s:.1f}-{e:.1f}s — dub speech near this slot is "
                         f"unreadable; cannot certify absence  {txt!r}")
                    continue
            if dwin is not None:
                # THE ACOUSTIC TIE-BREAKER. The transcript said "no match" — but is the dub
                # actually SILENT at this slot? A true drop is a hole in the audio (EP42's
                # verified misses all are). Speech OCCUPYING the slot with no usable
                # transcript means Whisper lost it under SFX/music, not that the line is
                # absent (EP43's false flags, all verified present by ear) — UNCHECKED.
                # MAJORITY coverage, not any-overlap: in dense dialogue a neighbouring
                # line's edge always clips the slot, and any-overlap marked 34/48 real
                # slots "occupied" and collapsed EP42 recall to 1/5. A dropped line leaves
                # most of its slot silent. Drift is clamped: a garbage median from poor
                # pairing must not shift every slot onto its neighbour.
                d0 = max(-3.0, min(3.0, drift0))
                slot = (s + d0, e + d0)
                dur = max(0.2, slot[1] - slot[0])
                cov = sum(max(0.0, min(slot[1], w[1]) - max(slot[0], w[0])) for w in dwin)
                if cov / dur >= 0.5:
                    n_unchecked += 1
                    _say(f"  UNCHECKED @{s:.1f}-{e:.1f}s — dub speech covers "
                         f"{cov / dur:.0%} of this slot but gave no usable transcript; "
                         f"cannot certify absence  {txt!r}")
                    continue
            if txt and dtext is not None:
                near_lines = [dtext[j] for j in range(len(dseg))
                              if abs(dseg[j][0] - (s + drift0)) <= 12.0]
                verdict = _judge(txt, original_lang, near_lines, dub_lang)
                if verdict == "garble":
                    n_unchecked += 1
                    _say(f"  UNCHECKED @{s:.1f}-{e:.1f}s — judge: not coherent dialogue  {txt!r}")
                    continue
                if verdict == "present":
                    _say(f"  cleared-by-judge @{s:.1f}-{e:.1f}s — a dub line conveys it  {txt!r}")
                    continue
            _say(f"  MISSING @{s:.1f}-{e:.1f}s best={best_seg:.2f}  {txt!r}")
            errors.append(_missing(i, s, e, dub_label, best_seg, txt))
    else:
        # SONAR engine only: re-check each candidate against the raw dub audio near its
        # expected (sync-corrected) position before reporting it missing.
        rescued = 0
        drifts = sorted(dseg[j][0] - oseg[i][0] for i, j, _ in pairs)
        med_drift = drifts[len(drifts) // 2] if drifts else 0.0
        _say(f"second pass: re-checking {len(missing)} candidate(s) against the dub audio "
             f"(sync offset {med_drift:+.2f}s)")
        for i in missing:
            s, e = oseg[i]
            best_seg = float(sim[i].max()) if sim.size else 0.0
            found, at = _rescue(dv, R[i], e - s, s + med_drift, _lang3(dub_lang), mu=mu)
            _say(f"  window @{s:.1f}-{e:.1f}s: aligned-best={best_seg:.2f} "
                 f"scan-best={found:.2f} @{at:.1f}s -> {'cleared' if found >= RESCUE_SIM else 'MISSING'}")
            if found >= RESCUE_SIM:
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
        dt = dtext[j] if dtext else None
        # Same evidence bar for EXTRA: an unreliable or song-like dub segment is noise,
        # not an improvised line.
        if dqual is not None and not _reliable(dqual[j]):
            continue
        if dt is not None and (_songlike(dt) or len(dt.split()) < 2):
            continue
        errors.append({
            "type": "EXTRA", "subtype": None, "severity": "info",
            "character": None, "channel": dub_label, "script_index": None,
            "script_start_s": None, "script_end_s": None,
            "audio_start_s": round(s, 3), "audio_end_s": round(e, 3),
            "drift_s": None, "coverage": None, "text": dt,
            "message": (f"The dub speaks at {s:.2f}–{e:.2f}s"
                        + (f" ({dt!r})" if dt else "")
                        + " with nothing matching it in the original — added or improvised."),
        })
    if n_unchecked:
        _say(f"coverage: {len(oseg) - n_unchecked}/{len(oseg)} original lines verifiable "
             f"({n_unchecked} unchecked — transcript unreliable on one side)")
    return _report(errors, oseg, dub_label, tol_s, n_unchecked=n_unchecked)


def _confidence(best: float, text: str | None) -> str:
    """How sure we are a flagged line is REALLY missing. Per the studio workflow the sound
    team quickly verifies each flag in Pro Tools, so over-flagging is fine — what they want
    is a triage order. Lower best-match = stronger evidence of absence; a full sentence is
    stronger evidence than a one-word grunt (grunts/shouts often go undubbed by design)."""
    words = len((text or "").split())
    if best < 0.35 and words >= 3:
        return "high"
    if best < 0.5:
        return "medium"
    return "low"


def _missing(i: int, s: float, e: float, ch: str, best: float,
             text: str | None = None) -> dict[str, Any]:
    return {
        "type": "MISSING", "subtype": None, "severity": "error",
        "character": None, "channel": ch, "script_index": i,
        "script_start_s": round(s, 3), "script_end_s": round(e, 3),
        "audio_start_s": None, "audio_end_s": None,
        "drift_s": None, "coverage": round(best, 3), "text": text,
        "confidence": _confidence(best, text),
        "message": (f"The original speaks at {s:.2f}–{e:.2f}s"
                    + (f" ({text!r})" if text else "")
                    + f" and the dub has nothing saying the same thing (best match "
                      f"{best:.0%}, confidence: {_confidence(best, text)}) — dropped, "
                      f"or moved by more than a few seconds."),
    }


def _report(errors: list[dict[str, Any]], oseg: list, ch: str, tol_s: float,
            n_unchecked: int = 0) -> dict[str, Any]:
    n = {t: sum(1 for e in errors if e["type"] == t) for t in ("MISSING", "MISALIGNED", "EXTRA")}
    total = len(oseg)
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
            "n_sync_warnings": 0, "n_original_regions": total,
            # HONESTY METRIC: how much of the original's dialogue we could actually verify.
            # A region where either side's transcript is unreliable is declared UNCHECKED,
            # never guessed at — the studio must know what the tool did not see.
            "n_unchecked": n_unchecked,
            "coverage": round(1.0 - n_unchecked / total, 3) if total else 0.0,
        },
    }


def _empty(why: str, tol_s: float, ch: str) -> dict[str, Any]:
    r = _report([], [], ch, tol_s)
    r["why"] = why
    return r
