"""Fetch files from Box server-to-server for the QC API.

VOX already has a Box OAuth popup that returns a **bearer access token** and lets the
user pick files (their ids). The QC endpoint takes that token + the picked file ids and
downloads the bytes here — the same token works whether it came from a user OAuth flow
(VOX's popup) or a service-account Client-Credentials-Grant (a future headless setup).

Endpoints used (Box Content API):
  GET /2.0/files/{id}?fields=name   -> the original filename (to preserve the extension,
                                       which the script parser + track discovery rely on)
  GET /2.0/files/{id}/content       -> the raw bytes (302 -> dl.boxcloud.com; we follow it)

A shared-link header is added when a link is supplied, so files nested under a shared
folder are reachable without explicit per-file collaboration.

Hardening (all from adversarial review):
  * collisions — two picked files with the same Box name are disambiguated, never
    overwritten (a silent overwrite would drop a dub track -> false "No audio").
  * unsafe names — '.', '..', Windows reserved device names (NUL, CON, …), and empty
    names fall back to a file-id name instead of escaping the dir / hitting a device.
  * size cap — per-file byte limit so a huge id can't fill the temp partition.
  * no secret leak — Box HTTP errors are re-raised WITHOUT the signed dl.boxcloud URL
    (which carries a token); only the status code + file id are surfaced.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx

_API = "https://api.box.com/2.0"
_TIMEOUT = httpx.Timeout(30.0, read=600.0)  # long read for multi-hundred-MB stems
# Per-file download cap so a malicious/huge id can't exhaust the temp partition.
_MAX_BYTES = int(os.environ.get("DQC_BOX_MAX_FILE_MB", "3072")) * 1024 * 1024  # 3 GB default

# Basenames that must never be used verbatim as a destination file.
_WIN_RESERVED = {"con", "prn", "aux", "nul",
                 *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class BoxFetchError(RuntimeError):
    """A Box fetch failure with a SAFE message (never carries a signed URL / token)."""


def _headers(token: str, shared_link: str | None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    if shared_link:
        # Box wants the raw shared-link URL here; scopes access to that link's tree.
        h["BoxApi"] = f"shared_link={shared_link}"
    return h


def _safe_name(name: str | None, fallback: str) -> str:
    """Basename only, path/traversal/reserved-name/empty -> fallback. Never escapes the dir."""
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    stem = name.split(".")[0].lower()
    if name in {"", ".", ".."} or stem in _WIN_RESERVED:
        return fallback
    return name


def _uniquify(name: str, used: set[str]) -> str:
    """Return a name not in `used` (case-insensitive), preserving the extension, so two
    same-named Box files land as 'x.wav' and 'x (2).wav' instead of overwriting."""
    if name.lower() not in used:
        used.add(name.lower())
        return name
    stem, dot, ext = name.rpartition(".")
    base, suffix = (stem, f".{ext}") if dot else (name, "")
    i = 2
    while f"{base} ({i}){suffix}".lower() in used:
        i += 1
    out = f"{base} ({i}){suffix}"
    used.add(out.lower())
    return out


def _raise_status(r: httpx.Response, file_id: str) -> None:
    """raise_for_status but sanitized — the raw HTTPStatusError text embeds the signed
    dl.boxcloud.com URL (a token), so we replace it with just the status + file id."""
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise BoxFetchError(f"Box returned HTTP {e.response.status_code} for file {file_id}") from None


def _fetch_name(client: httpx.Client, file_id: str, headers: dict[str, str]) -> str:
    r = client.get(f"{_API}/files/{file_id}", params={"fields": "name"}, headers=headers)
    _raise_status(r, file_id)
    return _safe_name(r.json().get("name", ""), f"{file_id}.bin")


def _stream_to(client: httpx.Client, file_id: str, out: Path, headers: dict[str, str]) -> Path:
    total = 0
    with client.stream("GET", f"{_API}/files/{file_id}/content", headers=headers) as r:
        _raise_status(r, file_id)
        with open(out, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1 << 20):
                total += len(chunk)
                if total > _MAX_BYTES:
                    raise BoxFetchError(
                        f"Box file {file_id} exceeds the {_MAX_BYTES // (1024 * 1024)} MB limit")
                f.write(chunk)
    return out


def download_file(
    token: str,
    file_id: str,
    dest_dir: str | Path,
    *,
    shared_link: str | None = None,
    name: str | None = None,
) -> Path:
    """Download one Box file into dest_dir, preserving its original name/extension.
    Streams to disk so a large WAV never sits fully in memory. Returns the local path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = _headers(token, shared_link)
    with httpx.Client(follow_redirects=True, timeout=_TIMEOUT) as client:
        fname = _safe_name(name, f"{file_id}.bin") if name else _fetch_name(client, file_id, headers)
        return _stream_to(client, file_id, dest_dir / fname, headers)


def list_folder(
    token: str,
    folder_id: str = "0",
    *,
    shared_link: str | None = None,
) -> dict[str, object]:
    """One level of a Box folder: {"folders": [{id,name}], "files": [{id,name,size}]}.

    Drives the hosted UI's Box picker (folder_id "0" = the account root). Paginates with
    usemarker so a 1000+-item episode folder isn't silently truncated.
    """
    headers = _headers(token, shared_link)
    folders: list[dict[str, object]] = []
    files: list[dict[str, object]] = []
    params: dict[str, str] = {"limit": "1000", "usemarker": "true",
                              "fields": "id,type,name,size"}
    with httpx.Client(follow_redirects=True, timeout=_TIMEOUT) as client:
        while True:
            r = client.get(f"{_API}/folders/{folder_id}/items", params=params, headers=headers)
            _raise_status(r, folder_id)
            data = r.json()
            for e in data.get("entries", []):
                if e.get("type") == "folder":
                    folders.append({"id": e.get("id"), "name": e.get("name")})
                elif e.get("type") == "file":
                    files.append({"id": e.get("id"), "name": e.get("name"),
                                  "size": e.get("size") or 0})
            marker = data.get("next_marker")
            if not marker:
                break
            params["marker"] = marker
    return {"id": folder_id, "folders": folders, "files": files}


def download_files(
    token: str,
    file_ids: list[str],
    dest_dir: str | Path,
    *,
    shared_link: str | None = None,
) -> list[Path]:
    """Download several Box files into one directory (e.g. the per-speaker dub tracks),
    disambiguating any same-named files so none is silently overwritten."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = _headers(token, shared_link)
    used: set[str] = set()
    out: list[Path] = []
    with httpx.Client(follow_redirects=True, timeout=_TIMEOUT) as client:
        for fid in file_ids:
            fname = _uniquify(_fetch_name(client, fid, headers), used)
            out.append(_stream_to(client, fid, dest_dir / fname, headers))
    return out
