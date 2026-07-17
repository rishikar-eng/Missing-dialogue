// Electron main process: starts the Python backend, then opens the UI window.
const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const fs = require("fs");
const os = require("os");

// Startup breadcrumb log — a packaged (GUI-subsystem) build prints nothing to a
// console, so without this a crash before the window opens is invisible. Written
// to a stable temp path so we can read it after a failed launch.
const LOG_FILE = path.join(os.tmpdir(), "dqc-startup.log");
function slog(...parts) {
  const line = `[${new Date().toISOString()}] ${parts.join(" ")}\n`;
  try { fs.appendFileSync(LOG_FILE, line); } catch {}
  console.log(...parts);
}
// A throw anywhere in the main process used to kill the app instantly with no
// trace (and orphan the backend). Log it instead of dying silently.
process.on("uncaughtException", (e) => slog("UNCAUGHT", e && e.stack ? e.stack : String(e)));
process.on("unhandledRejection", (e) => slog("UNHANDLED_REJECTION", e && e.stack ? e.stack : String(e)));
slog("=== main start === packaged:", String(app.isPackaged), "resources:", process.resourcesPath || "(dev)");

// Dev: prefer DQC_PYTHON, then a project-local .venv, then `python` on PATH.
function resolveDevPython() {
  if (process.env.DQC_PYTHON) return process.env.DQC_PYTHON;
  const venv =
    process.platform === "win32"
      ? path.join(__dirname, "..", ".venv", "Scripts", "python.exe")
      : path.join(__dirname, "..", ".venv", "bin", "python");
  return fs.existsSync(venv) ? venv : "python";
}

const PORT = 8765;
const isDev = !app.isPackaged;
let backend = null;
let win = null;

function startBackend() {
  const env = { ...process.env, DQC_PORT: String(PORT) };
  if (isDev) {
    // Dev: run from source (.venv / DQC_PYTHON / python).
    const py = resolveDevPython();
    console.log("[backend] dev python:", py);
    backend = spawn(py, ["run.py"], { cwd: path.join(__dirname, ".."), env });
  } else {
    // Packaged: the PyInstaller-frozen backend lives under resources/backend/.
    const exe = path.join(process.resourcesPath, "backend", "dqc-backend.exe");
    slog("[backend] packaged exe:", exe, "exists:", String(fs.existsSync(exe)));
    backend = spawn(exe, [], { env });
  }
  // A spawn 'error' (missing exe, AV lock, EBUSY) emits on the child; with no
  // listener Node re-throws it as a fatal uncaught exception. Handle it.
  backend.on("error", (e) => slog("[backend] SPAWN ERROR", e && e.stack ? e.stack : String(e)));
  backend.stdout && backend.stdout.on("data", (d) => slog("[backend]", d.toString().trim()));
  backend.stderr && backend.stderr.on("data", (d) => slog("[backend]", d.toString().trim()));
  backend.on("exit", (code) => slog("[backend] exited", String(code)));
}

function waitForBackend(onReady, tries = 0) {
  const req = http.get(`http://127.0.0.1:${PORT}/api/healthz`, (res) => {
    res.resume();
    if (res.statusCode === 200) onReady();
    else retry();
  });
  req.on("error", retry);
  function retry() {
    if (tries > 120) {
      console.error("[backend] did not become healthy in time");
      onReady();
      return;
    }
    setTimeout(() => waitForBackend(onReady, tries + 1), 500);
  }
}

function createWindow() {
  win = new BrowserWindow({
    width: 1200,
    height: 840,
    minWidth: 880,
    backgroundColor: "#0c0b0a",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.webContents.on("did-fail-load", (_e, code, desc, url) =>
    slog("[window] did-fail-load", String(code), desc, url));
  win.webContents.on("render-process-gone", (_e, details) =>
    slog("[window] render-process-gone", JSON.stringify(details)));
  if (isDev) {
    win.loadURL("http://localhost:5173");
  } else {
    const index = path.join(__dirname, "..", "dist", "index.html");
    slog("[window] loadFile:", index, "exists:", String(fs.existsSync(index)));
    win.loadFile(index).catch((e) => slog("[window] loadFile FAILED", String(e)));
  }
}

app.whenReady().then(() => {
  slog("app ready — starting backend");
  startBackend();
  waitForBackend(() => { slog("backend healthy (or timed out) — creating window"); createWindow(); });
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
}).catch((e) => slog("whenReady ERROR", e && e.stack ? e.stack : String(e)));

ipcMain.handle("pick-file", async (_e, filters) => {
  const r = await dialog.showOpenDialog(win, {
    properties: ["openFile"],
    filters: filters || [{ name: "Scripts", extensions: ["docx", "srt", "csv", "tsv"] }],
  });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle("pick-folder", async () => {
  const r = await dialog.showOpenDialog(win, { properties: ["openDirectory"] });
  return r.canceled ? null : r.filePaths[0];
});

function shutdown() {
  if (backend) {
    backend.kill();
    backend = null;
  }
}
app.on("before-quit", shutdown);
app.on("window-all-closed", () => {
  shutdown();
  app.quit();
});
