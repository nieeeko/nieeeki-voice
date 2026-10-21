"""nieeeki-Host: die "Haende" von nieeeki auf dem Windows-PC.

Werkzeuge per MCP. Zwei Betriebsarten:
  --stdio   Der Voice-Core startet den Host als Unterprozess (lokal, nicht im Netz erreichbar).
  sonst     Streamable HTTP auf NIEEEKI_HOST_BIND (spaeter Tailscale-IP, nie 0.0.0.0).

Sicherheitsmodell (wird HIER erzwungen, nicht im Prompt). Alles, was ein Modell schickt, ist
untrusted: auch Text aus Websuche, Mails oder Dateien kann im Modell als Anweisung wirken.
  read      sofort                (system_info, powershell_lesen mit reinen Get-/Test-Befehlen)
  change    sofort, protokolliert (webseite_oeffnen, programm_oeffnen, dark_mode, ordner_anlegen im
            Benutzerordner, github_repo_anlegen im eigenen Konto und privat, git_klonen nach Projekte)
  critical  nur Freigabe-Anfrage; ausgefuehrt erst nach Bestaetigung durch Nikolaj in der UI
  admin     als_admin: nur ueber den Admin-Helfer (voice-core/admin_helper.py, laeuft erhoeht). Der
            fuehrt nur aus, was auf Nikolajs Erlaubnisliste steht oder was er per Klick freigibt;
            die Freigabe prueft der Helfer selbst (Nikolajs Login mit zweitem Faktor)
  blocked   nie
Freigaben kann das Modell NICHT selbst erteilen: es gibt dafuer kein MCP-Werkzeug.
Jeder Aufruf landet im Audit-Log (JSONL).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

import windows_control as wc

AUDIT_LOG = Path(os.getenv("NIEEEKI_AUDIT_LOG", Path(__file__).with_name("nieeeki_audit.jsonl")))
BIND_HOST = os.getenv("NIEEEKI_HOST_BIND", "127.0.0.1")  # Tailscale-IP eintragen
BIND_PORT = int(os.getenv("NIEEEKI_HOST_PORT", "8765"))

MAX_URL_LEN = 2048
MAX_COMMAND_LEN = 1000
MAX_OUTPUT_CHARS = 6000

mcp = FastMCP("nieeeki-host", host=BIND_HOST, port=BIND_PORT)

# ---------- Programme: nur diese Liste, keine freien Pfade ----------
_LOCAL = os.environ.get("LOCALAPPDATA", "")
APPS: dict[str, list[str]] = {
    # Name -> Kandidaten (exe-Pfad oder Shell-URI); der erste vorhandene gewinnt
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        rf"{_LOCAL}\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"],
    "explorer": [r"C:\Windows\explorer.exe"],
    "notepad": [r"C:\Windows\System32\notepad.exe"],
    "rechner": ["calculator:"],
    "einstellungen": ["ms-settings:"],
    "vscode": [rf"{_LOCAL}\Programs\Microsoft VS Code\Code.exe"],
    "terminal": ["wt.exe"],
    "spotify": ["spotify:"],
}

# ---------- PowerShell: nur lesen, alles andere braucht Freigabe ----------
READ_CMDLET = re.compile(
    r"^(Get|Test|Measure|Select|Where|Sort|Format|Group)-[A-Za-z]+(\s|$)", re.IGNORECASE
)
# Zeichen/Woerter, mit denen man aus einem "harmlosen" Get-Befehl ausbrechen kann
ESCAPE_TOKENS = re.compile(
    r"[;&`]|\$\(|@\(|>|<|\binvoke-expression\b|\biex\b|-enc(odedcommand)?\b|\bstart-process\b"
    r"|\binvoke-\w+|\bset-|\bnew-|\bremove-|\bstop-|\bcopy-|\bmove-|\brename-|\bout-file\b",
    re.IGNORECASE,
)
BLOCKED_PATTERNS = [
    r"Set-MpPreference.*Disable", r"\bbcdedit\b", r"\bcipher\s+/w\b", r"ConvertTo-SecureString",
    r"\bnet\s+user\b", r"Get-Credential", r"\bvssadmin\b", r"\bwbadmin\b",
    r"(password|passwort|kennwort)",
]

PENDING: dict[str, dict] = {}
# Vom Voice-Core je Host-Start erzeugt (pc_host.py); ohne ihn fuehrt freigabe_entscheiden nichts aus.
APPROVAL_KEY = os.getenv("NIEEEKI_APPROVAL_KEY", "")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _audit(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


DUPLICATE_WINDOW_SECS = 6.0
_recent_actions: dict[str, float] = {}


def _duplicate(tool: str, **args) -> dict | None:
    """Gleiche Aktion mit gleichen Argumenten innerhalb weniger Sekunden: nicht nochmal ausfuehren.
    (Log 18.10.: Claude drueckte ctrl+2 zweimal direkt hintereinander.)"""
    key = tool + json.dumps(args, sort_keys=True, ensure_ascii=False)
    now = time.monotonic()
    for k, t in list(_recent_actions.items()):
        if now - t > DUPLICATE_WINDOW_SECS:
            del _recent_actions[k]
    if key in _recent_actions:
        _audit({"tool": tool, "risk": "duplicate", **{k: str(v)[:120] for k, v in args.items()}})
        return {"status": "duplicate", "summary": "Genau das wurde gerade schon ausgefuehrt und wird nicht "
                "wiederholt. Will Nikolaj es nochmal, sagt er es."}
    _recent_actions[key] = now
    return None


def _clean(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = _CONTROL_CHARS.sub("", text)
    if len(text) > limit:
        return text[:limit] + f"\n[gekuerzt, {len(text)} Zeichen insgesamt]"
    return text


# ---------- Validatoren (direkt vor der Aktion, unabhaengig vom Prompt) ----------
def validate_url(url: object) -> str:
    """Nur http(s)-Adressen ins oeffentliche Netz, ohne Zugangsdaten in der URL."""
    if not isinstance(url, str) or not url.strip() or len(url) > MAX_URL_LEN:
        raise ValueError("ungueltige URL")
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise ValueError("nur http- und https-Adressen")
    if not parts.hostname:
        raise ValueError("URL ohne Host")
    if parts.username or parts.password:
        raise ValueError("Zugangsdaten in der URL sind nicht erlaubt")
    host = parts.hostname.lower()
    if host in ("localhost",) or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("lokale Adressen sind nicht erlaubt")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and not ip.is_global:
        raise ValueError("private oder lokale IP-Adressen sind nicht erlaubt")
    if any(c in url for c in ('"', "\n", "\r", "\t", " ")):
        raise ValueError("URL enthaelt unzulaessige Zeichen")
    return url


def resolve_app(name: object) -> str:
    if not isinstance(name, str) or name.strip().lower() not in APPS:
        raise ValueError(f"unbekanntes Programm, erlaubt: {', '.join(sorted(APPS))}")
    for candidate in APPS[name.strip().lower()]:
        if candidate.endswith(":") or shutil.which(candidate) or Path(candidate).is_file():
            return candidate
    raise ValueError(f"{name} ist auf diesem PC nicht installiert")


def classify(command: object) -> str:
    """read | critical | blocked. Unbekanntes ist nie 'read'."""
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_LEN:
        return "blocked"
    if any(re.search(p, command, re.IGNORECASE) for p in BLOCKED_PATTERNS):
        return "blocked"
    if ESCAPE_TOKENS.search(command):
        return "critical"
    segments = [s.strip() for s in command.split("|")]
    if all(READ_CMDLET.match(s) for s in segments):
        return "read"
    return "critical"


def _launch(target: str, args: list[str] | None = None) -> None:
    if platform.system() != "Windows":
        raise RuntimeError("Host laeuft nicht unter Windows")
    if target.endswith(":") and not args:
        os.startfile(target)  # Shell-URI wie ms-settings:
        return
    # argv-Liste, keine Shell: nichts aus dem Modell wird als Befehl interpretiert
    subprocess.Popen([target, *(args or [])], close_fds=True)


# Lesen muss schnell gehen: der Host arbeitet Aufrufe nacheinander ab. Am 21.10. blockierte eine
# Suche ueber ganz C:\ den Host 60 s, die folgenden Aufrufe warteten und wurden abgebrochen.
READ_TIMEOUT_SECS = 20


def _powershell(command: str, timeout: int = 60) -> dict:
    if platform.system() != "Windows":
        return {"status": "error", "summary": "Host laeuft nicht unter Windows"}
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "summary": f"Nach {timeout} s abgebrochen. Nicht wiederholen: enger "
                "eingrenzen (z. B. nur im Benutzerordner, ohne -Recurse ueber ganze Laufwerke) oder "
                "Nikolaj nach dem Pfad fragen."}
    return {
        "status": "success" if p.returncode == 0 else "error",
        "exit_code": p.returncode,
        "stdout": _clean(p.stdout),
        "stderr": _clean(p.stderr, 2000),
    }


# ---------- MCP-Werkzeuge ----------
@mcp.tool()
def webseite_oeffnen(url: str, browser: str = "chrome") -> dict:
    """Oeffnet eine Webseite im Browser (change). Fuer Suchen auf Shops direkt die Such-URL bauen,
    z. B. https://www.amazon.de/s?k=wohnzimmer+lampe. browser: chrome oder edge."""
    try:
        safe_url = validate_url(url)
        exe = resolve_app(browser if browser in ("chrome", "edge") else "chrome")
    except ValueError as e:
        _audit({"tool": "webseite_oeffnen", "url": str(url)[:200], "risk": "rejected", "error": str(e)})
        return {"status": "rejected", "summary": str(e)}
    if dup := _duplicate("webseite_oeffnen", url=safe_url):
        return dup
    _audit({"tool": "webseite_oeffnen", "url": safe_url, "browser": browser, "risk": "change"})
    _launch(exe, [safe_url])
    return {"status": "success", "summary": f"{safe_url} in {browser} geoeffnet"}


@mcp.tool()
def programm_oeffnen(name: str) -> dict:
    """Startet ein installiertes Programm (change), z. B. WhatsApp, Spotify, VS Code, Claude, Chrome.
    Gesucht wird im Startmenue. Ist es schon offen, besser fenster_fokussieren nutzen.
    Shells, Registry und Admin-Werkzeuge startet nieeeki nicht."""
    try:
        try:
            target = resolve_app(name)
            label = name
        except ValueError:
            app = wc.pick_start_app(str(name), wc.start_menu_apps())
            target, label = None, app["Name"]
    except ValueError as e:
        _audit({"tool": "programm_oeffnen", "name": str(name)[:50], "risk": "rejected", "error": str(e)})
        return {"status": "rejected", "summary": str(e)}
    if dup := _duplicate("programm_oeffnen", name=label):
        return dup
    _audit({"tool": "programm_oeffnen", "name": label, "risk": "change"})
    if target:
        _launch(target)
    else:
        wc.launch_start_app(app["AppID"])
    return {"status": "success", "summary": f"{label} gestartet"}


HOME = Path(os.environ.get("USERPROFILE", str(Path.home()))).resolve()
SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", "credentials"}
OPEN_WITH = {"vscode": "vscode", "code": "vscode", "explorer": "explorer", "notepad": "notepad", "editor": "notepad"}


def validate_local_path(pfad: object) -> Path:
    """Nur vorhandene Pfade im eigenen Benutzerordner, keine Schluessel-/Zugangsdaten-Ordner."""
    if not isinstance(pfad, str) or not pfad.strip() or len(pfad) > 400:
        raise ValueError("Pfad fehlt")
    path = Path(pfad.strip().strip('"')).expanduser()
    if not path.is_absolute():
        raise ValueError("Bitte den vollen Pfad, z. B. C:\\Users\\nikos\\Projekte\\radar")
    path = path.resolve()
    if not path.is_relative_to(HOME):
        raise ValueError(f"Nur Pfade unter {HOME}")
    if any(part.lower() in SENSITIVE_PARTS for part in path.parts):
        raise ValueError("Ordner mit Zugangsdaten oeffnet nieeeki nicht")
    if not path.exists():
        raise ValueError(f"{path} gibt es nicht")
    return path


@mcp.tool()
def oeffnen_mit(pfad: str, programm: str = "vscode") -> dict:
    """Oeffnet einen Ordner oder eine Datei direkt in einem Programm (change): vscode (Standard, z. B.
    ein Projektordner), explorer oder notepad. Der richtige Weg, um Projekte/Dateien zu oeffnen;
    niemals Befehle wie 'code ...' in ein Fenster tippen. Nur Pfade im eigenen Benutzerordner."""
    try:
        path = validate_local_path(pfad)
        app_name = OPEN_WITH.get(str(programm).strip().lower())
        if not app_name:
            raise ValueError("programm: vscode, explorer oder notepad")
        if app_name == "notepad" and path.is_dir():
            raise ValueError("Notepad oeffnet nur Dateien")
        exe = resolve_app(app_name)
    except ValueError as e:
        _audit({"tool": "oeffnen_mit", "pfad": str(pfad)[:200], "risk": "rejected", "error": str(e)})
        return {"status": "rejected", "summary": str(e)}
    if dup := _duplicate("oeffnen_mit", pfad=str(path), programm=app_name):
        return dup
    _audit({"tool": "oeffnen_mit", "pfad": str(path), "programm": app_name, "risk": "change"})
    _launch(exe, [str(path)])  # argv, keine Shell
    return {"status": "success", "summary": f"{path.name} in {app_name} geoeffnet"}


# ---------- Ordner und GitHub: eigene Werkzeuge statt Klicken und PowerShell-Freigaben ----------
# 21.10.: Ein Repository per Klick auf github.com/new anzulegen dauerte Minuten (Bildmodell je Klick,
# Text landete neben dem Feld, die Adresse zum Klonen fehlte), und jeder neue Ordner brauchte eine
# Freigabe. Beides ist harmlos und umkehrbar, darum "change": sofort, mit Audit-Eintrag.
PROJECTS = Path(os.getenv("NIEEEKI_PROJECTS_DIR") or HOME / "Projekte").resolve()
NO_CREATE_PARTS = {"appdata"}  # Programmdaten und Autostart: dort legt nieeeki nichts an
REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
GITHUB_REPO = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")
GIT_TIMEOUT_SECS = 90


def validate_new_folder(pfad: object) -> Path:
    """Neuer Ordner nur im eigenen Benutzerordner, nicht in AppData, versteckten oder Zugangsdaten-Ordnern."""
    if not isinstance(pfad, str) or not pfad.strip() or len(pfad) > 400:
        raise ValueError("Pfad fehlt")
    path = Path(pfad.strip().strip('"')).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Bitte den vollen Pfad, z. B. {PROJECTS / 'neues-projekt'}")
    path = path.resolve()
    if path == HOME or not path.is_relative_to(HOME):
        raise ValueError(f"Nur Ordner unter {HOME}")
    parts = [p.lower() for p in path.relative_to(HOME).parts]
    if set(parts) & (SENSITIVE_PARTS | NO_CREATE_PARTS) or any(p.startswith(".") for p in parts):
        raise ValueError("Dort legt nieeeki keine Ordner an")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{path.name} ist eine Datei, kein Ordner")
    return path


def validate_repo_name(name: object) -> str:
    if not isinstance(name, str) or not REPO_NAME.fullmatch(name.strip()) or name.strip().lower().endswith(".git"):
        raise ValueError("Repository-Name: Buchstaben, Ziffern, Punkt, Minus oder Unterstrich, hoechstens 100 Zeichen")
    return name.strip()


def validate_github_repo(repo: object) -> tuple[str, str]:
    """besitzer/name oder https://github.com/besitzer/name(.git) -> (besitzer, name)."""
    m = GITHUB_REPO.fullmatch(repo.strip()) if isinstance(repo, str) else None
    if not m:
        raise ValueError("Repository als besitzer/name oder https://github.com/besitzer/name")
    return m.group(1), validate_repo_name(m.group(2))


def _gh() -> str:
    exe = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    if not Path(exe).is_file():
        raise RuntimeError("Die GitHub-Kommandozeile (gh) ist auf dem Laptop nicht installiert")
    return exe


def _run(args: list[str], timeout: int = GIT_TIMEOUT_SECS) -> subprocess.CompletedProcess:
    # argv-Liste, keine Shell: Name und Beschreibung werden nie als Befehl gelesen
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _clone(gh: str, owner: str, name: str) -> dict:
    """Nach Projekte\\<name> klonen; ein leerer Ordner dort ist in Ordnung, einer mit Inhalt nicht."""
    target = PROJECTS / name
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        return {"status": "rejected", "summary": f"Im Projekte-Ordner liegt schon {name} mit Inhalt; nichts ueberschrieben",
                "ordner": str(target)}
    PROJECTS.mkdir(parents=True, exist_ok=True)
    p = _run([gh, "repo", "clone", f"{owner}/{name}", str(target)])
    if p.returncode != 0:
        return {"status": "error", "summary": "Klonen hat nicht geklappt", "detail": _clean(p.stderr, 800)}
    return {"status": "success", "summary": f"{owner}/{name} liegt jetzt in Projekte\\{name}", "ordner": str(target)}


@mcp.tool()
def ordner_anlegen(pfad: str) -> dict:
    """Legt einen Ordner an (change, ohne Freigabe), z. B. im Projekte-Ordner. Nur im eigenen
    Benutzerordner, nie in AppData. Gibt es ihn schon, passiert nichts. Statt New-Item per PowerShell."""
    try:
        path = validate_new_folder(pfad)
    except ValueError as e:
        _audit({"tool": "ordner_anlegen", "pfad": str(pfad)[:200], "risk": "rejected", "error": str(e)})
        return {"status": "rejected", "summary": str(e)}
    if path.is_dir():
        return {"status": "success", "summary": f"Den Ordner {path.name} gibt es schon", "pfad": str(path)}
    _audit({"tool": "ordner_anlegen", "pfad": str(path), "risk": "change"})
    path.mkdir(parents=True)
    return {"status": "success", "summary": f"Ordner {path.name} angelegt", "pfad": str(path)}


@mcp.tool()
def github_repo_anlegen(name: str, privat: bool = True, beschreibung: str = "") -> dict:
    """Legt ein neues GitHub-Repository in Nikolajs Konto an (change, privat, ausser er will es
    oeffentlich) und klont es gleich nach Projekte\\<name>. Der richtige Weg statt github.com/new im
    Browser. Gibt Adresse und Ordner zurueck; gibt es das Repository schon, wird nur geklont."""
    try:
        name = validate_repo_name(name)
        gh = _gh()
    except (ValueError, RuntimeError) as e:
        _audit({"tool": "github_repo_anlegen", "name": str(name)[:100], "risk": "rejected", "error": str(e)})
        return {"status": "rejected", "summary": str(e)}
    desc = _CONTROL_CHARS.sub("", str(beschreibung or ""))[:200]
    if dup := _duplicate("github_repo_anlegen", name=name):
        return dup
    _audit({"tool": "github_repo_anlegen", "name": name, "privat": bool(privat), "risk": "change"})
    try:
        login = _run([gh, "api", "user", "--jq", ".login"], timeout=20)
        owner = login.stdout.strip()
        if login.returncode != 0 or not owner:
            return {"status": "error", "summary": "GitHub-Anmeldung fehlt: am Laptop einmal 'gh auth login'"}
        known = _run([gh, "repo", "view", f"{owner}/{name}", "--json", "visibility", "--jq", ".visibility"], timeout=20)
        created = known.returncode != 0
        if created:
            args = [gh, "repo", "create", f"{owner}/{name}", "--private" if privat else "--public"]
            if desc:
                args += ["--description", desc]
            made = _run(args)
            if made.returncode != 0:
                return {"status": "error", "summary": "GitHub hat das Repository nicht angelegt",
                        "detail": _clean(made.stderr, 800)}
        result = _clone(gh, owner, name)
    except subprocess.TimeoutExpired:
        return {"status": "error", "summary": "GitHub antwortet nicht (Zeitueberschreitung)"}
    result["url"] = f"https://github.com/{owner}/{name}"
    result["angelegt"] = created
    if not created:
        result["hinweis"] = f"Das Repository gab es schon ({known.stdout.strip().lower() or 'unbekannt'}), es wurde nur geklont."
    return result


@mcp.tool()
def git_klonen(repo: str) -> dict:
    """Klont ein GitHub-Repository in den Projekte-Ordner (change): repo = besitzer/name oder
    https://github.com/besitzer/name. Ziel ist immer Projekte\\<name>. Danach mit oeffnen_mit in
    VS Code oeffnen."""
    try:
        owner, name = validate_github_repo(repo)
        gh = _gh()
    except (ValueError, RuntimeError) as e:
        _audit({"tool": "git_klonen", "repo": str(repo)[:200], "risk": "rejected", "error": str(e)})
        return {"status": "rejected", "summary": str(e)}
    if dup := _duplicate("git_klonen", repo=f"{owner}/{name}"):
        return dup
    _audit({"tool": "git_klonen", "repo": f"{owner}/{name}", "risk": "change"})
    try:
        result = _clone(gh, owner, name)
    except subprocess.TimeoutExpired:
        return {"status": "error", "summary": "Klonen dauerte zu lange und wurde abgebrochen"}
    result["url"] = f"https://github.com/{owner}/{name}"
    return result


@mcp.tool()
def fenster_auflisten() -> dict:
    """Listet die offenen Fenster mit Titel und Programm (read). Nutzen, um zu sehen, was laeuft,
    z. B. ob VS Code oder WhatsApp schon offen ist."""
    windows = [{k: w[k] for k in ("titel", "programm")} for w in wc.list_windows()]
    _audit({"tool": "fenster_auflisten", "risk": "read", "count": len(windows)})
    return {"status": "success", "fenster": windows[:40], "vorne": wc.foreground()["titel"]}


def _highlight(result: dict, hwnd: int | None, point: tuple[int, int] | None = None) -> dict:
    """Fensterrechteck und Klickpunkt mitgeben: die Desktop-App zeigt dort einen Lichtrahmen."""
    if hwnd and (rect := wc.window_rect(hwnd)):
        result["rahmen"] = rect
    if point:
        result["punkt"] = [int(point[0]), int(point[1])]
    return result


def _locked(tool: str) -> dict | None:
    """Ist der Laptop gesperrt, laeuft jede Eingabe ins Leere: klar melden statt so zu tun als ob.
    Wichtig beim Fernzugriff vom Handy, wo Nikolaj den Bildschirm nicht sieht."""
    if not wc.is_locked():
        return None
    _audit({"tool": tool, "risk": "rejected", "error": "bildschirm gesperrt"})
    return {"status": "locked", "summary": "Der Laptop ist gesperrt. Tippen, Klicken und Ansehen gehen "
            "erst wieder, wenn Nikolaj ihn entsperrt."}


@mcp.tool()
def fenster_fokussieren(fenster: str) -> dict:
    """Holt ein offenes Fenster nach vorne (change). `fenster` = Teil des Titels oder Programmname,
    z. B. 'WhatsApp', 'Code', 'Chrome'."""
    if locked := _locked("fenster_fokussieren"):
        return locked
    try:
        w = wc.find_window(str(fenster))
    except ValueError as e:
        return {"status": "rejected", "summary": str(e)}
    _audit({"tool": "fenster_fokussieren", "titel": w["titel"], "programm": w["programm"], "risk": "change"})
    wc.focus(w["hwnd"])
    return _highlight({"status": "success", "summary": f"{w['titel']} ist vorne"}, w["hwnd"])


def _prepare_input(fenster: str | None) -> dict:
    """Optional Fenster fokussieren, dann pruefen, ob dort getippt werden darf."""
    if fenster:
        wc.focus(wc.find_window(fenster)["hwnd"])
    target = wc.foreground()
    wc.check_target(target["programm"], target["titel"])
    return target


@mcp.tool()
def tastatur_tippen(text: str, fenster: str = "") -> dict:
    """Tippt Text ins Fenster vorne oder vorher ins genannte Fenster (change), z. B. eine Nachricht in
    WhatsApp oder einen Prompt in den Claude-Chat in VS Code. Zeilenumbruch = Enter. Nur tippen, was
    Nikolaj so gewollt hat. In Konsolen, Admin- und Passwortfenster tippt nieeeki nie."""
    if not isinstance(text, str) or not text or len(text) > wc.MAX_TYPE_CHARS:
        return {"status": "rejected", "summary": f"Text fehlt oder ist laenger als {wc.MAX_TYPE_CHARS} Zeichen"}
    if locked := _locked("tastatur_tippen"):
        return locked
    try:
        target = _prepare_input(fenster or None)
    except ValueError as e:
        _audit({"tool": "tastatur_tippen", "fenster": str(fenster)[:80], "risk": "rejected", "error": str(e)})
        return {"status": "rejected", "summary": str(e)}
    if dup := _duplicate("tastatur_tippen", text=text, ziel=target["titel"]):
        return dup
    _audit({"tool": "tastatur_tippen", "ziel": target["titel"][:120], "programm": target["programm"],
            "zeichen": len(text), "risk": "change"})
    wc.type_text(text)
    return _highlight({"status": "success", "summary": f"{len(text)} Zeichen in {target['titel'][:60]} getippt"},
                      target["hwnd"])


@mcp.tool()
def tasten_druecken(kombination: str, fenster: str = "") -> dict:
    """Drueckt eine Taste oder Tastenkombination (change), z. B. 'enter', 'ctrl+t', 'ctrl+l', 'esc',
    'alt+tab'. Windows-Taste ist gesperrt; in Konsolen, Admin- und Passwortfenstern nie."""
    if locked := _locked("tasten_druecken"):
        return locked
    try:
        codes = wc.parse_combo(kombination)
        target = _prepare_input(fenster or None)
    except ValueError as e:
        _audit({"tool": "tasten_druecken", "kombination": str(kombination)[:40], "risk": "rejected", "error": str(e)})
        return {"status": "rejected", "summary": str(e)}
    if dup := _duplicate("tasten_druecken", kombination=kombination.lower(), ziel=target["titel"]):
        return dup
    _audit({"tool": "tasten_druecken", "kombination": kombination, "ziel": target["titel"][:120], "risk": "change"})
    wc.press(codes)
    return _highlight({"status": "success", "summary": f"{kombination} in {target['titel'][:60]} gedrueckt"},
                      target["hwnd"])


@mcp.tool()
def powershell_lesen(command: str, reason: str) -> dict:
    """Liest Informationen per PowerShell (nur Get-/Test-Befehle, optional mit | Select/Where/Sort/
    Format/Measure). Alles, was etwas veraendert, wird NICHT ausgefuehrt, sondern als Freigabe-
    Anfrage an Nikolaj gestellt (status=needs_approval). `reason` = wofuer, wird protokolliert."""
    risk = classify(command)
    _audit({"tool": "powershell_lesen", "command": str(command)[:MAX_COMMAND_LEN], "reason": reason, "risk": risk})
    if risk == "blocked":
        return {"status": "blocked", "summary": "Dieser Befehl ist fuer nieeeki gesperrt."}
    if risk == "critical":
        approval_id = uuid.uuid4().hex[:8]
        PENDING[approval_id] = {"command": command, "reason": reason}
        return {
            "status": "needs_approval",
            "approval_id": approval_id,
            "summary": "Das veraendert etwas am PC. Nikolaj muss es in der Oberflaeche freigeben; "
            "bis dahin passiert nichts.",
        }
    result = _powershell(command, timeout=READ_TIMEOUT_SECS)
    _audit({"tool": "powershell_lesen", "result": result["status"]})
    return result


@mcp.tool()
def system_info() -> dict:
    """Basisinfos zum PC (read)."""
    return {
        "status": "success",
        "os": platform.platform(),
        "machine": platform.node(),
        "python": platform.python_version(),
    }


@mcp.tool()
def dark_mode(enabled: bool) -> dict:
    """Windows-Dark-Mode fuer Apps und System an/aus (change, nur HKCU)."""
    value = 0 if bool(enabled) else 1
    key = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    cmd = (
        f"Set-ItemProperty -Path '{key}' -Name AppsUseLightTheme -Value {value}; "
        f"Set-ItemProperty -Path '{key}' -Name SystemUsesLightTheme -Value {value}"
    )
    _audit({"tool": "dark_mode", "enabled": bool(enabled), "risk": "change"})
    return _powershell(cmd)


@mcp.tool()
def bildschirmfoto(monitor: int = 0) -> dict:
    """Bildschirmfoto als JPEG (read). Nur fuer den Voice-Core, der es an ein Bildmodell gibt;
    wird dem Gespraechsmodell nicht angeboten. monitor 0 = der mit dem Fenster vorne."""
    if locked := _locked("bildschirmfoto"):
        return locked
    try:
        shot = wc.screenshot(int(monitor))
    except ValueError as e:
        return {"status": "rejected", "summary": str(e)}
    _audit({"tool": "bildschirmfoto", "monitor": shot["monitor"], "risk": "read"})
    return {"status": "success", **shot}


def _check_point(x: int, y: int) -> dict:
    left, top, right, bottom = wc.screen_bounds()
    if not (left <= x < right and top <= y < bottom):
        raise ValueError("Punkt liegt ausserhalb der Bildschirme")
    target = wc.window_at(x, y)
    wc.check_target(target["programm"], target["titel"])
    return target


@mcp.tool()
def maus_klicken(x: int, y: int, taste: str = "links", doppelt: bool = False) -> dict:
    """Klickt an einer Bildschirmposition (change). Nur fuer den Voice-Core (Koordinaten kommen aus
    dem Bildmodell). Nie auf Konsolen, Admin-, Anmelde- oder Passwortfenster."""
    if taste not in ("links", "rechts"):
        return {"status": "rejected", "summary": "taste: links oder rechts"}
    if locked := _locked("maus_klicken"):
        return locked
    try:
        x, y = int(x), int(y)
        target = _check_point(x, y)
    except (ValueError, TypeError) as e:
        _audit({"tool": "maus_klicken", "x": x, "y": y, "risk": "rejected", "error": str(e)})
        return {"status": "rejected", "summary": str(e)}
    _audit({"tool": "maus_klicken", "x": x, "y": y, "taste": taste, "doppelt": bool(doppelt),
            "ziel": target["titel"][:120], "programm": target["programm"], "risk": "change"})
    wc.click(x, y, taste, bool(doppelt))
    return _highlight({"status": "success", "summary": f"Klick bei {x},{y} in {target['titel'][:60]}"},
                      target["hwnd"], (x, y))


@mcp.tool()
def maus_scrollen(richtung: str = "runter", schritte: int = 5, x: int | None = None, y: int | None = None) -> dict:
    """Scrollt im Fenster vorne (change). richtung: hoch oder runter, schritte 1-20. x/y nur, wenn
    an einer bestimmten Bildschirmstelle gescrollt werden soll (sonst Mitte des Fensters vorne)."""
    if richtung not in ("hoch", "runter"):
        return {"status": "rejected", "summary": "richtung: hoch oder runter"}
    if locked := _locked("maus_scrollen"):
        return locked
    steps = max(1, min(20, int(schritte)))
    if x is None or y is None:
        win32gui = wc._win32()[0]
        left, top, right, bottom = win32gui.GetWindowRect(win32gui.GetForegroundWindow())
        x, y = (left + right) // 2, (top + bottom) // 2
    try:
        x, y = int(x), int(y)
        target = _check_point(x, y)
    except (ValueError, TypeError) as e:
        return {"status": "rejected", "summary": str(e)}
    _audit({"tool": "maus_scrollen", "richtung": richtung, "schritte": steps, "ziel": target["titel"][:120], "risk": "change"})
    wc.scroll(x, y, steps if richtung == "hoch" else -steps)
    return _highlight({"status": "success", "summary": f"{steps} Schritte {richtung} in {target['titel'][:60]}"},
                      target["hwnd"], (x, y))


# ---------- Admin: nur ueber den Admin-Helfer ----------
ADMIN_DIR = Path(os.getenv("NIEEEKI_ADMIN_DIR") or Path(os.getenv("ProgramData", r"C:\ProgramData")) / "nieeeki" / "admin")
ADMIN_PORT = int(os.getenv("NIEEEKI_ADMIN_PORT", "8766"))
ADMIN_TIMEOUT_SECS = 75  # PowerShell im Helfer hat 60 s
MAX_ADMIN_REPLY = 64 * 1024


def _admin_call(payload: dict) -> dict:
    """Eine Anfrage an den Admin-Helfer. Der Host gibt hier nichts frei: Erlaubnisliste und Nikolajs
    Token prueft der Helfer selbst."""
    try:
        key = (ADMIN_DIR / "client.key").read_text(encoding="ascii").strip()
    except OSError:
        return {"status": "error", "summary": "Der Admin-Zugriff ist noch nicht eingerichtet: einmal "
                "scripts\\admin-einrichten.ps1 am Laptop ausfuehren."}
    try:
        with socket.create_connection(("127.0.0.1", ADMIN_PORT), timeout=ADMIN_TIMEOUT_SECS) as s:
            s.sendall((json.dumps({**payload, "schluessel": key}, ensure_ascii=False) + "\n").encode())
            data = b""
            while not data.endswith(b"\n") and len(data) < MAX_ADMIN_REPLY:
                chunk = s.recv(8192)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data)
    except (OSError, ValueError):
        return {"status": "error", "summary": "Der Admin-Helfer antwortet nicht (Aufgabe nieeeki-admin "
                "startet bei der Anmeldung)."}
    return reply if isinstance(reply, dict) else {"status": "error", "summary": "Unerwartete Antwort"}


@mcp.tool()
def als_admin(aktion: str, grund: str, befehl: str = "", x: int = -1, y: int = -1) -> dict:
    """Etwas mit Administratorrechten tun (admin). aktion "powershell": `befehl` (eine Zeile) als Admin
    ausfuehren. aktion "klicken": Klick bei x/y in ein Admin-Fenster, z. B. das GIGABYTE Control
    Center (Koordinaten aus bildschirm_ansehen). `grund` = wofuer; steht auf der Freigabe-Karte.
    Steht die Aktion nicht auf Nikolajs Erlaubnisliste, passiert nichts, bis er sie in der
    Oberflaeche freigibt (status=needs_approval)."""
    if aktion not in ("powershell", "klicken"):
        return {"status": "rejected", "summary": "aktion: powershell oder klicken"}
    req: dict = {"op": "vorschlagen", "aktion": aktion, "grund": str(grund)[:200]}
    if aktion == "powershell":
        if isinstance(befehl, str) and any(re.search(p, befehl, re.IGNORECASE) for p in BLOCKED_PATTERNS):
            _audit({"tool": "als_admin", "aktion": aktion, "befehl": befehl[:MAX_COMMAND_LEN], "risk": "blocked"})
            return {"status": "blocked", "summary": "Das ist auch als Admin gesperrt."}
        req["befehl"] = befehl
    else:
        if locked := _locked("als_admin"):
            return locked
        if type(x) is not int or type(y) is not int:
            return {"status": "rejected", "summary": "x und y muessen ganze Zahlen sein"}
        req.update(x=x, y=y)
    result = _admin_call(req)
    _audit({"tool": "als_admin", **{k: str(v)[:MAX_COMMAND_LEN] for k, v in req.items() if k != "op"},
            "risk": "admin", "status": result.get("status")})
    return result


@mcp.tool()
def freigabe_entscheiden(approval_id: str, ja: bool, schluessel: str = "", token: str = "",
                         immer: bool = False) -> dict:
    """Nur fuer den Voice-Core, nach Nikolajs Klick auf eine Freigabe-Karte in der Oberflaeche.
    Doppelt gesichert: steht nicht in pc_host.LLM_TOOLS (das Gespraechsmodell sieht es nicht) und
    braucht den Schluessel, den der Voice-Core beim Start des Hosts erzeugt (NIEEEKI_APPROVAL_KEY).
    Admin-Freigaben gehen mit Nikolajs Token (`token`, wird nie protokolliert) an den Admin-Helfer;
    `immer` nimmt die Aktion dort in die Erlaubnisliste auf."""
    approval_id = str(approval_id)
    if not APPROVAL_KEY or not hmac.compare_digest(str(schluessel), APPROVAL_KEY):
        _audit({"tool": "freigabe_entscheiden", "approval_id": approval_id, "risk": "blocked",
                "error": "ohne gueltigen Schluessel"})
        return {"status": "blocked", "summary": "Freigaben kommen nur aus der Oberflaeche."}
    if approval_id not in PENDING:
        result = _admin_call({"op": "entscheiden", "approval_id": approval_id, "ja": bool(ja),
                              "token": str(token), "immer": bool(immer)})
        _audit({"tool": "freigabe_entscheiden", "approval_id": approval_id, "risk": "admin", "ja": bool(ja),
                "immer": bool(immer), "status": result.get("status")})
        return result
    if not bool(ja):
        action = PENDING.pop(approval_id, None)
        _audit({"tool": "freigabe_entscheiden", "approval_id": approval_id, "risk": "rejected", **(action or {})})
        return {"status": "rejected", "summary": "Abgelehnt, es wurde nichts ausgefuehrt."}
    return execute_approved(approval_id)


def execute_approved(approval_id: str) -> dict:
    """Fuehrt eine freigegebene Aktion aus. Erreichbar nur ueber freigabe_entscheiden, also nach
    Nikolajs Klick in der UI, nie vom Modell."""
    action = PENDING.pop(approval_id, None)
    if not action:
        return {"status": "error", "summary": "Unbekannte oder abgelaufene Freigabe."}
    _audit({"tool": "execute_approved", "approval_id": approval_id, **action})
    return _powershell(action["command"], timeout=600)


if __name__ == "__main__":
    mcp.run(transport="stdio" if "--stdio" in sys.argv else "streamable-http")
