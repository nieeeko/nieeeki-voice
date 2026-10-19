"""Tests fuer den Admin-Helfer (admin_helper.py): nichts laeuft ohne Freigabe oder Erlaubnisliste.

Ausfuehren:  uv run python -m unittest -v test_admin_helper.py   (im Ordner voice-core/)
Ausfuehrer (PowerShell, Klick, Fenster) sind Attrappen: hier laeuft nie etwas als Admin.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import admin_helper as ah

TOKEN = "sentinel-token-darf-nie-im-log-stehen"


class FakeVerifier:
    """Steht fuer auth_gate.TokenVerifier: nur TOKEN gilt (Nikolaj mit zweitem Faktor)."""

    def __init__(self):
        self.calls = 0

    async def check(self, token):
        self.calls += 1
        return None if token == TOKEN else "Token ungueltig"


class Fixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.allowlist = self.dir / "erlaubt.json"
        self.audit = self.dir / "admin-audit.jsonl"
        self.ran, self.clicked = [], []
        self.windows = {(100, 200): "GIGABYTE CONTROL CENTER", (5, 5): "Desktop"}
        self.t = 1000.0
        self.core = ah.AdminCore(
            self.allowlist, self.audit, FakeVerifier(),
            run_powershell=lambda cmd: self.ran.append(cmd) or {"status": "success", "stdout": "ok"},
            click=lambda x, y: self.clicked.append((x, y)),
            window_at=lambda x, y: self.windows.get((x, y)),
            clock=lambda: self.t,
        )

    def ps(self, befehl="Restart-Service Spooler", grund="Drucker haengt"):
        return self.core.propose({"aktion": "powershell", "befehl": befehl, "grund": grund})

    def log(self):
        return self.audit.read_text(encoding="utf-8") if self.audit.exists() else ""


class OhneFreigabeTests(Fixture):
    async def test_vorschlag_fuehrt_nichts_aus(self):
        r = self.ps()
        self.assertEqual(r["status"], "needs_approval")
        self.assertTrue(r["admin"])
        self.assertRegex(r["approval_id"], r"^[0-9a-f]{8}$")
        self.assertEqual(self.ran, [])

    async def test_ja_ohne_oder_mit_falschem_token_fuehrt_nichts_aus(self):
        aid = self.ps()["approval_id"]
        for token in [None, "", "falsch", "x" * 5000, 123]:
            with self.subTest(token=token):
                r = await self.core.decide({"approval_id": aid, "ja": True, "token": token})
                self.assertEqual(r["status"], "error")
        self.assertEqual(self.ran, [])
        self.assertIn(aid, self.core.pending)  # bleibt offen, Nikolaj kann noch richtig freigeben

    async def test_nur_echtes_ja_zaehlt(self):
        aid = self.ps()["approval_id"]
        r = await self.core.decide({"approval_id": aid, "ja": "true", "token": TOKEN})
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(self.ran, [])

    async def test_unbekannte_und_kaputte_ids(self):
        for aid in [None, "", "../x", "DEADBEEF", "deadbeef1", 12345678, "abcdef12"]:
            with self.subTest(aid=aid):
                r = await self.core.decide({"approval_id": aid, "ja": True, "token": TOKEN})
                self.assertEqual(r["status"], "error")
        self.assertEqual(self.ran, [])

    async def test_abgelaufene_freigabe(self):
        aid = self.ps()["approval_id"]
        self.t += ah.PENDING_TTL_SECS + 1
        r = await self.core.decide({"approval_id": aid, "ja": True, "token": TOKEN})
        self.assertEqual(r["status"], "error")
        self.assertEqual(self.ran, [])


class MitFreigabeTests(Fixture):
    async def test_freigabe_fuehrt_genau_einmal_aus(self):
        aid = self.ps()["approval_id"]
        r = await self.core.decide({"approval_id": aid, "ja": True, "token": TOKEN})
        self.assertEqual(r["status"], "success")
        self.assertEqual(self.ran, ["Restart-Service Spooler"])
        again = await self.core.decide({"approval_id": aid, "ja": True, "token": TOKEN})
        self.assertEqual(again["status"], "error")
        self.assertEqual(len(self.ran), 1)

    async def test_nein_verwirft(self):
        aid = self.ps()["approval_id"]
        r = await self.core.decide({"approval_id": aid, "ja": False})
        self.assertEqual(r["status"], "rejected")
        self.assertNotIn(aid, self.core.pending)
        self.assertEqual(self.ran, [])

    async def test_token_steht_nie_im_protokoll(self):
        aid = self.ps()["approval_id"]
        await self.core.decide({"approval_id": aid, "ja": True, "token": TOKEN, "immer": True})
        await self.core.decide({"approval_id": self.ps("Get-Service x")["approval_id"], "ja": True, "token": "falsch"})
        self.assertNotIn(TOKEN, self.log())
        self.assertNotIn("falsch\"", self.log())


class ErlaubnislisteTests(Fixture):
    async def test_immer_erlauben_nimmt_genau_diesen_befehl_auf(self):
        aid = self.ps()["approval_id"]
        r = await self.core.decide({"approval_id": aid, "ja": True, "token": TOKEN, "immer": True})
        self.assertEqual(r["erlaubnisliste"], "aufgenommen")
        self.assertEqual(json.loads(self.allowlist.read_text())["regeln"],
                         [{"aktion": "powershell", "befehl": "Restart-Service Spooler"}])
        # gleicher Befehl: sofort; anderer Befehl: wieder Freigabe
        self.assertEqual(self.ps()["status"], "success")
        self.assertEqual(self.ps("Restart-Service Spooler; Stop-Computer")["status"], "needs_approval")
        self.assertEqual(self.ran, ["Restart-Service Spooler", "Restart-Service Spooler"])

    async def test_kaputte_liste_zaehlt_nicht(self):
        for content in ["kein json", '{"regeln": "alles"}', '[{"aktion": "powershell", "befehl": "Restart-Service Spooler"}]',
                        '{"regeln": [{"aktion": "powershell", "befehl": ""}, {"aktion": "rm", "befehl": "x"}]}']:
            with self.subTest(content=content):
                self.allowlist.write_text(content, encoding="utf-8")
                self.assertEqual(self.ps()["status"], "needs_approval")
        self.assertEqual(self.ran, [])

    async def test_liste_mit_bom_aus_notepad_oder_powershell(self):
        self.allowlist.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"regeln": [{"aktion": "powershell", "befehl": "Restart-Service Spooler"}]}).encode())
        self.assertEqual(self.ps()["status"], "success")

    async def test_von_hand_definierte_regeln(self):
        self.allowlist.write_text(json.dumps({"regeln": [
            {"aktion": "klicken", "fenster": "GIGABYTE CONTROL CENTER"},
            {"aktion": "powershell", "befehl": "Get-PnpDevice -Class Display"},
        ]}), encoding="utf-8")
        self.assertEqual(self.core.propose({"aktion": "klicken", "x": 100, "y": 200, "grund": "Turbo"})["status"], "success")
        self.assertEqual(self.clicked, [(100, 200)])
        self.assertEqual(self.core.propose({"aktion": "klicken", "x": 5, "y": 5, "grund": "x"})["status"], "needs_approval")
        self.assertEqual(self.ps("Get-PnpDevice -Class Display")["status"], "success")


class SperrenTests(Fixture):
    async def test_gesperrt_auch_als_admin(self):
        for cmd in ["Set-MpPreference -DisableRealtimeMonitoring $true", "bcdedit /set x", "net user bob /add",
                    "Format-Volume -DriveLetter D", "diskpart", "Add-LocalGroupMember -Group Administrators -Member x",
                    r"Set-Content C:\ProgramData\nieeeki\admin\erlaubt.json '{}'", "Unregister-ScheduledTask nieeeki-admin"]:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.ps(cmd)["status"], "blocked")
        self.assertEqual(self.core.pending, {})

    async def test_gesperrt_auch_wenn_auf_der_liste(self):
        self.allowlist.write_text(json.dumps({"regeln": [{"aktion": "powershell", "befehl": "bcdedit /set x"}]}))
        self.assertEqual(self.ps("bcdedit /set x")["status"], "blocked")
        self.assertEqual(self.ran, [])

    async def test_eingaben_werden_geprueft(self):
        bad = [
            {"aktion": "cmd", "befehl": "dir", "grund": "x"},
            {"aktion": "powershell", "befehl": "", "grund": "x"},
            {"aktion": "powershell", "befehl": "Get-Date", "grund": ""},
            {"aktion": "powershell", "befehl": "x" * 1001, "grund": "x"},
            {"aktion": "powershell", "befehl": "Get-Date\nStop-Computer", "grund": "x"},
            {"aktion": "powershell", "befehl": "Get-Date", "grund": "g" * 201},
            {"aktion": "klicken", "x": True, "y": 1, "grund": "x"},
            {"aktion": "klicken", "x": "100", "y": 200, "grund": "x"},
            {"aktion": "klicken", "x": 99999, "y": 99999, "grund": "x"},
        ]
        for req in bad:
            with self.subTest(req=str(req)[:60]):
                self.assertEqual(self.core.propose(req)["status"], "error")
        self.assertEqual((self.ran, self.clicked, self.core.pending), ([], [], {}))

    async def test_klick_nur_wenn_dasselbe_fenster_noch_da_ist(self):
        aid = self.core.propose({"aktion": "klicken", "x": 100, "y": 200, "grund": "Turbo"})["approval_id"]
        self.windows[(100, 200)] = "Eingabeaufforderung (Administrator)"
        r = await self.core.decide({"approval_id": aid, "ja": True, "token": TOKEN})
        self.assertEqual(r["status"], "error")
        self.assertEqual(self.clicked, [])

    async def test_hoechstens_20_offene_freigaben(self):
        for i in range(ah.MAX_PENDING):
            self.assertEqual(self.ps(f"Get-Service s{i}")["status"], "needs_approval")
        self.assertEqual(self.ps("Get-Service zuviel")["status"], "error")


class ServerTests(Fixture):
    async def call(self, port, payload: bytes):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(payload)
        await writer.drain()
        line = await reader.readline()
        writer.close()
        return json.loads(line)

    async def test_schluessel_und_grenzen(self):
        server = await ah.serve(self.core, "k" * 32, port=0)
        port = server.sockets[0].getsockname()[1]
        self.assertEqual(server.sockets[0].getsockname()[0], "127.0.0.1")
        try:
            req = {"op": "vorschlagen", "aktion": "powershell", "befehl": "Get-Date", "grund": "x"}
            r = await self.call(port, (json.dumps({**req, "schluessel": "falsch"}) + "\n").encode())
            self.assertEqual(r["summary"], "nicht berechtigt")
            r = await self.call(port, (json.dumps(req) + "\n").encode())
            self.assertEqual(r["summary"], "nicht berechtigt")
            r = await self.call(port, b"x" * (ah.MAX_LINE + 10) + b"\n")
            self.assertEqual(r["status"], "error")
            r = await self.call(port, b"[1, 2]\n")
            self.assertEqual(r["status"], "error")
            r = await self.call(port, (json.dumps({**req, "schluessel": "k" * 32}) + "\n").encode())
            self.assertEqual(r["status"], "needs_approval")
            self.assertEqual(self.ran, [])
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
