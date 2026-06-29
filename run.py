"""Entry point for the Dialogue-QC backend.

Run from the repo root:  python run.py
Electron launches this (or the PyInstaller-frozen exe) on app start.
The port can be overridden with the DQC_PORT env var.
"""

from __future__ import annotations

import os

import uvicorn

from backend.server import app

if __name__ == "__main__":
    port = int(os.environ.get("DQC_PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
