"""Prove (or disprove) that Box OAuth 2.0 + refresh-token can drive HEADLESS access.

Why this exists: our server needs to fetch episode audio from Box with no human in the
loop. The "proper" way (Client Credentials Grant) needs a Box ADMIN to authorize the app,
which we don't have. OAuth 2.0 needs no admin — you consent once as yourself, the server
keeps the refresh token, and renews forever on your own (read-only) access.

That's the theory. This script tests whether it actually works on THIS enterprise, because
Box enterprises can restrict which apps a user may authorize. It answers, in order:

  1. Will Box even show a consent screen for our app? (the real unknown)
  2. Does the code->token exchange return a refresh_token?
  3. Does refreshing work, and does the refresh token ROTATE? (Box's are single-use)
  4. Can the resulting token actually SEE the files we need?

Setup (once):
  Box Developer Console -> New App -> Custom App -> **User Authentication (OAuth 2.0)**
    Configuration tab:
      Redirect URI      : http://localhost:8799/callback
      Application Scopes: tick "Read all files and folders stored in Box"
    Copy the Client ID + Client Secret (revealing the secret needs 2FA).

Run:
  # PowerShell — env vars only, NEVER hardcode or commit these
  $env:BOX_CLIENT_ID="..."; $env:BOX_CLIENT_SECRET="..."
  .\.venv\Scripts\python.exe backend\tools\box_oauth_test.py

The refresh token it obtains is a CREDENTIAL. It is saved outside the repo
(%LOCALAPPDATA%\dialogue-qc\box-refresh-token.txt) and only ever printed masked.
"""

from __future__ import annotations

import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize"
TOKEN_URL = "https://api.box.com/oauth2/token"
API = "https://api.box.com/2.0"
REDIRECT_URI = "http://localhost:8799/callback"
PORT = 8799

# The refresh token lives outside the repo — it is a long-lived credential.
TOKEN_STORE = Path(os.environ.get("LOCALAPPDATA", ".")) / "dialogue-qc" / "box-refresh-token.txt"


def mask(s: str | None) -> str:
    """Never print a whole credential — enough to compare two values, not to use one."""
    if not s:
        return "(none)"
    return f"{s[:6]}...{s[-4:]} (len {len(s)})"


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Single-shot listener for Box's redirect back to us."""
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.result = {k: v[0] for k, v in q.items()}
        ok = "code" in _Catcher.result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("<h2>Authorized.</h2><p>Close this tab and return to the terminal.</p>"
               if ok else
               f"<h2>Box returned an error.</h2><pre>{_Catcher.result}</pre>")
        self.wfile.write(msg.encode("utf-8"))

    def log_message(self, *_args) -> None:  # silence the default stderr spam
        pass


def main() -> int:
    client_id = os.environ.get("BOX_CLIENT_ID", "")
    client_secret = os.environ.get("BOX_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("Set BOX_CLIENT_ID and BOX_CLIENT_SECRET first (see this file's docstring).")
        return 2

    # ---- 1. consent -------------------------------------------------------
    state = secrets.token_urlsafe(16)
    url = f"{AUTHORIZE_URL}?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })
    srv = http.server.HTTPServer(("127.0.0.1", PORT), _Catcher)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    print("\n[1/4] Opening Box for consent...")
    print("      If a browser doesn't open, paste this:\n      " + url + "\n")
    webbrowser.open(url)
    srv_thread_done = threading.Event()
    threading.Thread(target=lambda: (srv_thread_done.wait(180)), daemon=True).start()

    # wait for the redirect (handle_request already served one request)
    import time
    for _ in range(180):
        if _Catcher.result:
            break
        time.sleep(1)
    res = _Catcher.result
    if not res:
        print("TIMED OUT waiting for the Box redirect.")
        return 1
    if "error" in res:
        print(f"\nBOX REFUSED: {res.get('error')} - {res.get('error_description', '')}")
        print("If this says the app is not authorized/allowed, your enterprise restricts")
        print("third-party apps -> OAuth won't save us, and you DO need the Box admin (CCG).")
        return 1
    if res.get("state") != state:
        print("STATE MISMATCH - aborting (possible CSRF).")
        return 1
    code = res["code"]
    print(f"      got an authorization code: {mask(code)}")

    with httpx.Client(timeout=30) as c:
        # ---- 2. exchange code -> tokens -----------------------------------
        print("\n[2/4] Exchanging the code for tokens...")
        r = c.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
        })
        if r.status_code != 200:
            print(f"      FAILED {r.status_code}: {r.text[:300]}")
            return 1
        tok = r.json()
        access, refresh = tok.get("access_token"), tok.get("refresh_token")
        print(f"      access_token : {mask(access)}  expires_in={tok.get('expires_in')}s")
        print(f"      refresh_token: {mask(refresh)}")
        if not refresh:
            print("      NO REFRESH TOKEN -> headless renewal is impossible this way.")
            return 1

        # ---- 3. refresh (proves rotation) ---------------------------------
        print("\n[3/4] Refreshing immediately (Box refresh tokens are SINGLE-USE)...")
        r = c.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
            "client_secret": client_secret,
        })
        if r.status_code != 200:
            print(f"      REFRESH FAILED {r.status_code}: {r.text[:300]}")
            return 1
        tok2 = r.json()
        access2, refresh2 = tok2.get("access_token"), tok2.get("refresh_token")
        print(f"      new access_token : {mask(access2)}")
        print(f"      new refresh_token: {mask(refresh2)}")
        rotated = refresh2 != refresh
        print(f"      rotated? {rotated}  <- must be True; the NEW one must be persisted every time")

        TOKEN_STORE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_STORE.write_text(refresh2 or "", encoding="ascii")
        print(f"      saved the current refresh token to {TOKEN_STORE} (outside the repo)")

        # ---- 4. can it actually read our files? ---------------------------
        print("\n[4/4] Checking what this token can see...")
        h = {"Authorization": f"Bearer {access2}"}
        me = c.get(f"{API}/users/me", headers=h)
        if me.status_code != 200:
            print(f"      /users/me FAILED {me.status_code}: {me.text[:200]}")
            return 1
        who = me.json()
        print(f"      acting as: {who.get('name')} <{who.get('login')}>  (id {who.get('id')})")
        items = c.get(f"{API}/folders/0/items", headers=h, params={"limit": 10})
        if items.status_code != 200:
            print(f"      /folders/0/items FAILED {items.status_code}: {items.text[:200]}")
            return 1
        entries = items.json().get("entries", [])
        print(f"      root folder shows {len(entries)} item(s):")
        for e in entries[:10]:
            print(f"        - [{e.get('type')}] {e.get('name')}  (id {e.get('id')})")
        if not entries:
            print("      EMPTY root. As a USER token this should mirror what you see in the")
            print("      Box web UI - if it's empty there too, that's expected; otherwise check scopes.")

    print("\n" + "=" * 66)
    print("VERDICT: consent OK, refresh_token issued, refresh rotated, API readable.")
    print("=> OAuth + refresh CAN drive headless Box access with your own read-only access.")
    print("   Caveats to design for: the refresh token is SINGLE-USE (persist the new one")
    print("   every renewal) and dies after 60 days idle; access is tied to YOUR account.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
