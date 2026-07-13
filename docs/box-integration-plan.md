# Dialogue QC — Box Integration Plan (Desktop App)

**Goal:** let the QC tool pull an episode's files **directly from a Box link** — the audio tracks, the script, and the original-language reference — instead of the reviewer manually downloading them from Box first and then browsing to them on disk.

Today a reviewer downloads everything from Box by hand, unzips it, then points the app at the folders. This plan removes that manual step: paste (or pick) a Box link, and the app fetches the files itself.

---

## Why it's worth doing
- **Removes a whole manual step** for every episode (download → unzip → locate → browse).
- **Fewer mistakes** — no more pointing the app at the wrong folder, or missing a track.
- **Stepping stone to automation** — the same Box connection is what the future cloud version uses to run QC automatically the moment files land in Box (see `cloud-hosting-plan.md`). Nothing built here is throwaway.

---

## How it will work

```
Reviewer pastes a Box link (or picks a Box folder)
        │
        ▼
  App backend authenticates to Box  ──▶  resolves the link
        │                                 (folder → lists the tracks inside)
        ▼
  Downloads the audio tracks (+ script, + original audio) to a temp folder
        │
        ▼
  Feeds them into the existing analysis — unchanged from here on
```

The rest of the tool (speech detection, missing/misaligned flags, loudness, voice IDs, reports, the missing-dialogue track) works exactly as it does now — Box just replaces the manual "get the files onto the machine" step.

**Honest note:** this automates the download; it doesn't eliminate it. The files still transfer over the internet (the backend does it for you instead of a browser), so a multi-GB episode still takes as long as a normal Box download.

---

## What Rian needs to set up in Box (one-time)

We register **one Box "Custom App"** in Rian's Box account. Concretely:

1. **Create the app** — Box Developer Console → Create Platform App → Custom App → **Server Authentication (Client Credentials Grant)**.
2. **Credentials produced:** a **Client ID** and a **Client Secret**.
3. **Access level:** *App + Enterprise Access* (so it can read content shared inside the Rian enterprise).
4. **Permission (scope):** *Read all files and folders* (download). Write access only if we later push reports back to Box.
5. **Enterprise ID** — noted from the console.
6. **Admin approval (the one blocker):** a Box **administrator** must authorize the app in the Admin Console (Apps → Custom Apps Manager). Until an admin approves it by its Client ID, it cannot read enterprise content.

**What that means for the ask:** we need (a) the **Client ID / Client Secret / Enterprise ID**, and (b) a **Box admin to authorize the app** once. After that it's automatic.

### Credentials checklist — what to get and hand over

| # | Credential / action | Where it comes from | Who does it |
|---|---|---|---|
| 1 | **Client ID** | Box Dev Console → the Custom App → Configuration tab | Anyone with Box dev access |
| 2 | **Client Secret** | Same Configuration tab (kept secret) | Anyone with Box dev access |
| 3 | **Enterprise ID** | Box Dev Console → General Settings (or Admin Console → Account & Billing) | Anyone with Box dev access |
| 4 | **App access level = App + Enterprise Access** | Custom App → Configuration | Anyone with Box dev access |
| 5 | **Scope: "Read all files and folders"** | Custom App → Application Scopes | Anyone with Box dev access |
| 6 | **Authorize the app** (by its Client ID) | Box **Admin Console** → Apps → Custom Apps Manager | A **Box administrator** (the one blocker) |

Hand the developer items **1, 2, 3** (Client ID, Client Secret, Enterprise ID) and confirm item **6** (admin authorization) is done. Nothing else is needed to switch Box import on.

### Handling the credentials safely
The Client Secret is a real secret (unlike the login-encryption key, which is public by design). It must **not** be shipped inside the installer we send to testers, or committed to the code repo. It will live in a local config/environment on the machine(s) that use Box import. (If we later want every reviewer to use Box import without sharing a secret, we switch to each user logging into their own Box account — see Open Decisions.)

---

## Proof of concept — already validated

Using a temporary Box developer token, we confirmed the full read path works against Rian's actual Box:
- **Authenticated** to the Box API successfully.
- **Resolved a real shared link** — it opened the project folder *"2026_04_09_018 (Kamen Rider)"* (~2 TB, under `JioStar / Rian - External Storage - For Media`).
- **Listed the contents and navigated the structure**: `Processed tracks (STSed) → Malayalam → EP 29`, right down to the delivered file.

So the connection, the shared-link access, and folder browsing all work today. Two practical findings shape the build:
1. **One link is a whole project folder**, organised by language then episode — so the app browses to the right episode (or takes a deeper link) rather than assuming one link = one episode.
2. **Each episode's dub is delivered as a single ZIP** (e.g. a ~175 MB `…E29….zip`) holding the per-speaker tracks — so the app will **download and unzip it automatically**, then analyse. (This is the same zip a reviewer downloads and unzips by hand today.)

## What changes in the app
- **New backend piece** (`box.py` + one endpoint): authenticate to Box, resolve a shared link, list a folder's contents, download the files to a temp folder, hand the paths to the existing analyzer.
- **New UI input:** an "Import from Box" field next to the current file pickers — paste a link (or, later, browse Box folders in-app).
- **No change** to the analysis, reports, or any existing feature.

Rough effort: the Box app setup + admin approval is ~1 day (mostly waiting on admin). The in-app work is a few days. Small, self-contained.

---

## Open decisions (to confirm before building)
1. **What a Box link contains** — is one episode's link a **single folder** holding the audio tracks (and maybe the script + original audio), or are there **separate links** per item? This decides whether the app takes one link or a link per field.
2. **Authentication model** — a **single service-account credential** (simplest; secret configured on the reviewer machines) vs **each reviewer logging into their own Box account** (no shared secret, cleaner security, more work). Recommendation: start with the service account for the internal team; revisit if distribution widens.

---

## Summary
Paste a Box link → the app pulls the episode's files and analyses them, no manual downloading. The only setup Rian needs is a one-time Box app registration + an admin approval, and three credentials handed to the dev. It's a small, contained change that also lays the groundwork for fully automated, on-ingest QC in the cloud.
