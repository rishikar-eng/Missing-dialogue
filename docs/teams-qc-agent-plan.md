# QC Agent — full plan (API-first, multi-agent)

*Updated 2026-07-21. For team review.*

**Goal:** turn dub QC into a conversation. Someone names a **series + episode** in natural language;
the system checks the source (Box) for the required assets, reports what's available, runs the QC
tool, and delivers the Excel report + the timeline "missing-audio" files back to the requester —
first in a **Teams channel**, but built so the same brain can back a Slack bot, a web widget, or
another internal tool without change.

Reuses the QC engine we already have (Box discovery, VAD/alignment + mapping fixes, per-episode
workbook, timeline ref-audio, live HTTPS, shared Box-token cache). The new work is a thin, layered
**agent** stack on top plus the Teams wiring.

---

## 1. Design principles (from the team's direction)

1. **API-first / embeddable.** Every layer is an HTTP API. The QC engine is provider-neutral; the
   agent is reachable as a single `/agent/chat` endpoint. Teams is just the first client — because
   it's all APIs, "use this as an extension to something else" works for free.
2. **Right-size the model per layer — smallest that meets the bar.** Not tied to a specific model:
   each layer uses the lowest-resource model that does its job. The per-series workers do only
   parse + structured tool-calls, so the smallest tier fits (**Haiku 4.5** today); the **router**
   steps up one tier because intent/series disambiguation is the harder call. A given worker can be
   bumped up later if its series proves to need more reasoning — the tiering is per-role, not fixed.
3. **Natural language.** Requests are plain English ("QC episode 42 of Gavv"), parsed by the model,
   not a fixed command syntax.
4. **No hardcoded file locations.** Where a series' assets live is **data, not code** — a series
   registry the agents read (and, where possible, Box *search* discovery) — so adding a show is a
   config entry, never a code change.
5. **One agent per series.** Kamen Rider gets its own agent; each other show gets its own Haiku
   agent; the router points at the right one.

---

## 2. Architecture — three layers + clients

```
        Teams  ·  (later) Slack / web widget / other internal tools
                    │  natural-language message  ("QC ep 42 of Gavv")
                    ▼
        ┌─────────────────────────────────────────────────────────┐
   L3   │  ROUTER agent  (bigger model — Sonnet 5 or Opus 4.8)     │
        │  parse intent + identify series → dispatch to its worker │
        └───────────────┬──────────────────────┬──────────────────┘
                        │                       │
        ┌───────────────▼──────┐   ┌────────────▼───────────────┐   … one per series
   L2   │ Kamen Rider agent    │   │ <Series B> agent           │
        │ (Haiku 4.5)          │   │ (Haiku 4.5)                │
        │ knows its Box layout │   │ knows its Box layout       │
        │ tools: check / run / │   │ tools: check / run / fetch │
        │ fetch results        │   │                            │
        └───────────────┬──────┘   └────────────┬───────────────┘
                        │  calls (HTTP)          │
        ┌───────────────▼────────────────────────▼───────────────┐
   L1   │  QC ENGINE API  (provider-neutral REST, on EC2)         │
        │  /availability · /run (async) · /result                │
        │  Box discovery · VAD+mapping · workbook · timeline audio│
        └─────────────────────────────────────────────────────────┘
                        │ reads/writes
                   Box  ·  series registry (config store)
```

- **L1 — QC Engine API.** The reusable core. Given a series+episode (resolved through the registry)
  or explicit asset locations, it checks availability, runs QC, and returns artifacts. This is the
  "extension to something else" surface — any system can call it directly, no agent required. Mostly
  exists today (`/api/jobs/box-episode`, `/api/qc`, `/api/report.xlsx`); we formalize three clean
  endpoints: `GET /availability`, `POST /run` (async → job id), `GET /result`.
- **L2 — per-series worker (Haiku 4.5).** One agent per show. Its **series knowledge** (Box folder
  conventions, naming quirks, language set, where the character list lives) comes from the registry
  + its system prompt — never hardcoded. It exposes a few **tools** that call L1: `check_availability`,
  `run_qc`, `get_result`. Haiku is cheap enough to run these conversationally.
- **L3 — router (bigger model).** Reads the incoming message, extracts `{series, episode, intent}`,
  and hands off to the matching series worker (or asks which series if ambiguous). Also the place
  for cross-series concerns later ("QC ep 42 of Gavv **and** Series B").
- **Clients.** Teams first (via Power Automate, below). Because L3 is just an HTTP endpoint
  (`POST /agent/chat`), a Slack app or web chat is another client, not a rewrite.

---

## 3. Models & cost

Principle: pick the **smallest model that clears each layer's bar**, not a fixed model.

| Layer | Model (starting point) | ID | Price /M (in/out) | Why this tier |
|---|---|---|---|---|
| Router (L3) | **team decision** — Sonnet 5 *or* Opus 4.8 | `claude-sonnet-5` / `claude-opus-4-8` | $3/$15 · $5/$25 | Intent + series disambiguation is the harder call → one tier up |
| Workers (L2) | **Haiku 4.5** (smallest that fits) | `claude-haiku-4-5` | $1/$5 | Only parse + structured tool-calls → the lowest tier is enough; bump a specific series later only if it needs it |

Notes for implementation:
- **Haiku 4.5 caveat:** it does **not** support the `effort` / adaptive-thinking parameters (those
  are Opus/Sonnet-5 only) — a request that sends them 400s. Workers use the plain thinking config
  (or none). Keep worker prompts tight; they're doing structured tool-calls, not deep reasoning.
- **Cost is tiny per request:** a check/run/report cycle is a handful of short model turns —
  cents at most, dominated by the router. If the router runs on Sonnet 5, the whole exchange is
  well under a cent in model spend (the QC compute on EC2 is the real cost, unchanged).
- The **agent loop** (model → tool → model) is driven by the SDK's tool runner, not hand-rolled.

---

## 4. No hardcoded file locations — the series registry

A **registry** is the single source of truth for where a series' assets live. It is **data**, editable
without a deploy (a JSON/DB record, or a config file in Box):

```json
{
  "kamen-rider-gavv": {
    "aliases": ["gavv", "krg", "kamen rider gavv"],
    "languages": ["Malayalam","Tamil","Telugu","Kannada","Bengali","Marathi"],
    "box": {
      "scripts_folder":  "<id-or-search-rule>",
      "premix_folder":   "<id-or-search-rule>",
      "char_list_file":  "<id-or-search-rule>",
      "voiceover_root":  "<id-or-search-rule>"
    },
    "naming": { "script": "Kamen_Rider_Gavv_S1_E{n}.docx", "premix": "..._PRE?MIX.wav", "...": "..." }
  }
}
```

Two ways to resolve locations, both non-hardcoded:
- **Registry lookup (v1):** the worker reads its series' record. Adding a show = add a record.
- **Box *search* discovery (robust upgrade):** give the worker a `search_box` tool + the series'
  naming conventions in its prompt, and it finds the script / premix / stems / char-list by name
  rather than by a stored folder id — resilient to folder reshuffles. (We already hit exactly this
  class of problem — the `PRE MIX` vs `PREMIX` spelling — so tolerant matching lives in the engine.)

v1 ships the registry with **one record (Kamen Rider Gavv)**; discovery is the reliability upgrade.

---

## 5. The conversation (what the user sees)

```
User (Teams):   @QC run episode 42 of Kamen Rider Gavv

Router → Gavv worker → availability card:
                EP 42 — Kamen Rider Gavv
                ✅ English script     ✅ Original audio (Hindi premix)
                ✅ Character list     Dub stems: ✅ Tamil (15) ✅ Kannada (14)
                                                 ✅ Bengali (13) ✅ Marathi (14)
                                                 ❌ Malayalam · ❌ Telugu — not delivered
                → 4 of 6 languages ready. Run QC on those 4?      [ Run QC ]  [ Cancel ]

User:           (Run QC)

Gavv worker:    ▶ Running EP 42 (4 languages)… ~8 min. I'll post here when done.
   … async …
Gavv worker → results card:
                EP 42 QC complete ✅   Tamil 5 · Kannada 5 · Bengali 2 · Marathi 0 missing
                ⚠ 1 label swap repaired, 1 split stem merged (Marathi)
                📎 EP42_QC.zip  (workbook + timeline missing-audio per language)
```

**Availability = 4 assets:** English script, original audio, **character list**, and dub stems per
language — the worker's `check_availability` tool returns all four.

---

## 6. Delivering the files INTO the channel

Teams cards/webhooks **can't attach a file** — the Files tab is SharePoint, so a real in-channel
file must be written there. Options (this is the item deferred for team discussion):

- **A — card with a [Download] button** *(works today).* Engine zips `EP{NN}_QC.zip` (workbook +
  every language's timeline audio), serves it over our HTTPS behind a short-lived token; the card
  has a download button. Zero SharePoint plumbing.
- **B — real file in the Files tab** *(the "in the group itself" ask).* A Power Automate step fetches
  the zip → **SharePoint "Create file"** → posts a file card. Native connectors, no Azure/Graph code.
- **C — Microsoft Graph upload** direct from the engine — richest, needs an Azure AD app; skip unless
  we outgrow B.

*Recommendation on the table: ship A → upgrade to B.* One zip per episode = a single drop even with
several languages.

---

## 7. Teams wiring (the client)

Teams integration is two directions:

| Direction | Mechanism | Status |
|---|---|---|
| **us → Teams** (post cards) | Power Automate "post to channel" + Adaptive Card (reuse `teams.js`) | ✅ have it |
| **Teams → us** (message + Run/Cancel) | Power Automate flow → `POST /agent/chat`; "post card & wait" for buttons | 🆕 to add |

No Azure needed — matches the PRD-generator stack. The **Microsoft 365 MCP connector** shows as
needing authorization in tooling; we don't use it for this build, so ignore it.

---

## 8. Implementation approach (for the build discussion)

Two ways to build the router + workers; both keep L1 unchanged:

- **(rec. v1) SDK tool-use, self-hosted.** Router and workers are Claude calls with tools, driven by
  the Anthropic SDK's **tool runner**, running as a small service next to the QC engine on EC2. We
  own the loop; QC stays on our box; cheapest; least infra. The router calls a `dispatch(series)`
  tool; each series worker calls the L1 tools. Fits "API-first, embeddable" — the whole thing is one
  `/agent/chat` endpoint.
- **(later) Managed Agents (CMA) multiagent.** Anthropic hosts the loop; the router is a **coordinator
  agent** with a **roster of per-series agents** (this is *literally* your router→per-series topology),
  each a persisted, versioned Haiku agent; our QC is called back via custom tools/MCP. Cleaner
  separation and per-series versioning, but more infra and it's beta. Worth it once there are several
  series and we want each one independently owned/versioned.

Start with the SDK approach; the layering means a later move to CMA is swapping L3/L2 hosting, not
rewriting L1.

---

## 9. Security & auth

- L1 and `/agent/chat` guarded by a shared secret (only the Teams flow / trusted callers know it);
  HTTPS already encrypts it.
- Who can trigger = who can post in the channel; optionally lock to a channel id.
- Result zip behind a short-lived token.
- `ANTHROPIC_API_KEY` in `.env` only (same secret rule as today). Box unchanged (shared-token cache).

---

## 10. Phased build

| Phase | Work | Who | Effort |
|---|---|---|---|
| 1 — L1 engine API | formalize `/availability` (incl. **char-list**), `/run`, `/result`; series registry (Gavv) | **me** | ½ day |
| 2 — L2 worker | Kamen Rider Haiku agent + its 3 tools (SDK tool runner); `/agent/chat` | **me** | ½ day |
| 3 — L3 router | bigger-model router: parse NL, identify series, dispatch | **me** | ½ day |
| 4 — Teams inbound | Power Automate message→`/agent/chat` + "post card & wait" for Run/Cancel | you/IT | ½ day |
| 5 — deliver (A) | zip workbook + timeline audio → results card with [Download] | **me** | ¼ day |
| 6 — file-in-channel (B) | Power Automate fetch zip → SharePoint Create file → file card | you/IT | ½ day |
| 7 — polish | Box-search discovery, add a 2nd series, errors, auth, proactive "new stems landed" ping | both | ongoing |

**≈ 2 days to a working bot** (Phases 1–5), + ½ day for true in-channel files.

---

## 11. Division of labor

- **I build from here (no Teams access needed):** all of L1 + L2 + L3 — the engine endpoints, the
  series registry, the Haiku worker(s), the router, the `/agent/chat` API, the zip packaging. Deploys
  to the existing EC2 service.
- **You / IT do in the Teams tenant:** the Power Automate flows + SharePoint permission. I'll write
  exact step-by-steps and the card JSON.

---

## 12. Decisions

DECIDED (2026-07-23):
- **Router model = Claude Sonnet 5** (`claude-sonnet-5`); workers = Haiku 4.5. ✅
- **Anthropic key = an existing Rian key**, added to the QC server's `.env` as `ANTHROPIC_API_KEY`. ✅
- **Series scope = Kamen Rider Gavv only** for v1. ✅
- **NL parsing = LLM** (natural language). ✅

BUILD STATUS — **L1+L2+L3 DONE & DEPLOYED, proven end-to-end on real Box (2026-07-23):**
- **L1 engine:** series_registry.json + box_discovery.py + episode_runner.py; endpoints
  `/api/agent/series`, `/availability`, `/run`, `/result`, `/download`. Full run validated
  (EP43 Tamil → zip = workbook + missing-audio).
- **L2 worker (Haiku 4.5):** backend/agent.py — check_availability/run_qc/get_result tools;
  `/api/agent/chat` with server-side sessions. Natural-language chat proven.
- **L3 router (Sonnet 5):** backend/router.py — rule fast-path + structured-output disambiguation;
  binds series per session. "gavv"/omitted/unknown all route correctly.
- **Teams:** `/api/agent/teams` Outgoing-Webhook receiver (HMAC). Setup guide: docs/teams-setup.md.
- Rian ANTHROPIC_API_KEY installed on the server (value never exposed).
- ⚠ char-list Box location still a registry TODO. Restart forgets in-flight jobs/sessions (by design).
- **REMAINING (you):** create the Teams Outgoing Webhook (or Power Automate flow) per
  docs/teams-setup.md and set DQC_TEAMS_SECRET; optional Adaptive Cards / Run button polish.

STILL OPEN (team):
- **File delivery** — [Download] button (A) vs real file in the Files tab (B) vs just a link. *(Rec: A→B.)*
- **Target channel / who can trigger** — the Teams channel + whether to restrict to members.
- **Approval step** — keep the Run/Cancel confirmation, or auto-run when all assets are present.
- **Next series** — which show gets the 2nd registry record.

---

## 13. Later
- Proactive: watch Box, ping the channel when a new episode's stems land.
- "QC the whole series 1–50" in one command → batch + rolled-up card.
- `qc.rian.io` friendly URL (A-record → 1-line Caddy change).
- Migrate L2/L3 to CMA multiagent once several series exist (per-series versioning).
