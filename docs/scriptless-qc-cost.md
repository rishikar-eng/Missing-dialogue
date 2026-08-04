# Scriptless (Audio-Only) QC — Cost per Episode

Cost to QC **one ~25-minute episode** against its original, per delivered dub language and
for a full 6-language episode. Compute runs on AWS Fargate (Mumbai); ASR usage billed by the
providers. Figures are estimates from measured run times (₹ at ~₹96/USD).

## Per language

| Cost item | Sarvam-only (fast mode) | Hybrid (deep mode: 5-draw union, both engines) |
|---|---|---|
| Fargate compute (16 vCPU task) | ~10–18 min → **₹25–35** | ~20–25 min → **₹40–55** |
| Groq Whisper | — | ₹0 (free tier) |
| Sarvam Saaras (~30 min speech across both sides @ ₹30/hr) | **~₹15** | ~₹15 |
| S3 / data transfer / ECR | < ₹2 | < ₹2 |
| **Total per language** | **~₹40–50** | **~₹60–70** |

## Full episode, all 6 languages

The original's separation and its Sarvam reading are computed once and shared; each
additional language pays only its own dub-side work.

| | Sarvam-only | Hybrid |
|---|---|---|
| 6 languages, concurrent (96-vCPU quota) | **~₹220–280 (≈ $2.5–3)** | **~₹330–400 (≈ $3.5–4.2)** |
| Wall-clock | ~15–20 min | ~35–50 min |

## Notes

- **Caching cuts repeat costs sharply**: re-running an unchanged episode, or adding a language
  later, skips separation and the original's transcription (roughly **half** the per-language cost).
- A **movie (~2.5 h)** scales ≈ 6× an episode: ~₹250–400 for all languages in fast mode.
- Sarvam's ₹1,000 signup credit covers roughly **60–70 single-language episode checks** before
  paid usage starts. Groq remains free-tier at current volumes.
- Fixed monthly infrastructure (the always-on dispatcher server) is separate from and
  unaffected by per-run volume.
