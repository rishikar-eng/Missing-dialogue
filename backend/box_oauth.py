"""Headless Box access via OAuth 2.0 refresh tokens — the NO-ADMIN path.

Why this exists: the proper server-to-server auth (CCG) needs a Box admin to authorize
the app, which is still pending. OAuth needs no admin: a human consents ONCE (see
backend/tools/box_oauth_test.py), the refresh token is stored on the server, and this
module renews access forever after — good enough to run hosted while the admin decides.

The two Box facts that shape all of this code:

  1. **Refresh tokens are SINGLE-USE.** Every refresh returns a NEW refresh token and
     kills the old one. If we use a refresh token and fail to persist its replacement,
     the connection is dead and a human must re-consent. So the new token is written
     ATOMICALLY (tmp file + os.replace) before the new access token is handed to anyone,
     and refreshing is serialized under a lock — two threads refreshing with the same
     token would burn it.

  2. **Refresh tokens die after 60 days idle.** Fine for a tool used weekly; documented
     so nobody is surprised after a long holiday.

Bootstrap: run  backend/tools/box_oauth_test.py  once on any machine, then copy the token
file to the server (or run the script there). Config via env:

  BOX_CLIENT_ID / BOX_CLIENT_SECRET   the OAuth app's credentials (never in git)
  DQC_BOX_TOKEN_FILE                  where the rotating refresh token lives
                                      (default: %LOCALAPPDATA%/dialogue-qc/box-refresh-token.txt,
                                       or ~/.dialogue-qc/box-refresh-token.txt elsewhere)
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import httpx

TOKEN_URL = "https://api.box.com/oauth2/token"
_REFRESH_EARLY_S = 300          # renew 5 min before expiry
_TIMEOUT = httpx.Timeout(30.0)


class BoxAuthError(RuntimeError):
    """Auth failure with a SAFE, actionable message (never echoes secrets/tokens)."""


def _default_token_file() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".dialogue-qc"
    if base:
        root = root / "dialogue-qc"
    return root / "box-refresh-token.txt"


def token_file() -> Path:
    return Path(os.environ.get("DQC_BOX_TOKEN_FILE") or _default_token_file())


_LOCK = threading.Lock()
_access: str | None = None
_access_expiry: float = 0.0


def configured() -> bool:
    """Creds present AND a refresh token exists — i.e. worth even trying."""
    return bool(os.environ.get("BOX_CLIENT_ID") and os.environ.get("BOX_CLIENT_SECRET")
                and token_file().is_file())


def status() -> dict[str, object]:
    """For /api/box/status and healthz — never includes token material."""
    tf = token_file()
    return {
        "configured": configured(),
        "creds_present": bool(os.environ.get("BOX_CLIENT_ID") and os.environ.get("BOX_CLIENT_SECRET")),
        "token_file": str(tf),
        "token_file_present": tf.is_file(),
        "access_cached": bool(_access and time.time() < _access_expiry),
    }


def _persist_refresh(new_refresh: str) -> None:
    """Atomic replace; the previous token is kept as .bak for forensics only (it is
    already burned — Box refresh tokens are single-use)."""
    tf = token_file()
    tf.parent.mkdir(parents=True, exist_ok=True)
    tmp = tf.with_suffix(".tmp")
    tmp.write_text(new_refresh, encoding="ascii")
    if tf.is_file():
        try:
            bak = tf.with_suffix(".bak")
            bak.write_text(tf.read_text(encoding="ascii"), encoding="ascii")
        except OSError:
            pass
    os.replace(tmp, tf)


def get_token(force_refresh: bool = False) -> str:
    """Return a valid access token, refreshing (and rotating the refresh token) as
    needed. Raises BoxAuthError with a fix-it message on any dead end."""
    global _access, _access_expiry
    with _LOCK:
        if not force_refresh and _access and time.time() < _access_expiry - 1:
            return _access

        cid = os.environ.get("BOX_CLIENT_ID", "")
        secret = os.environ.get("BOX_CLIENT_SECRET", "")
        if not cid or not secret:
            raise BoxAuthError("Box OAuth is not configured: set BOX_CLIENT_ID and BOX_CLIENT_SECRET")
        tf = token_file()
        if not tf.is_file():
            raise BoxAuthError(
                f"No Box refresh token at {tf}. Run backend/tools/box_oauth_test.py once "
                f"(a human consents in the browser), then retry.")
        refresh = tf.read_text(encoding="ascii").strip()
        if not refresh:
            raise BoxAuthError(f"Box refresh-token file {tf} is empty — re-run the consent flow")

        try:
            r = httpx.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": cid,
                "client_secret": secret,
            }, timeout=_TIMEOUT)
        except httpx.HTTPError as e:
            raise BoxAuthError(f"Could not reach Box to refresh the token: {type(e).__name__}") from None

        if r.status_code != 200:
            # 400 invalid_grant = token expired (60 days idle), revoked, or already used.
            raise BoxAuthError(
                f"Box refused the token refresh (HTTP {r.status_code}). The stored refresh "
                f"token is likely expired or revoked — re-run backend/tools/box_oauth_test.py "
                f"to re-consent.")
        tok = r.json()
        new_refresh = tok.get("refresh_token")
        access = tok.get("access_token")
        if not access or not new_refresh:
            raise BoxAuthError("Box's refresh response was missing tokens — not persisting anything")

        # Persist the NEW refresh token BEFORE returning the access token: the old one is
        # already burned, so losing the new one here would kill the connection.
        _persist_refresh(new_refresh)
        _access = access
        _access_expiry = time.time() + float(tok.get("expires_in") or 3600) - _REFRESH_EARLY_S
        return _access
