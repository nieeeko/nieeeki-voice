"""Live-Test von SignalStore gegen die echte Datenbank (plans/online-ohne-tailscale.md, offenes Paket 2).

test_rendezvous.py prueft dieselben Aufrufe nur gegen httpx.MockTransport. Hier laufen sie gegen PostgREST und die
Funktionen aus Migration 0002: uebernehmen, antworten, zweite Antwort greift nicht, ablehnen, aufraeumen.

Laeuft nur mit NIEEEKI_LIVE_DB=1 (sonst uebersprungen) und braucht SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY und
NIEEEKI_OWNER_ID (aus der .env im Projektordner oder der Umgebung). Immer im Schema nieeeki_voice_test, nie in DEV:
Dort fragt der laufende Voice-Core alle 2 s ab und wuerde die Probezeilen selbst uebernehmen. Achtung: purge() ist ein
RPC ueber das ganze Schema und entfernt dort auch fremde Zeilen, die aelter als 2 Minuten sind (im TEST-Schema egal).

Ausfuehren:  NIEEEKI_LIVE_DB=1 uv run python -m unittest -v test_rendezvous_live.py   (im Ordner voice-core/)
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from rendezvous import PLACEHOLDER, SignalStore

SCHEMA = "nieeeki_voice_test"
LIVE = os.getenv("NIEEEKI_LIVE_DB") == "1"
OFFER = {"sdp": "v=0\r\ns=live-test", "type": "offer"}


@unittest.skipUnless(LIVE, "nur mit NIEEEKI_LIVE_DB=1 (schreibt Probezeilen in nieeeki_voice_test)")
class LiveStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        self.store = SignalStore(
            os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"), SCHEMA, os.getenv("NIEEEKI_OWNER_ID")
        )
        if not self.store.enabled:
            self.skipTest("SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY oder NIEEEKI_OWNER_ID fehlt")
        self.owner = self.store.owner_id or ""
        self.http = self.store._http()
        self.ids: list[str] = []
        self.addAsyncCleanup(self._cleanup)
        # Nie fremde Zeilen anfassen: Der Test verlangt eine leere Tabelle fuer den Eigentuemer
        self.assertEqual(await self._rows(), [], "voice_signals im TEST-Schema ist nicht leer; nichts angefasst")

    async def _cleanup(self) -> None:
        for row_id in self.ids:
            await self.http.delete(f"/voice_signals?id=eq.{row_id}")
        await self.store.close()

    async def _rows(self) -> list[dict]:
        r = await self.http.get(
            f"/voice_signals?user_id=eq.{self.owner}&select=id,status,request,response&order=created_at.asc"
        )
        r.raise_for_status()
        return r.json()

    async def _row(self, row_id: str) -> dict:
        return next(row for row in await self._rows() if row["id"] == row_id)

    async def _insert(self, **columns: str) -> str:
        r = await self.http.post("/voice_signals", json={"user_id": self.owner, "request": OFFER, **columns})
        r.raise_for_status()
        row_id = r.json()[0]["id"]
        self.ids.append(row_id)
        return row_id

    async def test_uebernehmen_antworten_ablehnen_aufraeumen(self) -> None:
        # Uebernehmen: genau die offene Zeile, danach ist nichts mehr offen
        first = await self._insert()
        claimed = await self.store.claim(self.owner)
        assert claimed is not None
        self.assertEqual((claimed["id"], claimed["status"], claimed["request"]), (first, "claimed", OFFER))
        self.assertIsNone(await self.store.claim(self.owner))

        # Antworten: Die Antwort steht da, das Offer ist durch den Platzhalter ersetzt
        answer = {"sdp": "v=0\r\ns=antwort", "type": "answer", "pc_id": "live-test"}
        self.assertTrue(await self.store.answer(first, answer))
        row = await self._row(first)
        self.assertEqual((row["status"], row["response"], row["request"]), ("answered", answer, PLACEHOLDER))

        # Eine zweite Antwort und ein spaetes Ablehnen greifen nicht (nur 'claimed' laesst sich aendern)
        self.assertFalse(await self.store.answer(first, {"sdp": "anders", "type": "answer"}))
        self.assertFalse(await self.store.fail(first, "abbruch"))
        row = await self._row(first)
        self.assertEqual((row["status"], row["response"]), ("answered", answer))

        # Ablehnen: Kurzcode als Antwort, Offer ebenfalls ersetzt
        second = await self._insert()
        claimed = await self.store.claim(self.owner)
        assert claimed is not None
        self.assertEqual(claimed["id"], second)
        self.assertTrue(await self.store.fail(second, "geraet"))
        row = await self._row(second)
        self.assertEqual((row["status"], row["response"], row["request"]), ("failed", {"error": "geraet"}, PLACEHOLDER))

        # Aufraeumen: Eine abgelaufene Zeile wird nie uebernommen; purge entfernt sie, laesst aber alles, was
        # juenger als 2 Minuten ist (Fenster des Rate-Limits), auch bei "0 Sekunden"
        old_created = datetime.now(timezone.utc) - timedelta(minutes=5)
        old = await self._insert(
            created_at=old_created.isoformat(), expires_at=(old_created + timedelta(seconds=60)).isoformat()
        )
        self.assertIsNone(await self.store.claim(self.owner))
        self.assertEqual([r["id"] for r in await self._rows()], [old, first, second])
        await self.store.purge(0)
        self.assertEqual([r["id"] for r in await self._rows()], [first, second])


if __name__ == "__main__":
    unittest.main()
