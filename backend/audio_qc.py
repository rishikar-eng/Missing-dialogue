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

import json as _json
import os
import re
import threading
from typing import Any

import numpy as np

from . import align as _align

# SONAR speech encoders are per-language: ISO-639-3 codes.
LANG3 = {"hindi": "hin", "tamil": "tam", "telugu": "tel", "kannada": "kan",
         "bengali": "ben", "marathi": "mar", "malayalam": "mal", "english": "eng",
         "punjabi": "pan", "japanese": "jpn", "portuguese": "por"}
# Whisper wants ISO-639-1.
LANG1 = {"hindi": "hi", "tamil": "ta", "telugu": "te", "kannada": "kn", "bengali": "bn",
         "marathi": "mr", "malayalam": "ml", "english": "en", "japanese": "ja",
         "punjabi": "pa", "portuguese": "pt"}

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
        # The image pins OMP_NUM_THREADS=4, which silently capped a SOLO separation (the
        # steady state once caches exist: original warm, dub cold) at 4 of the task's cores.
        # Pair-warm workers set their own split; a solo run should use the whole machine.
        torch.set_num_threads(max(1, os.cpu_count() or 4))
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


def _warm_separation(path: str) -> None:
    """Child-process worker: populate one file's .voc16/.acc16 cache, using half the cores
    so two workers share the machine instead of fighting over one torch thread pool."""
    import torch
    torch.set_num_threads(max(1, (os.cpu_count() or 2) // 2))
    _separate(path)


def _warm_pair(paths: list[str]) -> bool:
    """Separate two cold files concurrently (Demucs dominates full-episode wall time and the
    two files are independent). Returns True if the caches were populated; any failure falls
    back to the sequential path, which redoes the work safely."""
    import concurrent.futures as _cf
    try:
        with _cf.ProcessPoolExecutor(max_workers=2) as ex:
            list(ex.map(_warm_separation, paths))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[audio_qc] parallel separation unavailable ({e}); separating sequentially",
              flush=True)
        return False


_VAD_TLS = threading.local()


def segment(audio16: np.ndarray) -> list[tuple[float, float]]:
    """Silero VAD over the ISOLATED dialogue → (start, end) speech windows.

    ONE MODEL PER THREAD. The instance used to be cached on this function and shared, but
    Silero's RNN carries hidden state across calls, so the two sides transcribing
    concurrently could interleave inside it and blow up with
    `select(): index 1 out of range for tensor of size [1, 64]` — a whole task lost, at
    random, only when both sides reached the VAD together (it killed an overlap-test run on
    2026-08-04 and is the likeliest cause of the earlier unexplained exit-139). The model is
    ~2 MB, so a per-thread copy is far cheaper than serialising the two sides.
    """
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    vad = getattr(_VAD_TLS, "vad", None)
    if vad is None:
        vad = _VAD_TLS.vad = load_silero_vad()
    reset = getattr(vad, "reset_states", None)
    if reset:
        reset()                       # never inherit the previous clip's hidden state
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
    prep = _prep_chunks(audio16)
    return _decode(prep, lang1) if prep else []


def _prep_chunks(audio16: np.ndarray):
    """VAD + concatenation + chunking — the DETERMINISTIC half of transcription.

    Split out from the API calls so the two independent readings of one side share a single
    Silero pass (it was being run once per pass, i.e. four times per episode) and so every
    chunk request can be issued concurrently.
    """
    wins = segment(audio16)
    if not wins:
        return None
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

    # Full episodes exceed Groq's 25 MB/request cap, so the concatenated speech is sent in
    # chunks split ONLY at VAD-window boundaries (never mid-utterance). PCM_16 mono 16 kHz is
    # 32 kB/s, so 560 s/chunk ≈ 18 MB — comfortably under the cap.
    CHUNK_S = 560.0
    chunks: list[tuple[np.ndarray, float]] = []
    start = 0
    while start < len(cmap):
        end = start
        t0 = cmap[start][0]
        while end < len(cmap) and (cmap[end][0] + cmap[end][2] - t0) <= CHUNK_S:
            end += 1
        if end == start:
            end = start + 1                        # one window longer than the cap: send alone
        chunks.append((np.concatenate(pieces[2 * start:2 * end]), t0))
        start = end
    return chunks, back


def _decode(prep, lang1: str, passes: int = 1) -> list:
    """Issue every (pass × chunk) transcription request CONCURRENTLY.

    These are pure network waits against Groq, and they were running strictly in sequence —
    eight of them for a double-passed 25-minute episode, which dominated the run once
    separation was cached. Concurrency is capped so the account's rate limit isn't tripped
    (a 429 already costs a 15 s+ backoff in _groq_call, which would undo the win).
    """
    import concurrent.futures as _cf
    chunks, back = prep
    out: list[list] = [[] for _ in range(passes)]
    tasks = [(p, c) for p in range(passes) for c in range(len(chunks))]
    if len(tasks) > 1:
        print(f"[audio_qc] {len(tasks)} transcription requests in flight "
              f"({passes} pass(es) × {len(chunks)} chunk(s))", flush=True)
    with _cf.ThreadPoolExecutor(max_workers=min(4, len(tasks))) as ex:
        futs = {ex.submit(_groq_call, chunks[c][0], lang1): (p, c) for p, c in tasks}
        for f in _cf.as_completed(futs):
            p, c = futs[f]
            t0 = chunks[c][1]
            for cs, ce, txt, q in f.result():
                s2, e2 = back(cs + t0), back(ce + t0)   # chunk-local + t0 = global concat time
                if e2 - s2 >= 0.2 and txt:
                    out[p].append((round(s2, 2), round(e2, 2), txt, q))
    for lst in out:
        lst.sort(key=lambda x: (x[0], x[1]))       # completion order isn't timeline order
    return out[0] if passes == 1 else out


def transcribe_two_passes(audio16: np.ndarray, lang1: str) -> tuple[list, list]:
    """Two independent readings of the same audio (Groq's ASR is nondeterministic), sharing
    one VAD pass and issuing all requests at once."""
    prep = _prep_chunks(audio16)
    if not prep:
        return [], []
    a, b = _decode(prep, lang1, passes=2)
    return a, b


def _sarvam_call(audio16: np.ndarray, s0: float, e0: float) -> tuple | None:
    """One Sarvam Saaras-v3 request for ONE VAD window (their sync API caps at 30 s — which
    fits us perfectly: one window per request means the transcript maps to the window
    exactly, so Sarvam draws carry ZERO segmentation jitter). Quality signals are synthesized:
    Sarvam has no no-speech/logprob, so nsp/alp are neutral and compression_ratio is computed
    from the text itself (the same repetition-garble signal, different source)."""
    import io as _io
    import zlib

    import httpx
    import soundfile as sf
    key = os.environ.get("SARVAM_API_KEY", "")
    if not key:
        return None
    buf = _io.BytesIO()
    sf.write(buf, audio16[int(s0 * 16000):int(e0 * 16000)], 16000, format="WAV",
             subtype="PCM_16")
    buf.seek(0)
    for attempt in range(3):
        try:
            r = httpx.post("https://api.sarvam.ai/speech-to-text",
                           headers={"api-subscription-key": key},
                           files={"file": ("w.wav", buf.getvalue())},
                           data={"model": "saaras:v3"}, timeout=120)
        except httpx.RequestError:
            import time as _t
            _t.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            import time as _t
            _t.sleep(5 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None
        txt = (r.json().get("transcript") or "").strip()
        if not txt:
            return None
        raw = txt.encode("utf-8")
        cr = len(raw) / max(1, len(zlib.compress(raw)))
        return (round(s0, 2), round(e0, 2), txt,
            {"nsp": 0.0, "alp": 0.0, "cr": round(cr, 2), "engine": "sarvam"})
    return None


_SARVAM_BASE = "https://api.sarvam.ai/speech-to-text/job/v1"


def _sarvam_req(method: str, url: str, **kw):
    """One Sarvam/Azure HTTP call with transient-error retries (429/5xx/connection). A batch
    job is minutes of server-side work reached through five small HTTP calls — one blip in
    any of them must not throw the job away. Raises on persistent failure; the caller treats
    ANY exception as 'batch unusable' and falls back to the per-window sync path."""
    import time as _t

    import httpx
    last = ""
    for attempt in range(4):
        try:
            r = httpx.request(method, url, timeout=kw.pop("timeout", 120), **kw)
        except httpx.RequestError as e:
            last = type(e).__name__
            _t.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            _t.sleep((15 if r.status_code == 429 else 5) * (attempt + 1))
            continue
        if r.status_code >= 300:
            raise RuntimeError(f"Sarvam batch: {method} {url.split('?')[0]} -> "
                               f"HTTP {r.status_code}: {r.text[:200]}")
        return r
    raise RuntimeError(f"Sarvam batch: {method} {url.split('?')[0]} kept failing ({last})")


_SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"


def _tcache_read(cache_path: str | None) -> list | None:
    """A cached transcript, or None to recompute.

    An EMPTY cache counts as a MISS. On 2026-08-10 Sarvam ran out of credits, returned
    nothing for Gavv EP41's Hindi original, and we persisted `[]` — which every later run
    would have read back as the settled truth "this audio contains no speech", silently
    flagging the entire episode as missing with no API call left to notice. A genuinely
    silent side is rare and costs one recompute; a poisoned cache costs an episode.
    """
    if not (cache_path and os.path.exists(cache_path)):
        return None
    try:
        rows = [(x[0], x[1], x[2], x[3]) for x in _json.load(open(cache_path))]
    except Exception:  # noqa: BLE001 — a corrupt cache just recomputes
        return None
    return rows or None


def _tcache_write(cache_path: str | None, out: list) -> None:
    """Persist a transcript — never an empty one (see _tcache_read)."""
    if not (cache_path and out):
        return
    try:
        _json.dump(out, open(cache_path, "w"))
    except OSError:
        pass


def transcribe_scribe(audio16: np.ndarray, cache_path: str | None = None,
                      lang1: str | None = None) -> list:
    """ONE ElevenLabs Scribe reading of a whole side — the fast alternative to Whisper's
    5-draw union, and the only engine that also covers non-Indic pairs (POA).

    Like the Sarvam batch path it uploads the `_prep_chunks` concatenation (every VAD window
    joined) as a single FLAC and maps word timestamps back through the same piecewise map,
    so one request replaces hundreds. Unlike Sarvam it returns REAL quality signals, so the
    gates work on measurement instead of synthesized zeros:

      alp = mean per-word logprob of the segment (Scribe's own confidence)
      nsp = share of the segment that is non-speech `audio_event` entries ([music], [tone]…)
      cr  = compression ratio of the text, as everywhere else

    `lang1` pins the language (ISO-639-1) — free insurance against a noisy stretch being
    decoded as some other language. Deterministic enough to run once and cache, like Sarvam.
    Returns [(start, end, text, q)]; [] when the side has no speech; raises on API failure.
    """
    import io as _io
    import json as _json
    import time as _t
    import zlib

    import httpx
    import soundfile as sf
    _hit = _tcache_read(cache_path)
    if _hit is not None:
        return _hit
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        return []
    prep = _prep_chunks(audio16)
    if not prep:
        return []
    chunks, back = prep
    full = np.concatenate([c for c, _ in chunks])
    buf = _io.BytesIO()
    sf.write(buf, full, 16000, format="FLAC")
    data = {"model_id": "scribe_v1", "timestamps_granularity": "word", "diarize": "false"}
    if lang1:
        data["language_code"] = lang1
    t0 = _t.time()
    print(f"[audio_qc] scribe: {len(full) / 16000.0:.0f}s of speech, "
          f"{buf.getbuffer().nbytes >> 20} MB FLAC"
          + (f", language pinned to {lang1}" if lang1 else ""), flush=True)
    last = None
    js = None
    for attempt in range(5):
        try:
            r = httpx.post(_SCRIBE_URL, headers={"xi-api-key": key},
                           files={"file": ("audio.flac", buf.getvalue(), "audio/flac")},
                           data=data, timeout=1800)
            if r.status_code == 200:
                js = r.json()
                break
            last = f"HTTP {r.status_code}: {r.text[:160]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"Scribe rejected the request — {last}")
        except (httpx.HTTPError, OSError) as e:  # transport hiccup: retry
            last = f"{type(e).__name__}: {e}"
        _t.sleep(5 * (attempt + 1))
    if js is None:
        raise RuntimeError(f"Scribe failed after 5 attempts (last: {last})")
    words = js.get("words") or []
    if not words:
        return [] if not (js.get("text") or "").strip() else []
    print(f"[audio_qc] scribe: {len(words)} words in {_t.time() - t0:.0f}s "
          f"(detected {js.get('language_code')})", flush=True)

    # Group words into lines. A break is a real pause, a sentence end, or — critically — a
    # concat-gap crossing: the concatenation joins windows that are far apart on the real
    # timeline, so a segment spanning one would claim a span of silence it never covers.
    segs: list[list] = []
    cur: list[dict] = []
    prev_end_c = None
    for w in words:
        if w.get("type") == "spacing":
            continue
        try:
            cs, ce = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if cur and prev_end_c is not None:
            gap_c = cs - prev_end_c                      # gap on the concatenated timeline
            gap_r = back(cs) - back(prev_end_c)          # …and on the real one
            crossed = gap_r - gap_c > 0.25               # a window boundary was jumped
            if crossed or gap_c > 0.7 or (cur[-1].get("text") or "").endswith((".", "?", "!", "।")):
                segs.append(cur)
                cur = []
        cur.append(w)
        prev_end_c = ce
    if cur:
        segs.append(cur)

    out = []
    for ws in segs:
        spoken = [w for w in ws if w.get("type") != "audio_event"]
        text = " ".join((w.get("text") or "").strip() for w in ws
                        if w.get("type") != "audio_event").strip()
        s2, e2 = back(float(ws[0]["start"])), back(float(ws[-1]["end"]))
        if not text or e2 - s2 < 0.2:
            continue
        lps = [float(w["logprob"]) for w in ws if isinstance(w.get("logprob"), (int, float))]
        raw = text.encode("utf-8")
        out.append((round(s2, 2), round(e2, 2), text, {
            "nsp": round(1.0 - len(spoken) / max(1, len(ws)), 2),   # non-speech share
            "alp": round(sum(lps) / len(lps), 3) if lps else 0.0,   # REAL mean logprob
            "cr": round(len(raw) / max(1, len(zlib.compress(raw))), 2),
            "engine": "scribe"}))
    out.sort(key=lambda x: (x[0], x[1]))
    print(f"[audio_qc] scribe: {len(out)} lines", flush=True)
    _tcache_write(cache_path, out)
    return out


def _sarvam_batch(audio16: np.ndarray) -> list | None:
    """ONE Sarvam Batch-API job for a whole side, instead of ~400 sync requests.

    The sync path sends every VAD window as its own 30s-capped request, which crawls into
    rate limits on a full episode. The Batch API takes files up to 2 h, so the same
    `_prep_chunks` concatenation built for Groq (all speech windows joined with 0.5 s gaps)
    is uploaded as a single FLAC, and the returned chunk-level timestamps are mapped back to
    the real timeline through the same piecewise map. Quality signals are synthesized
    exactly like the sync path: nsp/alp neutral, compression_ratio from the text itself.

    Trade against sync: segmentation comes from Saaras' own sentence chunking rather than
    mapping 1:1 onto our VAD windows — the batch and sync paths therefore produce different
    (both valid) line boundaries.

    with_diarization is MANDATORY, found by measurement, not in the docs: without it the
    batch output is 2 mega-chunks for 50 s of speech (21 s spans — the >12 s span gate
    would kill every candidate); with it the same audio returns 11 tight sentence-level
    entries, same text. Also measured: the timestamp text array arrives under
    `timestamps.words` (the docs say `chunks`) — both keys are read.

    Job flow (verified against the sarvamai SDK): init -> upload-files (presigned Azure
    URLs) -> PUT bytes -> start -> poll status -> download-files -> GET result JSON.
    Returns [(start, end, text, q)], or None when the batch result is unusable (caller
    falls back to sync). Raises on API failure — same contract for the caller.
    """
    import io as _io
    import time as _t
    import zlib

    import soundfile as sf
    key = os.environ.get("SARVAM_API_KEY", "")
    if not key:
        return None
    prep = _prep_chunks(audio16)
    if not prep:
        return []
    chunks, back = prep
    full = np.concatenate([c for c, _ in chunks])
    if len(full) / 16000.0 >= 7200.0:              # Batch API cap is 2 h per file
        return None
    buf = _io.BytesIO()
    sf.write(buf, full, 16000, format="FLAC")
    hdr = {"api-subscription-key": key}
    t0 = _t.time()

    r = _sarvam_req("POST", _SARVAM_BASE, headers=hdr, json={
        "job_parameters": {"model": "saaras:v3", "with_timestamps": True,
                           "with_diarization": True}})
    job_id = r.json()["job_id"]
    print(f"[audio_qc] sarvam batch: job {job_id} created "
          f"({len(full) / 16000.0:.0f}s of speech, {buf.getbuffer().nbytes >> 20} MB FLAC)",
          flush=True)

    name = "0.flac"
    r = _sarvam_req("POST", f"{_SARVAM_BASE}/upload-files", headers=hdr,
                    json={"job_id": job_id, "files": [name]})
    up_url = r.json()["upload_urls"][name]["file_url"]
    # Azure Block Blob presigned PUT — the x-ms-blob-type header is mandatory (this is
    # exactly what the official SDK sends).
    _sarvam_req("PUT", up_url, content=buf.getvalue(),
                headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "audio/flac"},
                timeout=600)
    _sarvam_req("POST", f"{_SARVAM_BASE}/{job_id}/start", headers=hdr)

    state, status = "", {}
    deadline = _t.time() + 2700                    # a stuck job must not hang the task
    while _t.time() < deadline:
        _t.sleep(10)
        try:
            status = _sarvam_req("GET", f"{_SARVAM_BASE}/{job_id}/status", headers=hdr).json()
        except RuntimeError:
            continue                               # one bad poll is not a dead job
        if status.get("job_state") != state:
            state = status.get("job_state")
            print(f"[audio_qc] sarvam batch: {state} ({_t.time() - t0:.0f}s)", flush=True)
        if state in ("Completed", "Failed"):
            break
    if state != "Completed":
        raise RuntimeError(f"Sarvam batch: job {job_id} ended {state or 'unfinished'}: "
                           f"{status.get('error_message')}")

    out_name = None
    for det in status.get("job_details") or []:
        if det.get("state") == "Success" and det.get("outputs"):
            out_name = det["outputs"][0]["file_name"]
            break
    if not out_name:
        raise RuntimeError(f"Sarvam batch: job {job_id} completed with no successful file")
    r = _sarvam_req("POST", f"{_SARVAM_BASE}/download-files", headers=hdr,
                    json={"job_id": job_id, "files": [out_name]})
    data = _sarvam_req("GET", r.json()["download_urls"][out_name]["file_url"],
                       timeout=300).json()
    print(f"[audio_qc] sarvam batch: done in {_t.time() - t0:.0f}s", flush=True)

    ts = data.get("timestamps") or {}
    texts = ts.get("words") or ts.get("chunks") or []
    ss = ts.get("start_time_seconds") or []
    es = ts.get("end_time_seconds") or []
    if not texts:
        # No timestamped chunks: an empty read of a silent side is a valid [], but a
        # transcript WITHOUT timestamps cannot be mapped to the timeline — unusable.
        return [] if not (data.get("transcript") or "").strip() else None
    if not (len(texts) == len(ss) == len(es)):
        return None
    out = []
    for txt, cs, ce in zip(texts, ss, es):
        txt = (txt or "").strip()
        s2, e2 = back(float(cs)), back(float(ce))  # concat timeline -> real timeline
        if txt and e2 - s2 >= 0.2:
            raw = txt.encode("utf-8")
            cr = len(raw) / max(1, len(zlib.compress(raw)))
            out.append((round(s2, 2), round(e2, 2), txt,
                        {"nsp": 0.0, "alp": 0.0, "cr": round(cr, 2), "engine": "sarvam"}))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def transcribe_sarvam(audio16: np.ndarray, cache_path: str | None = None) -> list:
    """Sarvam pass: one concurrent request per VAD window (windows >28 s are split).

    Sarvam is DETERMINISTIC (same window in, same transcript out) — only Whisper needs the
    5-draw lottery. So the reading is computed ONCE (by draw 1, which runs alone) and cached
    beside the input; concurrent draws 2..N read the file instead of re-issuing hundreds of
    identical requests, which was 5x the API traffic for zero information and the reason the
    both-sides run crawled into Sarvam's rate limits.

    AQC_SARVAM_BATCH=1 routes the whole side through ONE Batch-API job instead (~400 sync
    requests -> 1); any batch failure logs and falls back to the sync path unchanged."""
    import json as _json
    _hit = _tcache_read(cache_path)
    if _hit is not None:
        return _hit
    if os.environ.get("AQC_SARVAM_BATCH", "").strip() == "1":
        try:
            out = _sarvam_batch(audio16)
        except Exception as e:  # noqa: BLE001 — batch is an optimisation, never a new failure
            print(f"[audio_qc] sarvam batch unusable ({e}); falling back to sync windows",
                  flush=True)
            out = None
        if out is not None:
            _tcache_write(cache_path, out)
            return out
    import concurrent.futures as _cf
    wins = []
    for (s0, e0) in segment(audio16):
        while e0 - s0 > 28.0:
            wins.append((s0, s0 + 28.0))
            s0 += 28.0
        wins.append((s0, e0))
    out = []
    with _cf.ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(lambda w: _sarvam_call(audio16, w[0], w[1]), wins):
            if r:
                out.append(r)
    out.sort(key=lambda x: x[0])
    _tcache_write(cache_path, out)
    return out


def _merge_passes(a: list, b: list) -> list:
    """Union of two transcription passes of the SAME audio.

    Groq whisper segmentation is nondeterministic (28–48 lines observed for identical
    input), so a line one pass never renders simply cannot be flagged — EP42's 178.8s drop
    was lost this way: pass 1 gave two 2-char alp=-2.53 fragments where pass 2 read the
    full sentence. Pass-B segments are adopted only where reliable pass-A coverage is
    <50% (boundary jitter between passes must not create duplicate candidates for the
    one-to-one alignment), and junk pass-A fragments superseded by an adopted read retire.
    """
    def _cov(seg, others):
        s, e = seg[0], seg[1]
        got = sum(max(0.0, min(e, o[1]) - max(s, o[0])) for o in others)
        return got / max(0.2, e - s)

    rel_a = [x for x in a if _reliable(x[3], x[2])]
    out = list(a)
    for seg in b:
        if _reliable(seg[3], seg[2]) and _cov(seg, rel_a) < 0.5:
            out = [x for x in out if _reliable(x[3], x[2]) or _cov(x, [seg]) < 0.5]
            out.append(seg)
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def _groq_call(audio16: np.ndarray, lang1: str) -> list[tuple[float, float, str]]:
    """One transcription request against the Groq API, retrying anything transient.

    A single upstream hiccup must not throw away the whole run: one chunk of one pass got a
    500 from Groq and killed a four-minute EP41 job outright. Rate limits (429) and server
    errors (5xx) are both retried with backoff; only a genuine client error (bad key,
    oversized payload) is allowed to propagate.
    """
    import io as _io

    import httpx
    import soundfile as sf
    buf = _io.BytesIO()
    sf.write(buf, audio16, 16000, format="FLAC")   # lossless, ~2-3x smaller than PCM WAV
    buf.seek(0)
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set (needed for the text engine)")
    import time as _t
    last = ""
    for attempt in range(5):
        try:
            r = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions",
                           headers={"Authorization": f"Bearer {key}"},
                           files={"file": ("a.flac", buf.getvalue())},
                           data={"model": os.environ.get("AQC_ASR_MODEL", "whisper-large-v3"), "language": lang1,
                                 "response_format": "verbose_json"},
                           timeout=300)
        except httpx.RequestError as e:             # connection reset / read timeout
            last = f"{type(e).__name__}"
            _t.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            wait = 15 * (attempt + 1) if r.status_code == 429 else 5 * (attempt + 1)
            print(f"[audio_qc] Groq {last}; retrying in {wait}s "
                  f"(attempt {attempt + 1}/5)", flush=True)
            _t.sleep(wait)
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
    raise RuntimeError(f"Groq transcription failed after 5 attempts (last: {last})")



# A MISSING flag says "the dub never says this line". WHAT the dub does instead is a
# different, useful fact: a silent hole reads as a plain drop, while continuous
# low-dynamic-range energy is walla/ambience laid over an un-dubbed line — which is what
# BOTH ear-verified drops of POA EP12 turned out to be. Speech-like energy (big swings
# between words) means something IS spoken and the flag deserves more suspicion.
_FILL_SILENCE_REL_DB = -22.0     # this far below the dub's own typical speech = a hole
_FILL_FLAT_DYN_DB = 22.0         # less swing than this across the slot = a steady wash


def _dub_fill(dv: np.ndarray, s: float, e: float, drift: float,
              ref_db: float | None, dwin: list | None = None) -> dict | None:
    """Classify the dub audio under a missing line: silence | ambience | speech-like.
    Levels are RELATIVE to the dub's own typical speech level, so mastering/gain choices
    cannot move the verdict. Returns None when the slot cannot be measured.

    NEIGHBOUR SPEECH IS EXCLUDED. A line reaches here having passed the slot gate, which
    tolerates up to 40% of the slot being real dub speech from the line next door — dense
    dialogue always clips a neighbour's edge. Measuring the whole slot would let those few
    frames read as 'dub speaks here' and invite a reviewer to dismiss a genuine drop, so the
    VAD-confirmed dub windows are cut out first and only the REMAINDER is described. Too
    little remainder to judge honestly -> None (the column stays blank) rather than a guess.
    """
    if dv is None or ref_db is None or not len(dv):
        return None
    i0 = max(0, int((s + drift) * 16000))
    i1 = min(len(dv), int((e + drift) * 16000))
    if i1 - i0 < 3200:                                  # under 0.2 s: nothing to judge
        return None
    keep = np.ones(i1 - i0, dtype=bool)
    for wa, wb in (dwin or []):
        ja, jb = int(wa * 16000) - i0, int(wb * 16000) - i0
        if jb <= 0 or ja >= len(keep):
            continue
        keep[max(0, ja):min(len(keep), jb)] = False
    x = dv[i0:i1][keep].astype("float64")
    # 0.2 s of non-neighbour slot, matching the i1-i0 test above. This floor used to be
    # 0.5 s, which silently blanked the acoustic column for EVERY flag shorter than that —
    # including POA EP11's ear-verified real drop ('Alguem paga ele.', 0.42 s), whose dub
    # slot is digital silence. Short lines are exactly where the semantic score is least
    # trustworthy, so losing the physical measurement there was backwards. 0.2 s still
    # yields the 2+ frames the dynamic-range test below needs.
    if len(x) < 3200:
        return None
    hop = 1600                                          # 100 ms frames
    n = len(x) // hop
    if n < 2:
        return None
    fr = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
    # ROBUST STATISTICS, because one frame decides a verdict otherwise. A slot of digital
    # black with a single 100 ms leak from a neighbour's word onset used to read as
    # 'speech-like': the MEAN was dragged above the silence bar by that one frame, and the
    # dynamic range was max/min with min == 0, giving ~156 dB of "dynamics" from silence.
    # The workbook then told the reviewer "dub speaks here" over a genuine hole. Median and
    # a p90/p10 spread describe the same slot without letting one frame speak for twelve.
    mean_db = 20.0 * np.log10(max(float(np.median(fr)), 1e-9))
    hi, lo = float(np.percentile(fr, 90)), float(np.percentile(fr, 10))
    dyn_db = 20.0 * np.log10(max(hi, 1e-9) / max(lo, 1e-9))
    rel = mean_db - ref_db
    if rel <= _FILL_SILENCE_REL_DB:
        kind = "silence"
    elif dyn_db < _FILL_FLAT_DYN_DB:
        kind = "ambience"
    else:
        kind = "speech-like"
    return {"fill": kind, "fill_rel_db": round(rel, 1), "fill_dyn_db": round(dyn_db, 1)}


def _speech_ref_db(dv: np.ndarray, dseg: list) -> float | None:
    """The dub's OWN typical speech level (median 100 ms frame across its speech windows) —
    the yardstick every fill measurement is relative to."""
    if dv is None or not len(dv) or not dseg:
        return None
    vals = []
    for a, b in dseg[:400]:
        i0, i1 = max(0, int(a * 16000)), min(len(dv), int(b * 16000))
        n = (i1 - i0) // 1600
        if n < 1:
            continue
        x = dv[i0:i0 + n * 1600].astype("float64").reshape(n, 1600)
        vals.append(np.sqrt((x ** 2).mean(axis=1)))
    if not vals:
        return None
    allf = np.concatenate(vals)
    return 20.0 * np.log10(max(float(np.median(allf)), 1e-9))


# --- Feature A: consecutive missing lines are ONE finding -------------------
# 18 of POA EP12's 33 flags were the untranslated opening song: eighteen "separate" findings
# that are really one fact — nobody dubbed 00:16-01:22. Grouping is presentation only:
# every line is still reported (recall untouched), they just carry a block id so the
# summary can count the block once and the report can say what it really is.
_BLOCK_GAP_S = 10.0      # measured on EP12: 10 s captures the whole song, 8 s missed one line
_BLOCK_MIN = 4           # EP12's real drops sat in groups of 1 and 2 — well clear of this
_SONG_EDGE_S = 15.0      # how far past the outermost confirmed lyric a song still reaches
_BLOCK_MAX_SLOT_COV = 0.35   # a block means the dub is SILENT throughout, not just unmatched


def _norm_words(t: str | None) -> set:
    """Content words of a line, lowercased and stripped of punctuation."""
    import re as _re
    return {w for w in _re.split(r"[^0-9a-zA-ZÀ-ɏऀ-ൿ]+",
                                 (t or "").lower()) if len(w) >= 2}


def _song_bank(series: str | None) -> set:
    """Lyrics this SERIES is known to leave untranslated, learned from every episode we have
    already run. A show's theme recurs in all of its episodes, so a block detected in EP10
    teaches EP25 — which matters because the reprise rule can otherwise only match against
    the block found in the SAME episode. EP25 proved the gap: its detected block is the
    OPENING song, so a mid-episode burst of the CLOSING song ("Voce diz quando se vira") had
    nothing local to match and was reported as dropped dialogue.
    Best-effort: no bank, no behaviour change."""
    if not series:
        return set()
    try:
        import json as _json

        import boto3
        b = os.environ.get("DQC_S3_BUCKET", "")
        if not b:
            return set()
        key = f"songbank/{re.sub(r'[^A-Za-z0-9]+', '-', series).strip('-').lower()}.json"
        body = boto3.client("s3").get_object(Bucket=b, Key=key)["Body"].read()
        return {w for w in _json.loads(body).get("words", []) if len(w) > 2}
    except Exception:  # noqa: BLE001
        return set()


def _save_song_bank(series: str | None, errors: list[dict]) -> None:
    """Merge this run's block lyrics into the series bank so later episodes benefit."""
    if not series:
        return
    try:
        import json as _json

        import boto3
        b = os.environ.get("DQC_S3_BUCKET", "")
        if not b:
            return
        # A LYRIC IS A LINE THAT COMES BACK IN ANOTHER EPISODE.
        #
        # A block is a purely structural artefact — 4+ consecutive missing lines with the
        # dub quiet — which is also the exact signature of an un-dubbed SCENE or a bad
        # delivery. Banking every block taught the series that ordinary dialogue was lyric,
        # permanently and silently (POA's live bank had absorbed 122 words of plain
        # vocabulary). But testing a block for internal repetition is the wrong instrument
        # too: POA's theme does not repeat WITHIN an episode, it repeats ACROSS them, so
        # that test refused to learn the real songs and the closing theme came back as
        # four high-confidence "drops".
        #
        # So block lines are recorded as CANDIDATES with the episode that produced them,
        # and words are promoted to the bank only once the same line has appeared in two
        # DIFFERENT episodes. A dropped scene happens once; a title theme happens weekly.
        episode = str(os.environ.get("AQC_EPISODE") or "").strip() or "?"
        cand_key = f"songbank/{re.sub(r'[^A-Za-z0-9]+', '-', series).strip('-').lower()}.candidates.json"
        s3 = boto3.client("s3")
        try:
            cands = _json.loads(s3.get_object(Bucket=b, Key=cand_key)["Body"].read())
        except Exception:  # noqa: BLE001
            cands = {}
        for e in errors:
            if e.get("type") == "MISSING" and e.get("block"):
                line = " ".join(sorted(w for w in _norm_words(e.get("text")) if len(w) > 2))
                if len(line.split()) < 3:
                    continue                   # too generic to identify a lyric by
                seen = set(cands.get(line, []))
                seen.add(episode)
                cands[line] = sorted(seen)
        s3.put_object(Bucket=b, Key=cand_key, Body=_json.dumps(cands).encode())
        words = set()
        for line, eps in cands.items():
            if len(eps) >= 2:                  # the same line, in two different episodes
                words |= set(line.split())
        if not words:
            print(f"[audio_qc] song bank: {len(cands)} candidate lines, none yet seen in "
                  f"two episodes — nothing promoted", flush=True)
            return
        key = f"songbank/{re.sub(r'[^A-Za-z0-9]+', '-', series).strip('-').lower()}.json"
        # Derived from the candidate ledger every time rather than unioned into the old
        # bank, so the bank stays a pure function of what has actually been observed twice
        # — a wrong promotion can be undone by correcting the ledger instead of living
        # forever in a set nothing ever prunes.
        merged = sorted(words)
        s3.put_object(Bucket=b, Key=key, Body=_json.dumps({"words": merged}).encode())
        print(f"[audio_qc] song bank for {series}: {len(merged)} words promoted from "
              f"{sum(1 for e in cands.values() if len(e) >= 2)} of {len(cands)} candidate "
              f"lines", flush=True)
    except Exception:  # noqa: BLE001
        pass


def _tag_song_reprise(errors: list[dict], bank: set | None = None) -> int:
    """A missing line whose words also appear INSIDE a detected block is the same song.

    POA EP10 flagged three lines at 14:31-14:56 that a listener confirmed absent from the
    dub — but the identical lyrics recur in the closing-song block at 38:16-38:36, so it is
    the song playing again mid-episode, not dropped dialogue. Grouping alone could not catch
    them: they sit ~16 s apart (over the block window) and number only three (under its
    minimum). Text is the giveaway, because spoken dialogue does not repeat verbatim.

    Tags only — the line stays in the report, the workbook and the markers, exactly like a
    block member. It is excluded from the headline finding count and labelled so a reviewer
    sees at a glance that the same words sing elsewhere in the episode.
    """
    blocked = [e for e in errors if e.get("type") == "MISSING" and e.get("block")]
    loose = [e for e in errors if e.get("type") == "MISSING" and not e.get("block")]
    if not loose or (not blocked and not bank):
        return 0
    lyr = [(e["block"]["id"], e.get("script_start_s"), _norm_words(e.get("text")))
           for e in blocked]
    if bank:                       # the series' known lyrics, learned from earlier episodes
        lyr.append((0, None, set(bank)))
    n = 0
    for e in loose:
        w = _norm_words(e.get("text"))
        # too short/generic to judge on words alone
        if len(w) < 3 or len((e.get("text") or "").strip()) < 10:
            continue
        for bid, bt, bw in lyr:
            if len(bw) < 3:
                continue
            shared = w & bw
            # Denominator is THIS line's own word count, not min(). With min(), a
            # three-word lyric built from function words ('eu sei que') scored 1.00
            # against any longer line that merely contained those three words, so one
            # short lyric could demote a whole episode's real drops. The question that
            # matters is "how much of the FLAGGED line is lyric", which is len(w).
            if len(shared) >= 3 and len(shared) / len(w) >= 0.75:
                e["song_reprise"] = {"block": bid, "matches_at": bt,
                                     "source": "series bank" if bid == 0 else "this episode"}
                n += 1
                break

    # SONG NEIGHBOURHOOD. Word overlap can only judge a line with three-plus content words,
    # so the short lines of a theme ("Te deixar ir.", "Me sentir assim...") slip through and
    # arrive at the top of the queue as high-confidence drops — four of them did on EP11.
    # A song is contiguous in time: where two or more confirmed lyric lines bracket a
    # stretch, the untagged flags between them belong to the same performance. Requiring
    # two anchors, and only reaching inside the stretch they bound, keeps this from
    # swallowing ordinary dialogue that merely sits near a song.
    # Anchors use a WEAKER bar than demotion does. Two shared lyric words is not enough to
    # call a line a reprise, but it is enough to say "a song is happening around here" —
    # and a stretch bracketed by two such hints, each independently pointing at the same
    # known lyrics, is strong evidence even though neither hint is on its own.
    anchors = sorted(e["script_start_s"] for e in errors
                     if e.get("script_start_s") is not None
                     and (e.get("song_reprise")
                          or any(len(_norm_words(e.get("text")) & bw) >= 2
                                 for _, _, bw in lyr if len(bw) >= 3)))
    if len(anchors) >= 2:
        lo, hi = anchors[0] - _SONG_EDGE_S, anchors[-1] + _SONG_EDGE_S
        for e in loose:
            t = e.get("script_start_s")
            if e.get("song_reprise") or t is None or not (lo <= t <= hi):
                continue
            e["song_reprise"] = {"block": None, "matches_at": None,
                                 "source": "between confirmed lyric lines"}
            n += 1
    return n


def _group_missing(errors: list[dict]) -> int:
    """Tag contiguous runs of MISSING lines (dub quiet throughout) with a shared block id.
    Returns the number of blocks found. Mutates the error dicts; removes nothing."""
    miss = sorted((e for e in errors if e.get("type") == "MISSING"
                   and e.get("script_start_s") is not None),
                  key=lambda e: e["script_start_s"])
    if not miss:
        return 0
    runs, cur = [], [miss[0]]
    for e in miss[1:]:
        prev = cur[-1]
        prev_end = prev.get("script_end_s") or prev["script_start_s"]
        quiet = (e.get("slot_speech_cov") or 0.0) <= _BLOCK_MAX_SLOT_COV
        prev_quiet = (prev.get("slot_speech_cov") or 0.0) <= _BLOCK_MAX_SLOT_COV
        # ADJACENT ORIGINAL LINES, not merely nearby ones. The block's claim is "no dub
        # audio throughout this stretch"; chaining on the time gap alone let four
        # unrelated real drops at 100/109/118/127 s merge into one block even though the
        # lines at 104/113/122 s between them were dubbed correctly. That both stated a
        # falsehood and collapsed four findings into one. Consecutive script indices are
        # what the claim actually requires.
        i_prev, i_cur = prev.get("script_index"), e.get("script_index")
        adjacent = (i_prev is None or i_cur is None or i_cur == i_prev + 1)
        if (e["script_start_s"] - prev_end <= _BLOCK_GAP_S and quiet and prev_quiet
                and adjacent):
            cur.append(e)
        else:
            runs.append(cur)
            cur = [e]
    runs.append(cur)
    bid = 0
    for run in runs:
        if len(run) < _BLOCK_MIN:
            continue
        bid += 1
        b0 = run[0]["script_start_s"]
        b1 = run[-1].get("script_end_s") or run[-1]["script_start_s"]
        for i, e in enumerate(run):
            e["block"] = {"id": bid, "start": round(b0, 2), "end": round(b1, 2),
                          "n": len(run), "first": i == 0}
    return bid


# Scribe reports a genuine mean per-word logprob, so a low one means the ENGINE doubts its
# own words — the signature of words invented over laughter/noise. See _reliable.
_SCRIBE_MIN_ALP = -0.35
# A single-word reference line ("Sim.", "Tá.", "É!") gives the matcher almost nothing to match
# on, and 44% of POA's individual flags are exactly that. They are NOT suppressed (a real
# "Obrigada." can exist) — just marked so the report ranks them below findings worth acting on.
# The bar is ONE word, not two, because it must not demote a verified catch: EP12's real drop
# "Mentiroso total." is two words, and a 2-word rule would have buried it.
_FRAGMENT_MAX_WORDS = 1
# DURATION LOOKED like the sharpest discriminator and IS NOT — recorded so nobody retries it.
# Measured from the reports (not estimated): real drops run 0.42 s (Alguem paga ele), 0.93 s
# (Mentiroso total), 1.58 s (charlatao), 2.96 s (Damaris); false flags run 0.38 s (Ou entao) to
# 1.70 s (Pra soltar esse romano). The classes overlap almost entirely, so no threshold
# separates them — an 0.85 s bar demoted a VERIFIED drop, which the EP11 stem run then
# demonstrated in production. The rule was shipped on a validation that compared the threshold
# against durations invented for the fixture rather than measured, and reverted on real data.
_FRAGMENT_MIN_SECONDS = 0.0     # disabled: word count only


def _reliable(q: dict | None, text: str | None = None) -> bool:
    """Whisper's own confidence in a segment. A flag built on a garbled transcript is noise —
    EP43's fight scene produced ~20 false 'missing' from exactly that.

    avg_logprob is shared by every segment of one ~30 s decode window, so one noisy window
    poisons its good neighbours (this gated out a REAL missing line on EP42). A full sentence
    is its own evidence of real speech, so bad alp only disqualifies SHORT segments."""
    if not q or q.get("nsp", 1.0) >= 0.5:
        return False
    # compression_ratio is PER SEGMENT (unlike alp) — the standard whisper garble signal.
    # Repetitive junk compresses well; that's what multi-word fight-scene garble is.
    # ENGINE-AWARE threshold: Sarvam synthesizes nsp=0/alp=0, so cr is its ONLY active
    # check — and measured Sarvam garble sits at 1.85-2.03, under Whisper's 2.4 bar
    # (which never fired for Sarvam). 26 legit flagged lines across 4 runs all measure
    # cr <= 1.27, so 1.7 splits garble from real text with >0.4 clearance on both sides.
    # Whisper keeps 2.4 (its native signal, its standard threshold).
    if q.get("cr", 1.0) > (1.7 if q.get("engine") == "sarvam" else 2.4):
        return False
    words = len((text or "").split())
    # SCRIBE's alp is a REAL per-word mean logprob (Whisper's is shared across a whole ~30 s
    # decode window, which is why it only disqualifies short segments there). Measured on the
    # POA ear-checks: the one confirmed hallucination — laughter transcribed as "Se impostor"
    # — scored -0.578, while every verified REAL drop scored -0.031, -0.046 and -0.220 and a
    # correctly-present line scored -0.000. -0.35 sits in that gap with room on both sides and
    # removes ~10% of flags. Applied regardless of length: a confident engine saying it is
    # unsure is evidence, not something a word count should override.
    if q.get("engine") == "scribe" and q.get("alp", 0.0) < _SCRIBE_MIN_ALP:
        return False
    return q.get("alp", -9.0) > -1.2 or words >= 4


# THE JUDGE NEEDS A BUDGET. It is called once per missing candidate, serially, and retries
# a 429 three times at 10/20/30 s. Groq's quota is per-day, so once an account has burned
# its tokens EVERY remaining candidate costs a full minute of sleeping — a run that takes
# four minutes warm sat at 75% for eighteen (2026-08-10, EP11, with 11,962 tokens left on
# the key and an 11-hour reset). The judge is an optional refinement that fails open, so
# when it starts failing it must stand down and let the flags through rather than hold the
# whole episode hostage. Budget is per-run; compare() resets it.
_JUDGE_MAX_S = 240.0        # total wall-clock the judge may spend on one episode
_JUDGE_MAX_FAILS = 3        # consecutive API failures before standing down for the run
_judge_state: dict[str, Any] = {"spent": 0.0, "fails": 0, "off": False}


def _judge_reset() -> None:
    _judge_state.update(spent=0.0, fails=0, off=False)


def _judge(ref_text: str, ref_lang: str, dub_lines: list[str], dub_lang: str) -> str:
    """Last gate before a MISSING flag: a cheap LLM look at the actual text, doing exactly
    what a human reviewer does — 'is this original line even coherent dialogue, and does any
    nearby dub line say roughly the same thing?' Embedding scores can't reject fluent-looking
    ASR garble; a language model can. Fails OPEN (keeps the flag) — over-flagging is the
    acceptable error here. Returns 'missing' | 'present' | 'garble'."""
    import json as _json
    import time as _t

    import httpx
    akey = os.environ.get("ANTHROPIC_API_KEY", "")
    key = os.environ.get("GROQ_API_KEY", "")
    if not (akey or key) or _judge_state["off"]:
        return "missing"
    if _judge_state["spent"] >= _JUDGE_MAX_S:
        _judge_state["off"] = True
        print(f"[audio_qc] judge stood down after {_JUDGE_MAX_S:.0f}s — remaining "
              f"candidates keep their flags (fail open)", flush=True)
        return "missing"
    listing = "\n".join(f"{k + 1}. {t}" for k, t in enumerate(dub_lines)) or "(none)"
    prompt = (f"You are QC-checking a dubbed TV episode.\n"
              f"Original line ({ref_lang}): {ref_text}\n"
              f"Dub lines spoken near that same moment ({dub_lang}):\n{listing}\n\n"
              'Reply with STRICT JSON only: {"coherent": true|false, "conveyed": true|false}. '
              '"coherent" = the original line reads as a real piece of dialogue, not '
              'speech-recognition gibberish. "conveyed" = at least one dub line expresses '
              "roughly the same meaning (translations are loose; judge meaning, not words).")
    # PREFER THE PAID ACCOUNT. Groq's free tier is a per-DAY token quota, and one QC run
    # spends a judge call per candidate — a handful of episodes exhausts it, after which
    # the judge is dead for eleven hours and every flag it would have cleared ships as a
    # false positive. The studio already pays for Anthropic (the Teams agent uses the same
    # key), and at Haiku prices a judged episode costs on the order of two paise. Groq
    # stays as the fallback for the desktop app, where only that key may be configured.
    def _post():
        if akey:
            return httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": akey, "anthropic-version": "2023-06-01"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 64,
                      "temperature": 0,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=60)
        return httpx.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": "llama-3.3-70b-versatile", "temperature": 0,
                                "response_format": {"type": "json_object"},
                                "messages": [{"role": "user", "content": prompt}]},
                          timeout=60)

    def _content(r):
        j = r.json()
        if akey:                                   # Anthropic returns a content block list
            return "".join(b.get("text", "") for b in j.get("content", []))
        return j["choices"][0]["message"]["content"]

    _t0 = _t.time()
    try:
        for attempt in range(3):
            try:
                r = _post()
                if r.status_code == 429:
                    # Sleep only while there is budget left to sleep in — the quota is
                    # per-DAY, so on an exhausted key every candidate would otherwise
                    # burn the full 60 s ladder and the episode never finishes.
                    nap = min(10 * (attempt + 1),
                              max(0.0, _JUDGE_MAX_S - (_judge_state["spent"] + _t.time() - _t0)))
                    if nap <= 0:
                        break
                    _t.sleep(nap)
                    continue
                r.raise_for_status()
                txt = _content(r).strip()
                if not txt.startswith("{"):        # tolerate a ```json fence or a preamble
                    i, j = txt.find("{"), txt.rfind("}")
                    txt = txt[i:j + 1] if 0 <= i < j else txt
                v = _json.loads(txt)
                _judge_state["fails"] = 0
                if not v.get("coherent"):
                    return "garble"
                return "present" if v.get("conveyed") else "missing"
            except Exception:  # noqa: BLE001 — judge trouble must never suppress a real flag
                break
        _judge_state["fails"] += 1
        if _judge_state["fails"] >= _JUDGE_MAX_FAILS:
            _judge_state["off"] = True
            print(f"[audio_qc] judge stood down after {_JUDGE_MAX_FAILS} consecutive "
                  f"failures — remaining candidates keep their flags (fail open)", flush=True)
        return "missing"
    finally:
        _judge_state["spent"] += _t.time() - _t0


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

    _judge_reset()                      # the judge's time budget is per episode
    _cold = [p for p in ([original_path] + ([] if dub_is_clean else [dub_path]))
             if not (os.path.exists(p + ".voc16.npy") and os.path.exists(p + ".acc16.npy"))]
    if len(_cold) == 2:
        _say("separating original and dub concurrently (2 workers)")
        _warm_pair(_cold)
    if os.environ.get("AQC_NO_SEP", "").strip() == "1":
        # EXPERIMENT: skip Demucs on the reference and hand Scribe the raw mix. Separation is
        # 8-11 min of every run — the largest remaining cost once downloads are parallel — and
        # it is not obviously helping ASR: Scribe is trained on real-world audio, while Demucs
        # leaves artefacts that can be transcribed as words (POA EP21's laughter hallucination
        # is a candidate). Detection quality decides, not speed.
        _say("NO-SEP: using the original mix directly (Demucs skipped)")
        ov = _load16(original_path)
    else:
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
        _INDIC = ("hindi", "tamil", "telugu", "kannada", "bengali", "marathi",
                  "malayalam", "punjabi")
        osegs = dsegs = None
        if os.environ.get("AQC_SCRIBE_ONLY", "").strip() == "1":
            # ElevenLabs Scribe: one reading per side, both sides concurrently. No Indic
            # guard — it covers every pair we handle (including POA's pt→en, where Sarvam
            # cannot help and the Whisper union is the slow default). Language-pinned.
            _say("SCRIBE-ONLY: one ElevenLabs Scribe reading per side")
            import concurrent.futures as _cfs
            with _cfs.ThreadPoolExecutor(max_workers=2) as _tp:
                # The cache key must encode HOW the audio was prepared: a transcript of the
                # SEPARATED reference and one of the RAW mix are different readings of the
                # same file, and keying both on the file id silently served the wrong one
                # (it made the first no-sep A/B a no-op that looked like a perfect match).
                _osuf = (".raw.scribe.json"
                         if os.environ.get("AQC_NO_SEP", "").strip() == "1" else ".scribe.json")
                _fo = _tp.submit(transcribe_scribe, ov, original_path + _osuf,
                                 LANG1.get(original_lang.lower()))
                _fd = _tp.submit(transcribe_scribe, dv, dub_path + ".scribe.json",
                                 LANG1.get(dub_lang.lower()))
                osegs, dsegs = _fo.result(), _fd.result()
            if not osegs or not dsegs:
                _say("SCRIBE returned no transcript (key/credits/outage?) — "
                     "FALLING BACK to the default path")
                osegs = dsegs = None
        if (osegs is None and os.environ.get("AQC_SARVAM_ONLY", "").strip() == "1"
                and original_lang.lower() in _INDIC and dub_lang.lower() in _INDIC):
            # EXPERIMENT (user-requested): Sarvam alone, one deterministic reading per
            # side. No union (identical redraws), no Whisper self-diagnostics for the
            # gates, no cross-engine check — the ear-check verdict decides its fate.
            _say("SARVAM-ONLY: one deterministic Saaras reading per side")
            if os.environ.get("AQC_SARVAM_BATCH", "").strip() == "1":
                # Batch jobs are minutes of SERVER-side work reached through polling, so
                # the two sides' jobs run concurrently on Sarvam's machines — sequential
                # submission would serialize two waits for no reason. The sync path keeps
                # its sequential order: both sides at 8-way each is what previously
                # crawled into Sarvam's account-level rate limit.
                import concurrent.futures as _cfs
                with _cfs.ThreadPoolExecutor(max_workers=2) as _tp:
                    _fo = _tp.submit(transcribe_sarvam, ov, original_path + ".sarvam.json")
                    _fd = _tp.submit(transcribe_sarvam, dv, dub_path + ".sarvam.json")
                    osegs, dsegs = _fo.result(), _fd.result()
            else:
                osegs = transcribe_sarvam(ov, original_path + ".sarvam.json")
                dsegs = transcribe_sarvam(dv, dub_path + ".sarvam.json")
            if not osegs or not dsegs:
                # Dead key / exhausted credits / outage: Sarvam returns nothing rather than
                # erroring per window. An empty reading must NEVER ship as "no dialogue
                # found" — fall back to the Whisper path and say so loudly.
                _say("SARVAM returned no transcript (credits/key/outage?) — "
                     "FALLING BACK to Whisper double-pass")
                osegs = dsegs = None
        if osegs is None:
            # Both sides transcribe CONCURRENTLY — they are independent API work, and the dub
            # used to wait for the original's slowest request (including any 429 backoff).
            # The dub is double-passed for the same reason as the reference: a MISSING flag
            # asserts the dub never says the line, and every ear-disproved EP41/EP43 flag traced
            # to a present dub line that one nondeterministic pass failed to render.
            import concurrent.futures as _cfs
            _say("transcribing original + dub via Groq (concurrent)")
            # Sarvam can also carry the REFERENCE's second reading (AQC_SARVAM_REF=1) — Hindi is
            # its best language and the ref side is where hallucination-over-music noise is born.
            # Guarded to Indic originals: the EP42 validation clip has a JAPANESE reference,
            # which Sarvam does not speak.
            _sarvam_ref = (os.environ.get("AQC_SARVAM_REF", "").strip() == "1"
                           and original_lang.lower() in ("hindi", "tamil", "telugu", "kannada",
                                                         "bengali", "marathi", "malayalam",
                                                         "punjabi"))
            with _cfs.ThreadPoolExecutor(max_workers=3 if _sarvam_ref else 2) as _tp:
                if _sarvam_ref:
                    _fo = _tp.submit(transcribe_groq, ov,
                                     LANG1.get(original_lang.lower(), original_lang))
                    _fos = _tp.submit(transcribe_sarvam, ov, original_path + ".sarvam.json")
                else:
                    _fo = _tp.submit(transcribe_two_passes, ov,
                                     LANG1.get(original_lang.lower(), original_lang))
                if os.environ.get("AQC_SARVAM_DUB", "").strip() == "1":
                    # Sarvam carries the dub's second reading: an Indic-specialist engine on the
                    # side where OUR false flags are born (dub lines Whisper fails to render).
                    _fd = _tp.submit(transcribe_groq, dv, LANG1.get(dub_lang.lower(), dub_lang))
                    _fs = _tp.submit(transcribe_sarvam, dv, dub_path + ".sarvam.json")
                    _p1, _p2 = (_fo.result(), _fos.result()) if _sarvam_ref else _fo.result()
                    _q1, _q2 = _fd.result(), _fs.result()
                else:
                    _fd = _tp.submit(transcribe_two_passes, dv,
                                     LANG1.get(dub_lang.lower(), dub_lang))
                    _p1, _p2 = (_fo.result(), _fos.result()) if _sarvam_ref else _fo.result()
                    _q1, _q2 = _fd.result()
            osegs = _merge_passes(_p1, _p2)
            dsegs = _merge_passes(_q1, _q2)
            _say(f"ref double-pass: {len(_p1)}+{len(_p2)} → {len(osegs)} lines")
            _say(f"dub double-pass: {len(_q1)}+{len(_q2)} → {len(dsegs)} lines")
        oseg = [(s, e) for s, e, _, _ in osegs]
        dseg = [(s, e) for s, e, _, _ in dsegs]
        otext = [t for _, _, t, _ in osegs]
        dtext = [t for _, _, t, _ in dsegs]
        oqual = [q for _, _, _, q in osegs]
        dqual = [q for _, _, _, q in dsegs]
        _say(f"lines: original={len(oseg)} dub={len(dseg)}")
        try:
            _fill_ref = _speech_ref_db(dv, dseg)
        except Exception:  # noqa: BLE001
            _fill_ref = None
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
            slot_cov = None
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
                slot_cov = cov / dur
                # 0.4, down from 0.5: the one measured REAL drop (EP38 grunt) has ~0%
                # dub speech in its slot — a true drop is an acoustic hole — while 9 of
                # 26 ear-verified FALSE flags sat at 0.41-0.49 and survived the old bar
                # (incl. EP41 @916.6, slot 0.49, production 2026-08-04).
                if cov / dur >= 0.4:
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
            _err = _missing(i, s, e, dub_label, best_seg, txt, slot_cov=slot_cov)
            try:
                # descriptive extra — must NEVER be able to kill a detection run
                _f = _dub_fill(dv, s, e, max(-3.0, min(3.0, drift0)), _fill_ref, dwin)
            except Exception:  # noqa: BLE001
                _f = None
            if (len((txt or "").split()) <= _FRAGMENT_MAX_WORDS
                    or (_FRAGMENT_MIN_SECONDS > 0 and (e - s) < _FRAGMENT_MIN_SECONDS)):
                _err["fragment"] = True
            if _f:
                _err.update(_f)
            _say(f"  MISSING @{s:.1f}-{e:.1f}s best={best_seg:.2f}"
                 + (f" [dub: {_f['fill']}]" if _f else "") + f"  {txt!r}")
            errors.append(_err)
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
        if dqual is not None and not _reliable(dqual[j], dt):
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
    return _report(errors, oseg, dub_label, tol_s, n_unchecked=n_unchecked,
                   series=os.environ.get("AQC_SERIES") or None)


def _confidence(best: float, text: str | None, slot_cov: float | None = None) -> str:
    """How sure we are a flagged line is REALLY missing. Per the studio workflow the sound
    team quickly verifies each flag in Pro Tools, so over-flagging is fine — what they want
    is a triage order. Lower best-match = stronger evidence of absence; a full sentence is
    stronger evidence than a one-word grunt (grunts/shouts often go undubbed by design).
    HIGH additionally demands the dub audio be near-silent in the slot (<20% speech
    coverage): a "verify me first" label must be backed by an acoustic hole, not just a bad
    text match — an EP41 flag rated high despite dub speech at the slot was false by ear."""
    words = len((text or "").split())
    if best < 0.35 and words >= 3 and (slot_cov is None or slot_cov < 0.2):
        return "high"
    if best < 0.5:
        return "medium"
    return "low"


# --- confidence tiers ------------------------------------------------------------------
# Free parameters, named so they can be argued with. They are NOT fitted to the ear-verified
# set: there are 7 confirmed drops on POA, and a rule tuned on 7 points is a rule that has
# memorised 7 points. They are set from what each signal physically means, then checked
# against the whole corpus for distributional sanity (tools/tier_eval.py).
_HOLE_SLOT_COV = 0.05      # below this the dub simply is not speaking in the slot
_OCCUPIED_SLOT_COV = 0.25  # above this the dub IS speaking there — a weak absence claim
_HIGH_MIN_WORDS = 3        # one- and two-word lines are grunts/names as often as dialogue
_HIGH_MIN_SECONDS = 0.35   # shorter than this the slot itself is barely measurable
_STRONG_SEMANTIC = 0.35    # a genuinely poor text match, used only when acoustics are mute


def _tier_from_features(e: dict[str, Any]) -> str:
    """The confidence tier for one MISSING flag, from the features recorded on it.

    Deliberately ordered: CONTEXT first, then ACOUSTICS, then SEMANTICS.

    Context first, because a line can be perfectly absent from the dub and still not be a
    defect — POA's untranslated opening theme is absent by design, and a long un-dubbed
    stretch is one editorial fact rather than forty independent ones. Nothing may promote
    those, however strong the evidence of absence.

    Acoustics next, because 'the dub has no audio where this line belongs' is a measurement
    of the delivered file. The semantic score is a similarity between machine translations
    of machine transcripts of two languages, and when the two disagree the measurement is
    the better witness. On POA EP11 the old rule ranked 27 of 43 flags LOW while their dub
    slots were completely silent, and buried the one ear-verified real drop at LOW because
    a wrong dub line elsewhere scored 0.73 against it.

    Semantics last, and only to break ties or to speak when the acoustics cannot.
    """
    words = len((e.get("text") or "").split())
    blk = e.get("block") or {}
    if e.get("song_reprise") or e.get("fragment") or words <= 1:
        return "low"
    if (blk.get("n") or 0) >= 4:
        return "medium" if blk.get("first") else "low"

    sc = e.get("slot_speech_cov")
    fill = e.get("fill")
    best = e.get("coverage")
    try:
        dur = float(e.get("script_end_s", 0)) - float(e.get("script_start_s", 0))
    except (TypeError, ValueError):
        dur = 0.0

    # WHEN fill EXISTS IT OVERRULES slot_cov, because they measure different things.
    # slot_cov counts ALL dub speech overlapping the slot, and the slot gate upstream
    # deliberately tolerates up to 40% of it as bleed from the line next door. _dub_fill
    # then cuts those neighbour windows out and describes what is actually left. So a line
    # with slot_cov 0.36 and fill 'silence' is a hole with a neighbour clipping its edge —
    # reading 0.36 as "the dub speaks here" demoted three ear-verified real drops
    # (EP11 @427.0 and @1863.7, EP25 @767.7, all fill=silence at slot 0.30-0.36).
    # slot_cov is only consulted when _dub_fill could not measure at all.
    if fill:
        occupied = fill == "speech-like"
        hole = fill == "silence"
        quiet = fill == "ambience"
    else:
        occupied = sc is not None and sc >= _OCCUPIED_SLOT_COV
        hole = sc is not None and sc < _HOLE_SLOT_COV
        quiet = False

    if occupied:                      # the dub speaks under the line — weakest case there is
        return "low"
    if hole and words >= _HIGH_MIN_WORDS and dur >= _HIGH_MIN_SECONDS:
        return "high"
    if hole or quiet:
        return "medium"
    if best is not None and best < _STRONG_SEMANTIC:
        return "medium"
    return "low"


def _retier(errors: list[dict[str, Any]]) -> None:
    """Re-rank every MISSING flag once the whole picture exists.

    _confidence() runs inside _missing() at detection time, so it cannot see the three
    signals that matter most: the dub fill (attached immediately afterwards), block
    membership and song-reprise tags (both computed here in _report). This pass runs after
    all of them, which is the only point at which a tier can be honest.
    """
    for e in errors:
        if e.get("type") == "MISSING":
            e["confidence"] = _tier_from_features(e)


def _missing(i: int, s: float, e: float, ch: str, best: float,
             text: str | None = None, slot_cov: float | None = None) -> dict[str, Any]:
    return {
        "type": "MISSING", "subtype": None, "severity": "error",
        "character": None, "channel": ch, "script_index": i,
        "script_start_s": round(s, 3), "script_end_s": round(e, 3),
        "audio_start_s": None, "audio_end_s": None,
        "drift_s": None, "coverage": round(best, 3), "text": text,
        "slot_speech_cov": round(slot_cov, 3) if slot_cov is not None else None,
        "confidence": _confidence(best, text, slot_cov),
        "message": (f"The original speaks at {s:.2f}–{e:.2f}s"
                    + (f" ({text!r})" if text else "")
                    + f" and the dub has nothing saying the same thing (best match "
                      f"{best:.0%}, confidence: {_confidence(best, text, slot_cov)}) — dropped, "
                      f"or moved by more than a few seconds."),
    }


def _report(errors: list[dict[str, Any]], oseg: list, ch: str, tol_s: float,
            n_unchecked: int = 0, series: str | None = None) -> dict[str, Any]:
    try:
        n_blocks = _group_missing(errors)      # presentation only; never fatal
    except Exception:  # noqa: BLE001
        n_blocks = 0
    try:
        n_reprise = _tag_song_reprise(errors, _song_bank(series))
    except Exception:  # noqa: BLE001
        n_reprise = 0
    try:
        _save_song_bank(series, errors)        # teach the bank for the next episode
    except Exception:  # noqa: BLE001
        pass
    try:
        # LAST, once block + song context exists: re-rank on the full picture.
        _retier(errors)
    except Exception:  # noqa: BLE001 — a ranking failure must not lose the findings
        pass
    n = {t: sum(1 for e in errors if e["type"] == t) for t in ("MISSING", "MISALIGNED", "EXTRA")}
    conf = {c: sum(1 for e in errors if e["type"] == "MISSING" and e.get("confidence") == c)
            for c in ("high", "medium", "low")}
    # FINDINGS vs LINES. 18 of POA EP12's 33 missing lines were one untranslated song, i.e.
    # ONE thing to act on. A grouped block counts once here; the individual lines are all
    # still in `errors`, so nothing is hidden and recall is untouched.
    grouped_lines = sum(1 for e in errors if e.get("block"))
    reprise_lines = sum(1 for e in errors if e.get("song_reprise") and not e.get("block"))
    frag_lines = sum(1 for e in errors if e.get("fragment") and not e.get("block")
                     and not e.get("song_reprise"))
    n_findings = max(n_blocks,
                     n["MISSING"] - grouped_lines - reprise_lines - frag_lines + n_blocks)
    fills = {}
    for e in errors:
        if e.get("type") == "MISSING" and e.get("fill"):
            fills[e["fill"]] = fills.get(e["fill"], 0) + 1
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
            # triage order for the sound team: verify high first, low last
            "n_missing_by_confidence": conf,
            # one contiguous un-dubbed stretch = one finding, however many lines it spans
            "n_missing_findings": n_findings,
            "n_missing_blocks": n_blocks,
            # lines that are the same song sung elsewhere in the episode, not dialogue
            "n_song_reprise": n_reprise,
            # one/two-word reference lines — reported, but ranked below real findings
            "n_fragments": frag_lines,
            # what the dub does under a missing line: silence | ambience | speech-like
            "missing_by_dub_fill": fills,
        },
    }


def _empty(why: str, tol_s: float, ch: str) -> dict[str, Any]:
    r = _report([], [], ch, tol_s)
    r["why"] = why
    return r


def _spawn_draw(o: str, d: str, ol: str, dl: str, clean: bool,
                model: str | None = None) -> dict:
    """Entry point for a spawned draw process. Lives HERE (an importable module) because
    multiprocessing 'spawn' re-imports the worker's module — and the runner script executes
    its whole pipeline at import, which is exactly what killed the first concurrent flight."""
    if model:
        # Per-draw model choice. Free-tier Groq rate ceilings are PER MODEL, so turbo draws
        # ride a separate pool from the large-v3 anchor draw — real parallelism without a
        # paid tier, and turbo is 2.8x cheaper if a paid tier ever arrives.
        os.environ["AQC_ASR_MODEL"] = model
    return compare(o, d, original_lang=ol, dub_lang=dl, dub_label="dub", dub_is_clean=clean)
