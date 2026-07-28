"""Order-aware, one-to-one sequence alignment for dubbing QC.

Why not nearest-neighbour: matching each reference line to its best-scoring dub line
independently is too permissive — several ref lines can claim the SAME dub line, so a
genuinely dropped line borrows its neighbour's match and is never reported (a FALSE
NEGATIVE, the worst error for QC). That is exactly what let a silenced line slip through.

A dub preserves the ORDER of the dialogue, so this is a global sequence alignment (the
Needleman-Wunsch/bitext-alignment shape), not a lookup:
  * every dub segment is consumed at most once      -> one-to-one
  * alignment is monotonic                           -> order-aware
  * a ref line aligned to a GAP is genuinely MISSING -> real recall
  * a dub line aligned to a GAP is EXTRA
Merges are supported (one ref line may map to two adjacent dub lines, and vice versa),
since ASR/VAD segment the two sides differently.

Scores are cosine similarities in [-1, 1]; `gap` is the cost of leaving a line unmatched.
A pair only counts as a match if it also beats `min_sim`.
"""
from __future__ import annotations

import numpy as np

import os
_ALLOW_2TO1 = os.environ.get("DQC_ALLOW_2TO1") == "1"   # see the mv==4 branch below

GAP = 0.35          # leaving a line unmatched costs this much "similarity"
MIN_SIM = 0.50      # a pair below this is never treated as a match
TIME_BAND = 25.0    # s — a match may not shift more than this (guards wild long-range pairings)


def align(sim: np.ndarray, ref_t: list[tuple[float, float]], dub_t: list[tuple[float, float]],
          gap: float = GAP, min_sim: float = MIN_SIM, band: float = TIME_BAND,
          allow_merge: bool = True):
    """Globally align two timed line sequences by similarity.

    sim: [n_ref, n_dub] cosine similarities. ref_t/dub_t: (start, end) per line.
    Returns (pairs, missing, extra):
      pairs   [(i, j, score)]  ref i matched dub j   (merges appear as repeated i or j)
      missing [i]              ref lines matched to nothing  -> MISSING
      extra   [j]              dub lines matched to nothing  -> EXTRA
    """
    n, m = sim.shape
    NEG = -1e9
    # ADAPTIVE thresholds: same-language pairs score ~1.0 while cross-language ones sit
    # around 0.4-0.7, so one fixed cutoff either misses real drops or flags good matches.
    # Calibrate off the data: the typical best-match score sets the scale.
    if n and m:
        level = float(np.median(sim.max(axis=1)))
        min_sim = max(0.30, min(min_sim, 0.62 * level))
        gap = max(0.20, 0.55 * level)      # unmatched must cost near a real match's worth


    def s(i, j):
        """Pair score, disqualified if too weak or too far apart in time."""
        if abs(ref_t[i][0] - dub_t[j][0]) > band:
            return NEG
        v = float(sim[i, j])
        return v if v >= min_sim else NEG

    # dp[i][j] = best score aligning ref[:i] with dub[:j]
    dp = np.full((n + 1, m + 1), NEG, dtype=np.float64)
    bt = np.zeros((n + 1, m + 1), dtype=np.int8)      # 0=diag 1=up(ref gap) 2=left(dub gap)
                                                      # 3=merge 2 dub  4=merge 2 ref
    dp[0, 0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            best, mv = NEG, 1
            if i > 0 and j > 0:                        # 1:1 match
                c = s(i - 1, j - 1)
                if c > NEG and dp[i - 1, j - 1] + c > best:
                    best, mv = dp[i - 1, j - 1] + c, 0
            if allow_merge and i > 0 and j > 1:        # one ref line covers two dub lines
                c = max(s(i - 1, j - 2), s(i - 1, j - 1))
                if c > NEG and dp[i - 1, j - 2] + c - 0.05 > best:
                    best, mv = dp[i - 1, j - 2] + c - 0.05, 3
            # Two ref lines covered by ONE dub line. This is the move that quietly restores
            # many-to-one matching: without a physical check, a dropped line just shares its
            # neighbour's dub segment and is never reported. A single dub segment can only
            # really hold two lines if it is about as long as both of them together.
            # DISABLED BY DEFAULT (set DQC_ALLOW_2TO1=1 to re-enable). Collapsing two ref
            # lines onto one dub segment is precisely the shape a DROPPED line takes, so
            # this move trades a missed drop (unacceptable in QC) for a tidier alignment.
            # A duration guard was not enough: same-language embeddings score ~1.0 broadly,
            # so both halves still cleared the bar. Erring toward a false positive is right.
            if _ALLOW_2TO1 and allow_merge and i > 1 and j > 0:
                span = (max(ref_t[i - 1][1], ref_t[i - 2][1])
                        - min(ref_t[i - 1][0], ref_t[i - 2][0]))
                dur = dub_t[j - 1][1] - dub_t[j - 1][0]
                if span > 0 and dur >= 0.7 * span:
                    c = min(s(i - 2, j - 1), s(i - 1, j - 1))   # BOTH halves must match
                    if c > NEG and dp[i - 2, j - 1] + c - 0.05 > best:
                        best, mv = dp[i - 2, j - 1] + c - 0.05, 4
            if i > 0 and dp[i - 1, j] - gap > best:    # ref line unmatched (candidate MISSING)
                best, mv = dp[i - 1, j] - gap, 1
            if j > 0 and dp[i, j - 1] - gap > best:    # dub line unmatched (EXTRA)
                best, mv = dp[i, j - 1] - gap, 2
            dp[i, j], bt[i, j] = best, mv

    pairs, missing, extra = [], [], []
    i, j = n, m
    while i > 0 or j > 0:
        mv = bt[i, j]
        if i > 0 and j > 0 and mv == 0:
            pairs.append((i - 1, j - 1, float(sim[i - 1, j - 1]))); i, j = i - 1, j - 1
        elif mv == 3 and i > 0 and j > 1:
            # one ref line spans two dub lines: keep only the halves that really match, so a
            # merge can never smuggle a sub-threshold pair in and hide a dropped line.
            kept = [(i - 1, jj, float(sim[i - 1, jj])) for jj in (j - 1, j - 2)
                    if s(i - 1, jj) > NEG]
            pairs += kept or []
            if not kept:
                missing.append(i - 1)
            i, j = i - 1, j - 2
        elif mv == 4 and i > 1 and j > 0:
            kept = [(ii, j - 1, float(sim[ii, j - 1])) for ii in (i - 1, i - 2)
                    if s(ii, j - 1) > NEG]
            pairs += kept
            missing += [ii for ii in (i - 1, i - 2) if s(ii, j - 1) <= NEG]
            i, j = i - 2, j - 1
        elif mv == 1 and i > 0:
            missing.append(i - 1); i -= 1
        else:
            extra.append(j - 1); j -= 1
    return pairs[::-1], missing[::-1], extra[::-1]
