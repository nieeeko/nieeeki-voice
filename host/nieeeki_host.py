"""nieeeki-Host: die "Haende" von nieeeki auf dem Windows-PC.

Werkzeuge per MCP. Zwei Betriebsarten:
  --stdio   Der Voice-Core startet den Host als Unterprozess (lokal, nicht im Netz erreichbar).
  sonst     Streamable HTTP auf NIEEEKI_HOST_BIND (spaeter Tailscale-IP, nie 0.0.0.0).

Sicherheitsmodell (wird HIER erzwungen, nicht im Prompt). Alles, was ein Modell schickt, ist
untrusted: auch Text aus Websuche, Mails oder Dateien kann im Modell als Anweisung wirken.
  read      sofort                (system_info, powershell_lesen mit reinen Get-/Test-Befehlen)
  change    sofort, protokolliert (webseite_oeffnen, programm_oeffnen, dark_mode)
  critical  nur Freigabe-Anfrage; ausgefuehrt erst nach Bestaetigung durch Nikolaj in der UI
  blocked   nie
Freigaben kann das Modell NICHT selbst erteilen: es gibt dafuer kein MCP-Werkzeug.
Jeder Aufruf landet im Audit-Log (JSONL).
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

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
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _audit(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


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


def _powershell(command: str, timeout: int = 60) -> dict:
    if platform.system() != "Windows":
        return {"status": "error", "summary": "Host laeuft nicht unter Windows"}
    p = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=timeout,
    )
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
    _audit({"tool": "webseite_oeffnen", "url": safe_url, "browser": browser, "risk": "change"})
    _launch(exe, [safe_url])
    return {"status": "success", "summary": f"{safe_url} in {browser} geoeffnet"}


@mcp.tool()
def programm_oeffnen(name: str) -> dict:
    """Startet ein Programm aus der festen Liste (change): chrome, edge, explorer, notepad, rechner,
    einstellungen, vscode, terminal, spotify."""
    try:
        target = resolve_app(name)
    except ValueError as e:
        _audit({"tool": "programm_oeffnen", "name": str(name)[:50], "risk": "rejected", "error": str(e)})
        return {"status": "rejected", "summary": str(e)}
    _audit({"tool": "programm_oeffnen", "name": name, "risk": "change"})
    _launch(target)
    return {"status": "success", "summary": f"{name} gestartet"}


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
    result = _powershell(command)
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


def execute_approved(approval_id: str) -> dict:
    """Fuehrt eine freigegebene Aktion aus. Bewusst KEIN MCP-Werkzeug: Freigaben kommen spaeter
    aus der UI (Tabelle actions, Klick durch Nikolaj), nie vom Modell."""
    action = PENDING.pop(approval_id, None)
    if not action:
        return {"status": "error", "summary": "Unbekannte oder abgelaufene Freigabe."}
    _audit({"tool": "execute_approved", "approval_id": approval_id, **action})
    return _powershell(action["command"], timeout=600)


if __name__ == "__main__":
    mcp.run(transport="stdio" if "--stdio" in sys.argv else "streamable-http")
