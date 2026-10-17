"""Soniox-TTS, das seinen Stream erst beim ersten Text oeffnet.

Pipecat oeffnet den Soniox-Stream schon, wenn Claude zu antworten beginnt (on_turn_context_created),
damit der erste Ton schneller kommt. Braucht Claude aber lange (am 17.10.2026 einmal 13 s), laeuft der
leere Stream in Soniox' Leerlauf-Timeout: erst "408 request_timeout", dann "400 Stream not found",
und die Antwort bleibt stumm. Hier wird der Stream deshalb erst mit dem ersten Satz geoeffnet.
Kostet ein paar Millisekunden, verliert aber keine Antworten mehr.
"""

from __future__ import annotations

from loguru import logger
from websockets.protocol import State

from pipecat.services.soniox.tts import SonioxTTSService


class LazySonioxTTSService(SonioxTTSService):
    async def on_turn_context_created(self, context_id: str):
        # Bewusst nichts tun: kein Stream ohne Text (siehe Modul-Docstring).
        return

    async def run_tts(self, text: str, context_id: str):
        try:
            if not self._websocket or self._websocket.state is State.CLOSED:
                await self._connect()
            await self._send_config(context_id)  # idempotent, nur beim ersten Satz wirksam
        except Exception as e:
            logger.warning(f"{self}: Soniox-Stream {context_id} konnte nicht geoeffnet werden: {e}")
        async for frame in super().run_tts(text, context_id):
            yield frame
