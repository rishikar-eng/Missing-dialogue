"""Freeze the FastAPI backend into a single standalone executable with PyInstaller.

Output: backend-dist/dqc-backend.exe  (electron-builder bundles this as a resource).

Run (in the env that has the backend deps + pyinstaller):
    python build_backend.py

soundfile ships its own libsndfile binary; we collect it so the frozen exe has no
external native dependency.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "backend-dist"


def _conda_dll_args() -> list[str]:
    """conda-built Pythons keep C-extension DLLs (libffi for _ctypes, expat for
    pyexpat, openssl, lzma, bz2 ...) in <prefix>/Library/bin, which PyInstaller
    doesn't search — so the frozen exe fails with 'DLL load failed'. Bundle the
    common offenders and add the dir to the search path. No-op on plain CPython."""
    args: list[str] = []
    lib_bin = Path(sys.base_prefix) / "Library" / "bin"
    if not lib_bin.is_dir():
        return args
    args += ["--paths", str(lib_bin)]
    patterns = ("ffi*.dll", "*expat*.dll", "libssl*.dll", "libcrypto*.dll",
                "liblzma.dll", "libbz2.dll", "sqlite3.dll")
    seen: set[str] = set()
    for pat in patterns:
        for p in lib_bin.glob(pat):
            if p.name.lower() not in seen:
                seen.add(p.name.lower())
                args += ["--add-binary", f"{p}{os.pathsep}."]
    return args


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed. Run:  pip install pyinstaller")
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "dqc-backend",
        "--onefile",
        "--noconfirm",
        "--clean",
        "--distpath", str(OUT),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        # uvicorn loads these dynamically; PyInstaller can't see them statically.
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        # soundfile's bundled libsndfile + data
        "--collect-binaries", "soundfile",
        "--collect-data", "soundfile",
        # onnxruntime native libs + the Silero VAD model
        "--collect-all", "onnxruntime",
        # Rian login proxy deps (AES payload encryption + HTTP client) — bundle fully
        # so the frozen backend can reach the auth API.
        "--collect-all", "cryptography",
        "--collect-submodules", "httpx",
        "--collect-submodules", "httpcore",
        "--add-data", f"{ROOT / 'backend' / 'models' / 'silero_vad.onnx'}{os.pathsep}backend/models",
        # ElevenLabs voice bank (per-language voice IDs shown in the character table)
        "--add-data", f"{ROOT / 'backend' / 'data' / 'voice_bank.json'}{os.pathsep}backend/data",
        *_conda_dll_args(),
        str(ROOT / "run.py"),
    ]
    print("Running:", " ".join(cmd))
    rc = subprocess.run(cmd).returncode
    if rc == 0:
        exe = OUT / ("dqc-backend.exe" if sys.platform == "win32" else "dqc-backend")
        print(f"\nBuilt: {exe}  ({exe.stat().st_size / 1e6:.0f} MB)" if exe.exists() else "\nBuild finished")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
