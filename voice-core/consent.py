"""Ja ist Ja: Hat nieeeki gefragt und Nikolaj zugestimmt, fuehrt sie aus, statt erneut zu fragen.

21.10.: "Soll ich ...?" - "Ja" - "Dafuer brauche ich noch ... Soll ich ...?" - "Ja" - ... bis zu
fuenfmal hintereinander. Die Regel im Prompt allein reichte nicht. Dieser Prozessor sitzt direkt
vor Claude: Ist Nikolajs letzte Aeusserung ein kurzes Ja auf nieekis Rueckfrage, haengt er einen
festen Hinweis an, bevor Claude antwortet.
"""

from __future__ import annotations

import re

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

CONSENT_NOTE = (
    "[Hinweis fuer nieeeki: Das ist Nikolajs Ja zu deiner Frage. Fuehre den ganzen Auftrag jetzt aus, "
    "ohne weitere Rueckfrage und ohne den Auftrag zu wiederholen. Fehlt eine Kleinigkeit, nimm das "
    "Naheliegende und sag hinterher, was du angenommen hast.]"
)
YES = re.compile(
    r"^\W*(ja|jo|jep|jup|jawohl|jawoll|genau|okay|ok|klar|alles klar|gerne?|bitte|passt|richtig|stimmt|"
    r"sicher|auf jeden fall|einverstanden|gut|mach|machs?|leg los|los|starte?|weiter|tu das|tu es)\b",
    re.IGNORECASE,
)
NO = re.compile(r"\b(nein|nicht|nee|ne|stopp?|warte|halt|lieber nicht|abbrechen)\b", re.IGNORECASE)
MAX_WORDS = 12  # laengere Saetze sind neue Auftraege, kein reines Ja


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_question(text: str) -> bool:
    t = text.strip().rstrip(" .")
    return t.endswith("?") or "soll ich" in t.lower()


def consent_index(messages: list[dict]) -> int | None:
    """Index der letzten Nachricht, wenn sie Nikolajs Ja auf nieekis letzte Rueckfrage ist."""
    if not messages or messages[-1].get("role") != "user":
        return None
    text = _text(messages[-1].get("content")).strip()
    if not text or CONSENT_NOTE in text or text.startswith("["):
        return None  # Systemhinweise (Pause, Unterbrechung, Freigabe) sind kein Ja
    if not YES.match(text) or NO.search(text) or len(text.split()) > MAX_WORDS:
        return None
    for message in reversed(messages[:-1]):
        if message.get("role") == "assistant":
            said = _text(message.get("content"))
            if said.strip():
                return len(messages) - 1 if _is_question(said) else None
            continue  # nur Werkzeugaufruf ohne Text: weiter zurueck
        return None  # dazwischen etwas anderes (z. B. Werkzeugergebnis): keine offene Frage
    return None


def mark_consent(context) -> bool:
    """Haengt den Hinweis an Nikolajs Ja an. True, wenn markiert wurde."""
    messages = list(context.messages)
    index = consent_index(messages)
    if index is None:
        return False
    message = dict(messages[index])
    content = message.get("content")
    if isinstance(content, list):
        message["content"] = [*content, {"type": "text", "text": CONSENT_NOTE}]
    else:
        message["content"] = f"{_text(content)} {CONSENT_NOTE}"
    messages[index] = message
    context.set_messages(messages)
    return True


class ConsentGuard(FrameProcessor):
    """Sitzt direkt vor dem LLM und markiert ein Ja auf eine Rueckfrage."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            mark_consent(frame.context)
        await self.push_frame(frame, direction)
