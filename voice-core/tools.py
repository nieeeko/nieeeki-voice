"""Werkzeuge, die nieeeki im Gespraech aufrufen kann (Pipecat-Funktionsaufrufe).

aktuelle_zeit  Datum, Wochentag, Uhrzeit in Nikolajs Zeitzone (sie soll nie raten).
merken         Fakt, Vorliebe oder Lektion dauerhaft ins Gedaechtnis (Supabase, memories).
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from memory import MemoryStore

TZ = ZoneInfo(os.getenv("NIEEEKI_TIMEZONE", "Europe/Berlin"))
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]
MEMORY_KIND = {"fakt": "fact", "vorliebe": "preference", "lektion": "lesson"}

ZEIT_SCHEMA = FunctionSchema(
    name="aktuelle_zeit",
    description=(
        "Liefert Datum, Wochentag und Uhrzeit bei Nikolaj. Immer aufrufen, wenn es um Uhrzeit, "
        "Datum, Wochentag oder relative Tage (heute, morgen, naechste Woche) geht."
    ),
    properties={},
    required=[],
)

MERKEN_SCHEMA = FunctionSchema(
    name="merken",
    description=(
        "Speichert etwas dauerhaft im Gedaechtnis. Aufrufen, wenn Nikolaj sagt 'merk dir ...', "
        "etwas Persoenliches erzaehlt, das spaeter nuetzlich ist, oder dich korrigiert "
        "(dann art=lektion). Keine Passwoerter, Konto- oder Gesundheitsdaten."
    ),
    properties={
        "inhalt": {
            "type": "string",
            "description": "Ein eigenstaendiger Satz in der dritten Person, "
            "z. B. 'Nikolaj trinkt Kaffee schwarz.'",
        },
        "art": {"type": "string", "enum": list(MEMORY_KIND)},
    },
    required=["inhalt", "art"],
)


def jetzt_text(now: datetime | None = None) -> dict:
    now = now or datetime.now(TZ)
    return {
        "datum": f"{WOCHENTAGE[now.weekday()]}, {now.day}. {MONATE[now.month - 1]} {now.year}",
        "uhrzeit": now.strftime("%H:%M"),
        "zeitzone": str(TZ),
    }


def register_tools(llm, memory: MemoryStore) -> ToolsSchema:
    """Registriert die Handler am LLM und liefert das Schema fuer den Kontext."""

    async def aktuelle_zeit(params: FunctionCallParams):
        await params.result_callback(jetzt_text())

    async def merken(params: FunctionCallParams):
        inhalt = str(params.arguments.get("inhalt", "")).strip()
        kind = MEMORY_KIND.get(str(params.arguments.get("art", "")), "fact")
        if not inhalt:
            await params.result_callback({"gespeichert": False, "grund": "leerer Inhalt"})
            return
        if not memory.enabled:
            await params.result_callback({"gespeichert": False, "grund": "Gedaechtnis ist aus"})
            return
        ok = await memory.remember(kind, inhalt, importance=4 if kind == "lesson" else 3)
        logger.info("merken ({}): {!r} -> {}", kind, inhalt, "ok" if ok else "fehlgeschlagen")
        await params.result_callback({"gespeichert": ok})

    llm.register_function("aktuelle_zeit", aktuelle_zeit)
    llm.register_function("merken", merken)
    return ToolsSchema(standard_tools=[ZEIT_SCHEMA, MERKEN_SCHEMA])
