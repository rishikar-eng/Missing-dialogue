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
   │     parses check / run / status with regex, NO LLM (see §4 — 5-second limit!)
   │     ├─ check  -> box_discovery.check_episode()  -> _availability_card()  [Adaptive Card]
   │     ├─ run    -> _launch_run()  -> Fargate task(s) or in-process job
   │     └─ status -> run_store / fargate.status_parallel() -> results + download links
   │
   └─ (the richer LLM agent lives on /api/agent/chat — backend/agent.py + router.py)
```

| File | What it does |
|---|---|
| `backend/server.py` | **All the Teams surface.** `_teams_fast`, `_availability_card`, `agent_go` (the Run button), `_launch_run`, `/api/agent/dl` (downloads) |
| `backend/agent.py` | L2 worker agent (Claude Haiku) — natural-language path, used by `/api/agent/chat` |
| `backend/router.py` | L3 router (Claude Sonnet) — picks which series a message is about |
| `backend/series_registry.json` | Which shows exist + their Box folder ids. **Data, not code** — add a show without touching code |
| `backend/box_discovery.py` | Finds script/audio/dub tracks in Box by naming convention |
| `backend/fargate.py` | Dispatches heavy runs to AWS Fargate; reads status/results from S3 |
| `backend/run_store.py` | Persists runs to disk so `status` survives a server restart |
| `docs/teams-setup.md` | How the Teams webhook was registered |

---

## 4. ⚠️ The one hard constraint: the 5-second reply window

Teams **Outgoing Webhooks must respond in ~5 seconds** or the user sees
*"Sorry, there was a problem."* This single rule shapes the whole design.

- We originally called the LLM agent from Teams. It took ~13 s → **every message failed**.
- Fix: `_teams_fast` is a **deterministic, LLM-free** path (regex intent parsing). Measured:
  `check` ≈ 2.8 s, `run` ≈ 0.1 s, `status` ≈ 0.1 s.
- Box lookups were also parallelised (9.9 s → 2.1 s) to fit inside that budget.

**If you add anything to the Teams path, keep the reply under ~3 s.** Anything slow must be
kicked off in the background and reported later via `status`. Do not "just call the LLM" in
`_teams_fast` — that regression has already broken this feature once.

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

## 7. Known UX gaps — good starting projects

1. **No proactive completion message.** A run finishes silently; the user must poll `status`.
   Teams Outgoing Webhooks *cannot* push messages — fixing this properly needs an **Incoming
   Webhook** (or a Bot Framework app) to post into the channel when a run completes. High value.
2. **Errors are raw.** Failures surface as truncated exception text. They should be plain
   English with a suggested next step.
3. **The mention must be picked from autocomplete.** Typing `@qc` as plain text silently does
   nothing (no request reaches the server) — this confused the first users. Worth an onboarding
   message or a pinned help card.
4. **No `help` command** listing what the bot understands.
5. **Intent parsing is regex** — "kick off 42", "is it done?", typos, or a bare "42" may miss.
   Either broaden the patterns or fall back to the LLM path *asynchronously* (never inline —
   see §4).
6. **Only one series is registered** (Kamen Rider Gavv), so series disambiguation is untested.
7. **Progress is coarse** — `status` says "running" with no percentage or ETA.
8. **Long messages**: six languages × counts + links is a wall of text; an Adaptive Card table
   would read far better than plain text.
9. **Download links render as raw URLs** ← *good first task, already agreed as yours*.
   A finished 6-language run currently posts something like:
   ```
   ✅ Bengali: 4 missing, 13 mismatch — https://13-205-42-228.sslip.io/api/agent/dl?d=eyJzMyI6…
   ```
   The signed token is ~150 chars, so each line wraps 2–3 times and the actual counts get
   buried. Teams renders **markdown** in webhook replies, so `[Download report](url)` collapses
   it to one clickable word. Built in `_teams_fast`'s parallel-status branch in
   `backend/server.py` (search for `_dl_s3(` and the `lines.append(f"✅ {lang}…")` loop).
   Worth doing alongside #8 — an Adaptive Card with one row per language, counts in columns and
   a Download action button, would replace the whole text blob.

---

## 8. Conventions & guardrails

- Match the surrounding style: FastAPI + pydantic, comments explain *why* not *what*.
- **Never** commit secrets (§0). Check `git status` before every commit.
- Keep `_teams_fast` free of LLM/network-heavy calls (§4).
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
