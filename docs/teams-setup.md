# Teams testing setup — QC chat (your account only)

The QC chat agent is **live on the server**. This page is the ~10-minute Teams-side wiring so
you can talk to it from a Teams channel. Two options — pick one. Everything stays in a team
**you own**; no one else is added.

- **Server endpoint (both options use the same brain):** `https://13-205-42-228.sslip.io`
- **Try it now (no Teams):** it already works over HTTPS — e.g. from a terminal:
  ```
  curl -s -X POST -H "X-API-Key: <DQC_API_KEY>" -H "Content-Type: application/json" \
    -d '{"message":"QC episode 42 of Gavv"}' \
    https://13-205-42-228.sslip.io/api/agent/chat
  ```
  (the key is in the server `.env`; the link file on your Desktop has it too.)

---

## Option A — Outgoing Webhook (quickest, no premium license)

Best for a fast solo test. One limitation: **Teams expects a reply within ~5 seconds.**
"Run this episode" and status checks return instantly; a first *availability* check can
occasionally brush that 5 s (Box + model round-trip) and show "couldn't reach the app" even
though the server finished — just resend. If that annoys you, use Option B.

**1. Create the webhook (in a team you own):**
- Teams → the team's **⋯ (More options)** → **Manage team** → **Apps** tab → **Create an
  outgoing webhook** (bottom of the page).
- **Name:** `QC` (this is what you @mention)
- **Callback URL:** `https://13-205-42-228.sslip.io/api/agent/teams`
- **Description:** "Dialogue QC assistant"
- Click **Create.** Teams shows a **security token** — copy it.

**2. Give the server that token** (it verifies every request's HMAC with it). Either paste it
into a file and tell me, or set it yourself:
```
ssh -i "C:\Users\Rishi\Desktop\dialogue-qc\dialogue-qc-key.pem" ubuntu@13.205.42.228
sudo sed -i '/^DQC_TEAMS_SECRET=/d' /home/ubuntu/app/.env
echo 'DQC_TEAMS_SECRET=<paste-the-webhook-token>' | sudo tee -a /home/ubuntu/app/.env
sudo systemctl restart dialogue-qc
```

**3. Chat.** In any channel of that team:
```
@QC QC episode 42 of Gavv
```
The bot replies with availability and asks you to confirm; reply `@QC yes, run it`, then
`@QC is it done?` — when finished it gives the per-language missing/extra counts and a
download link. Each channel thread is its own conversation (kept in context server-side).

---

## Option B — Power Automate flow (most reliable, needs the HTTP action)

No 5-second limit — the flow waits for the response, so availability never times out. Needs
the **HTTP** action (Power Automate premium, included in most M365 business plans).

**Build one flow** (Power Automate → Create → Automated cloud flow):

1. **Trigger:** Teams — *"When a new channel message is added"* (pick your team + channel).
   *(or "When I am @mentioned" if you prefer to summon it.)*
2. **Action — HTTP** (POST):
   - **URI:** `https://13-205-42-228.sslip.io/api/agent/chat`
   - **Headers:** `X-API-Key: <DQC_API_KEY>` and `Content-Type: application/json`
   - **Body:**
     ```json
     {
       "message": "@{triggerBody()?['body/plainTextContent']}",
       "session_id": "@{triggerBody()?['conversation/id']}"
     }
     ```
     (use the dynamic-content pickers for the message text and the conversation/thread id —
     field names vary slightly by trigger; the message text and a stable thread id are all
     that matter.)
3. **Action — Teams "Reply with a message in a channel"** (or "Post message"):
   - Post: `@{body('HTTP')?['reply']}` back into the same channel/thread.

Save, then post in the channel: `QC episode 42 of Gavv`.

---

## What you can say (either option)

- `check what's available for episode 42 of Gavv`
- `QC episode 41` *(series inferred — only Gavv is registered)*
- `run it` / `yes` *(after an availability check, to start QC)*
- `just Tamil and Telugu for 43` *(a language subset)*
- `is it done?` / `status?` → when finished: per-language missing/extra + a **download link**
  for `EP{NN}_QC.zip` (workbook + missing-audio). The link needs the API key (your flow/session
  already has it); opening it in a browser that has the `?key=` share cookie also works.

## Notes
- Series scope is **Kamen Rider Gavv** for now (the registry has one entry). Ask about another
  show and it replies "which series?" until that show's Box folders are added to
  `backend/series_registry.json`.
- Restarting the server (a deploy) forgets in-flight jobs and chat sessions — a running QC job
  is lost and you'd re-run; harmless for testing.
- The character-list check currently shows "not delivered" because its Box location isn't in
  the registry yet (drop its folder-id/file-id into `series_registry.json` to enable it).
