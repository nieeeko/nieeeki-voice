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

const { app, BrowserWindow, Menu, Tray, dialog, ipcMain, nativeImage, powerSaveBlocker, screen, session, shell } =
  require("electron");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const UI_URL = process.env.NIEEEKI_UI_URL || "http://localhost:3000";
const VOICE_CORE_URL = process.env.NIEEEKI_VOICE_CORE_URL || "http://localhost:7860";
const UI_ORIGIN = new URL(UI_URL).origin;
const CONFIG_FILE = path.join(app.getPath("userData"), "config.json");
let REPO = null; // Projektordner mit voice-core/ und ui/, siehe resolveRepo()

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

// ---------- Projektordner ----------
// Entwicklung: der Ordner ueber desktop/. Installiert: NIEEEKI_REPO, sonst die gemerkte Wahl, sonst
// ~/Projekte/nieeeki-voice, sonst einmal nachfragen. Gueltig ist nur ein Ordner mit voice-core/bot.py.
function isRepo(dir) {
  return Boolean(dir) && fs.existsSync(path.join(dir, "voice-core", "bot.py"));
}

function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8"));
  } catch {
    return {};
  }
}

function writeConfig(patch) {
  fs.mkdirSync(path.dirname(CONFIG_FILE), { recursive: true });
  fs.writeFileSync(CONFIG_FILE, JSON.stringify({ ...readConfig(), ...patch }, null, 2));
}

function resolveRepo() {
  const candidates = app.isPackaged
    ? [process.env.NIEEEKI_REPO, readConfig().repo, path.join(os.homedir(), "Projekte", "nieeeki-voice")]
    : [process.env.NIEEEKI_REPO, path.resolve(__dirname, "..")];
  const found = candidates.find(isRepo);
  if (found || !app.isPackaged) return found || null;
  const picked = dialog.showOpenDialogSync({
    title: "nieeeki: Projektordner nieeeki-voice waehlen",
    properties: ["openDirectory"],
  })?.[0];
  if (!isRepo(picked)) {
    dialog.showErrorBox(
      "nieeeki",
      "Im gewaehlten Ordner fehlt voice-core/bot.py. nieeeki startet Voice-Core und Oberflaeche darum nicht selbst.",
    );
    return null;
  }
  writeConfig({ repo: picked });
  return picked;
}

// Logs und PIDs immer in desktop/logs des Projekts: Entwicklungs- und installierte App sehen dieselben
// Dienste (Neustart aus dem Tray trifft auch einen Voice-Core, den die andere Variante gestartet hat).
function logDir() {
  return REPO ? path.join(REPO, "desktop", "logs") : path.join(app.getPath("userData"), "logs");
}

// ---------- Dienste (Voice-Core, UI) ----------
function uvCommand() {
  const uv = path.join(os.homedir(), ".local", "bin", "uv.exe");
  return fs.existsSync(uv) ? uv : "uv";
}

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
  const dir = logDir();
  fs.mkdirSync(dir, { recursive: true });
  const log = fs.openSync(path.join(dir, `${name}.log`), "a");
  // argv-Liste ohne Shell
  const child = spawn(command, args, {
    cwd,
    windowsHide: true,
    stdio: ["ignore", log, log],
    env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
  });
  child.on("exit", (code) => console.log(`${name} beendet (${code})`));
  children.push(child);
  // PID merken: so kann ein naechster Start einen verwaisten Dienst sicher beenden
  fs.writeFileSync(path.join(dir, `${name}.pid`), String(child.pid));
}

// Ganzen Prozessbaum beenden: "uv run" startet Python als Kindprozess; nur uv zu beenden liess den
// alten Voice-Core weiterlaufen (Port 7860 belegt, neue Version wurde nie geladen).
function killTree(pid) {
  if (!pid) return;
  spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true });
}

function killStale(name) {
  const pidFile = path.join(logDir(), `${name}.pid`);
  if (!fs.existsSync(pidFile)) return;
  killTree(Number(fs.readFileSync(pidFile, "utf8")));
  fs.rmSync(pidFile, { force: true });
}

function stopServices() {
  for (const c of children.splice(0)) killTree(c.pid);
  for (const name of ["voice-core", "ui"]) fs.rmSync(path.join(logDir(), `${name}.pid`), { force: true });
}

async function ensureServices() {
  if (process.env.NIEEEKI_MANAGE_SERVICES === "off" || !REPO) return;
  // Reste eines frueheren Laufs dieser App beenden, damit immer der aktuelle Code laeuft
  killStale("voice-core");
  await new Promise((r) => setTimeout(r, 800));
  if (!(await isUp(VOICE_CORE_URL))) {
    startService("voice-core", uvCommand(), ["run", "bot.py"], path.join(REPO, "voice-core"));
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

// Schleierbreite in geraeteunabhaengigen Pixeln: 96 DIP entsprechen nominal 2,54 cm (Windows rechnet
// Skalierung schon ein). Standard 4,55 cm (19.10.: 30 % schmaler als die ersten 6,5 cm);
// NIEEEKI_VEIL_CM aendert das.
function veilWidthDip() {
  const cm = Math.min(12, Math.max(2, Number(process.env.NIEEEKI_VEIL_CM) || 4.55));
  return Math.round((cm * 96) / 2.54);
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
    win.nieeekiBounds = display.bounds; // fuer den Lichtrahmen: welcher Ausschnitt gehoert hierher
    win.setIgnoreMouseEvents(true); // Klicks gehen durch auf darunterliegende Programme
    win.setAlwaysOnTop(true, "screen-saver");
    win.setVisibleOnAllWorkspaces(true);
    win.loadFile(path.join(__dirname, "overlay.html"), { query: { veil: String(veilWidthDip()) } });
    win.once("ready-to-show", () => borderOn && !fullscreenApp && win.showInactive());
    return win;
  });
}

// ---------- Vollbild-Programme (Spiele, Videos, Praesentationen) ----------
// Bei Vollbild stoert der Schleier und kann Spiele aus dem schnellen Darstellungsmodus druecken.
// Windows meldet den Zustand ueber SHQueryUserNotificationState: 2 Vollbild-Programm, 3 Direct3D-
// Vollbild (Spiele), 4 Praesentation. Electron kann das nicht selbst fragen, darum ein kleiner
// PowerShell-Waechter; er beendet sich, sobald die Desktop-App weg ist.
let fullscreenApp = false;
let fullscreenWatcher = null;
const FULLSCREEN_STATES = new Set([2, 3, 4]);

function watchFullscreen() {
  const script = [
    "$ProgressPreference = 'SilentlyContinue'",
    "Add-Type -Namespace Nieeeki -Name Shell -MemberDefinition '[DllImport(\"shell32.dll\")] public static extern int SHQueryUserNotificationState(out int state);'",
    `while (Get-Process -Id ${process.pid} -ErrorAction SilentlyContinue) { $s = 0; [void][Nieeeki.Shell]::SHQueryUserNotificationState([ref]$s); [Console]::Out.WriteLine($s); Start-Sleep -Milliseconds 1500 }`,
  ].join("; ");
  const encoded = Buffer.from(script, "utf16le").toString("base64"); // ohne Anfuehrungszeichen-Probleme
  fullscreenWatcher = spawn("powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], {
    windowsHide: true,
    stdio: ["ignore", "pipe", "ignore"],
  });
  let buffer = "";
  let last = null;
  fullscreenWatcher.stdout.on("data", (chunk) => {
    buffer += chunk.toString();
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop();
    for (const line of lines) {
      const busy = FULLSCREEN_STATES.has(Number(line.trim()));
      // erst bei zwei gleichen Abfragen hintereinander umschalten: kein Flackern
      if (busy === last && busy !== fullscreenApp) {
        fullscreenApp = busy;
        updateOverlayVisibility();
      }
      last = busy;
    }
  });
  fullscreenWatcher.on("exit", () => {
    fullscreenWatcher = null;
    if (!quitting) setTimeout(watchFullscreen, 5000);
  });
}

function updateOverlayVisibility() {
  const show = borderOn && !fullscreenApp;
  overlays.forEach((w) => !w.isDestroyed() && (show ? w.showInactive() : w.hide()));
}

// ---------- Weckwort "nieeeki" ----------
// voice-core/wake.py lauscht lokal am Mikrofon, kein Ton verlaesst den PC. Ein Treffer geht an die
// Oberflaeche, die dann das Gespraech aufbaut; solange es laeuft, pausiert der Lauscher (Mikrofon frei).
let wakeProc = null;
let wakeRestart = null;
let conversationActive = false;

function wakeModelPath() {
  return process.env.NIEEEKI_WAKE_MODEL || (REPO ? path.join(REPO, "voice-core", "models", "nieeeki.onnx") : null);
}

function wakeAvailable() {
  const model = wakeModelPath();
  return Boolean(REPO && model && fs.existsSync(model));
}

function wakeEnabled() {
  return readConfig().weckwort !== false; // Standard an; der Tray-Schalter merkt sich "aus"
}

function startWake() {
  clearTimeout(wakeRestart);
  if (wakeProc || quitting || !wakeAvailable() || !wakeEnabled()) return;
  const dir = logDir();
  fs.mkdirSync(dir, { recursive: true });
  const log = fs.openSync(path.join(dir, "wake.log"), "a");
  const proc = spawn(uvCommand(), ["run", "wake.py"], {
    cwd: path.join(REPO, "voice-core"),
    windowsHide: true,
    stdio: ["pipe", "pipe", log],
    env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
  });
  fs.closeSync(log); // der Kindprozess hat seine eigene Kopie
  wakeProc = proc;
  proc.stdin.on("error", () => {}); // Lauscher schon weg: Schreiben ins Leere ist egal
  if (conversationActive) proc.stdin.write("pause\n");
  let buffer = "";
  proc.stdout.on("data", (chunk) => {
    buffer += chunk.toString();
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop();
    for (const line of lines) onWakeLine(line);
  });
  proc.on("exit", (code) => {
    if (wakeProc !== proc) return; // absichtlich beendet oder schon ersetzt
    wakeProc = null;
    // 2 = kein Modell: nicht neu starten. Sonst nach 5 s wieder (z. B. Mikrofon kurz weg)
    if (!quitting && code !== 2) wakeRestart = setTimeout(startWake, 5000);
  });
}

function stopWake() {
  clearTimeout(wakeRestart);
  const proc = wakeProc;
  wakeProc = null;
  if (proc) killTree(proc.pid); // "uv run" startet Python als Kindprozess
}

function onWakeLine(line) {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  if (msg?.fehler) console.log(`Weckwort: ${msg.fehler}`);
  if (!msg?.weckwort || conversationActive || !mainWin || mainWin.isDestroyed()) return;
  mainWin.webContents.send("wake", { score: Number(msg.score) || 0 });
  // Sofort sichtbar: der Schleier leuchtet auf, bevor die Verbindung steht und "Ja?" kommt
  for (const w of overlays) if (!w.isDestroyed() && w.isVisible()) w.webContents.send("wake");
}

// Die Oberflaeche meldet, ob gerade ein Gespraech laeuft: dann pausiert der Lauscher
ipcMain.on("conversation", (event, active) => {
  if (!mainWin || event.sender !== mainWin.webContents) return; // nur das Hauptfenster darf senden
  conversationActive = Boolean(active);
  wakeProc?.stdin.write(conversationActive ? "pause\n" : "weiter\n");
});

// Nicht gepackte App: Windows muss electron.exe MIT dem App-Ordner starten
function loginItem() {
  return app.isPackaged ? {} : { path: process.execPath, args: [__dirname] };
}

// Umstieg auf die installierte App: einen Autostart der Entwicklungs-App (electron.exe + desktop/)
// uebernimmt sie, sonst starten beim Anmelden beide (doppelter Schleier, doppelte Dienste).
function adoptDevAutostart() {
  // Probestart aus desktop/dist im Projekt: Autostart nicht auf den Build-Ordner umbiegen
  if (!app.isPackaged || !REPO || process.execPath.toLowerCase().startsWith(REPO.toLowerCase())) return;
  const dev = {
    path: path.join(REPO, "desktop", "node_modules", "electron", "dist", "electron.exe"),
    args: [path.join(REPO, "desktop")],
  };
  const items = (app.getLoginItemSettings(dev).launchItems || []).filter(
    (item) => item.path.toLowerCase() === dev.path.toLowerCase(),
  );
  if (!items.length) return;
  for (const item of items) app.setLoginItemSettings({ ...dev, name: item.name, openAtLogin: false });
  app.setLoginItemSettings({ openAtLogin: true });
}

function raisePriority() {
  for (const m of app.getAppMetrics()) {
    try {
      os.setPriority(m.pid, os.constants.priority.PRIORITY_ABOVE_NORMAL);
    } catch {
      // Prozess schon weg oder keine Rechte: egal
    }
  }
}

// Installiert liegt das Symbol neben app.asar (extraResources), sonst neben main.js
function iconPath() {
  return app.isPackaged ? path.join(process.resourcesPath, "icon.ico") : path.join(__dirname, "icon.ico");
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
          updateOverlayVisibility();
        },
      },
      {
        label: wakeAvailable() ? 'Weckwort "nieeeki"' : "Weckwort (Modell fehlt)",
        type: "checkbox",
        enabled: wakeAvailable(),
        checked: wakeAvailable() && wakeEnabled(),
        click: (item) => {
          writeConfig({ weckwort: item.checked });
          if (item.checked) startWake();
          else stopWake();
        },
      },
      {
        label: "Mit Windows starten",
        type: "checkbox",
        checked: app.getLoginItemSettings(loginItem()).openAtLogin,
        click: (item) => app.setLoginItemSettings({ ...loginItem(), openAtLogin: item.checked }),
      },
      { type: "separator" },
      {
        label: "Voice-Core neu starten",
        click: async () => {
          killStale("voice-core");
          await ensureServices();
          mainWin.reload();
        },
      },
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
    speaker: Math.max(0, Math.min(7, Math.trunc(Number(sample?.speaker)) || 0)),
  };
  for (const w of overlays) if (!w.isDestroyed()) w.webContents.send("levels", s);
});

// ---------- Lichtrahmen: wo nieeeki gerade handelt ----------
// Kommt von der Oberflaeche (Host-Ergebnis: Fensterrechteck und Klickpunkt in Bildschirmpixeln).
// Electron rechnet in geraeteunabhaengige Pixel um; jedes Rand-Fenster bekommt nur seinen Ausschnitt.
ipcMain.on("highlight", (event, data) => {
  if (!mainWin || event.sender !== mainWin.webContents) return; // nur das Hauptfenster darf senden
  const ints = (v, n) => (Array.isArray(v) && v.length === n && v.every(Number.isInteger) ? v : null);
  const rect = ints(data?.rahmen, 4);
  const point = ints(data?.punkt, 2);
  if (!rect && !point) return;
  const dip = (x, y) => screen.screenToDipPoint({ x, y });
  const corners = rect && [dip(rect[0], rect[1]), dip(rect[2], rect[3])];
  const tap = point && dip(point[0], point[1]);
  for (const w of overlays) {
    if (w.isDestroyed() || !w.isVisible()) continue;
    const b = w.nieeekiBounds;
    const frame = corners && {
      x: corners[0].x - b.x, y: corners[0].y - b.y, w: corners[1].x - corners[0].x, h: corners[1].y - corners[0].y,
    };
    const dot = tap && { x: tap.x - b.x, y: tap.y - b.y };
    const showFrame = frame && frame.x < b.width && frame.y < b.height && frame.x + frame.w > 0 && frame.y + frame.h > 0;
    const showDot = dot && dot.x >= 0 && dot.y >= 0 && dot.x < b.width && dot.y < b.height;
    if (showFrame || showDot) {
      w.webContents.send("highlight", { ok: Boolean(data?.ok), rahmen: showFrame ? frame : null, punkt: showDot ? dot : null });
    }
  }
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
  // Audio bekommt Vorrang, wenn Chrome & Co. beim Oeffnen kurz die CPU belegen (Aussetzer)
  setInterval(raisePriority, 5000);
  REPO = resolveRepo();
  adoptDevAutostart();
  createTray();
  await ensureServices();
  createMainWindow();
  createOverlays();
  watchFullscreen();
  startWake();
  screen.on("display-added", createOverlays);
  screen.on("display-removed", createOverlays);
  screen.on("display-metrics-changed", createOverlays);
});

app.on("before-quit", () => {
  quitting = true;
  fullscreenWatcher?.kill();
  stopWake();
  stopServices();
});

app.on("window-all-closed", (e) => e.preventDefault()); // lebt im Tray weiter
