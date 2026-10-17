// Rand-Fenster: empfaengt nur Pegel.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("overlay", {
  onLevels: (callback) => ipcRenderer.on("levels", (_event, sample) => callback(sample)),
});
