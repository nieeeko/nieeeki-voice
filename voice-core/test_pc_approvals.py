"""Tests fuer Freigaben von PC-Aktionen: nur per Klick in der UI, nie durch das Modell.

Ausfuehren:  uv run python -m unittest -v test_pc_approvals.py   (im Ordner voice-core/)
"""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import activity
import pc_host


class ModellSiehtKeineFreigabeTests(unittest.TestCase):
    def test_freigabe_werkzeug_nicht_fuers_modell(self):
        for name in ("freigabe_entscheiden", "maus_klicken", "bildschirmfoto"):
            self.assertNotIn(name, pc_host.LLM_TOOLS)

    def test_schluessel_geht_nur_als_umgebung_an_den_host(self):
        with mock.patch.object(pc_host, "MCPClient") as fake:
            client = pc_host.host_client()
        params = fake.call_args.kwargs["server_params"]
        self.assertEqual(set(params.env), {"NIEEEKI_APPROVAL_KEY"})  # keine API-Schluessel des Voice-Core
        self.assertEqual(params.env["NIEEEKI_APPROVAL_KEY"], client.nieeeki_approval_key)
        self.assertEqual(len(client.nieeeki_approval_key), 32)
        self.assertEqual(fake.call_args.kwargs["tools_filter"], pc_host.LLM_TOOLS)


class EntscheidungTests(unittest.IsolatedAsyncioTestCase):
    def host(self, antwort='{"status": "success", "summary": "ok"}'):
        return SimpleNamespace(
            nieeeki_approval_key="k" * 32, _call_tool_text=mock.AsyncMock(return_value=antwort)
        )

    async def test_klick_geht_mit_schluessel_an_den_host(self):
        host = self.host()
        result = await pc_host.decide_pc_approval(host, "a1b2c3d4", True)
        host._call_tool_text.assert_awaited_once_with(
            "freigabe_entscheiden",
            {"approval_id": "a1b2c3d4", "ja": True, "schluessel": "k" * 32, "token": "", "immer": False},
        )
        self.assertEqual(result["status"], "success")

    async def test_admin_freigabe_traegt_token_und_immer(self):
        host = self.host()
        await pc_host.decide_pc_approval(host, "a1b2c3d4", True, "jwt-von-nikolaj", True)
        args = host._call_tool_text.await_args.args[1]
        self.assertEqual((args["token"], args["immer"]), ("jwt-von-nikolaj", True))

    async def test_nur_echte_werte_fuer_token_und_immer(self):
        for token, immer in [(123, "true"), ("x" * 5000, 1), (None, None)]:
            with self.subTest(token=str(token)[:10], immer=immer):
                host = self.host()
                await pc_host.decide_pc_approval(host, "a1b2c3d4", True, token, immer)
                args = host._call_tool_text.await_args.args[1]
                self.assertEqual((args["token"], args["immer"]), ("", False))

    def test_admin_steht_dem_modell_zur_verfuegung_freigabe_nicht(self):
        self.assertIn("als_admin", pc_host.LLM_TOOLS)
        self.assertNotIn("freigabe_entscheiden", pc_host.LLM_TOOLS)

    async def test_unsinnige_ids_gehen_nicht_an_den_host(self):
        host = self.host()
        for aid in [None, "", "../x", "DEADBEEF", "deadbeef1", "a1b2c3d", 12345678]:
            with self.subTest(aid=aid):
                self.assertEqual((await pc_host.decide_pc_approval(host, aid, True))["status"], "error")
        host._call_tool_text.assert_not_awaited()
        self.assertEqual((await pc_host.decide_pc_approval(None, "a1b2c3d4", True))["status"], "error")


class LichtrahmenTests(unittest.TestCase):
    def test_rahmen_und_punkt_werden_weitergereicht(self):
        result = json.dumps({"status": "success", "rahmen": [100, 50, 1900, 1000], "punkt": [400, 300]})
        self.assertEqual(activity.highlight_fields(result), {"rahmen": [100, 50, 1900, 1000], "punkt": [400, 300]})

    def test_nur_gueltige_formen(self):
        for result in [
            '{"rahmen": [1, 2, 3]}', '{"rahmen": "0,0,1,1"}', '{"punkt": [1.5, 2]}',
            '{"punkt": [true, false]}', "kein json", None, '{"status": "success"}',
        ]:
            with self.subTest(result=result):
                self.assertEqual(activity.highlight_fields(result), {})


class KarteTests(unittest.TestCase):
    def test_needs_approval_wird_karte(self):
        result = json.dumps({"status": "needs_approval", "approval_id": "a1b2c3d4", "summary": "Bitte freigeben"})
        card = activity.approval_card(
            "powershell_lesen", {"command": "Stop-Process -Name notepad", "reason": "Notepad schliessen"}, result
        )
        self.assertEqual(card["type"], "freigaben")
        self.assertEqual(card["job"], "pc")
        self.assertEqual(card["items"], [
            {"id": "a1b2c3d4", "grund": "Notepad schliessen", "detail": "Stop-Process -Name notepad"}
        ])
        self.assertEqual(activity._status(result), "wartet")

    def test_admin_karte_zeigt_genau_die_aktion(self):
        result = json.dumps({"status": "needs_approval", "admin": True, "approval_id": "a1b2c3d4"})
        card = activity.approval_card("als_admin", {"aktion": "powershell", "befehl": "Restart-Service Spooler",
                                                    "grund": "Drucker haengt"}, result)
        self.assertEqual(card["items"], [{"id": "a1b2c3d4", "grund": "Drucker haengt",
                                          "detail": "Restart-Service Spooler", "admin": True}])
        self.assertEqual(card["projekt"], "Laptop als Admin")
        klick = json.dumps({"status": "needs_approval", "approval_id": "a1b2c3d4", "fenster": "GIGABYTE CONTROL CENTER"})
        card = activity.approval_card("als_admin", {"aktion": "klicken", "x": 600, "y": 400, "grund": "Turbo"}, klick)
        self.assertEqual(card["items"][0]["detail"], "Klick bei 600,400 in GIGABYTE CONTROL CENTER")

    def test_andere_ergebnisse_ohne_karte(self):
        for result in ['{"status": "success"}', '{"status": "needs_approval"}', "kein json", None]:
            with self.subTest(result=result):
                self.assertIsNone(activity.approval_card("powershell_lesen", {}, result))


if __name__ == "__main__":
    unittest.main()
