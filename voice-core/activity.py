"""Meldet der Oberflaeche live, was nieeeki gerade tut (Aktivitaets-Zeitleiste).

Sitzt direkt hinter dem LLM. Beim Start eines Werkzeugaufrufs schickt er
{"type": "aktivitaet", "status": "laeuft", ...}, beim Ergebnis "fertig", "abgelehnt" oder "fehler".
Argumente werden gekuerzt und nie komplett weitergereicht (getippte Texte koennten privat sein).
"""

from __future__ import annotations

import json

from pipecat.frames.frames import Frame, FunctionCallResultFrame, FunctionCallsStartedFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame

# Welches Argument je Werkzeug als kurzer Hinweis in der Zeitleiste stehen darf
DETAIL_KEYS = {
    "webseite_oeffnen": "url", "programm_oeffnen": "name", "oeffnen_mit": "pfad",
    "fenster_fokussieren": "fenster", "tasten_druecken": "kombination", "websuche": "anfrage",
    "canvas_zeigen": "titel", "auf_bildschirm_klicken": "ziel", "bildschirm_ansehen": "frage",
    "merken": "art", "entwicklung_vorbereiten": "projekt", "maus_scrollen": "richtung",
    "als_admin": "aktion", "ordner_anlegen": "pfad", "github_repo_anlegen": "name", "git_klonen": "repo",
}


def _detail(name: str, args: dict) -> str:
    key = DETAIL_KEYS.get(name)
    if key and isinstance(args, dict) and args.get(key):
        return str(args[key])[:80]
    if name == "tastatur_tippen" and isinstance(args, dict):
        return f"{len(str(args.get('text', '')))} Zeichen"
    return ""


def _parse(result) -> dict | None:
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def highlight_fields(result) -> dict:
    """Fensterrechteck und Klickpunkt aus dem Host-Ergebnis: Lichtrahmen im Schleier der Desktop-App."""
    data = _parse(result) or {}
    out = {}
    for key, size in (("rahmen", 4), ("punkt", 2)):
        value = data.get(key)
        if isinstance(value, list) and len(value) == size and all(type(v) is int for v in value):
            out[key] = value
    return out


def approval_card(name: str, args: dict, result) -> dict | None:
    """PC-Aktion, die Nikolaj freigeben muss (Host: status=needs_approval) -> Karte in der UI."""
    data = _parse(result)
    if not data or data.get("status") != "needs_approval" or not data.get("approval_id"):
        return None
    args = args if isinstance(args, dict) else {}
    if name == "als_admin":
        # Admin: genau das zeigen, was der Helfer nach der Freigabe ausfuehrt
        if args.get("aktion") == "klicken":
            detail = f"Klick bei {args.get('x')},{args.get('y')} in {data.get('fenster') or 'unbekanntes Fenster'}"
        else:
            detail = str(args.get("befehl") or "")
        return {
            "type": "freigaben", "job": "pc", "projekt": "Laptop als Admin",
            "items": [{"id": str(data["approval_id"]), "grund": str(args.get("grund") or "Admin-Aktion")[:120],
                       "detail": detail[:300], "admin": True}],
        }
    return {
        "type": "freigaben", "job": "pc", "projekt": "Laptop",
        "items": [{
            "id": str(data["approval_id"]),
            "grund": str(args.get("reason") or name)[:120],
            "detail": str(args.get("command") or data.get("summary", ""))[:300],
        }],
    }


def _status(result) -> str:
    data = _parse(result)
    if data is None:
        return "fertig"
    if isinstance(data, dict):
        status = str(data.get("status", "")).lower()
        if status == "needs_approval":
            return "wartet"
        if status in ("rejected", "blocked", "duplicate", "nicht_gefunden", "locked"):
            return "abgelehnt"
        if status == "error" or data.get("ok") is False:
            return "fehler"
    return "fertig"


class ActivityReporter(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, FunctionCallsStartedFrame):
            for call in frame.function_calls:
                await self.push_frame(RTVIServerMessageFrame(data={
                    "type": "aktivitaet", "id": call.tool_call_id, "name": call.function_name,
                    "detail": _detail(call.function_name, call.arguments), "status": "laeuft",
                }))
        elif isinstance(frame, FunctionCallResultFrame):
            await self.push_frame(RTVIServerMessageFrame(data={
                "type": "aktivitaet", "id": frame.tool_call_id, "name": frame.function_name,
                "detail": _detail(frame.function_name, frame.arguments),
                "status": "fehler" if frame.error else _status(frame.result),
                **({} if frame.error else highlight_fields(frame.result)),
            }))
            if card := approval_card(frame.function_name, frame.arguments, frame.result):
                await self.push_frame(RTVIServerMessageFrame(data=card))
        await self.push_frame(frame, direction)
