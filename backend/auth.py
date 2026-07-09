"""Login proxy to the Rian API (https://api.rian.io).

The desktop app authenticates users against Rian before unlocking. We proxy the
call through this local backend (server-to-server) rather than from the browser,
so: (a) no CORS, (b) credentials never sit in browser JS, (c) one place to point
at a different environment.

Rian specifics (from the VOX STS API reference):
  * Base is https://api.rian.io ; auth lives under /v1 (NOT /api — the /api/... in
    the older doc, and api.rian.com, are placeholders/parked).
  * Request bodies are AES-256-CBC encrypted (PKCS7), Base64, sent as the raw body
    with header `x-encrypted-pl: 1`. The key/IV ship in Rian's own web bundle, so
    they're obfuscation, not secrets. Responses are PLAIN JSON (never encrypted).
  * Field codes: em=email, pw=password. Envelope: {"status": <code>, "data": {...}}
      200  -> success; data.at = "Bearer <JWT>", data.rt = refresh, + profile
      3001 -> OTP required (2FA); resend with gotp=1 to send a code, then otp
      1024 -> wrong password (data.ra = attempts left) ; 1025 -> locked

MVP scope: authentication GATE only — validate the user, hand the frontend the
profile + token; no further authenticated Rian calls, no persistence.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

RIAN_API_BASE = os.environ.get("RIAN_API_BASE", "https://api.rian.io")
RIAN_LOGIN_PATH = os.environ.get("RIAN_LOGIN_PATH", "/v1/Auth/LoginUser")
RIAN_LOGOUT_PATH = os.environ.get("RIAN_LOGOUT_PATH", "/v1/Auth/Logout")
# Key B — API payload encryption (from the VOX API docs; shipped in the web bundle).
_AES_KEY = os.environ.get("RIAN_AES_KEY", "RIAN=CRYPTO=AES256=20221107=$#2@").encode()
_AES_IV = os.environ.get("RIAN_AES_IV", "RIANCRYPTOAES256").encode()
_TIMEOUT = 20.0


def _encrypt(obj: Any) -> str:
    """AES-256-CBC + PKCS7, Base64 — the exact scheme Rian's PayloadMiddleware expects."""
    data = json.dumps(obj, separators=(",", ":")).encode()
    padder = PKCS7(algorithms.AES.block_size).padder()
    data = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def _post_encrypted(path: str, obj: Any, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "x-encrypted-pl": "1", "rf": "w", **(extra_headers or {})}
    with httpx.Client(base_url=RIAN_API_BASE, timeout=_TIMEOUT) as c:
        r = c.post(path, content=_encrypt(obj), headers=headers)  # body = raw Base64 string
    try:
        body = r.json()  # responses are plain JSON
    except Exception:
        body = {"status": r.status_code, "data": None, "message": r.text[:500]}
    body["_http"] = r.status_code
    return body


def login(em: str, pw: str, gotp: int = 0, otp: str | None = None) -> dict[str, Any]:
    """Call Rian LoginUser (encrypted). Returns Rian's JSON envelope verbatim (+ _http)."""
    # The live /v1 app sends just {em, pw}. Only add the 2FA fields when actually
    # doing an OTP step (avoids sending params the endpoint doesn't expect).
    payload: dict[str, Any] = {"em": em, "pw": pw}
    if gotp:
        payload["gotp"] = gotp
    if otp:
        payload["otp"] = otp
    return _post_encrypted(RIAN_LOGIN_PATH, payload)


def logout(rt: str, at: str | None = None) -> dict[str, Any]:
    """Best-effort logout (revoke refresh token)."""
    try:
        return _post_encrypted(RIAN_LOGOUT_PATH, {"rt": rt}, {"Authorization": at} if at else None)
    except Exception:
        return {"status": 200, "data": None}
