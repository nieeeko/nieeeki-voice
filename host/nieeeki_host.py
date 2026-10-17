"""nieeeki-Host: die "Haende" von nieeeki auf dem Windows-PC.

Laeuft als Windows-Dienst mit Admin-Rechten und stellt Werkzeuge per MCP (streamable HTTP)
bereit - erreichbar NUR im Tailscale-Netz (bind auf die Tailscale-IP, nie 0.0.0.0).

Sicherheitsmodell (wird HIER erzwungen, nicht im Prompt):
  read      -> sofort
  change    -> sofort, protokolliert
  critical  -> legt eine Freigabe-Anfrage an; ausgefuehrt erst nach Bestaetigung in UI/Stimme
  blocked   -> nie (Passwoerter, Zahlungsdaten, Sicherheitssoftware abschalten)

Stufe 1 (dieses Geruest): PowerShell, Prozesse, Systeminfo, Dark-Mode.
Stufe 2/3 folgen: UI Automation, Computer Use (Screenshot/Klick), Playwright-Browser.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

AUDIT_LOG = Path(os.getenv("NIEEEKI_AUDIT_LOG", "nieeeki_audit.jsonl"))
BIND_HOST = os.getenv("NIEEEKI_HOST_BIND", "127.0.0.1")  # Tailscale-IP eintragen
BIND_PORT = int(os.getenv("NIEEEKI_HOST_PORT", "8765"))

mcp = FastMCP("nieeeki-host", host=BIND_HOST, port=BIND_PORT)

# Befehle, die immer eine Freigabe brauchen
CRITICAL_PATTERNS = [
    r"\bRemove-Item\b", r"\bdel\b", r"\brm\b", r"\bFormat-", r"\bSet-ItemProperty\b.*HKLM",
    r"\bNew-ItemProperty\b", r"\breg\s+(add|delete)\b", r"\b(Stop|Set|New|Remove)-Service\b",
    r"\bwinget\s+(install|uninstall|upgrade)\b", r"\bSet-NetFirewall", r"\bRestart-Computer\b",
    r"\bStop-Computer\b", r"\bInvoke-WebRequest\b.*-Method\s+Post",
]
# Befehle, die nie automatisch laufen
BLOCKED_PATTERNS = [
    r"Set-MpPreference.*Disable", r"\bbcdedit\b", r"\bcipher\s+/w\b", r"ConvertTo-SecureString",
    r"\bnet\s+user\b.*\/add",
]

PENDING: dict[str, dict] = {}


def _audit(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def classify(command: str) -> str:
    if any(re.search(p, command, re.I) for p in BLOCKED_PATTERNS):
        return "blocked"
    if any(re.search(p, command, re.I) for p in CRITICAL_PATTERNS):
        return "critical"
    if re.match(r"^\s*(Get-|Test-|Select-|Measure-|Where-|Format-List|Format-Table)", command, re.I):
        return "read"
    return "change"


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
        "stdout": p.stdout[-8000:],
        "stderr": p.stderr[-4000:],
    }


@mcp.tool()
def run_powershell(command: str, reason: str) -> dict:
    """PowerShell-Befehl ausfuehren. `reason` = wofuer (wird protokolliert).
    Kritische Befehle liefern status=needs_approval und eine approval_id."""
    risk = classify(command)
    _audit({"tool": "run_powershell", "command": command, "reason": reason, "risk": risk})
    if risk == "blocked":
        return {"status": "blocked", "summary": "Dieser Befehl ist fuer nieeeki gesperrt."}
    if risk == "critical":
        approval_id = uuid.uuid4().hex[:8]
        PENDING[approval_id] = {"command": command, "reason": reason}
        return {
            "status": "needs_approval",
            "approval_id": approval_id,
            "summary": f"Kritische Aktion - bitte bestaetigen: {reason}",
        }
    result = _powershell(command)
    _audit({"tool": "run_powershell", "result": result["status"]})
    return result


@mcp.tool()
def approve(approval_id: str, confirmation_phrase: str) -> dict:
    """Freigegebene kritische Aktion ausfuehren. Nur aufrufen, wenn Nikolaj per UI-Klick oder
    mit der Phrase 'nieeeki, bestaetigt' zugestimmt hat."""
    if confirmation_phrase.strip().lower() != "nieeeki, bestaetigt":
        return {"status": "error", "summary": "Bestaetigung fehlt oder falsch."}
    action = PENDING.pop(approval_id, None)
    if not action:
        return {"status": "error", "summary": "Unbekannte oder abgelaufene Freigabe."}
    _audit({"tool": "approve", "approval_id": approval_id, **action})
    return _powershell(action["command"], timeout=600)


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
def set_dark_mode(enabled: bool) -> dict:
    """Windows-Dark-Mode fuer Apps und System an/aus (change, HKCU)."""
    value = 0 if enabled else 1
    key = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    cmd = (
        f"Set-ItemProperty -Path '{key}' -Name AppsUseLightTheme -Value {value}; "
        f"Set-ItemProperty -Path '{key}' -Name SystemUsesLightTheme -Value {value}"
    )
    _audit({"tool": "set_dark_mode", "enabled": enabled})
    return _powershell(cmd)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
