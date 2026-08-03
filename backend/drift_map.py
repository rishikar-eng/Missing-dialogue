"""Per-region time offset between two mixes, from envelope cross-correlation.

A dub mix does not hold one constant offset against its original: recaps, eyecatches and ad
bumpers land at different places, so EP38's true offset swings from ~+1 s to ~+17 s within one
episode. The pipeline used a single global median (clamped to ±3 s), which put the acoustic
gates 14+ s away from the audio they were supposed to inspect. A previous fix attempt used the
median of the few TEXT-alignment anchors near each candidate and regressed badly (EP39 5→16
flags) — a median over ~3 noisy anchors is far less robust than the global one.

This map instead correlates the two mixes' loudness envelopes directly: for each 25 s window
of the original, find the shift of the dub whose envelope matches best within ±40 s. It is
AUDIO evidence, dense everywhere the episode has structure (speech or effects), and each
estimate carries its own confidence: the correlation peak and its margin over the runner-up.
Validated on the real EP38 ST_MIX pair — recovered the ear-checked per-window drift of
1.06–16.9 s to ±0.02 s, while a cross-episode control pair scores peak 0.36 / margin 0.03,
so low-confidence windows are cleanly identifiable and fall back to the global median.
"""
from __future__ import annotations

import numpy as np

FPS = 100                 # envelope frames per second (10 ms)
WIN_S = 25.0              # correlation window
HOP_S = 5.0               # window hop
MAX_SHIFT_S = 40.0        # search range either way
MIN_PEAK = 0.55           # below: the window has no usable structure
MIN_MARGIN = 0.10         # peak must beat the runner-up by this much


def envelope(audio16: np.ndarray) -> np.ndarray:
    """100 Hz log-energy envelope, mean-removed so correlation ignores level differences."""
    n = (len(audio16) // 160) * 160
    if n == 0:
        return np.zeros(0, dtype="float32")
    frames = audio16[:n].reshape(-1, 160).astype("float64")
    e = np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-8)
    return (e - e.mean()).astype("float32")


def _xcorr_peak(a: np.ndarray, eb: np.ndarray, pos: int) -> tuple[float, float, float]:
    """Best shift (seconds) of the dub envelope `eb` against window `a` taken at frame `pos`
    of the original, plus the peak correlation and its margin over the best peak ≥1 s away."""
    a = a - a.mean()
    na = float(np.linalg.norm(a)) or 1.0
    shifts = np.arange(-int(MAX_SHIFT_S * FPS), int(MAX_SHIFT_S * FPS) + 1)
    scores = np.full(len(shifts), -1.0, dtype="float64")
    for k, s in enumerate(shifts):
        start = pos + s
        if start < 0 or start + len(a) > len(eb):
            continue
        seg = eb[start:start + len(a)]
        seg = seg - seg.mean()
        nb = float(np.linalg.norm(seg))
        if nb < 1e-6:
            continue
        scores[k] = float(np.dot(a, seg)) / (na * nb)
    best = int(np.argmax(scores))
    peak = float(scores[best])
    far = np.abs(shifts - shifts[best]) >= FPS          # ≥1 s away from the winner
    margin = peak - (float(scores[far].max()) if far.any() else 0.0)
    return shifts[best] / FPS, peak, margin


class DriftMap:
    def __init__(self, points: list[tuple[float, float, float, float]], fallback: float):
        # points: (center_t, drift_s, peak, margin) — confident windows only
        self.points = points
        self.fallback = fallback

    def at(self, t: float) -> float:
        """Drift at time t: the nearest confident window's estimate, else the fallback."""
        if not self.points:
            return self.fallback
        best = min(self.points, key=lambda p: abs(p[0] - t))
        return best[1] if abs(best[0] - t) <= 60.0 else self.fallback

    def confident_near(self, t: float) -> bool:
        return any(abs(p[0] - t) <= 60.0 for p in self.points)

    def summary(self) -> str:
        if not self.points:
            return f"no confident windows; global fallback {self.fallback:+.1f}s everywhere"
        d = sorted(p[1] for p in self.points)
        return (f"{len(self.points)} confident windows, drift {d[0]:+.1f}..{d[-1]:+.1f}s "
                f"(median {d[len(d) // 2]:+.1f}s), fallback {self.fallback:+.1f}s")


def build(orig16: np.ndarray, dub16: np.ndarray, fallback: float = 0.0) -> DriftMap:
    """Map drift(t) across the episode from the two RAW mixes' envelopes."""
    ea, eb = envelope(orig16), envelope(dub16)
    win, hop = int(WIN_S * FPS), int(HOP_S * FPS)
    pts: list[tuple[float, float, float, float]] = []
    pos = 0
    while pos + win <= len(ea):
        a = ea[pos:pos + win]
        if float(a.std()) >= 0.05:                   # a silent window has nothing to match on
            d, peak, margin = _xcorr_peak(a, eb, pos)
            if peak >= MIN_PEAK and margin >= MIN_MARGIN:
                pts.append((round((pos + win / 2) / FPS, 2), round(d, 2), peak, margin))
        pos += hop
    if pts:
        med = sorted(p[1] for p in pts)[len(pts) // 2]
    else:
        med = fallback
    return DriftMap(pts, med)
