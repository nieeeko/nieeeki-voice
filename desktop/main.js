// nieeeki-Desktop: der Client auf dem PC.
//
// * Hauptfenster mit der bekannten Oberflaeche (localhost:3000), OHNE Hintergrund-Drosselung:
//   Mikrofon und Sprachausgabe laufen weiter, auch wenn Chrome, Amazon & Co. vorne sind.
// * Stimmen-Rand: pro Monitor ein durchsichtiges, nicht anklickbares Fenster ganz oben, das einen
//   welligen Farbrand zeichnet (Nikolaj seeblau, nieeeki indigo).
// * Tray-Symbol mit "Mit Windows starten"; Schliessen versteckt nur ins Tray, das Gespraech bleibt.
// * Startet bei Bedarf Voice-Core und UI selbst (NIEEEKI_MANAGE_SERVICES=off schaltet das ab).
//
// Sicherheit: contextIsolation + sandbox, die Seite bekommt nur die Funktion "Pegel melden".
// Mikrofon nur fuer die eigene Oberflaeche; fremde Seiten oeffnen im normalen Browser.

const { app, BrowserWindow, Menu, Tray, ipcMain, nativeImage, powerSaveBlocker, screen, session, shell } =
  require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const UI_URL = process.env.NIEEEKI_UI_URL || "http://localhost:3000";
const VOICE_CORE_URL = process.env.NIEEEKI_VOICE_CORE_URL || "http://localhost:7860";
const REPO = path.resolve(__dirname, "..");
const UI_ORIGIN = new URL(UI_URL).origin;

// Chromium-Drosselung fuer verdeckte/Hintergrund-Fenster aus (die Ursache der Abbrueche)
app.commandLine.appendSwitch("disable-renderer-backgrounding");
app.commandLine.appendSwitch("disable-background-timer-throttling");
app.commandLine.appendSwitch("disable-backgrounding-occluded-windows");
app.commandLine.appendSwitch("disable-features", "CalculateNativeWinOcclusion,IntensiveWakeUpThrottling");

let mainWin = null;
let tray = null;
let overlays = [];
let borderOn = true;
let quitting = false;
const children = [];

if (!app.requestSingleInstanceLock()) {
  app.quit();
}

// ---------- Dienste (Voice-Core, UI) ----------
function isUp(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(true);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(1500, () => {
      req.destroy();
      resolve(false);
    });
  });
}

function startService(name, command, args, cwd) {
  const logDir = path.join(__dirname, "logs");
  fs.mkdirSync(logDir, { recursive: true });
  const log = fs.openSync(path.join(logDir, `${name}.log`), "a");
  // argv-Liste ohne Shell
  const child = spawn(command, args, {
    cwd,
    windowsHide: true,
    stdio: ["ignore", log, log],
    env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
  });
  child.on("exit", (code) => console.log(`${name} beendet (${code})`));
  children.push(child);
}

async function ensureServices() {
  if (process.env.NIEEEKI_MANAGE_SERVICES === "off") return;
  if (!(await isUp(VOICE_CORE_URL))) {
    const uv = path.join(os.homedir(), ".local", "bin", "uv.exe");
    startService("voice-core", fs.existsSync(uv) ? uv : "uv", ["run", "bot.py"], path.join(REPO, "voice-core"));
  }
  if (!(await isUp(UI_URL))) {
    const nextBin = path.join(REPO, "ui", "node_modules", "next", "dist", "bin", "next");
    startService("ui", "node", [nextBin, "dev"], path.join(REPO, "ui"));
  }
  for (let i = 0; i < 90; i++) {
    if ((await isUp(UI_URL)) && (await isUp(VOICE_CORE_URL))) return;
    await new Promise((r) => setTimeout(r, 1000));
  }
}

// ---------- Fenster ----------
function createMainWindow() {
  mainWin = new BrowserWindow({
    width: 1400,
    height: 900,
    title: "nieeeki",
    icon: iconPath(),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  mainWin.loadURL(UI_URL);

  // Fremde Seiten nie im nieeeki-Fenster: externe Links gehen in den normalen Browser
  mainWin.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWin.webContents.on("will-navigate", (event, url) => {
    if (new URL(url).origin !== UI_ORIGIN) event.preventDefault();
  });

  mainWin.on("close", (event) => {
    if (!quitting) {
      event.preventDefault();
      mainWin.hide();
    }
  });
}

function createOverlays() {
  overlays.forEach((w) => !w.isDestroyed() && w.destroy());
  overlays = screen.getAllDisplays().map((display) => {
    const { x, y, width, height } = display.bounds;
    const win = new BrowserWindow({
      x, y, width, height,
      transparent: true,
      frame: false,
      resizable: false,
      movable: false,
      focusable: false,
      skipTaskbar: true,
      hasShadow: false,
      alwaysOnTop: true,
      show: false,
      webPreferences: {
        preload: path.join(__dirname, "overlay-preload.js"),
        contextIsolation: true,
        sandbox: true,
        backgroundThrottling: false,
      },
    });
    win.setIgnoreMouseEvents(true); // Klicks gehen durch auf darunterliegende Programme
    win.setAlwaysOnTop(true, "screen-saver");
    win.setVisibleOnAllWorkspaces(true);
    win.loadFile(path.join(__dirname, "overlay.html"));
    win.once("ready-to-show", () => borderOn && win.showInactive());
    return win;
  });
}

// Nicht gepackte App: Windows muss electron.exe MIT dem App-Ordner starten
function loginItem() {
  return app.isPackaged ? {} : { path: process.execPath, args: [__dirname] };
}

function iconPath() {
  return path.join(REPO, "ui", "src", "app", "favicon.ico");
}

function createTray() {
  tray = new Tray(nativeImage.createFromPath(iconPath()));
  tray.setToolTip("nieeeki");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "nieeeki oeffnen", click: () => { mainWin.show(); mainWin.focus(); } },
      {
        label: "Stimmen-Rand",
        type: "checkbox",
        checked: borderOn,
        click: (item) => {
          borderOn = item.checked;
          overlays.forEach((w) => (borderOn ? w.showInactive() : w.hide()));
        },
      },
      {
        label: "Mit Windows starten",
        type: "checkbox",
        checked: app.getLoginItemSettings(loginItem()).openAtLogin,
        click: (item) => app.setLoginItemSettings({ ...loginItem(), openAtLogin: item.checked }),
      },
      { type: "separator" },
      { label: "Beenden", click: () => { quitting = true; app.quit(); } },
    ]),
  );
  tray.on("click", () => { mainWin.show(); mainWin.focus(); });
}

// ---------- Pegel von der Oberflaeche an alle Rand-Fenster ----------
ipcMain.on("levels", (event, sample) => {
  if (!mainWin || event.sender !== mainWin.webContents) return; // nur das Hauptfenster darf senden
  const s = {
    user: clamp(Number(sample?.user)),
    bot: clamp(Number(sample?.bot)),
    cut: Boolean(sample?.cut),
  };
  for (const w of overlays) if (!w.isDestroyed()) w.webContents.send("levels", s);
});

function clamp(v) {
  return Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0;
}

app.on("second-instance", () => { if (mainWin) { mainWin.show(); mainWin.focus(); } });

app.whenReady().then(async () => {
  // Mikrofon nur fuer die eigene Oberflaeche
  session.defaultSession.setPermissionRequestHandler((wc, permission, callback, details) => {
    const origin = details.requestingUrl ? new URL(details.requestingUrl).origin : "";
    callback(permission === "media" && origin === UI_ORIGIN);
  });
  powerSaveBlocker.start("prevent-app-suspension");
  createTray();
  await ensureServices();
  createMainWindow();
  createOverlays();
  screen.on("display-added", createOverlays);
  screen.on("display-removed", createOverlays);
  screen.on("display-metrics-changed", createOverlays);
});

app.on("before-quit", () => {
  quitting = true;
  for (const c of children) if (!c.killed) c.kill();
});

app.on("window-all-closed", (e) => e.preventDefault()); // lebt im Tray weiter
