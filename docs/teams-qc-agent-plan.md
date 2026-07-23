# Teams QC Agent — full plan

*Updated 2026-07-21. For team review.*

**Goal:** turn dub QC into a conversation inside a Teams channel. Someone asks for a series +
episode; the bot checks Box for the required assets, reports what's available, runs the QC tool, and
delivers the Excel report + the timeline "missing-audio" files back into the channel — no one has to
touch the server, Box, or a browser.

This reuses everything already built (Box discovery, VAD/alignment, mapping fixes, the per-episode
workbook, the timeline ref-audio, the live HTTPS server, the shared Box-token cache). The genuinely
new work is a thin "agent" layer plus the Teams-side wiring.

---

## 1. The conversation (what the user sees)

```
User (in a Teams channel):   @QC run episode 42 of Kamen Rider Gavv

QC bot (availability card):   EP 42 — Kamen Rider Gavv
                              ✅ English script        Kamen_Rider_Gavv_S1_E42.DOCX
                              ✅ Original audio         Gavv_#42_..._PREMIX.wav (Hindi premix)
                              ✅ Character list         KAMEN RIDER CHARACTER LIST & VOICES.xlsx
                              Dub stems:
                                ✅ Tamil (15)   ✅ Kannada (14)   ✅ Bengali (13)  ✅ Marathi (14)
                                ❌ Malayalam · ❌ Telugu — not delivered yet
                              → 4 of 6 languages ready. Run QC on those 4?
                              [ Run QC ]   [ Cancel ]

User:                         (clicks Run QC)

QC bot:                       ▶ Running EP 42 (4 languages)… ~8 min. I'll post here when done.
   … (async) …
QC bot (results card):        EP 42 QC complete ✅
                              Tamil    5 missing · 29 mismatch · 93 extra
                              Telugu   —        Kannada  5 missing · …
                              Bengali  2 missing · …     Marathi  0 missing
                              ⚠ Delivery note: 1 label swap repaired, 1 split stem merged (Marathi)
                              📎 EP42_QC.zip  — workbook + timeline missing-audio per language
                              (posted as a real file in the channel's Files tab)
```

Two bot turns: an **availability card** (with Run/Cancel), then a **results card** (with the file).

---

## 2. What already exists (≈ 80% of the work)

| Capability | Where | Role in the agent |
|---|---|---|
| Box discovery | `box_batch.find_script / find_stems / find_original` | the availability check |
| Analysis + per-episode multi-language workbook | `box_batch.run_episode`, `excel_report` | the QC run |
| Timeline + stitched missing-audio | `box_batch._write_ref_audio` | the audio deliverables |
| Async episode job | `/api/jobs/box-episode` (202 + poll) | run without blocking the webhook |
| Shared Box-token cache | `box_oauth` (flock) | agent + web UI + batches coexist |
| **Public HTTPS** | Caddy + Let's Encrypt, `https://13-205-42-228.sslip.io` | Teams can POST to us / fetch files |
| Outbound Teams posting | `PRD-generator/.../teams.js` (Adaptive Card v1.5 via Power Automate) | post the cards |

**Status change since the first draft:** the public-HTTPS endpoint — previously flagged as "the one
true blocker" — is **live as of 2026-07-21**, so this project is unblocked.

---

## 3. Architecture

```
Teams channel
   │  "@QC run episode 42 of Gavv"
   ▼
Power Automate  INBOUND flow ── HTTPS POST ─▶  QC Agent  (new endpoints on the existing FastAPI service)
                                                 │  POST /api/teams/check
   ◀── availability card + [Run]/[Cancel] ──────┤    1. LLM parse → {series, episode}
Teams channel                                    │    2. Box availability (script/original/char-list/6 langs)
   │  click "Run QC"                             │    3. build availability Adaptive Card
   ▼                                             │  POST /api/teams/run   (on Run)
Power Automate  INBOUND flow ─────────────────▶ │    4. async QC (reuse box-episode job)
                                                 │    5. zip workbook + timeline FLACs
   ◀── results card + EP42_QC.zip ──────────────┤    6. results card + file
Teams channel  (Files tab)                       ▼
                                          workbook.xlsx + *_MISSING_timeline.flac  →  EP42_QC.zip
```

---

## 4. The two halves of Teams integration

Teams integration is directional and needs both:

| Direction | Mechanism | Status |
|---|---|---|
| **us → Teams** (post the cards) | Power Automate "post to channel" + Adaptive Card (reuse `teams.js`) | ✅ have it |
| **Teams → us** (receive the command + the Run/Cancel click) | Power Automate inbound flow (below) | 🆕 to add |

**Inbound = a Power Automate flow** (no Azure, matches the PRD-generator stack):
- Trigger: *"When a new channel message is added"* (or *"when I'm mentioned"*) → **HTTP POST** the
  message text to `/api/teams/check`.
- For the buttons: Power Automate's **"Post an Adaptive Card and wait for a response"** gives real
  in-card Run/Cancel buttons and calls `/api/teams/run` on Run — no bot registration needed.
- (Alternative, richer: an **Azure Bot Service** app — full interactivity, proactive messages — but
  needs an Azure AD registration. Overkill for v1; revisit only if Power Automate limits bite.)

---

## 5. Availability check (the 4 assets)

`check_episode(series, ep)` returns a structured presence report and drives the availability card:

1. **English script** — `find_script` (rejects non-English language-tagged variants).
2. **Original audio** — `find_original` (Hindi premix; now separator-tolerant so `PRE MIX` matches).
3. **Character list** — **new** `find_char_list(series)`: a known per-series Box file
   (e.g. *KAMEN RIDER CHARACTER LIST & VOICES.xlsx*). Feeds the mapping roster; report present/absent.
4. **Dub stems, per language** — `find_stems` + track counts; missing languages are reported and
   skipped, not failed.

**Series registry** — today the Box folder IDs are hardcoded for one show. Generalise to a small
table so a new series is a config entry, not code:
```
SERIES = {
  "kamen-rider-gavv": {
     "aliases": ["gavv", "krg", "kamen rider gavv"],
     "scripts": "375861426771", "premix": "377097256586",
     "char_list": "<box-file-id>",
     "voiceover": {"Tamil": "379646596612", "Telugu": "379644054186", …},
  },
}
```
**v1 supports Kamen Rider Gavv only** (registry has one entry; adding a show = supply its folder IDs).

---

## 6. Delivering the files INTO the channel

Teams cards/webhooks post **text + Adaptive Cards only — they cannot attach a file.** The Files tab
is just SharePoint, so a real in-channel file must be written there. Options, cheapest first:

- **A — card with a [Download] button** *(works today).* QC finishes → server zips `EP{NN}_QC.zip`
  (workbook + each language's `_MISSING_timeline.flac`) and serves it over our HTTPS behind a
  short-lived token; the results card has a **[Download]** button. One click. Not literally "in" the
  channel, but zero SharePoint plumbing.
- **B — real file in the Files tab via Power Automate** *(the "in the group itself" ask).* The flow
  does **HTTP GET** (fetch the zip) → **SharePoint "Create file"** into the channel's library →
  **"Post message"** linking it. Teams renders a file card. All native connectors — **no Azure, no
  Graph code.**
- **C — Microsoft Graph upload** direct from our server (`PUT …/drive/root:/EP42_QC.zip:/content`) —
  richest, but needs an Azure AD app + `Files.ReadWrite`. Skip unless we outgrow B.

One **zip per episode** keeps it a single file drop even with several languages × two outputs each.
Timeline FLACs are ~99% silence and compress to KB–low-MB, so a per-episode zip stays well within
Teams/SharePoint limits.

*Recommendation on the table: ship A, upgrade to B — but this is the main item for the team to weigh.*

---

## 7. Understanding the command (natural language)

Current lean: **LLM (natural language)** — "hey can you QC episode 42 of Gavv" is understood, not
just a fixed syntax. Implementation: one Claude Messages API call that extracts
`{series, episode, intent: check|run}` from the message; falls back to a regex if the key is absent
or the call fails (so a malformed message still works). The LLM can also write the two card summaries
in plain language.
- **Needs:** an `ANTHROPIC_API_KEY` on the service.
- **Cost:** a few cents per command at most (tiny prompts); latency ~1s, hidden behind the async run.
- (A rule-based-only mode remains the zero-dependency fallback if the team prefers no LLM.)

---

## 8. Security & auth

- **Inbound endpoints** (`/api/teams/*`) guarded by a **shared secret** header that only the Power
  Automate flow knows (rejects random internet POSTs). HTTPS already encrypts it.
- **Who can trigger** = who can post in the channel (Teams membership). Optionally restrict to a
  specific channel ID.
- **Download links / the zip** behind a short-lived token so the artifact isn't world-readable.
- **Box** — unchanged; the shared-token cache lets the agent, the web UI, and batches coexist.
- No new secrets in git (same rule as today: `.env` only).

---

## 9. Phased build

| Phase | Work | Who | Effort |
|---|---|---|---|
| 1 — availability core | `check_episode` (script/original/**char-list**/6 langs) + `/api/teams/check`; LLM intent-parse; availability card; series registry (Gavv) | **me** (server) | ½ day |
| 2 — inbound flow | Power Automate "message → POST" + "post card & wait" for Run/Cancel | you/IT (Teams) | ½ day, click-ops |
| 3 — run + deliver (A) | on Run → async QC → zip → results card with [Download] | **me** (server) | ½ day |
| 4 — file in Files tab (B) | Power Automate: fetch zip → SharePoint Create file → post file card | you/IT (Teams) | ½ day |
| 5 — polish | LLM summaries, errors, auth hardening, rate-limit, proactive "new stems landed" ping | both | ongoing |

**≈ 2 days to a working bot** (Phases 1–3), **+½ day** for true in-channel files (Phase 4).

---

## 10. Division of labor

- **I build from here (no Teams access needed):** all server-side — `check_episode` incl. char-list,
  the `/api/teams/check` + `/api/teams/run` endpoints, the series registry, the LLM parse/summary,
  the zip packaging, the tokenized download. Deployable to the existing EC2 service immediately.
- **You / IT do in the Teams tenant (click-ops I can't reach):** the 2 Power Automate flows and the
  SharePoint "Create file" permission on the channel's library. I'll write exact step-by-steps and
  the card JSON.
- The **Microsoft 365 MCP connector** shows as needing authorization in tooling — **not required**
  for this build (we use our own endpoints + Power Automate), so ignore it.

---

## 11. Open decisions for the team

1. **File delivery** — download button (A, live fastest) vs real file in the Files tab (B, the
   "in the group" experience) vs just a link. *(Recommendation: A now → B.)*
2. **Which channel / who can trigger** — the target Teams channel, and whether to restrict runs to
   its members only.
3. **LLM parsing** — confirm natural-language (needs `ANTHROPIC_API_KEY`) vs a fixed command syntax.
   *(Current lean: LLM.)*
4. **Series scope** — Gavv only for v1 (current lean), or wire additional shows now (need their Box
   folder IDs).
5. **Approval step** — keep the explicit Run/Cancel confirmation, or auto-run when all assets are
   present?

---

## 12. Nice-to-haves (later)
- **Proactive**: watch Box and ping the channel when a new episode's stems land ("EP 46 Tamil is
  ready — QC it?").
- **"QC the whole series 1–50"** in one command → batch + a rolled-up card.
- **Threaded** results so each episode's QC is its own conversation.
- `qc.rian.io` friendly URL (ask IT for an A-record → 1-line Caddy change).
