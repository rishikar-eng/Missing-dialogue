# Dialogue QC — AWS Hosting + Box Server-to-Server Plan (v2)

**Goal:** run Dialogue QC as a small web service on AWS, and have it pull each episode's
inputs **directly from Box** (audio zip, script, character list) with no manual download —
mirroring how our teammate hosted his S2S tool on a "free" AWS account.

This supersedes the read-only `box-integration-plan.md` and the serverless-only
`cloud-hosting-plan.md` by (a) correcting a **2025 change to the AWS Free Tier**, (b) adding a
**RAM feasibility** reality-check, and (c) adding the **Box write/rename** story.

> Two things to reconcile with the teammate's handout when it arrives: **(1)** was his AWS
> account created *before* 2025-07-15 (legacy 12-month free tier) or *after* (new credit model)?
> **(2)** did he run on a single always-on EC2/Lightsail box, or serverless? Those answers pick
> our path below.

---

## 1. The AWS "free" reality (verified — this changed in July 2025)

AWS overhauled the Free Tier on **2025-07-15**:

| | Account created **before** 2025-07-15 (legacy) | Account created **today** (new) |
|---|---|---|
| Model | 12-month per-service trials + always-free | **Credit-based**: $100 on signup + up to $100 earned (5×$20 tasks) = **up to $200** |
| EC2 free | **750 hrs/mo of t2/t3.micro free for 12 months** | **No micro trial** — EC2 is funded from the credits |
| Expiry | 12 months | Free plan ends at **6 months OR when credits run out**, whichever first |
| Always-free (Lambda 1M req + 400k GB-s/mo, 100 GB/mo egress) | Yes | **Yes — persists on any plan** |

**Why it matters:** if the teammate's "free account" was a **pre-July-2025** account, he got a
year of free t3.micro and just ran his tool on it. A **fresh** account today does **not** get
that — you'd spend ~$200 of credits over ~6 months, then pay on-demand. **The always-free
Lambda allowance, however, is unchanged and is effectively free forever for our load.**

*Sources: AWS Free Tier update blog (2025), AWS billing docs, Free Tier FAQ — all confirmed.*

---

## 2. Feasibility gotcha: don't run the VAD on a 1 GB micro

The audio compute is **RAM-bound**, and the classic free instance (t2/t3.micro = **1 GiB**) will
likely **OOM-kill** a run:

- Python + onnxruntime + numpy resident base: **~300–500 MB**
- One 25-min stem decoded at native rate: **~150–300 MB** (+ read buffers)
- → peak **~600–800 MB per track**, leaving nothing for the OS on 1 GiB.
- Loading all 12–28 tracks at once would be multi-GB — OOMs anything under ~8 GB.

**The fix (do this regardless of host):**
1. **Downsample each stem to 16 kHz mono *before* VAD** — Silero only needs 16 kHz; this cuts
   per-track RAM ~5–10×. (Our code already has `resample_16k`; we'd read → resample → free the
   native array, and only keep native-rate briefly where true-peak loudness needs it.)
2. **Process one stem at a time** (a strict semaphore) on small boxes — no parallel VAD.
3. Minimum reliable RAM: **2 GiB** (t3.small). Comfortable: **4 GiB** (t3.medium) or Lambda @3–4 GB.

---

## 3. Two hosting options

### Option A — Single box (simplest; likely matches the teammate's approach)
```
Browser (React static site) ──HTTPS──▶  EC2 or Lightsail box
                                          run FastAPI + Silero VAD directly
                                          pull inputs from Box → /tmp → analyze → return report
```
- **EC2 t3.small (2 GB, ~$15/mo)** or **t3.medium (4 GB, ~$30/mo)**; or **Lightsail $10–12/mo**
  (fixed price, transfer bundled). **Start/stop the box** to only pay while running.
- Pros: closest to the current code (already a FastAPI server), easy to reason about, matches a
  typical solo-dev handout. Cons: pay for idle unless you stop it; you manage the box (nginx,
  TLS, systemd, updates).
- **Not** a 512 MB Lightsail nano or 1 GB micro for the compute — API/frontend only.

### Option B — Serverless (cheapest; scales to zero) — *the existing cloud-hosting-plan*
```
S3 static frontend ─▶ async invoke ─▶ container-image Lambda @3–4 GB (15-min cap)
                                        download Box zip → /tmp (≤10 GB) → VAD one stem
                                        at a time → write report.json/csv → S3
                          browser polls S3 / a status endpoint for the finished report
```
- onnxruntime + numpy + model fit easily in the **10 GB container image**; a ~3-min run at 3 GB
  ≈ **540 GB-s/episode**, so the always-free **400,000 GB-s/month covers ~700 episodes/mo at $0**.
- Must be **asynchronous** — API Gateway times out at 29 s, our job runs minutes. `POST /qc`
  returns a job id; browser polls (same progress pattern we already have).
- Pros: ~**$0/mo** for testing load, nothing to babysit, scales. Cons: more moving parts to set
  up once (Lambda container, S3, async/polling); cold starts.

**Recommendation:** if the teammate's handout is a single-EC2 recipe and we want to move fast,
do **Option A** on a **t3.small (start/stop)** for the pilot. If we want it durable and near-free,
**Option B** is the better home and the code port is nearly the same (swap "read local path" →
"read from S3/Box", wrap as an async job).

---

## 4. Box server-to-server integration

Same read path we already validated, now running **on the server** instead of the desktop.

**Auth — Client Credentials Grant (CCG), no user/browser:**
```
POST https://api.box.com/oauth2/token
  grant_type=client_credentials, client_id=…, client_secret=…,
  box_subject_type=enterprise, box_subject_id=<enterprise id>
→ { access_token, expires_in: ~3600 }   (no refresh token — just re-request on 401)
```
Requires the Custom App to be **authorized in the Admin Console** (Apps → Custom Apps Manager) —
the one admin blocker, already documented.

**Read / fetch (scope: *Read all files* = `root_readonly`):**
- Resolve a shared link: `GET /2.0/shared_items` + header `boxapi: shared_link=<url>`
- List a folder: `GET /2.0/folders/{id}/items?limit=1000&usemarker=true`
- Download bytes: `GET /2.0/files/{id}/content` (follows a 302 to `dl.boxcloud.com`)

The server fetches the **episode audio zip**, the **script docx**, and the **character-list docx**
this way, unzips the audio to temp, and feeds the existing analyzer. Inbound transfer Box→AWS is
**$0**.

---

## 5. Does Box have a rename API? — **Yes.** (but read §6 first)

```
Rename:  PUT https://api.box.com/2.0/files/{file_id}   body {"name": "New Name.wav"}
Move:    PUT https://api.box.com/2.0/files/{file_id}   body {"parent": {"id": "<folder id>"}}
         (rename + move can be one call; optional  If-Match: <etag>  → 412 if changed since read)
```
- **Scope required:** `root_readwrite` ("Read and write all files and folders") — a **bigger
  permission** than the read-only one we validated, so the admin must re-authorize the app.
- **Permission required:** the service account needs **Editor+** collaboration on the folder.
- A rename **keeps the file id and does not create a new version**, so existing **shared links
  don't break** (links key off id, not name).

Other writes (all need `root_readwrite` + Editor): replace content / new version
(`POST upload.box.com/api/2.0/files/{id}/content`), upload a new file
(`POST upload.box.com/api/2.0/files/content`; **>50 MB must use chunked upload** — relevant for
result artifacts), set **metadata** (`POST /2.0/files/{id}/metadata/global/properties` — free-form
key/values), add a **comment** (`POST /2.0/comments`).

---

## 6. Strong recommendation: don't rename the studio's *source* files

Even though rename is safe for links, mutating source files in a **shared production folder** is
operationally risky: the change is instant and **visible to every collaborator** (activity feed,
notifications) and can break other people's **name-based** references, bookmarks, scripts, or
sync/FTP tooling.

**Prefer non-destructive write-back** for QC results:
- **Metadata** on the file (`global/properties`) — invisible clutter-free, queryable ("qc_status:
  fail, missing: 19").
- **A comment** summarizing pass/fail.
- **Upload result artifacts** (report.csv, missing-lines.wav) to a **separate output folder we own**,
  leaving the source folder effectively read-only.

If we *do* want auto-renaming (e.g. fix a mislabeled stem `JJilip Stomach` → `Jilip Stomach`), gate
it behind an explicit human confirmation, and honor **rate limits** (1000 req/min/user, 240
uploads/min; back off on HTTP 429 + `Retry-After`).

---

## 7. What changes in the code (small, mostly a file-source swap)

- **Input source:** `analyze` reads audio by local path today; add a Box fetch step
  (auth → resolve link → download zip + docx → unzip to temp → hand paths to the existing pipeline).
  The "browser can't read local paths" issue disappears server-side.
- **Async job shape** (Option B, or any long run behind API Gateway): `POST /qc` → job id → poll.
- **Memory discipline:** downsample-to-16k-then-free + one-track-at-a-time (see §2).
- **Frontend:** the React app is already the UI; host the Vite build as a static site (S3 or the box).
- **Secrets:** Box Client Secret lives in an env var / AWS Secrets Manager on the server — **never**
  in the frontend or git (same rule as before).
- The analysis core (parse · VAD · align · loudness · roster · grouped) ports **unchanged**.

---

## 8. Rough cost (light testing load)

| Path | Setup | Monthly |
|---|---|---|
| Serverless (Lambda always-free) | container image + S3 + async | **~$0** (within 400k GB-s/mo ≈ 700 episodes) |
| Single box, start/stop | t3.small/medium | **<$1–$5** if stopped when idle |
| Single box, 24/7 | t3.small (2 GB) | **~$16** (compute + small EBS) |
| Fresh account credits | — | covered by the **$100–$200** for the first ~6 months |

Per episode: **sub-cent to ~$0.01** of compute + transient storage. Box→AWS transfer: **$0**.

---

## 9. Open items (settle with the handout + the boss)

1. **Which AWS account** — reuse a pre-July-2025 account (legacy free t3.micro) or open fresh
   (credit model)? Drives §1.
2. **Single box vs serverless** — match the teammate's handout; recommend t3.small start/stop for a
   quick pilot, Lambda for the durable version.
3. **Box scope** — read-only for fetch is enough to start; only request `root_readwrite` if/when we
   write results back, and even then prefer metadata/output-folder over renaming source.
4. **Admin authorization** — the boss/Box admin must authorize the Custom App (and grant Editor
   collaboration if we ever write). Same blocker as the desktop Box plan.
5. **Auth for the web app** — reuse the Rian login gate in front of the hosted UI.
