// Hauptfenster: die Oberflaeche darf nur Pegel an den Stimmen-Rand melden, sonst nichts.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("nieeekiDesktop", {
  isDesktop: true,
  levels: (sample) => ipcRenderer.send("levels", sample),
});
