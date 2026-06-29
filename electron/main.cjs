// Electron main process: starts the Python backend, then opens the UI window.
const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const fs = require("fs");

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
    backend = spawn(exe, [], { env });
  }
  backend.stdout.on("data", (d) => console.log("[backend]", d.toString().trim()));
  backend.stderr.on("data", (d) => console.log("[backend]", d.toString().trim()));
  backend.on("exit", (code) => console.log("[backend] exited", code));
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
  if (isDev) win.loadURL("http://localhost:5173");
  else win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
}

app.whenReady().then(() => {
  startBackend();
  waitForBackend(createWindow);
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

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
