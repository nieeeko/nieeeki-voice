"""nieeeki-Admin-Helfer: fuehrt Aktionen mit Administratorrechten aus, aber nur mit Nikolajs Freigabe.

Laeuft erhoeht als Aufgabe "nieeeki-admin" (RunLevel Highest, bei Anmeldung) aus
C:\\ProgramData\\nieeeki\\admin. Dort duerfen nur Administratoren schreiben: Python, dieser Code,
admin.env und die Erlaubnisliste. Eingerichtet von scripts/admin-einrichten.ps1 (einmal mit UAC).

Der normale PC-Host (ohne Adminrechte, Werkzeug als_admin) schlaegt Aktionen vor. Ausgefuehrt wird
  - sofort, wenn die Aktion auf der Erlaubnisliste steht (erlaubt.json),
  - sonst erst nach Nikolajs Klick in der Oberflaeche. Die Freigabe traegt sein Supabase-Token; der
    Helfer prueft es selbst (Signatur, Konto, zweiter Faktor). Text vom Modell genuegt nie.
Gesperrt bleibt, was nie per Freigabe laufen soll (Virenschutz aus, Bootkonfiguration, Konten,
Passwoerter, Datentraeger loeschen, der Helfer selbst).

Aktionen:
  powershell  Befehl als Admin (hoechstens 1000 Zeichen, eine Zeile, 60 s, Ausgabe gekuerzt)
  klicken     Mausklick an einem Bildschirmpunkt (physische Pixel), z. B. im GIGABYTE Control Center.
              Bei der Ausfuehrung muss dasselbe Fenster unter dem Punkt liegen wie beim Vorschlag.

Protokoll: TCP 127.0.0.1:8766, je Verbindung eine JSON-Zeile hin und eine zurueck. Jede Anfrage
traegt "schluessel" (client.key; lesen duerfen nur Nikolajs Konto und Administratoren).
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import hmac
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8766
MAX_LINE = 16 * 1024
MAX_COMMAND_LEN = 1000
MAX_REASON_LEN = 200
MAX_TOKEN_LEN = 4096
MAX_OUTPUT_CHARS = 6000
MAX_PENDING = 20
PENDING_TTL_SECS = 600
COMMAND_TIMEOUT_SECS = 60
ACTIONS = ("powershell", "klicken")
APPROVAL_ID = re.compile(r"^[0-9a-f]{8}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Wie BLOCKED_PATTERNS in host/nieeeki_host.py, dazu Admin-eigene Sperren
BLOCKED_PATTERNS = [
    r"Set-MpPreference.*Disable", r"\bbcdedit\b", r"\bcipher\s+/w\b", r"ConvertTo-SecureString",
    r"\bnet\s+user\b", r"Get-Credential", r"\bvssadmin\b", r"\bwbadmin\b",
    r"(password|passwort|kennwort)",
    r"\bFormat-Volume\b", r"\bClear-Disk\b", r"\bInitialize-Disk\b", r"\bdiskpart\b",
    r"\bAdd-LocalGroupMember\b", r"\bnet\s+localgroup\b",
    r"nieeeki[\\/]+admin", r"nieeeki-admin",  # der Helfer, seine Erlaubnisliste und seine Aufgabe
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(text: object, limit: int) -> str:
    s = "" if text is None else str(text)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    return s if len(s) <= limit else s[:limit] + f"\n[gekuerzt, {len(s)} Zeichen]"


def blocked(command: str) -> bool:
    return any(re.search(p, command, re.IGNORECASE) for p in BLOCKED_PATTERNS)


@dataclass
class Proposal:
    id: str
    aktion: str
    grund: str
    befehl: str = ""
    x: int = 0
    y: int = 0
    fenster: str = ""
    created: float = field(default_factory=time.monotonic)

    def public(self) -> dict:
        if self.aktion == "powershell":
            return {"aktion": "powershell", "befehl": self.befehl, "grund": self.grund}
        return {"aktion": "klicken", "x": self.x, "y": self.y, "fenster": self.fenster, "grund": self.grund}


class AdminCore:
    """Pruefen, merken, freigeben, ausfuehren. Ohne Netz und ohne Windows testbar (Ausfuehrer injiziert)."""

    def __init__(self, allowlist: Path, audit: Path, verifier, *, run_powershell, click, window_at,
                 clock=time.monotonic):
        self.allowlist = allowlist
        self.audit_path = audit
        self.verifier = verifier
        self.run_powershell = run_powershell
        self.click = click
        self.window_at = window_at
        self.clock = clock
        self.pending: dict[str, Proposal] = {}

    # ---------- Protokoll ----------
    def audit(self, event: dict) -> None:
        event = {"ts": _now_iso(), **event}
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ---------- Erlaubnisliste ----------
    def rules(self) -> list[dict]:
        """Gueltige Regeln aus erlaubt.json; Kaputtes zaehlt nicht (im Zweifel fragen)."""
        try:
            data = json.loads(self.allowlist.read_text(encoding="utf-8-sig"))  # von Hand bearbeitet: oft mit BOM
        except (OSError, ValueError):
            return []
        out = []
        for r in data.get("regeln", []) if isinstance(data, dict) else []:
            if not isinstance(r, dict):
                continue
            if r.get("aktion") == "powershell" and isinstance(r.get("befehl"), str) and r["befehl"].strip():
                out.append({"aktion": "powershell", "befehl": r["befehl"].strip()})
            elif r.get("aktion") == "klicken" and isinstance(r.get("fenster"), str) and r["fenster"].strip():
                out.append({"aktion": "klicken", "fenster": r["fenster"].strip()})
        return out

    def allowed(self, p: Proposal) -> bool:
        for r in self.rules():
            if r["aktion"] == p.aktion == "powershell" and r["befehl"] == p.befehl:
                return True
            if r["aktion"] == p.aktion == "klicken" and r["fenster"] == p.fenster:
                return True
        return False

    def remember(self, p: Proposal) -> dict:
        rule = {"aktion": "powershell", "befehl": p.befehl} if p.aktion == "powershell" else \
            {"aktion": "klicken", "fenster": p.fenster}
        rules = self.rules()
        if rule not in rules:
            rules.append(rule)
            tmp = self.allowlist.with_suffix(".tmp")
            tmp.write_text(json.dumps({"regeln": rules}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.allowlist)
        return rule

    # ---------- Pruefen ----------
    def parse(self, req: dict) -> Proposal:
        aktion = req.get("aktion")
        if aktion not in ACTIONS:
            raise ValueError("unbekannte Aktion")
        grund = req.get("grund")
        if not isinstance(grund, str) or not grund.strip() or len(grund) > MAX_REASON_LEN:
            raise ValueError("Grund fehlt oder ist zu lang")
        p = Proposal(id=uuid.uuid4().hex[:8], aktion=aktion, grund=_clean(grund.strip(), MAX_REASON_LEN),
                     created=self.clock())
        if aktion == "powershell":
            cmd = req.get("befehl")
            if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > MAX_COMMAND_LEN:
                raise ValueError("Befehl fehlt oder ist zu lang")
            if _CONTROL_CHARS.search(cmd):
                raise ValueError("Befehl muss eine Zeile ohne Steuerzeichen sein")
            p.befehl = cmd.strip()
        else:
            x, y = req.get("x"), req.get("y")
            if type(x) is not int or type(y) is not int:
                raise ValueError("Klickpunkt muss aus ganzen Zahlen bestehen")
            title = self.window_at(x, y)
            if title is None:
                raise ValueError("Punkt liegt ausserhalb der Bildschirme")
            p.x, p.y, p.fenster = x, y, _clean(title, 200)
        return p

    def _expire(self) -> None:
        now = self.clock()
        for pid in [k for k, v in self.pending.items() if now - v.created > PENDING_TTL_SECS]:
            self.pending.pop(pid)

    # ---------- Ausfuehren ----------
    def execute(self, p: Proposal) -> dict:
        if p.aktion == "powershell":
            return self.run_powershell(p.befehl)
        now_title = self.window_at(p.x, p.y)
        if now_title != p.fenster:
            return {"status": "error", "summary": "Unter dem Punkt liegt inzwischen ein anderes Fenster; "
                    "nichts angeklickt."}
        self.click(p.x, p.y)
        return {"status": "success", "summary": f"Als Admin geklickt in {p.fenster}."}

    # ---------- Anfragen ----------
    def propose(self, req: dict) -> dict:
        self._expire()
        try:
            p = self.parse(req)
        except ValueError as e:
            self.audit({"op": "vorschlagen", "status": "error", "error": str(e)})
            return {"status": "error", "summary": str(e)}
        if p.aktion == "powershell" and blocked(p.befehl):
            self.audit({"op": "vorschlagen", "status": "blocked", **p.public()})
            return {"status": "blocked", "summary": "Das ist auch als Admin gesperrt."}
        if self.allowed(p):
            result = self.execute(p)
            self.audit({"op": "ausgefuehrt", "quelle": "erlaubnisliste", **p.public(), "status": result.get("status")})
            return {**result, "erlaubnisliste": True}
        if len(self.pending) >= MAX_PENDING:
            self.audit({"op": "vorschlagen", "status": "error", "error": "zu viele offene Freigaben"})
            return {"status": "error", "summary": "Zu viele offene Admin-Freigaben."}
        self.pending[p.id] = p
        self.audit({"op": "vorschlagen", "status": "needs_approval", "approval_id": p.id, **p.public()})
        return {
            "status": "needs_approval", "admin": True, "approval_id": p.id,
            **({"fenster": p.fenster} if p.aktion == "klicken" else {}),
            "summary": "Das braucht Adminrechte. Nikolaj muss es in der Oberflaeche freigeben; bis dahin "
            "passiert nichts.",
        }

    async def decide(self, req: dict) -> dict:
        self._expire()
        aid = req.get("approval_id")
        if not isinstance(aid, str) or not APPROVAL_ID.fullmatch(aid) or aid not in self.pending:
            return {"status": "error", "summary": "Unbekannte oder abgelaufene Admin-Freigabe."}
        p = self.pending[aid]
        if req.get("ja") is not True:
            self.pending.pop(aid)
            self.audit({"op": "entscheiden", "approval_id": aid, "status": "rejected", **p.public()})
            return {"status": "rejected", "summary": "Abgelehnt, es wurde nichts ausgefuehrt."}
        token = req.get("token")
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LEN:
            reason = "Freigabe ohne Anmeldung"
        else:
            reason = await self.verifier.check(token)
        if reason:
            self.audit({"op": "entscheiden", "approval_id": aid, "status": "denied", "grund_abgelehnt": reason})
            return {"status": "error", "summary": f"Freigabe nicht anerkannt: {reason}."}
        self.pending.pop(aid)
        result = self.execute(p)
        event = {"op": "ausgefuehrt", "quelle": "freigabe", "approval_id": aid, **p.public(),
                 "status": result.get("status")}
        if req.get("immer") is True:
            event["regel"] = self.remember(p)
            result = {**result, "erlaubnisliste": "aufgenommen"}
        self.audit(event)
        return result

    async def handle(self, req: dict, key: str) -> dict:
        given = req.get("schluessel")
        if not key or not isinstance(given, str) or not hmac.compare_digest(given, key):
            self.audit({"op": str(req.get("op"))[:20], "status": "error", "error": "falscher Schluessel"})
            return {"status": "error", "summary": "nicht berechtigt"}
        op = req.get("op")
        if op == "vorschlagen":
            return self.propose(req)
        if op == "entscheiden":
            return await self.decide(req)
        if op == "liste":
            return {"status": "success", "regeln": self.rules()}
        return {"status": "error", "summary": "unbekannte Anfrage"}


# ---------- Windows ----------
def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # wie der Host: physische Pixel je Monitor
    except (AttributeError, OSError):
        pass


def window_at(x: int, y: int) -> str | None:
    """Titel des Hauptfensters unter einem Punkt; None ausserhalb aller Bildschirme."""
    user32 = ctypes.windll.user32
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # virtueller Bildschirm
    width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    if not (left <= x < left + width and top <= y < top + height):
        return None
    hwnd = user32.WindowFromPoint(ctypes.wintypes.POINT(x, y))
    root = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(root, buf, 256)
    return buf.value


def click(x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


# Fester Systempfad statt Suche ueber PATH: den Benutzer-Teil von PATH darf jedes normale Programm aendern
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def run_powershell(command: str) -> dict:
    try:
        p = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "summary": f"Abgebrochen nach {COMMAND_TIMEOUT_SECS} s."}
    return {
        "status": "success" if p.returncode == 0 else "error",
        "exit_code": p.returncode,
        "stdout": _clean(p.stdout, MAX_OUTPUT_CHARS),
        "stderr": _clean(p.stderr, 2000),
    }


# ---------- Server ----------
async def serve(core: AdminCore, key: str, port: int = PORT) -> asyncio.base_events.Server:
    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            req = json.loads(line)
            reply = await core.handle(req, key) if isinstance(req, dict) else {"status": "error", "summary": "kein Objekt"}
        except (ValueError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            reply = {"status": "error", "summary": "ungueltige Anfrage"}
        writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
        try:
            await writer.drain()
        finally:
            writer.close()

    # Nur Loopback: weder Tailscale noch das Heimnetz erreichen den Helfer
    return await asyncio.start_server(on_connect, "127.0.0.1", port, limit=MAX_LINE)


def _verifier():
    from dotenv import dotenv_values

    from auth_gate import TokenVerifier

    # Nur aus admin.env neben dem Helfer (Admin-Ordner), nie aus voice-core/.env: sonst koennte ein
    # normales Programm eine fremde Supabase-Adresse eintragen und sich selbst Freigaben ausstellen.
    cfg = dotenv_values(HERE / "admin.env")
    return TokenVerifier(cfg.get("SUPABASE_URL"), cfg.get("NIEEEKI_OWNER_ID"), cfg.get("SUPABASE_PUBLISHABLE_KEY"))


async def main() -> None:
    _dpi_aware()
    key = (HERE / "client.key").read_text(encoding="ascii").strip()
    core = AdminCore(HERE / "erlaubt.json", HERE / "admin-audit.jsonl", _verifier(),
                     run_powershell=run_powershell, click=click, window_at=window_at)
    core.audit({"op": "start", "admin": bool(ctypes.windll.shell32.IsUserAnAdmin()), "pid": os.getpid()})
    server = await serve(core, key)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
