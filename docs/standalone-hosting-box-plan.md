# Dialogue QC — Standalone Hosting + Box Server-to-Server: End-to-End Implementation Plan

**Status:** ready to implement · **Date:** 16 Jul 2026 · **Audience:** the engineer/model writing the code

**Goal:** run Dialogue QC as a standalone hosted web service that pulls episode audio + scripts
**directly from Box** with no human clicking anything, and returns the Missing/Misaligned/Extra
report. The analysis logic is finished and must not change — this is a **hosting + auth + job-shape**
job, not an algorithm job.

> **Supersedes** `docs/aws-hosting-box-plan-v2.md` on two points (§1). Keep that doc for the
> RAM-feasibility analysis (§2 of it) and the Box write/rename story, both still valid.

---

## 1. Two corrections to earlier assumptions — read first

Both came out of reading the teammate's actual repo (`C:\Users\Rishi\Desktop\stsproject`, "VOX").

### ❌ "A teammate hosted his S2S tool on a free AWS account" — **not true**

All 104 commits are by one author and there is **no AWS deployment anywhere**. The real topology is:

- **Frontend** → Vercel (static Vite build, auto-deploy from GitHub `main`)
- **`api/*` proxy** → Vercel serverless (Node), pinned `maxDuration: 10`, memory 1024
- **`sts-backend/`** (the heavy Node+Python+ffmpeg work) → **Docker container on Railway/Render**

Every AWS string in that repo belongs to **Rian's vendor internals** (their own error codes like
*"Failed to download fargate batch input param file from S3"*) or is unactioned boilerplate
("Deploy dist/ to: Vercel, Netlify, AWS S3 + CloudFront"). Hard evidence it ran on Railway:

```ts
// sts-backend/server/index.ts:55-57  (commit 04702f6)
// Bind to 0.0.0.0 so platform proxies (Railway/Render) can reach the app — the default
// host-only bind makes the container unreachable and yields 502s.
```
```ts
// src/pages/SpeechToSpeechPage.tsx:296
// the Railway proxy occasionally drops the first racing request …
```

**Consequence:** there is no AWS recipe to copy. If we go AWS we are greenfield. The proven
in-house pattern is **Docker → Railway/Render**.

### ❌ "We can reuse VOX's Box integration" — **not true for headless**

VOX holds **no Box client_id, no client_secret, no JWT key**. It brokers OAuth through *Rian's*
server (`GET /v1/Box/OAuthUrl` → `POST /v1/Box/OAuthExchange`) and drives an interactive popup.
The token is **~1 hour, in-memory only, no refresh token** — by explicit policy:

```ts
// src/services/boxAuthService.ts:3-6
// The Box access token is short-lived (~1 hour) and is intentionally NOT persisted …
// On a page reload the user re-authenticates.
```

**Consequence:** any unattended job built on that token dies within the hour. Standalone needs
**our own Box app + Client Credentials Grant (CCG)**. Code shared with VOX: **zero**.

### ✅ What VOX *does* give us (worth copying)

1. **The S2S model is validated**: browser passes *file references + token*, the **server fetches
   the bytes**. "No file bytes touch the browser." That is exactly what `/api/qc` already does.
2. **The side-car pattern**: Docker container, bind `0.0.0.0`, honor injected `$PORT`, treat disk
   as ephemeral, CORS via a `FRONTEND_ORIGIN` allowlist, `/api/health`, SSE progress with a ~15s
   heartbeat + `X-Accel-Buffering: no`, wired to the frontend by one `VITE_*_BACKEND_URL` env var.
3. **The async contract**: Rian returns **`202` + `operationId`**, the client polls
   `GetOperationStatus` (5s interval, 30-min timeout, terminal on status 3/4). **Our QC API should
   use the same shape** so it feels native to VOX later.
4. **Vercel cannot host our compute** — 10s timeout, 4.5 MB body. Confirmed in `vercel.json`, not
   just prose.

---

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| **Host** | **Railway (Hobby, $5/mo)** | Docker, Singapore region, scale-to-zero, teammate already deploys there. Actual usage ≈ $0.82/mo — the $5 minimum dominates. Unblocked **today**. |
| **Box auth** | **CCG** (`box_subject_type=enterprise`) | No user in the loop; no keypair (unlike JWT). Box's default for new Server apps. |
| **Box access** | **Collaborate the Service Account onto the folder** (Viewer) | Least privilege, no enterprise-wide impersonation, colleague can do it in 10s. See §4.3. |
| **Job shape** | **`202` + `job_id` + poll** | Runs take 1-5 min. Railway **closes a request with no data transfer after 5 minutes**. Also matches VOX's existing contract. |
| **AWS** | **Unblock in parallel; Lambda is the endgame** | Not a blocker for shipping. See §8. |

### Runner-up hosts (if Railway is rejected)

| Option | Cost | Note |
|---|---|---|
| **AWS Lambda** (container + **Function URL**) | **~$0.10–0.50/mo** | Genuinely cheapest. 400k GB-s/mo always-free ≈ **740 runs/mo**; our ~150 runs = 20% of it. **Must use a Function URL (15-min), NOT API Gateway (29s cap).** Blocked on account activation. |
| **Fly.io** (`bom` **Mumbai**) | ~$1–4/mo scale-to-zero, $11.11 always-on | Only option with a real **Mumbai** region *and* scale-to-zero *and* no 5-min timeout games. |
| **Hetzner CX23** | €5.49/mo | Cheapest always-on 4 GB, no timeouts — but **EU-only** (no India region). Fine for a 1-5 min batch job. |
| ❌ **Render** | 2 GB=$25, **4 GB=$85** | 100-min timeout is lovely but **17× Railway** and **no scale-to-zero**. 2 GB will OOM us. |
| ❌ **Cloud Run** | ~$1/mo | **`/tmp` is tmpfs — downloaded WAVs consume RAM.** Actively fights multi-hundred-MB downloads. |

> 🚨 **Do not use any 512 MB free tier** (Render Free/Starter, Fly's 256 MB preset). Our peak is
> **2–4 GB**; they will OOM-kill. Even **Render Standard's 2 GB is a trap** — it's the very edge.
> **Provision 4 GB.**

---

## 3. Target architecture

```
                      ┌──────────────────────────────────────────┐
  Browser (React) ────▶  Railway: Docker container (4 GB)        │
   or VOX later        │   FastAPI + Silero VAD (onnxruntime)    │
                       │   serves the built React UI at  /       │
                       │                                          │
                       │   POST /api/qc      -> 202 {job_id}     │
                       │   GET  /api/qc/{id} -> status|result    │
                       │   GET  /api/health                       │
                       └───────────────┬──────────────────────────┘
                                       │  CCG token (minted server-side, cached ~55 min)
                                       ▼
                          Box  api.box.com  /  dl.boxcloud.com
                          (Service Account is a *collaborator* on the folder)
```

Key properties:
- **No Box secret ever reaches the browser.** Client ID/secret live in Railway env vars only.
- **No user OAuth popup.** The server mints its own token.
- Downloads stream to the container's **ephemeral** `/tmp` and are deleted after each job.

---

## 4. PART 1 — Box setup (HUMAN steps; these BLOCK the code)

> ⚠️ **Terminology changed (2024-11-12):** "Custom App" → **Platform App**. In the Admin Console
> the admin looks for **Apps → Platform Apps Manager** (not "Custom Apps Manager").

### 4.1 Create the app (Rishi)
1. Enable **2FA** on the Box account — *required just to reveal the client secret*.
2. [Developer Console](https://app.box.com/developers/console) → **New App** → **Server** → **Create**.
   CCG is the **default** for new Server apps.
3. **Configuration** tab → **Application Scopes** → tick **"Read all files and folders stored in Box"**
   (`root_readonly`). Leave **App Access Only** (default).
4. Copy **Client ID** + **Client Secret** (Configuration tab).
5. Copy the **Enterprise ID**: account icon (top-right of Developer Console) → **Copy Enterprise ID**.

### 4.2 Admin authorization (BOSS / Box admin — **mandatory**)
> Box: *"Server authentication applications using JWT or Client Credentials Grant **must be
> authorized by a Box Admin or Co-Admin before use**."*

- Easiest: Configuration tab → **Authorize** (if you're admin) or **Submit** to email a request.
- Manual: give the admin the **Client ID** → Admin Console → **Apps** → **Platform Apps Manager**
  → **Add App** → paste Client ID.

**If skipped, the failure looks like:** `400 invalid_grant` ("Grant credentials are invalid") at the
token endpoint, and/or `unauthorized_client` — *"This app is not authorized by the enterprise"* — on
API calls. Handle **both** as a distinct, actionable error (§5.1).

⚠️ **The Service Account does not exist until the admin authorizes the app.**

### 4.3 🔴 THE GOTCHA that breaks every first CCG integration

A CCG enterprise token is **not** an admin and **not** "the enterprise". It is a distinct
auto-created user — the **Service Account** — and:

> **"A Service Account has its own folder tree, which starts empty."**

So `GET /2.0/folders/0/items` returns **empty, not an error**. Your colleague's folder isn't
permission-denied — it is **not in this user's account at all**.

**The fix (standard, least-privilege):**
1. Developer Console → **General** tab → copy the Service Account email:
   `AutomationUser_<ServiceID>_<random>@boxdevedition.com`
2. **Ask the folder owner (Pranav / whoever owns the JioStar folder) to invite that email as a
   collaborator with role `Viewer`** on the episode folder. Ten seconds in the Box web UI.

Rejected alternatives: `box_subject_type=user` and the `As-User` header both require
**App + Enterprise Access** + **Generate User Access Tokens** + re-authorization, and grant
*enterprise-wide impersonation* to solve a single-folder problem. Don't.

> **Scopes ≠ permissions.** `root_readonly` does **not** grant access to the folder. You need
> **both** the scope *and* the collaboration.

### 4.4 Sanity checks (do these before writing pipeline code)
```bash
# 1. Auth works? Returns the Service Account's id + login.
curl -H "authorization: Bearer $TOKEN" https://api.box.com/2.0/users/me
# 2. Empty result here == step 4.3 not done. It is NOT a bug.
curl -H "authorization: Bearer $TOKEN" https://api.box.com/2.0/folders/0/items
```

### 4.5 Later changes
Any **scope or access-level change → the admin must Reauthorize the app, AND you must request a
fresh token** (existing tokens don't pick up new scopes). Switching CCG↔JWT does *not* need
re-authorization.

---

## 5. PART 2 — Code changes

Everything below is additive. **Do not touch** `alignment.py`, `characters.py`, `content_map.py`,
`loudness.py`, `scriptless.py`, or `_analyze_pipeline`'s scoring logic.

### 5.1 NEW: `backend/box_auth.py` — mint + cache the CCG token

This is the core gap. `box_fetch.py` already accepts a bearer token; this produces one.

```python
"""Mint Box Client-Credentials-Grant tokens for headless server-to-server access."""

TOKEN_URL = "https://api.box.com/oauth2/token"

class BoxAuthError(RuntimeError):
    """Auth failure with a SAFE message — never echoes the client_secret."""

def get_token(*, force_refresh: bool = False) -> str:
    """Return a cached-or-fresh Box access token.

    Reads BOX_CLIENT_ID / BOX_CLIENT_SECRET / BOX_ENTERPRISE_ID from env.
    Thread-safe (a lock; the VAD pool is multi-threaded).
    """
```

**Requirements — these are exact, follow them:**

- `POST https://api.box.com/oauth2/token`, `Content-Type: application/x-www-form-urlencoded`, body:
  ```
  grant_type=client_credentials
  client_id=<BOX_CLIENT_ID>
  client_secret=<BOX_CLIENT_SECRET>
  box_subject_type=enterprise
  box_subject_id=<BOX_ENTERPRISE_ID>
  ```
- Response: `{"access_token": "...", "expires_in": 4123, "restricted_to": [], "issued_token_type": "bearer"}`
- **⚠️ Read `expires_in` from the response — DO NOT hardcode 3600.** Box's own documented example
  returns **4123**.
- **There is NO refresh token for CCG.** Cache the token; refresh when
  `now >= issued_at + expires_in - 300` (5-min safety margin); re-request on `401`.
- **Thread-safety:** guard the cache with a `threading.Lock` — `_analyze_pipeline` runs a
  `ThreadPoolExecutor`.
- **Error mapping (important for support):**
  - `400 invalid_grant` **or** `unauthorized_client` → raise `BoxAuthError` with a message naming
    the real cause: *"Box app is not authorized by the enterprise admin, or the client
    ID/secret/enterprise ID are wrong. Ask the Box admin to authorize the app (Admin Console →
    Apps → Platform Apps Manager → Add App) using client ID `<id>`."*
  - **Never** include `client_secret` in any exception, log line, or HTTP response.
- Parse defensively: rely only on `access_token` and `expires_in` (Box's docs contradict themselves
  on `token_type` vs `issued_token_type`).

### 5.2 CHANGE: `backend/box_fetch.py` — make the token optional

Currently every entry point requires a caller-supplied `token`. Keep that (VOX still passes one),
but **fall back to CCG** when it's absent:

```python
def download_file(token: str | None, file_id: str, dest_dir, *, shared_link=None, name=None) -> Path:
    token = token or box_auth.get_token()
    ...
```
Same for `download_files`. Also: on a `401` from Box, call `box_auth.get_token(force_refresh=True)`
**once** and retry — a token can expire mid-job on a long multi-track download.

**Add 429 handling** (currently missing): honor the `retry-after` header (seconds); otherwise
exponential backoff with jitter. ⚠️ Rate limits are **per user**, and the Service Account is **one
user** — all our traffic shares a single **1000 req/min** bucket. Do not parallelize around it.

### 5.3 CHANGE: `backend/server.py` — async jobs (this is required, not optional)

**Why:** a run takes 1–5 minutes. **Railway closes a request that transfers no data after 5
minutes.** A silent 5-minute VAD run gets cut. (Railway explicitly recommends polling.)

Replace the synchronous `/api/qc` with the VOX-native contract:

| Endpoint | Behavior |
|---|---|
| `POST /api/qc` | validate → create `job_id` (uuid4) → start worker thread → **return `202 {"job_id": ..., "status": "queued"}` immediately** |
| `GET /api/qc/{job_id}` | `{"status": "queued"\|"running"\|"done"\|"error", "progress": {"stage","done","total"}, "result": <report or null>, "error": <str or null>}` |
| `GET /api/health` | `{"status":"ok"}` — Railway healthcheck |

- Keep the existing `QCRequest` fields; `box_token` becomes **optional** (omitted ⇒ CCG).
- **Job store:** an in-process `dict[str, Job]` + `threading.Lock`. Single instance, so this is fine.
  **Reap jobs older than `DQC_JOB_TTL_S` (default 3600)** or the dict leaks.
- **Bound concurrency:** a global semaphore of `DQC_MAX_CONCURRENT_JOBS` (**default 1**). Two
  concurrent 4 GB jobs OOM a 4 GB box.
- **Always clean up** the temp dir in a `finally`, including on error/timeout.

> 🔴 **`PROGRESS` and `STATE` are module-level globals** (`server.py:55,58`). That's correct for the
> single-user desktop app but **wrong for a multi-user server** — concurrent runs would stomp each
> other. **Move progress into the per-job record.** Leave the desktop `/api/analyze` +
> `/api/progress` path alone (it still works single-user); the hosted service must use per-job state.

### 5.4 CHANGE: memory discipline (`_analyze_pipeline`)

`server.py:214` fixes VAD parallelism at `min(4, cpu_count, total)`, and each worker holds one
**native-rate** stem (~150–300 MB) plus `envelope()`. On a 4 GB container that is too hot.

```python
n_workers = max(1, min(int(os.environ.get("DQC_VAD_WORKERS", "4")), os.cpu_count() or 2, total))
```
Set **`DQC_VAD_WORKERS=2`** on Railway (leave the desktop default at 4). Free the native array as
soon as `resample_16k` + `envelope` are done.

### 5.5 CHANGE: serve the React build + CORS

- Mount the Vite build so one container serves both:
  ```python
  app.mount("/", StaticFiles(directory="dist", html=True), name="ui")   # AFTER all /api routes
  ```
- CORS: allowlist from **`DQC_FRONTEND_ORIGIN`** (comma-separated), mirroring `sts-backend`'s
  `FRONTEND_ORIGIN`. Do **not** use `*` — this service reaches client dialogue.
- The UI must call the API at a **relative** `/api/...` when co-served (no `127.0.0.1:8765`).
  Make the base URL an env-driven constant in `src/api.ts` (currently hardcoded, `api.ts:2`).

### 5.6 Config — all env vars

| Var | Required | Default | Notes |
|---|---|---|---|
| `BOX_CLIENT_ID` | yes | — | |
| `BOX_CLIENT_SECRET` | yes | — | 🔴 **real secret** — Railway env var only. Never in git, never `VITE_`-prefixed, never logged |
| `BOX_ENTERPRISE_ID` | yes | — | |
| `DQC_API_KEY` | yes | — | see §7 |
| `DQC_FRONTEND_ORIGIN` | yes | — | comma-separated CORS allowlist |
| `PORT` | — | 8765 | **injected by Railway — must honor it** |
| `DQC_VAD_WORKERS` | — | 4 | **set to 2** on a 4 GB box |
| `DQC_MAX_CONCURRENT_JOBS` | — | 1 | |
| `DQC_JOB_TTL_S` | — | 3600 | |
| `DQC_BOX_MAX_FILE_MB` | — | 3072 | already implemented |

> **Secrets rule (learned from VOX's own mistake):** `VITE_*` vars are **bundled into the browser
> bundle**. `stsproject/.env.example:11` ships `VITE_ELEVENLABS_API_KEY` — a provider key in client
> JS, violating its own `ENV_SETUP.md`. **Never put the Box secret behind a `VITE_` prefix.**

---

## 6. PART 3 — Container + deploy

### 6.1 `Dockerfile`
```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY dist/ ./dist/            # built React UI (npm run build:ui first, or multi-stage)
COPY run.py .
ENV PORT=8765
EXPOSE 8765
# 🔴 0.0.0.0 — the exact bug the teammate hit; localhost bind = unreachable = 502
CMD ["sh", "-c", "uvicorn backend.server:app --host 0.0.0.0 --port ${PORT:-8765}"]
```
Prefer a multi-stage build (`node:20` → `npm run build:ui` → copy `dist/`).

**Container non-negotiables** (all learned from `sts-backend`):
1. Bind **`0.0.0.0`**, never localhost.
2. Honor the **injected `$PORT`**.
3. Disk is **ephemeral** — files vanish on redeploy/restart. Fine for us (download → analyze →
   delete), but never treat `/tmp` as storage.

### 6.2 Railway
1. New project → Deploy from GitHub repo → it auto-detects the `Dockerfile`.
2. **Region: Asia Southeast (Singapore)** — available on Hobby.
3. Set every env var from §5.6.
4. Healthcheck path `/api/health`.
5. **Resources: 4 GB.** (Railway bills actual usage — $10/GB/mo — so bursting to 4 GB on ~7.5 hrs/mo
   is pennies; the **$5 Hobby minimum dominates**.)
6. Scale-to-zero: app sleeps after 10 min idle, **no compute charge while asleep**. First request
   after sleep pays a cold start — acceptable.

**Expected bill: $5/mo.**

---

## 7. PART 4 — Auth for the service (must be decided, not defaulted)

⚠️ **`sts-backend` has NO authentication at all** — a CORS origin allowlist is its only gate, and
CORS is a *browser* convention, not a security control (curl ignores it). We must not copy that:
this service reads **client dialogue** from Box and will sit on a public URL.

**Ship with (simplest sufficient):** a static **`DQC_API_KEY`** required on every `/api/*` request
via an `X-API-Key` header; constant-time compare; reject with `401`. Plus the
`DQC_FRONTEND_ORIGIN` CORS allowlist. `/api/health` stays open for the platform healthcheck.

**Later, if this merges into VOX:** validate the Rian JWT, or route QC through Rian. Note VOX's
axios instance **auto-encrypts every POST** with Rian's AES key (`x-encrypted-pl: 1`) — a non-Rian
service will choke on that; VOX would need `x-skip-encryption` or a separate axios instance (it
already precedents this with `fileApi`).

Also reuse the existing **Rian login gate** in front of the UI.

---

## 8. PART 5 — The AWS path (parallel, non-blocking)

### 8.1 What's actually wrong with your account
The "wait 24 hours" mail is **account activation** (payment-method + identity/phone verification).
Credits appear **only after activation completes** — there is no claim/redeem step. AWS's stated
ceiling is 24 hours; you're past it, so it's stalled.

> 🇮🇳 **Most likely cause:** Indian debit/credit cards block **international / recurring**
> transactions by default under RBI rules, so AWS's verification charge silently fails and the
> account sits in limbo.

**Fix, in order:**
1. **Enable international + online/recurring payments** in your bank app. **Prefer a credit card**
   over a debit card. Then Billing console → **Payment preferences** → re-verify.
2. **Open a support case — this is FREE.** Billing & account support is included in **Basic Support
   for every account**; you do *not* need a paid plan. Support Center → Create case → **"Account and
   billing"**. If you can't sign in: <https://support.aws.amazon.com/#/contacts/aws-account-support>.
3. You **cannot** launch resources while activation is pending.

### 8.2 🔴 When it activates, choose the **Paid plan**

| | Free plan | Paid plan |
|---|---|---|
| Signup credit | **$100** | **$100** |
| Earnable credits | up to $100 | up to $100 |
| Services | *select* only | **all** |
| **When credits run out** | 🔴 **account CLOSES** | continues, pay-as-you-go |

**Picking Paid does NOT forfeit the $100.** Both grant the identical $200. The **Free plan closes
your account** after 6 months or when credits deplete — a trap for an ongoing internal tool. The
second $100 is *earned* via the **"Explore AWS"** widget on Console Home (filter: "Earn AWS
credits"); activities must be done within 6 months, credits expire 12 months after signup.

### 8.3 The Lambda endgame (~$0.10–0.50/mo)
If we ever want the bill at essentially zero:
- **3-min run @ 3 GB = 540 GB-s.** Always-free is **400,000 GB-s/mo ≈ 740 runs/mo**. Our ~150
  runs/mo = **20%** of the free tier — ~5× headroom. Requests: 150 vs 1M free.
- Fits: **15-min timeout ✅, 10 GB RAM ✅, 10 GB `/tmp` ✅** (and Lambda's `/tmp` is **real
  disk**, unlike Cloud Run's RAM-backed tmpfs — structurally better for our big WAVs).
- 🔴 **Use a Lambda Function URL, NOT API Gateway** — API Gateway caps at **29s** and would kill
  the job; Function URLs hold the connection the full 15 min.
- The always-free Lambda tier persists on **both** plans in 2026 — but only stays *usable*
  long-term on the **Paid** plan (see 8.2).

---

## 9. Phases & effort

| Phase | Work | Effort | Blocks on |
|---|---|---|---|
| **0** | Box app + **admin authorization** + **collaborate the Service Account** onto the folder (§4) | ~1 hr + admin turnaround | 🔴 **Box admin / boss** |
| **1** | `box_auth.py` (CCG mint+cache), `box_fetch.py` token fallback + 401 retry + 429 backoff (§5.1–5.2) | ~half day | Phase 0 for live testing |
| **2** | Async job shape (`202` + `job_id` + poll), per-job progress, concurrency semaphore, TTL reaping (§5.3) | ~1 day | — |
| **3** | Serve React build, CORS allowlist, `DQC_API_KEY`, env config, `DQC_VAD_WORKERS=2` (§5.4–5.6, §7) | ~half day | — |
| **4** | Dockerfile + Railway deploy + healthcheck + smoke test (§6) | ~half day | — |
| **5** | *(optional)* AWS unblock → Lambda container + Function URL (§8) | ~1 day | AWS activation |

**Critical path is Phase 0 — it's a human/admin step, so start it today.** Phases 1–4 can be
written and tested against a **developer token** (Developer Console → "Generate Developer Token",
60-min) before the admin authorizes anything.

---

## 10. Risks & open questions

| Risk | Mitigation |
|---|---|
| 🔴 **Admin won't authorize the app** (same blocker as the desktop Box plan) | Nothing headless works without it. Escalate early; it's a 1-minute click for the admin. |
| 🔴 **Service Account can't see the folder** | The §4.3 collaboration step. Symptom is an **empty** `/folders/0/items`, not an error — do not debug it as a bug. |
| **Railway 5-min silent-request cut** | The `202` + poll design (§5.3) makes this a non-issue. |
| **4 GB OOM** | `DQC_VAD_WORKERS=2`, `DQC_MAX_CONCURRENT_JOBS=1`, free native arrays early. |
| **Box secret leakage** | Env var only; never `VITE_`; never logged; sanitize all Box errors (`box_fetch.py` already strips signed `dl.boxcloud.com` URLs — keep that discipline in `box_auth.py`). |
| **Rate limit (1000/min, one SA bucket)** | Honor `retry-after` + backoff. Box also warns of *undocumented* throttling for bulk-download patterns like ours. |
| **Shared-link access unverified** | No official doc confirms a non-collaborator SA can read a shared link; a `collaborators`-level link definitely won't work. **Use collaboration (§4.3), not shared links.** |

**Open questions for the boss:**
1. Who is the Box **admin** for the JioStar enterprise, and will they authorize the app? *(Pranav
   Marathe owns the folder per earlier notes.)*
2. Is **$5/mo Railway** acceptable, or does the boss specifically want **AWS** for optics/policy?
3. Should this stay standalone, or eventually merge into **VOX** as the `qc_ready` step? (VOX's
   `/qc` screen is deliberately a hollow link-out — *"This page just shows the external QC editor
   link — NOT a full QC interface"* — so an external QC tool is the **intended** architecture.)

---

## 11. Acceptance tests (definition of done)

1. `GET /api/health` → `200 {"status":"ok"}` on the public Railway URL.
2. `GET /2.0/users/me` with a CCG token returns the **Service Account**; `/folders/0/items` lists
   the **collaborated episode folder**.
3. `POST /api/qc` with **only Box file ids and no `box_token`** → `202 {job_id}`; polling reaches
   `done`; the report **matches the desktop run for the same episode**.
4. A run **>5 minutes** completes without the connection being cut.
5. Missing/incorrect `X-API-Key` → `401`.
6. Un-authorized app → a **clear, actionable** error naming the admin step (not a raw 400).
7. `BOX_CLIENT_SECRET` appears in **no** log line, error body, or client payload.
8. Temp files are gone after each job (success **and** failure).
9. Two concurrent submissions do not corrupt each other's progress/report.

---

## 12. Reference — verified facts behind this plan

- **Box CCG:** token `POST https://api.box.com/oauth2/token`; `expires_in` ~60 min (docs example
  **4123** — read it, don't hardcode); **no refresh token**; admin authorization **mandatory**;
  Service Account tree **starts empty**; `root_readonly` for read+download; scope change ⇒
  **re-authorize + new token**; **1000 req/min per user**, 429 + `retry-after`.
  <https://developer.box.com/guides/authentication/client-credentials>
- **AWS Free Tier (post 2025-07-15):** $100 signup + up to $100 earned on **both** plans; **Free
  plan closes the account** when credits run out; Lambda 1M req + **400k GB-s/mo** still
  always-free in 2026; billing support free on Basic.
  <https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans.html>
- **Railway:** RAM $10/GB/mo, Hobby **$5/mo** minimum, Singapore region, scale-to-zero after 10 min;
  **requests with no data transfer closed after 5 min**.
  <https://docs.railway.com/pricing> · <https://docs.railway.com/networking/public-networking/specs-and-limits>
- **VOX topology:** Vercel (`maxDuration: 10`, 4.5 MB body) + Docker on Railway/Render; **no AWS**;
  Box OAuth brokered by Rian, ~1h token, no refresh, cannot run headless.
