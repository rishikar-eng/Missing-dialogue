"""Speech detection via Silero VAD (ONNX) — detects *human speech* specifically
and ignores background music / sound-effects (which energy thresholding can't).

Public surface is unchanged: ``detect_speech_regions(wav_path) -> [{start, end}]``
in seconds, so alignment/characters don't need to know which detector is used.

Tuned to NOT drop short utterances (a brief line between two long ones must still
be caught), per the QC requirements.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
import soundfile as sf

_TARGET_SR = 16000
_WINDOW = 512  # samples per inference step at 16 kHz (Silero v5 requirement)

_MODEL_PATH = Path(__file__).resolve().parent / "models" / "silero_vad.onnx"
_session: ort.InferenceSession | None = None
_session_lock = threading.Lock()


def _get_session() -> ort.InferenceSession:
    """The shared Silero session. ONNX Runtime sessions are thread-safe for
    concurrent .run() calls (each carries its own state tensors), so analyze can
    VAD several tracks in parallel; the lock only guards lazy creation."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                if not _MODEL_PATH.exists():
                    raise FileNotFoundError(f"Silero VAD model missing at {_MODEL_PATH}")
                opts = ort.SessionOptions()
                opts.inter_op_num_threads = 1
                opts.intra_op_num_threads = 1
                _session = ort.InferenceSession(
                    str(_MODEL_PATH), sess_options=opts, providers=["CPUExecutionProvider"]
                )
    return _session


def load_mono_native(wav_path: Path) -> tuple[np.ndarray, int]:
    """Mono float32 at the file's NATIVE sample rate + that rate. Use this when
    the caller needs true sample peaks (clipping detection) — resampling to 16k
    under-reads peaks. Feed the same array to ``resample_16k`` for VAD."""
    data, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data, sr


def resample_16k(data: np.ndarray, sr: int) -> np.ndarray:
    """Resample a mono signal to the 16 kHz VAD rate (linear interp)."""
    if sr != _TARGET_SR and len(data) > 1:
        n_new = int(round(len(data) * _TARGET_SR / sr))
        data = np.interp(
            np.linspace(0.0, len(data) - 1, n_new), np.arange(len(data)), data
        ).astype(np.float32)
    return np.ascontiguousarray(data, dtype=np.float32)


_CONTEXT = 64  # Silero v5 prepends the previous window's last 64 samples to each 512.


def _speech_probs(audio: np.ndarray) -> np.ndarray:
    """Per-window (512-sample) speech probability across the whole clip.

    Each model input is [64 context samples | 512 new samples] = 576, with the
    context carried over from the prior window (this is required by Silero v5 —
    feeding a bare 512-sample window makes it output ~0 for everything).
    """
    session = _get_session()
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr = np.array(_TARGET_SR, dtype=np.int64)
    context = np.zeros((1, _CONTEXT), dtype=np.float32)
    n_windows = len(audio) // _WINDOW
    probs = np.empty(n_windows, dtype=np.float32)
    run = session.run
    for i in range(n_windows):
        chunk = audio[i * _WINDOW : (i + 1) * _WINDOW].reshape(1, -1)
        x = np.concatenate([context, chunk], axis=1)  # (1, 576)
        out, state = run(["output", "stateN"], {"input": x, "state": state, "sr": sr})
        probs[i] = out[0, 0]
        context = x[:, -_CONTEXT:]
    return probs


def load_mono_16k(wav_path: Path) -> np.ndarray:
    """Public loader: mono, 16 kHz float32 — the exact signal VAD analyses."""
    return resample_16k(*load_mono_native(wav_path))


def detect_speech_regions(
    wav_path: Path,
    threshold: float = 0.5,
    min_speech_ms: int = 90,      # keep short interjections (don't omit small lines)
    min_silence_ms: int = 120,
    speech_pad_ms: int = 120,
    audio: np.ndarray | None = None,
    **_ignored: Any,
) -> list[dict[str, float]]:
    """Return voiced regions [{"start": s, "end": s}] (seconds) using Silero VAD.

    Pass a preloaded ``audio`` (mono 16 kHz, from ``load_mono_16k``) to skip the
    file read — lets a caller compute VAD and loudness from a single load.
    Extra kwargs are accepted and ignored so existing energy-VAD call sites keep
    working unchanged.
    """
    if audio is None:
        audio = load_mono_16k(wav_path)
    if len(audio) < _WINDOW:
        return []

    probs = _speech_probs(audio)
    neg_threshold = max(0.15, threshold - 0.15)
    min_speech = min_speech_ms / 1000 * _TARGET_SR
    min_silence = min_silence_ms / 1000 * _TARGET_SR
    pad = int(speech_pad_ms / 1000 * _TARGET_SR)
    audio_len = len(audio)

    regions: list[list[int]] = []
    triggered = False
    start = 0
    temp_end = 0
    for i, p in enumerate(probs):
        sample = i * _WINDOW
        if p >= threshold:
            temp_end = 0
            if not triggered:
                triggered = True
                start = sample
        elif p < neg_threshold and triggered:
            if temp_end == 0:
                temp_end = sample
            if sample - temp_end >= min_silence:
                if temp_end - start >= min_speech:
                    regions.append([start, temp_end])
                triggered = False
                temp_end = 0
    if triggered and audio_len - start >= min_speech:
        regions.append([start, audio_len])

    # Pad each region, clamp to the clip, and merge any that now overlap.
    out: list[dict[str, float]] = []
    for s, e in regions:
        s_pad = max(0, s - pad)
        e_pad = min(audio_len, e + pad)
        if out and s_pad <= out[-1]["_e"]:
            out[-1]["_e"] = e_pad
        else:
            out.append({"_s": s_pad, "_e": e_pad})
    return [
        {"start": round(r["_s"] / _TARGET_SR, 3), "end": round(r["_e"] / _TARGET_SR, 3)}
        for r in out
    ]
