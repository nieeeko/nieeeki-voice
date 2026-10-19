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
}


def _detail(name: str, args: dict) -> str:
    key = DETAIL_KEYS.get(name)
    if key and isinstance(args, dict) and args.get(key):
        return str(args[key])[:80]
    if name == "tastatur_tippen" and isinstance(args, dict):
        return f"{len(str(args.get('text', '')))} Zeichen"
    return ""


def _status(result) -> str:
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (TypeError, json.JSONDecodeError):
        return "fertig"
    if isinstance(data, dict):
        status = str(data.get("status", "")).lower()
        if status in ("rejected", "blocked", "duplicate", "nicht_gefunden"):
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
            }))
        await self.push_frame(frame, direction)
