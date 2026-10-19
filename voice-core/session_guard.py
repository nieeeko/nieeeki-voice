"""Nur ein Gespraech gleichzeitig: ein neues beendet das laufende.

Am 19.10.2026 sprachen zwei Pipelines parallel: die Oberflaeche hatte ihr Gespraech beim Neuladen
nicht beendet (die Verbindung lief unsichtbar weiter), dann startete das Weckwort ein zweites, und
dessen Begruessung lief mitten in die Antwort des ersten. nieeeki hat nur einen Nutzer; zwei
gleichzeitige Gespraeche sind nie gewollt. Das neue gewinnt (z. B. Handy uebernimmt vom Laptop).
"""

from __future__ import annotations

from loguru import logger


class SingleSession:
    def __init__(self):
        self.current = None

    async def take(self, connection) -> None:
        """Neues Gespraech anmelden; ein laufendes wird getrennt (dessen Aufraeumen laeuft normal)."""
        old, self.current = self.current, connection
        if old is None or old is connection:
            return
        logger.info("Neues Gespraech: das laufende wird beendet (nur eines gleichzeitig)")
        try:
            await old.disconnect()
        except Exception as e:  # die alte Verbindung kann schon halb weg sein
            logger.warning(f"Altes Gespraech liess sich nicht sauber trennen: {e}")

    def release(self, connection) -> None:
        """Gespraech beendet: freigeben, falls nicht schon ein neueres uebernommen hat."""
        if self.current is connection:
            self.current = None
