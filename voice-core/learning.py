"""Lernen nach dem Gespraech: Haiku liest das Gespraech und haelt neue Fakten, Vorlieben
und Nikolajs Sprechweise fest. Die Eintraege landen in memories und stehen beim naechsten
Gespraech ueber recall_profile() im System-Prompt.

Laeuft im Hintergrund nach dem Auflegen, blockiert das Gespraech also nie.
"""

from __future__ import annotations

import json
import os

import anthropic
from loguru import logger

from memory import MemoryStore

MIN_USER_TURNS = 3
MAX_ENTRIES = 5
TURN_MARKERS = "●○◐"  # Satzende-Marker der LLM-Turn-Erkennung, nicht Teil des Gesagten

SCHEMA = {
    "type": "object",
    "properties": {
        "eintraege": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "art": {"type": "string", "enum": ["fact", "preference", "behavior"]},
                    "inhalt": {"type": "string"},
                    "wichtigkeit": {"type": "integer"},
                },
                "required": ["art", "inhalt", "wichtigkeit"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["eintraege"],
    "additionalProperties": False,
}

SYSTEM = f"""Du wertest ein Gespraech zwischen Nikolaj und seiner Sprachassistentin nieeeki aus.
Nikolajs Saetze stammen aus einer Spracherkennung und koennen falsch erkannte Woerter enthalten.

Halte hoechstens {MAX_ENTRIES} NEUE Eintraege fest, die nieeeki in kuenftigen Gespraechen helfen:
- behavior: Nikolajs Sprechweise, damit nieeeki natuerlicher mit ihm redet. Zum Beispiel
  typische Woerter und Fachbegriffe, Fuellwoerter, wie knapp oder ausfuehrlich er redet, sein Ton,
  wie er Fragen stellt, wie lang er Antworten mag.
- preference: Vorlieben und Abneigungen, auch zu nieekis Antworten.
- fact: Fakten ueber Nikolaj, seine Projekte, Werkzeuge und sein Umfeld.

Regeln:
- Nur was das Gespraech klar belegt. Nichts erfinden, nichts aus einem einzelnen Versprecher ableiten.
- Nichts, was unter "Bereits bekannt" steht, auch nicht umformuliert.
- Jeder Eintrag ist ein eigenstaendiger, kurzer Satz in der dritten Person ("Nikolaj ...").
- Keine Passwoerter, Konto-, Gesundheits- oder Daten ueber andere Personen.
- wichtigkeit von 1 (Detail) bis 5 (praegt jedes Gespraech).
- Gibt es nichts Neues, liefere eine leere Liste."""


def _clean(text: str) -> str:
    return text.lstrip(TURN_MARKERS).strip()


async def learn_from_session(
    memory: MemoryStore, transcript: list[tuple[str, str]], model: str
) -> int:
    """Wertet das Gespraech aus und speichert neue Eintraege. Gibt die Anzahl zurueck."""
    if not memory.enabled:
        return 0
    lines = [(role, _clean(text)) for role, text in transcript if _clean(text)]
    if sum(1 for role, _ in lines if role == "user") < MIN_USER_TURNS:
        logger.debug("Lernen uebersprungen: zu kurzes Gespraech")
        return 0

    known = await memory.recall_profile(limit=100)
    gespraech = "\n".join(
        f"{'Nikolaj' if role == 'user' else 'nieeeki'}: {text}" for role, text in lines
    )
    bekannt = "\n".join(f"- {k}" for k in known) or "(noch nichts)"

    try:
        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = await client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            messages=[
                {
                    "role": "user",
                    "content": f"Bereits bekannt:\n{bekannt}\n\nGespraech:\n{gespraech}",
                }
            ],
        )
    except anthropic.APIStatusError as e:
        logger.warning("Lernen: API-Fehler {} {}", e.status_code, e.message)
        return 0
    except anthropic.APIConnectionError as e:
        logger.warning("Lernen: keine Verbindung ({})", e)
        return 0

    if response.stop_reason in ("refusal", "max_tokens"):
        logger.warning("Lernen: abgebrochen ({})", response.stop_reason)
        return 0
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        entries = json.loads(text)["eintraege"][:MAX_ENTRIES]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Lernen: Antwort nicht lesbar ({})", e)
        return 0

    saved = 0
    for e in entries:
        content = str(e.get("inhalt", "")).strip()
        if not content:
            continue
        importance = min(5, max(1, int(e.get("wichtigkeit", 3))))
        if await memory.remember(e["art"], content, importance=importance):
            saved += 1
            logger.info("Gelernt ({}): {}", e["art"], content)
    return saved
