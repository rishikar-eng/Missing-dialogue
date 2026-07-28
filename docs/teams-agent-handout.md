# Dialogue QC — Teams Chat Agent: Contributor Handout

**Audience:** an engineer (and their AI coding agent) picking up the **Teams chat experience**.
**Scope of your work:** the conversational UX — what the bot says, the Adaptive Cards, error
messages, command parsing, progress reporting. You should NOT need to touch the QC analysis
engine (VAD/alignment/Excel) to do that.

> ⚠️ **THIS REPOSITORY IS PUBLIC.** Never commit a credential, token, `.pem`, `.env`, or
> customer audio. Secrets arrive separately (see §2). `.gitignore` already blocks the usual
> suspects — do not add exceptions.

---

## 1. What this product is (60 seconds)

**Dialogue QC** checks a *dubbed* episode against its source and reports what's wrong:
lines **Missing**, delivered by the **wrong speaker (Mismatch)**, **Misaligned** in time, or
**Extra**. It produces an Excel workbook + reference audio of the missing bits, which the
recording studio uses to re-record.

It runs in three places, all sharing one backend:
1. **Electron desktop app** (offline, original product).
2. **Hosted web UI** on an AWS EC2 box.
3. **Microsoft Teams chat agent** ← *your area*.

The Teams agent is how the production team actually uses it day-to-day:
```
@QC check gavv ep 42     -> availability card (script/audio/languages ready) + [Run QC] button
@QC run ep 42            -> starts the run (fans out to cloud compute)
@QC status               -> progress, then per-language results + download links
```

---

## 2. Getting set up

### 2.1 Code
The repo is public — just clone it. (You only need write access to push branches; ask Rishi
to add you as a collaborator for that.)
```bash
git clone https://github.com/rishikar-eng/Missing-dialogue.git
cd Missing-dialogue
python -m venv .venv                     # Python 3.11 recommended
.venv/Scripts/pip install -r requirements-server.txt   # server deps (incl. anthropic, boto3)
```

### 2.2 Credentials
**Not in this repo.** Rishi will send you a separate `CREDENTIALS.txt` privately (1Password /
direct message — *not* email, *not* a git commit). Put its values in a local `.env` file at
the repo root; `.env` is git-ignored.

You do **not** need every credential. For Teams UX work you mainly need `DQC_TEAMS_SECRET`
(or better, your *own* test webhook — see §6.1). Ask before requesting Box/AWS access.

### 2.3 Run the backend locally
```bash
.venv/Scripts/python run.py          # serves http://127.0.0.1:8765
curl http://127.0.0.1:8765/api/healthz
```

---

## 3. Architecture — where the Teams code lives

```
Teams channel
   │  user types "@QC check gavv ep 42"
   ▼
POST /api/agent/teams          ← backend/server.py  (HMAC-verified Outgoing Webhook)
   │
   ├─ _teams_fast()            ← THE MAIN FUNCTION YOU'LL EDIT (server.py)
   │     parses help / runs / check / run / status with regex, NO LLM (§4 — 5-second limit!)
   │     ├─ check   -> box_discovery.check_episode()  -> _availability_card()  [Adaptive Card]
   │     ├─ run     -> _launch_run()  -> Fargate task(s) or in-process job  (+ notify.watch)
   │     ├─ status  -> _run_status()  -> _status_text() + _status_card()
   │     └─ (no match) -> _ask_async(): instant ack, LLM answer PUSHED later (§4.1)
   │
   └─ (the older LLM chat surface lives on /api/agent/chat — backend/agent.py + router.py)

                    ┌─ backend/notify.py — daemon thread; posts the finished run into the
                    │   channel via an Incoming Webhook so nobody has to poll (§5.1)
```

| File | What it does |
|---|---|
| `backend/server.py` | **All the Teams surface.** `_teams_fast` (commands), `_run_status` (a run's state as data), `_status_text` / `_status_card` (rendering), `_availability_card`, `agent_go` (the Run button), `_launch_run`, `_ask_async`, `/api/agent/dl` |
| `backend/notify.py` | Proactive completion messages via a Teams **Incoming Webhook** — the only way this bot can speak unprompted (§5.1) |
| `backend/results.py` | Loads a finished run's FULL per-language results (local disk or S3) + the query helpers the LLM's tools use. Background use only — it's megabytes |
| `backend/xlang.py` | Cross-language root cause (script problem vs one vendor's gap). Shared by the workbook and the chat reply so they can't disagree |
| `backend/agent.py` | `answer_about_run()` — the read-only tool loop behind `_ask_async`; plus the older L2 worker agent used by `/api/agent/chat` |
| `backend/router.py` | L3 router (Claude Sonnet) — picks which series a message is about |
| `backend/series_registry.json` | Which shows exist + their Box folder ids. **Data, not code** — add a show without touching code |
| `backend/box_discovery.py` | Finds script/audio/dub tracks in Box by naming convention |
| `backend/fargate.py` | Dispatches heavy runs to AWS Fargate; reads status/results from S3; `ensure_summary` builds+caches the cross-language view |
| `backend/run_store.py` | Persists runs to disk so `status` survives a restart; also the notifier's work queue |
| `docs/teams-setup.md` | How the Teams webhook was registered |

**Where a number in a Teams message comes from.** The engine's full result is reduced ONCE, by
`episode_runner._lang_summary()`, into a compact per-language digest. That digest is what gets
written to S3 (`status.json`) for cloud runs and to the run store for local ones, and it is
what every reply renders. **If you want to show something new in Teams, add it there first** —
adding it to the renderer alone will find nothing to render. Every field except
`missing`/`mismatch`/`extra` must be read with a default: records written by older builds don't
have them.

---

## 4. ⚠️ The one hard constraint: the 5-second reply window

Teams **Outgoing Webhooks must respond in ~5 seconds** or the user sees
*"Sorry, there was a problem."* This single rule shapes the whole design.

- We originally called the LLM agent from Teams. It took ~13 s → **every message failed**.
- Fix: `_teams_fast` is a **deterministic, LLM-free** path (regex intent parsing). Measured:
  `check` ≈ 2.8 s, `run` ≈ 0.1 s, `status` ≈ 0.1 s.
- Box lookups were also parallelised (9.9 s → 2.1 s) to fit inside that budget.

**If you add anything to the Teams path, keep the reply under ~3 s.** Anything slow must be
kicked off in the background and reported later. Do not "just call the LLM" in `_teams_fast` —
that regression has already broken this feature once.

Measured after the current changes (`help` 0.6 s cold / <25 ms warm, `runs` 24 ms, `status`
5 ms with AWS stubbed, `ask` ack 5 ms). The only real cost on the status path is AWS, which is
now one batched `describe_tasks` plus concurrent S3 reads — it used to be 12 serial round-trips
for six languages.

### 4.1 The escape hatch: answer asynchronously
An unrecognised message goes to `_ask_async()`, which replies *immediately* ("looking into
that…"), runs Claude Haiku with read-only tools over the run's full results on a background
thread, and **pushes** the answer into the channel. That's how you get natural-language
answers without touching the 5 s budget. It needs both an Incoming Webhook (§5.1) and
`ANTHROPIC_API_KEY`; without either it falls back to the help text.

The tools (`backend/agent.py::_ASK_TOOLS`, backed by `backend/results.py`) are
`run_overview`, `list_findings`, `character_report`, `cross_language_check`. The system prompt
is explicit that QC detects *whether* someone spoke, never *what* they said — if you extend
these tools, keep that guardrail or the bot will start inventing acting notes.

---

## 5. How a run actually executes (so your progress messages are truthful)

`run` does **not** compute anything in the web process. It launches **one AWS Fargate task per
language** (6 languages → 6 parallel tasks, ~6 min wall-clock instead of ~20 sequential).
Each task writes its report zip + a `status.json` to S3. `status` then aggregates:

- per-language: ✅ done (with counts + a download link) / 🔄 running / ⏭ not delivered / ⚠️ failed
- when **all** languages finish, a **cross-language Summary** workbook is built once, cached in
  S3, and linked — it flags whether a gap is a script problem (missing in every language) or one
  dub team's miss (missing in only one).

Download links are short signed URLs (`/api/agent/dl?d=…`) that redirect to a presigned S3 URL,
so Teams messages stay readable.

`status` no longer just prints counts. It leads with a **verdict** (🔴 a whole character was
never delivered · 🟠 lines missing/mistimed · 🟢 clean — the same scale in the roll-up and the
per-language rows), sorts languages worst-first, and carries the findings that change what
someone does next: out-of-sync tracks, script rows that failed to parse (= lines never
checked), mapping issues, loudness flags, and the cross-language root cause. An Adaptive Card
renders the same data as a table with a collapsed "missing-line examples" drill-down —
embedded in the payload, so expanding costs no server call.

**One caveat to preserve:** the root-cause reading is computed only from languages that
actually produced a result. When some failed, the heading says so explicitly ("across the 3 of
6 languages that completed") — otherwise "absent in EVERY language" would send the studio to
the script for what is really a missing result.

### 5.1 Proactive completion (setup)
`backend/notify.py` posts the finished run into the channel so nobody polls. Outgoing Webhooks
cannot push, so this uses an **Incoming Webhook**: in Teams, channel → ⋯ → Connectors →
Incoming Webhook → copy the URL, then set

```
DQC_TEAMS_INCOMING       one URL (single-channel deployments — the usual case)
DQC_TEAMS_INCOMING_MAP   {"<conversation id>": "<url>"}  for several channels
DQC_NOTIFY_POLL_S        poll interval, default 45s
```

With neither set the watcher never starts and everything behaves as before (polling still
works) — and the `run` confirmation says "ask status" instead of promising a message that can
never arrive. A run is marked `notified` in the run store after one attempt, successful or
not: retrying a rotated webhook URL every 45 s forever is worse than one lost announcement.

---

## 6. Testing without breaking production

### 6.1 Strongly recommended: your own Teams webhook
Create your **own** team + Outgoing Webhook (see `docs/teams-setup.md`) pointing at your own
tunnel or the shared server. This gives you a private secret and avoids spamming the
production channel. Ask Rishi if you truly need the production secret.

### 6.2 Test the webhook without Teams at all (fastest loop)
Requests are authenticated with **HMAC-SHA256 over the raw body**, base64, sent as
`Authorization: HMAC <sig>`. You can drive the whole flow from Python:

```python
import hmac, hashlib, base64, json, httpx
SECRET = "<DQC_TEAMS_SECRET>"          # base64 string from your credentials file
def teams(msg, conv="devtest"):
    body = json.dumps({"type": "message", "text": f"<at>QC</at> {msg}",
                       "conversation": {"id": conv}}).encode()
    sig = base64.b64encode(hmac.new(base64.b64decode(SECRET), body, hashlib.sha256).digest()).decode()
    r = httpx.post("http://127.0.0.1:8765/api/agent/teams", content=body,
                   headers={"Authorization": "HMAC " + sig}, timeout=30)
    return r.json()

print(teams("check gavv ep 42"))   # -> text + an Adaptive Card attachment
print(teams("status"))
```
`conversation.id` is the session key — use different ids to simulate different channels.

### 6.3 Preview Adaptive Cards
Paste the JSON that `_availability_card()` returns into <https://adaptivecards.io/designer>
(target: *Microsoft Teams*) to iterate on layout without a deploy.

### 6.4 Deploying
The server is a `systemd` service on EC2. Deploy = `git pull` + `sudo systemctl restart
dialogue-qc`. **Coordinate with Rishi before restarting** — a restart kills any in-process job
(Fargate runs survive, but the web process forgets in-flight local jobs).

---

## 7. Known UX gaps

**Closed** (2026-07-28): proactive completion (§5.1) · plain-English errors with a next step
(`_friendly_error`) · a `help` command (and `runs` for history) · async LLM fallback for
unrecognised messages (§4.1) · Adaptive Card status instead of a text wall · the counts a
reviewer actually needs (undelivered characters, sync warnings, unparsed script rows, root
cause) · two reporting bugs where a run with failed languages could still print a green
"✅ QC done" header.

**Still open — good starting projects:**

1. **The mention must be picked from autocomplete.** Typing `@qc` as plain text silently does
   nothing (no request reaches the server) — this confused the first users. The `help` text now
   says so, but a pinned onboarding card in the channel would be better.
2. **Progress is coarse** — a running fan-out says "3 done, 2 running" with no ETA. The run
   store has `created_at`/`updated_at` on every past run, so a median per-language duration is
   available to estimate one.
3. **Only one series is registered** (Kamen Rider Gavv), so series disambiguation is untested.
4. **The Run button leaves Teams.** `Action.OpenUrl` → an HTML confirmation page in a browser.
   `Action.Submit` would keep it in-channel, but that needs a real Bot Framework app rather
   than an Outgoing Webhook — which would also remove the 5 s limit entirely and allow typing
   indicators. That's the one remaining *structural* upgrade.
5. **`_ask_async` answers about the channel's LATEST run only.** "Compare ep 41 and 42" or
   "which vendor drops the most lines this month" would need the tools to take a job id.

---

## 8. Conventions & guardrails

- Match the surrounding style: FastAPI + pydantic, comments explain *why* not *what*.
- **Never** commit secrets (§0). Check `git status` before every commit. `.gitignore` covers
  `.env`, `.env.*` **and** `*.env` — all three patterns are needed (`.env*` misses
  `server-keys.env`; `*.env` misses `.env.local`).
- Keep `_teams_fast` free of LLM/network-heavy calls (§4). Slow work goes on a thread and comes
  back through `notify.post`.
- New data for a reply starts in `episode_runner._lang_summary`, not in the renderer.
- `_run_status()` returns **data**, not a formatted message, so the polled reply and the pushed
  completion message are the same thing. Keep it that way — they drifted apart before.
- The QC engine (`alignment.py`, `characters.py`, `content_map.py`, `excel_report.py`) is
  validated against real studio deliveries — changing it changes QC results. Stay out unless
  that's the task.
- `series_registry.json` is data: adding a show should need no code change.
- Test with the HMAC script (§6.2) before deploying.

## 9. Deeper background
- `HANDOFF.md` — full project primer (architecture, pipeline, gotchas).
- `docs/teams-setup.md` — webhook registration steps.
- `docs/teams-qc-agent-plan.md` — why the agent is layered the way it is.
- `docs/fargate-plan.md` — the on-demand compute design.
