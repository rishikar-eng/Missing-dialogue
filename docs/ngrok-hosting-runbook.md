# Dialogue QC — free hosting via ngrok (run-book)

Host the **script + audio QC** from your own machine and share a link with the team. $0,
no admin, no cloud account. Files are read on your PC (from a folder you choose) — nothing
uploads. The one dependency is that **your PC stays on** while people use it.

> This is the no-admin, no-AWS path we landed on after the AWS account stayed blocked.
> When AWS activates, `docs/standalone-hosting-box-plan.md` is the durable upgrade.

---

## One-time setup (~5 min)

1. **Python env** (if not already):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\pip install -r requirements.txt
   ```
2. **ngrok**: install + **update it** (winget's package is stale — 3.3.1 — and accounts
   require >= 3.20.0, which fails with a confusing `ERR_NGROK_121`):
   ```powershell
   winget install Ngrok.Ngrok
   ngrok update                       # -> 3.39.x. Don't skip this.
   ```
   Then create a **free** account at <https://dashboard.ngrok.com>, copy your authtoken
   (Dashboard -> Getting Started -> **Your Authtoken**), and register it once:
   ```powershell
   ngrok config add-authtoken <YOUR_TOKEN>
   ```
3. *(Recommended)* Claim your **one free static domain** (Dashboard → Domains). It makes the
   share link **stable across restarts** — otherwise the URL changes every launch. You'll get
   something like `your-name.ngrok-free.app`.

---

## Every time you host

```powershell
# ephemeral URL (changes each run):
.\host.ps1 -DataRoot "D:\Path\To\Episodes"

# stable URL (with your reserved free domain):
.\host.ps1 -DataRoot "D:\Path\To\Episodes" -Domain your-name.ngrok-free.app
```

`host.ps1` does everything: builds the UI if needed, generates a **persistent API key**
(saved to `%LOCALAPPDATA%\dialogue-qc\host-key.txt` so the link stays valid), starts the
backend with the right env vars, opens the tunnel, and prints (+ copies) the share link:

```
https://your-name.ngrok-free.app/?key=Xy7...           ← send THIS to the team
```

- **`-DataRoot`** is the folder the team can browse — point it at the parent folder that
  holds your episode folders (each with its script + a `tracks/` subfolder). They can only
  see inside it; the rest of your disk is off-limits.
- Teammates open the link, sign in with their Rian account, click **Browse…** to pick the
  script + audio folder from your shared files, and hit **Analyse**.
- **Ctrl+C** stops both the backend and the tunnel.

---

## How teammates use it

1. Open the share link. ngrok shows a one-time "Visit Site" warning → click through.
2. The `?key=` authenticates them and is then stored in their browser and dropped from the
   address bar (so it isn't sitting in their history).
3. Sign in (Rian login), **Browse…** → pick the script file and the tracks folder → **Analyse**.
4. The run happens on your machine (1–5 min); progress streams; the report + playable clips
   + downloadable TXT/CSV appear just like the desktop app.

---

## What's protected (and what isn't)

**Protected** (hardened + adversarially reviewed before shipping):
- **API key** on every request — header for the app, a `dqc_key` cookie for the audio
  players/downloads (so the key never lands in a URL or log after the first link click).
- **File browser** is locked to `-DataRoot` (no traversal, no wider filesystem, refuses to
  run without a key), and FastAPI's `/docs`/`/openapi.json` are disabled when a key is set.
- **One analysis at a time** (memory-bounded); long runs survive tunnel blips.

**Know the limits:**
- 🔴 **Single active session.** Everyone shares the *latest* analysis for playback/tolerance/
  remap. If two people analyse near-simultaneously, the second overwrites the first's review
  state. Fine for a small team taking turns; not true multi-tenant isolation.
- 🔑 **The key is a shared bearer token in the link.** Treat the link like a password. To
  rotate it: delete `%LOCALAPPDATA%\dialogue-qc\host-key.txt` and relaunch — a new key is
  minted and old links stop working.
- 📉 **ngrok free bandwidth (~1 GB/mo).** The QC pulls audio locally, so normal use is tiny
  JSON. But the **full-episode WAV downloads** (missing-lines *timeline* / combined dub) are
  large — a handful can eat the monthly cap. Use them sparingly, or grab those on the host PC.
- 💻 **Your PC is the server** — it must stay awake and online (`powercfg /change
  standby-timeout-ac 0`), and there's no redundancy. A reboot drops in-flight jobs.
- 🌐 The link is public-by-URL. The API key is the only gate — anyone with the link + a Rian
  login gets in. Don't post it anywhere public.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **`ERR_NGROK_121` / "agent version is too old"** | **`ngrok update`.** winget's package ships a stale **3.3.1**; accounts require **>= 3.20.0**. Hit this on first run. |
| "Could not open the ngrok tunnel …" | The launcher now prints ngrok's own error underneath — read that line, not the guess. |
| `ERR_NGROK_4018` / "not authenticated" | `ngrok config add-authtoken <TOKEN>` (step 2) |
| Teammate sees "This server needs an access key" | They opened a bare URL — resend the full `…/?key=…` link |
| Browse button missing | `-DataRoot` not set or unreadable; check the path exists |
| URL changed after restart | Use `-Domain your-name.ngrok-free.app` (claim it free, step 3) |
| "No audio tracks found" | Point the folder picker at the folder that directly contains the `.wav` stems |
| Backend won't start | `.\.venv\Scripts\pip install -r requirements.txt`, then retry |

---

## What changed in the code (for reference)

Additive; the **desktop app is unchanged** when no `DQC_*` env vars are set.

- `backend/jobs.py` — in-process async-job registry (submit → poll), a shared `heavy_slot`
  semaphore bounding concurrent analyses.
- `backend/server.py` — API-key middleware (header / cookie / `?key=`), `/api/jobs/analyze`
  (202 + poll) + `/api/jobs/{id}`, `/api/browse` (root-locked file browser), extended
  `/api/healthz`, `DQC_VAD_WORKERS`, static `dist/` mount, docs disabled under a key, and a
  STATE lock for consistent reads.
- `src/api.ts`, `src/App.tsx` — same-origin API base in hosted mode, key capture (in-memory +
  cookie + localStorage), job-based analyze with blip-tolerant polling, and a server-side
  file-picker modal. `src/screens/LoginScreen.tsx` hides the "skip sign-in" hatch when hosted.

**Env vars** (set by `host.ps1`): `DQC_API_KEY` (required for auth), `DQC_DATA_ROOT` (enables
+ locks browsing), `DQC_VAD_WORKERS` (default 4; host uses 2), `DQC_PORT`,
`DQC_MAX_CONCURRENT_JOBS` (1), `DQC_JOB_TTL_S` (3600).
