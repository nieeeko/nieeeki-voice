// Hauptfenster: die Oberflaeche bekommt nur diese wenigen Funktionen, keinen Zugriff auf Node/Electron.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("nieeekiDesktop", {
  isDesktop: true,
  levels: (sample) => ipcRenderer.send("levels", sample),
  highlight: (data) => ipcRenderer.send("highlight", data), // Lichtrahmen: wo nieeeki gerade handelt
  // Weckwort: Treffer vom lokalen Lauscher (voice-core/wake.py); liefert eine Abmelde-Funktion
  onWake: (callback) => {
    const handler = (_event, data) => callback({ score: Number(data?.score) || 0 });
    ipcRenderer.on("wake", handler);
    return () => ipcRenderer.removeListener("wake", handler);
  },
  conversation: (active) => ipcRenderer.send("conversation", Boolean(active)), // Lauscher pausieren
});
