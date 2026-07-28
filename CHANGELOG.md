# Changelog

Notable changes to **Dialogue QC** — the desktop app, the hosted pipeline, and the Teams chat
agent. Newest first. Dates are the date the work landed on a branch.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project has no
released version numbers yet; entries are grouped by the change set that introduced them.

---

## [Unreleased] — Teams agent: informative replies · 2026-07-28

Branch `feat/teams-agent-informative-replies`.

The Teams agent reported two numbers per language (missing, wrong-speaker) out of the ten-odd
findings the engine already computes, and a run finished in silence because Teams Outgoing
Webhooks cannot push. This change set makes the reply say what to **do**, and say it unprompted.

### Added
- **Cross-language root cause in chat** — whether a gap repeats across every language (points at
  the script or the character mapping) or shows up in one language only (points at that dub
  vendor). Extracted verbatim from `excel_report._summary` into `backend/xlang.py`, which the
  workbook now calls too, so the two cannot disagree.
- **Proactive completion messages** (`backend/notify.py`) — a daemon thread announces a finished
  run in the channel through a Teams **Incoming Webhook**, so nobody has to poll `status`.
  Configure with `DQC_TEAMS_INCOMING` (or `DQC_TEAMS_INCOMING_MAP` for several channels);
  unset, the watcher never starts and polling behaves exactly as before.
- **Async natural-language answers** — an unrecognised message gets an instant acknowledgement,
  then Claude Haiku answers on a background thread using four read-only tools over the run's
  *full* results (`backend/results.py`: `run_overview`, `list_findings`, `character_report`,
  `cross_language_check`) and pushes the answer. This is what makes "why is Shoma missing in
  Tamil but fine in Malayalam?" answerable without touching the 5 s webhook window.
- **Adaptive Card for `status`** — a per-language table, colour by severity, and a collapsed
  "🔎 Missing-line examples" drill-down with real timecodes and script lines. The drill-down is
  embedded in the card payload, so expanding it costs no server round-trip.
- **`help` and `runs` commands** — what the bot understands, and the recent runs in this channel
  (so an earlier episode's report link is findable without scrolling).
- **Example missing lines, per language** — timecode + character + the scripted line, carried in
  the run summary so chat can show them without opening the workbook.
- `run_store.recent_for()`, `run_store.unnotified()`.

### Changed
- **`episode_runner._lang_summary()` is now the single place** a language's analysis is reduced
  to the digest every consumer sees (S3 `status.json`, the run store, the LLM worker). Widening
  it once carried undelivered characters, misaligned counts, out-of-sync tracks, unparsed script
  rows, mapping/loudness flags and example lines to all of them at once. `missing`/`mismatch`/
  `extra` keep their names and meaning; every new key must be read with a default, because
  records written by older builds don't have them.
- **Verdict-first replies** on one severity scale shared by the roll-up and the per-language rows
  (🔴 a whole character undelivered · 🟠 lines missing or mistimed · 🟢 clean). Languages sort
  worst-first instead of alphabetically.
- **`_run_status()` returns data, not a formatted message**, so the polled reply and the pushed
  completion message are produced by the same code and cannot drift apart.
- **Plain-English failures** (`_friendly_error`) — known failure modes (no script in Box, expired
  Box token, interrupted download, ECS capacity, out of memory) map to a sentence and a next
  step instead of truncated exception text.
- **`fargate.ensure_summary()` returns `{key, headline}`** and caches the chat-sized root-cause
  headline in S3 beside the Summary workbook, so `status` doesn't re-read every language's
  results on each call.
- The `run` confirmation only promises a completion message when a push channel is actually
  configured.

### Fixed
- **A green header over failed languages.** With 4 languages done and 2 errored, `status` printed
  `✅ EP 42 QC done (4 languages)` — `running == 0` was the only test. Failures now carry their
  own tally and their own header.
- **Launch failures vanished from the tally.** `running += 0` meant a language whose ECS task
  never started was counted as neither running nor failed, so a single launch failure could flip
  the header to "done".
- **An overstated root cause on a partial run.** The reading is built only from languages that
  reported in; when coverage is incomplete the heading now says so ("across the 3 of 6 languages
  that completed") rather than claiming "absent in EVERY language" and sending the studio to the
  script for what is really a missing result.

### Performance
- `fargate.status_parallel()` was 12 serial AWS round-trips for six languages, inside a 5 s
  budget. Now one batched `describe_tasks` (it accepts up to 100 ARNs) plus concurrent S3 reads.
  Measured replies: `status` 5 ms, `runs` 24 ms, ask-acknowledgement 5 ms (AWS stubbed).

### Security
- `.gitignore`: added `*.env`. The existing `.env` / `.env.*` patterns did not match
  `server-keys.env`, so a `git add -A` would have committed a live API key to this **public**
  repository's permanent history. All three patterns are needed — `.env*` misses
  `server-keys.env`, `*.env` misses `.env.local`.

### Documentation
- `docs/teams-agent-handout.md` rewritten around the current design: where a number in a reply
  comes from, the 5 s constraint and its escape hatch, Incoming Webhook setup, and a gap list
  split into closed and still-open.

### Verification
Behaviour of the `xlang.py` extraction was checked row-for-row against the previous
implementation across 8 episode shapes (absent everywhere, absent in one language only,
group-bundled bit-parts, zero-line characters, single language), and the Summary workbook
rebuilt to confirm the sheet is unchanged. The Teams surface was driven over the real
HMAC-signed webhook against stubbed run records covering local, cloud-single and cloud-fan-out
runs in every terminal state, plus a live end-to-end Anthropic call for the async path. Not
verified locally, for want of credentials: the live Box `check` path and a real Fargate run.

---

## 2026-07-24 — Cloud compute, Box-driven QC, voice-ID checking

### Added
- **AWS Fargate compute** — heavy QC runs dispatch to on-demand Fargate tasks
  (`DQC_COMPUTE=fargate`) instead of running in the web process, with the EC2 box staying the
  always-on brain. `backend/job_entry.py` is the one-shot task; results land in S3.
- **Per-language fan-out** — one 2-vCPU task per language, so an episode finishes in roughly the
  slowest single language's time rather than the sum. `status` aggregates the per-language
  outcomes with short signed download links.
- **Cross-language Summary aggregator** — each language task uploads its result JSON; when all
  finish, the side-by-side + `CROSS-LANGUAGE CHECK` sheet is built once, cached in S3 and linked.
- **Voice-ID check in the QC workbook** — validates each delivered track's ElevenLabs voice id
  against the studio's master sheet, fetched live from Box (etag-cached, committed
  `voice_bank.json` as fallback). Distinguishes *not in list* / *no voice id* / *duplicate id* /
  *verify match* / *generic bit-part*.
- **Fast LLM-free Teams path** — `check` / `run` / `status` parsed deterministically to fit the
  5 s Outgoing-Webhook window; the full LLM agent stays on `/api/agent/chat`.
- **Run persistence** (`backend/run_store.py`) so `status` and download links survive a restart.
- `Dockerfile`, `requirements-server.txt`, `docs/fargate-plan.md`.

### Fixed
- The card's **Run QC** button now uses the same launcher as the typed `run`, recording the run
  under the Teams conversation — fixes "No QC run here yet" straight after clicking Run.
- S3 downloads presign against the regional endpoint, so the link serves directly instead of
  307-redirecting and breaking the host-signed URL.
- Quieter voice-ID checking: generic bit-parts stay `—`, and benign fuzzy matches that share a
  name word (`Suga` ~ `Kenzo Suga`) no longer read "verify match".

### Security
- Git-ignored private keys (`*.pem`), `.env` files and voice-key exports — this repo is public.

---

## Earlier

Before 2026-07-24 the project was a fully-offline Windows desktop app (Electron + React + a
local FastAPI backend) that checked a dubbed episode against its script using Silero VAD, with
no cloud, no API keys and no audio leaving the machine. That app still works and still ships;
the hosted pipeline and the Teams agent were built on top of the same engine. See `HANDOFF.md`
for the full architectural primer and the hard-won gotchas.
