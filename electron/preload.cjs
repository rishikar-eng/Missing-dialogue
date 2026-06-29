// Exposes a minimal, safe API to the renderer (native pickers + drop-path resolver).
const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  pickFile: (filters) => ipcRenderer.invoke("pick-file", filters),
  pickFolder: () => ipcRenderer.invoke("pick-folder"),
  // Resolve the absolute path of a drag-and-dropped File (Electron 32+ way).
  getPathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file) || null;
    } catch {
      return null;
    }
  },
});
