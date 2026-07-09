// Dev launcher for Electron.
// Two robustness fixes over calling `electron .` directly:
//   1) DELETE ELECTRON_RUN_AS_NODE (can't be merely set empty — Electron treats any
//      defined value, including "", as "run as Node" and never opens a window).
//   2) Started after Vite is up (see the wait-on in package.json, which now targets
//      http://localhost:5173 so it works whether Vite binds IPv4 or IPv6).
delete process.env.ELECTRON_RUN_AS_NODE;
const { spawn } = require("child_process");
const electron = require("electron"); // resolves to the electron.exe path
const child = spawn(electron, ["."], { stdio: "inherit" });
child.on("exit", (code) => process.exit(code ?? 0));
