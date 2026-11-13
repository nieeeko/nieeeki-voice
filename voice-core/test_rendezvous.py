"""Tests fuer rendezvous.py: Offers aus Supabase holen, ohne dass der Laptop etwas annimmt (Weg B, Schritt 4).

Ausfuehren:  uv run python -m unittest -v test_rendezvous.py   (im Ordner voice-core/)
Fast alles mit Fakes (kein Netz, keine Datenbank, kein WebRTC): geprueft wird die Entscheidung je Zeile.
EchterHandlerTests nutzen den echten Pipecat-Handler mit einem echten aiortc-Client.
"""

import asyncio
import contextlib
import json
import unittest
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

import rendezvous
from rendezvous import (
    PLACEHOLDER,
    Rendezvous,
    SignalStore,
    ice_servers_from_env,
    install_log_filter,
    install_rendezvous,
    validate_offer,
)

OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
KEY = "GEHEIM-SCHLUESSEL"
UNSET = object()


def row(request=None, user_id=OWNER, row_id="row-1"):
    if request is None:
        request = {"sdp": "v=0\r\no=- 1", "type": "offer", "pc_id": None, "restart_pc": False}
    return {"id": row_id, "user_id": user_id, "request": request, "status": "claimed"}


@dataclass
class Args:
    webrtc_connection: Any
    body: Any
    session_id: str


class FakeConnection:
    def __init__(self, connected=True):
        self.connected, self.disconnected = connected, False

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.disconnected = True


class FakeStore:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.answers, self.failures, self.purges, self.claims, self.answer_calls = [], [], [], 0, 0
        self.closed = False
        self.claim_error = self.purge_error = self.fail_error = None
        self.answer_script = []  # Ausnahmen werden geworfen, Wahrheitswerte zurueckgegeben

    async def claim(self, owner):
        self.claims += 1
        if self.claim_error:
            raise self.claim_error
        return self.rows.pop(0) if self.rows else None

    async def answer(self, row_id, response):
        self.answer_calls += 1
        if self.answer_script:
            step = self.answer_script.pop(0)
            if isinstance(step, Exception):
                raise step
            if not step:
                return False
        self.answers.append((row_id, response))
        return True

    async def fail(self, row_id, code):
        if self.fail_error:
            raise self.fail_error
        self.failures.append((row_id, code))
        return True

    async def purge(self, secs):
        self.purges.append(secs)
        if self.purge_error:
            raise self.purge_error

    async def close(self):
        self.closed = True


class FakeHandler:
    def __init__(self, error=None, error_after_callback=False, new_connection=True, delay=0.0, answer=UNSET, connected=True):
        self.requests, self.connections = [], []
        self.error, self.error_after_callback = error, error_after_callback
        self.new_connection, self.delay, self.connected = new_connection, delay, connected
        self._answer = {"sdp": "ANSWER", "type": "answer", "pc_id": "pc-1"} if answer is UNSET else answer
        self.closed = False

    async def handle_web_request(self, request, webrtc_connection_callback):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error and not self.error_after_callback:
            raise self.error
        if self.new_connection:
            connection = FakeConnection(self.connected)
            self.connections.append(connection)
            await webrtc_connection_callback(connection)
        if self.error and self.error_after_callback:
            raise self.error
        return self._answer

    async def close(self):
        self.closed = True


async def fast_sleep(secs):
    await asyncio.sleep(0)


class Fixture(unittest.IsolatedAsyncioTestCase):
    def make(self, store=None, handler=None, verifier=None, args_factory=Args, **kw):
        self.store = store or FakeStore()
        self.handler = handler or FakeHandler()
        self.started = []
        self.cancelled = []

        async def bot(args):
            self.started.append(args)

        kw.setdefault("connect_grace_secs", 0.01)
        self.rdv = Rendezvous(
            self.store, self.handler, bot, OWNER, args_factory=args_factory, verifier=verifier, **kw
        )
        return self.rdv

    def blocking_bot(self):
        """bot(), der laeuft, bis er abgebrochen wird (zeigt, ob ein Task weggeraeumt wurde)."""
        gate = asyncio.Event()

        async def bot(args):
            try:
                await gate.wait()
            except asyncio.CancelledError:
                self.cancelled.append(args)
                raise

        self.rdv._bot = bot
        return gate

    @staticmethod
    def bot_tasks():
        return [t for t in asyncio.all_tasks() if t.get_name() == "rendezvous-bot"]

    async def assert_bots_cancelled(self, tasks):
        """Ein abgebrochener Task, der nie lief, fuehrt seinen except-Zweig nie aus: darum den Task selbst pruefen."""
        await asyncio.sleep(0.01)
        self.assertTrue(tasks)
        self.assertTrue(all(t.cancelled() for t in tasks))
        self.assertEqual(len(self.rdv._tasks), 0)


class ValidateTests(unittest.TestCase):
    def test_gueltiges_offer(self):
        offer = validate_offer({"sdp": "v=0", "type": "offer", "pc_id": None, "restart_pc": False})
        self.assertEqual(offer, {"sdp": "v=0", "type": "offer", "pc_id": None, "restart_pc": False})

    def test_neuverhandlung_und_neustart(self):
        offer = validate_offer({"sdp": "v=0", "type": "offer", "pc_id": "pc-1", "restart_pc": True})
        self.assertEqual((offer["pc_id"], offer["restart_pc"]), ("pc-1", True))

    def test_request_data_des_clients_wird_verworfen(self):
        offer = validate_offer({"sdp": "v=0", "type": "offer", "requestData": {"anlass": "weckwort"}})
        self.assertNotIn("requestData", offer)
        self.assertNotIn("request_data", offer)

    def test_geraetesignatur_wird_durchgereicht_und_streng_geprueft(self):
        offer = validate_offer({"sdp": "v=0", "type": "offer", "sig": "abc", "ts": 1800000000})
        self.assertEqual((offer["sig"], offer["ts"]), ("abc", 1800000000))
        gut = {"sdp": "v=0", "type": "offer"}
        for extra in ({"sig": ""}, {"sig": "x" * 201}, {"sig": 5}, {"ts": "1"}, {"ts": True}, {"ts": 0}, {"ts": -5},
                      {"ts": 1.5}, {"ts": 10**12}):
            with self.subTest(extra=extra):
                self.assertIsNone(validate_offer({**gut, **extra}))

    def test_abweichungen_werden_abgewiesen(self):
        gut = {"sdp": "v=0", "type": "offer"}
        schlecht = [
            None, [], "offer", {},
            {**gut, "extra": 1},                      # unbekannter Schluessel
            {**gut, "type": "answer"},
            {"type": "offer"},                        # ohne sdp
            {"sdp": "", "type": "offer"},
            {"sdp": "x" * 30001, "type": "offer"},
            {"sdp": 42, "type": "offer"},
            {**gut, "pc_id": ""}, {**gut, "pc_id": "x" * 101}, {**gut, "pc_id": 5},
            {**gut, "restart_pc": "ja"},
            {**gut, "requestData": []}, {**gut, "request_data": "x"},
        ]
        for request in schlecht:
            with self.subTest(request=str(request)[:60]):
                self.assertIsNone(validate_offer(request))


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def store(self, handler):
        return SignalStore(
            "https://x.supabase.co", KEY, "nieeeki_voice_test", OWNER, transport=httpx.MockTransport(handler)
        )

    async def test_claim_ruft_die_funktion_mit_dem_eigentuemer(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"], seen["body"], seen["headers"] = str(request.url), json.loads(request.content), request.headers
            return httpx.Response(200, json=[{"id": "r1"}])

        store = self.store(handler)
        self.assertEqual(await store.claim(OWNER), {"id": "r1"})
        self.assertEqual(seen["url"], "https://x.supabase.co/rest/v1/rpc/claim_voice_signal")
        self.assertEqual(seen["body"], {"p_user_id": OWNER})
        self.assertEqual(seen["headers"]["accept-profile"], "nieeeki_voice_test")
        self.assertEqual(seen["headers"]["content-profile"], "nieeeki_voice_test")
        self.assertEqual(seen["headers"]["authorization"], f"Bearer {KEY}")
        await store.close()

    async def test_claim_ohne_offene_zeile(self):
        store = self.store(lambda r: httpx.Response(200, json=[]))
        self.assertIsNone(await store.claim(OWNER))
        await store.close()

    async def test_answer_schreibt_antwort_und_ersetzt_das_offer(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"], seen["body"] = str(request.url), json.loads(request.content)
            return httpx.Response(200, json=[{"id": "r1"}])

        store = self.store(handler)
        self.assertTrue(await store.answer("r1", {"sdp": "A", "type": "answer", "pc_id": "p"}))
        self.assertIn("/voice_signals?id=eq.r1&status=eq.claimed&select=id", seen["url"])  # nur die ID zurueck, nicht das SDP
        self.assertEqual(seen["body"]["status"], "answered")
        self.assertEqual(seen["body"]["response"]["sdp"], "A")
        self.assertEqual(seen["body"]["request"], PLACEHOLDER)
        await store.close()

    async def test_answer_false_wenn_die_zeile_nicht_mehr_uebernommen_ist(self):
        store = self.store(lambda r: httpx.Response(200, json=[]))
        self.assertFalse(await store.answer("r1", {"sdp": "A", "type": "answer"}))
        await store.close()

    async def test_fail_schreibt_nur_einen_kurzcode(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=[{"id": "r1"}])

        store = self.store(handler)
        await store.fail("r1", "form")
        self.assertEqual(seen["body"], {"status": "failed", "response": {"error": "form"}, "request": PLACEHOLDER})
        await store.close()

    async def test_purge(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"], seen["body"] = str(request.url), json.loads(request.content)
            return httpx.Response(200, json=3)

        store = self.store(handler)
        await store.purge(180)
        self.assertTrue(seen["url"].endswith("/rest/v1/rpc/purge_voice_signals"))
        self.assertEqual(seen["body"], {"p_older_than": "180 seconds"})
        await store.close()

    async def test_http_fehler_werden_nicht_verschluckt(self):
        store = self.store(lambda r: httpx.Response(500, json={}))
        with self.assertRaises(httpx.HTTPStatusError):
            await store.claim(OWNER)
        await store.close()

    def test_ohne_zugangsdaten_ist_es_aus(self):
        self.assertFalse(SignalStore(None, "k", "s", OWNER).enabled)
        self.assertFalse(SignalStore("u", None, "s", OWNER).enabled)
        self.assertFalse(SignalStore("u", "k", "s", None).enabled)
        self.assertTrue(SignalStore("u", "k", "s", OWNER).enabled)


class ZeileTests(Fixture):
    async def test_neue_verbindung_startet_bot_mit_eigenem_body(self):
        self.make()
        await self.rdv.process(row())
        await asyncio.sleep(0)  # der bot()-Task laeuft an
        self.assertEqual(len(self.handler.requests), 1)
        request = self.handler.requests[0]
        self.assertEqual((request.sdp, request.type, request.pc_id), ("v=0\r\no=- 1", "offer", None))
        self.assertIsNone(request.request_data)
        self.assertEqual(len(self.started), 1)
        self.assertIs(self.started[0].webrtc_connection, self.handler.connections[0])
        self.assertEqual(self.started[0].body, {"kanal": "phone"})
        self.assertEqual(self.store.answers, [("row-1", {"sdp": "ANSWER", "type": "answer", "pc_id": "pc-1"})])
        self.assertEqual(self.store.failures, [])

    async def test_was_der_client_in_request_data_schickt_erreicht_bot_nie(self):
        self.make()
        request = {"sdp": "v=0", "type": "offer", "requestData": {"body": {"anlass": "weckwort"}}}
        await self.rdv.process(row(request))
        await asyncio.sleep(0)
        self.assertEqual(self.started[0].body, {"kanal": "phone"})
        self.assertIsNone(self.handler.requests[0].request_data)

    async def test_neuverhandlung_startet_kein_neues_bot(self):
        self.make(handler=FakeHandler(new_connection=False))
        await self.rdv.process(row({"sdp": "v=0 neu", "type": "offer", "pc_id": "pc-1", "restart_pc": False}))
        await asyncio.sleep(0)
        self.assertEqual(self.handler.requests[0].pc_id, "pc-1")
        self.assertEqual(self.started, [])
        self.assertEqual(len(self.store.answers), 1)

    async def test_ice_neustart_wird_durchgereicht(self):
        self.make(handler=FakeHandler(new_connection=False))
        await self.rdv.process(row({"sdp": "v=0 neustart", "type": "offer", "pc_id": "pc-1", "restart_pc": True}))
        self.assertTrue(self.handler.requests[0].restart_pc)

    async def test_handler_fehler_ergibt_kurzcode_handler(self):
        self.make(handler=FakeHandler(error=RuntimeError("kaputt")))
        await self.rdv.process(row())
        self.assertEqual(self.store.failures, [("row-1", "handler")])
        self.assertEqual(self.store.answers, [])
        self.assertEqual(self.started, [])

    async def test_haengender_handler_wird_abgebrochen(self):
        self.make(handler=FakeHandler(delay=5), handler_timeout_secs=0.05)
        await self.rdv.process(row())
        self.assertEqual(self.store.failures, [("row-1", "handler")])

    async def test_fehler_nach_dem_callback_raeumt_die_verbindung_weg(self):
        # Review: Der Handler hat die Verbindung samt bot() schon angelegt und scheitert danach.
        self.make(handler=FakeHandler(error=RuntimeError("nach dem Callback"), error_after_callback=True))
        self.blocking_bot()
        await self.rdv.process(row())
        bots = self.bot_tasks()
        self.assertEqual(self.store.failures, [("row-1", "handler")])
        self.assertTrue(self.handler.connections[0].disconnected)
        await self.assert_bots_cancelled(bots)  # der bot()-Task wurde abgebrochen

    async def test_args_factory_wirft_im_callback(self):
        def kaputt(**kw):
            raise TypeError("args")

        self.make(args_factory=kaputt)
        await self.rdv.process(row())
        self.assertEqual(self.store.failures, [("row-1", "handler")])
        self.assertEqual(self.store.answers, [])

    async def test_antwort_ohne_pc_id_wird_verworfen(self):
        self.make(handler=FakeHandler(answer={"sdp": "A", "type": "answer"}))
        self.blocking_bot()
        await self.rdv.process(row())
        bots = self.bot_tasks()
        self.assertEqual(self.store.failures, [("row-1", "handler")])
        self.assertTrue(self.handler.connections[0].disconnected)
        await self.assert_bots_cancelled(bots)

    async def test_leere_antwort_wird_verworfen(self):
        for antwort in (None, {}):
            with self.subTest(antwort=antwort):
                self.make(handler=FakeHandler(answer=antwort, new_connection=False))
                await self.rdv.process(row())
                self.assertEqual(self.store.failures, [("row-1", "handler")])

    async def test_antwort_schreiben_scheitert_einmal_dann_klappt_es(self):
        self.make(sleep=fast_sleep)
        self.store.answer_script = [httpx.ConnectError("kein Netz"), True]
        await self.rdv.process(row())
        self.assertEqual(self.store.answer_calls, 2)
        self.assertEqual(len(self.store.answers), 1)
        self.assertFalse(self.handler.connections[0].disconnected)

    async def test_antwort_schreiben_scheitert_zweimal_verbindung_wird_verworfen(self):
        self.make(sleep=fast_sleep)
        self.blocking_bot()
        self.store.answer_script = [httpx.ConnectError("a"), httpx.ConnectError("b")]
        await self.rdv.process(row())  # wirft nicht
        bots = self.bot_tasks()
        self.assertEqual(self.store.answers, [])
        self.assertTrue(self.handler.connections[0].disconnected)
        await self.assert_bots_cancelled(bots)

    async def test_zeile_nicht_mehr_uebernommen_verbindung_wird_verworfen(self):
        self.make()
        self.blocking_bot()
        self.store.answer_script = [False]  # z. B. von purge oder Ablauf weggeraeumt
        await self.rdv.process(row())
        bots = self.bot_tasks()
        self.assertTrue(self.handler.connections[0].disconnected)
        await self.assert_bots_cancelled(bots)

    async def test_ablehnung_die_nicht_geschrieben_werden_kann_reisst_nicht_mit(self):
        self.make()
        self.store.fail_error = httpx.ConnectError("kein Netz")
        await self.rdv.process(row(user_id=OTHER))  # darf nicht werfen

    async def test_abbruch_mitten_in_der_zeile_gibt_dem_proxy_einen_5xx_code(self):
        self.make(handler=FakeHandler(delay=5))
        task = asyncio.create_task(self.rdv.process(row()))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.failures, [("row-1", "abbruch")])

    async def test_fremder_eigentuemer_wird_abgewiesen(self):
        self.make()
        await self.rdv.process(row(user_id=OTHER))
        self.assertEqual(self.store.failures, [("row-1", "owner")])
        self.assertEqual(self.handler.requests, [])

    async def test_eigentuemer_wird_ohne_gross_klein_verglichen(self):
        self.make()
        await self.rdv.process(row(user_id=OWNER.upper()))
        self.assertEqual(self.store.failures, [])
        self.assertEqual(len(self.store.answers), 1)

    async def test_formfehler_werden_abgewiesen(self):
        self.make()
        await self.rdv.process(row({"sdp": "v=0", "type": "offer", "boese": True}))
        self.assertEqual(self.store.failures, [("row-1", "form")])
        self.assertEqual(self.handler.requests, [])

    async def test_dasselbe_offer_wird_nicht_zweimal_angenommen(self):
        self.make()
        await self.rdv.process(row(row_id="a"))
        await self.rdv.process(row(row_id="b"))
        self.assertEqual([a[0] for a in self.store.answers], ["a"])
        self.assertEqual(self.store.failures, [("b", "wiederholt")])
        self.assertEqual(len(self.handler.requests), 1)

    async def test_wiederholung_verfaellt_nach_der_frist(self):
        now = [0.0]
        self.make(clock=lambda: now[0])
        await self.rdv.process(row(row_id="a"))
        now[0] += 700  # laenger als SEEN_TTL_SECS
        await self.rdv.process(row(row_id="b"))
        self.assertEqual([a[0] for a in self.store.answers], ["a", "b"])

    async def test_geraetebindung_nein_ja_und_fehler(self):
        async def nein(r, o):
            return False

        async def ja(r, o):
            return True

        async def kaputt(r, o):
            raise ValueError("x")

        self.make(verifier=nein)
        await self.rdv.process(row(row_id="n"))
        self.assertEqual(self.store.failures, [("n", "geraet")])
        self.assertEqual(self.handler.requests, [])
        self.make(verifier=kaputt)
        await self.rdv.process(row(row_id="k"))
        self.assertEqual(self.store.failures, [("k", "abbruch")])  # Ausfall des Pruefers: 5xx, kein Urteil ueber das Geraet
        self.assertEqual(self.handler.requests, [])
        self.make(verifier=ja)
        await self.rdv.process(row(row_id="j"))
        self.assertEqual([a[0] for a in self.store.answers], ["j"])

    async def test_close_schliesst_den_pruefer(self):
        class Pruefer:
            closed = False

            async def __call__(self, r, o):
                return True

            async def close(self):
                Pruefer.closed = True

        self.make(verifier=Pruefer())
        await self.rdv.close()
        self.assertTrue(Pruefer.closed)

    async def test_zeile_ohne_id_wird_ignoriert(self):
        self.make()
        await self.rdv.process({"user_id": OWNER, "request": {"sdp": "v=0", "type": "offer"}})
        self.assertEqual((self.store.answers, self.store.failures, self.handler.requests), ([], [], []))

    async def test_nie_sdp_im_log(self):
        meldungen = []
        sink = logger.add(lambda m: meldungen.append(str(m)), level="DEBUG")
        try:
            self.make(handler=FakeHandler(error=RuntimeError("GEHEIMES-SDP-IM-FEHLER")))
            await self.rdv.process(row({"sdp": "v=0\r\nGEHEIMES-SDP", "type": "offer"}))
            await self.rdv.process(row({"sdp": "v=0", "type": "offer", "boese": "GEHEIMES-SDP"}, row_id="x"))
        finally:
            logger.remove(sink)
        self.assertTrue([m for m in meldungen if "abgelehnt (form)" in m])  # der Weg wurde wirklich durchlaufen
        self.assertTrue([m for m in meldungen if "Handler fehlgeschlagen" in m])
        self.assertFalse([m for m in meldungen if "GEHEIMES-SDP" in m])

    async def test_bot_task_bleibt_referenziert_bis_er_fertig_ist(self):
        gate = asyncio.Event()
        self.make()

        async def langsam(args):
            await gate.wait()

        self.rdv._bot = langsam
        await self.rdv._on_connection("c")
        self.assertEqual(len(self.rdv._tasks), 1)
        gate.set()
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.rdv._tasks), 0)

    async def test_abstuerzender_bot_wird_geloggt_und_reisst_nichts_mit(self):
        meldungen = []
        sink = logger.add(lambda m: meldungen.append(str(m)), level="WARNING")
        try:
            self.make()

            async def bricht(args):
                raise RuntimeError("bot kaputt")

            self.rdv._bot = bricht
            await self.rdv._on_connection("c")
            await asyncio.sleep(0.01)
        finally:
            logger.remove(sink)
        self.assertEqual(len(self.rdv._tasks), 0)
        self.assertTrue([m for m in meldungen if "bot() beendet mit Fehler (RuntimeError)" in m])
        self.assertFalse([m for m in meldungen if "bot kaputt" in m])  # nur die Fehlerart


class VerbindungTests(Fixture):
    async def test_verbindung_die_nicht_steht_wird_verworfen(self):
        self.make(handler=FakeHandler(connected=False), connect_grace_secs=0.02)
        self.blocking_bot()
        await self.rdv.process(row())
        self.assertFalse(self.handler.connections[0].disconnected)  # noch Schonfrist
        await asyncio.sleep(0.1)
        self.assertTrue(self.handler.connections[0].disconnected)
        self.assertEqual(len(self.cancelled), 1)

    async def test_stehende_verbindung_bleibt(self):
        self.make(handler=FakeHandler(connected=True), connect_grace_secs=0.02)
        gate = self.blocking_bot()
        await self.rdv.process(row())
        await asyncio.sleep(0.1)
        self.assertFalse(self.handler.connections[0].disconnected)
        self.assertEqual(self.cancelled, [])
        gate.set()

    async def test_neuverhandlung_ohne_neue_verbindung_hat_keinen_waechter(self):
        self.make(handler=FakeHandler(new_connection=False), connect_grace_secs=0.01)
        await self.rdv.process(row({"sdp": "v=0 neu", "type": "offer", "pc_id": "pc-1"}))
        self.assertEqual(len(self.rdv._watchers), 0)

    async def test_beenden_bricht_waechter_sofort_ab_statt_auf_sie_zu_warten(self):
        # Gemessen: close() wartete 3 s auf Waechter, die bis zu CONNECT_GRACE_SECS schlafen.
        self.make(handler=FakeHandler(connected=True), connect_grace_secs=60)
        await self.rdv.process(row())
        self.assertEqual(len(self.rdv._watchers), 1)
        t0 = asyncio.get_running_loop().time()
        await self.rdv.close()
        self.assertLess(asyncio.get_running_loop().time() - t0, 1.0)
        self.assertEqual(len(self.rdv._watchers), 0)
        self.assertTrue(self.store.closed)

    async def test_haengender_handler_blockiert_das_beenden_nicht(self):
        from unittest import mock

        class Haengt(FakeHandler):
            async def close(self):
                await asyncio.sleep(60)

        self.make(handler=Haengt())
        with mock.patch.object(rendezvous, "CLOSE_WAIT_SECS", 0.05):
            t0 = asyncio.get_running_loop().time()
            await self.rdv.close()
        self.assertLess(asyncio.get_running_loop().time() - t0, 2.0)
        self.assertTrue(self.store.closed)  # der Rest wurde trotzdem aufgeraeumt

    async def test_beenden_laesst_laufenden_gespraechen_kurz_zeit_zum_aufraeumen(self):
        from unittest import mock

        self.make()
        aufgeraeumt = []

        async def bot(args):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                aufgeraeumt.append(True)
                raise

        self.rdv._bot = bot
        await self.rdv._on_connection(FakeConnection())
        await asyncio.sleep(0.01)
        with mock.patch.object(rendezvous, "CLOSE_WAIT_SECS", 0.05):
            await self.rdv.close()
        self.assertEqual(aufgeraeumt, [True])  # abgebrochen, nicht stehengelassen
        self.assertEqual(len(self.rdv._tasks), 0)


class SchleifeTests(Fixture):
    def runner(self, polls):
        sleeps = []

        async def sleep(secs):
            sleeps.append(secs)
            if len(sleeps) >= polls:
                raise asyncio.CancelledError

        return sleeps, sleep

    async def test_raeumt_beim_start_auf_und_wartet_wenn_nichts_da_ist(self):
        sleeps, sleep = self.runner(2)
        self.make(sleep=sleep, poll_secs=2.0, purge_older_secs=180)
        with self.assertRaises(asyncio.CancelledError):
            await self.rdv.run()
        self.assertEqual(self.store.purges, [180])  # einmal beim Start
        self.assertEqual(sleeps, [2.0, 2.0])

    async def test_bearbeitet_eine_zeile_und_schaut_gleich_wieder_nach(self):
        sleeps, sleep = self.runner(1)
        self.make(store=FakeStore([row(row_id="a")]), sleep=sleep)
        with self.assertRaises(asyncio.CancelledError):
            await self.rdv.run()
        self.assertEqual([a[0] for a in self.store.answers], ["a"])
        self.assertEqual(self.store.claims, 2)  # nach "a" ohne Pause nochmal, dann leer -> Pause

    async def test_raeumt_regelmaessig_auf(self):
        now = [0.0]
        sleeps = []

        async def sleep(secs):
            sleeps.append(secs)
            now[0] += 30
            if len(sleeps) >= 5:
                raise asyncio.CancelledError

        self.make(sleep=sleep, clock=lambda: now[0], purge_every_secs=60)
        with self.assertRaises(asyncio.CancelledError):
            await self.rdv.run()
        self.assertEqual(len(self.store.purges), 3)  # t=0, t=60, t=120

    async def test_fehlerhaftes_aufraeumen_blockiert_das_abholen_nicht(self):
        # Review: purge und claim stehen nicht im selben Fehlerpfad. Fehlt die Funktion (404), laeuft claim weiter.
        sleeps, sleep = self.runner(3)
        self.make(sleep=sleep, purge_every_secs=60)
        self.store.purge_error = httpx.HTTPStatusError("404", request=httpx.Request("POST", "https://x"), response=httpx.Response(404))
        with self.assertRaises(asyncio.CancelledError):
            await self.rdv.run()
        self.assertEqual(self.store.claims, 3)
        self.assertEqual(len(self.store.purges), 1)  # und nicht jede Runde neu versucht

    async def test_abfragefehler_beenden_die_schleife_nicht_und_die_pause_waechst(self):
        sleeps, sleep = self.runner(4)
        self.make(store=FakeStore(), sleep=sleep, poll_secs=2.0)
        self.store.claim_error = httpx.ConnectError("kein Netz")
        with self.assertRaises(asyncio.CancelledError):
            await self.rdv.run()
        self.assertEqual(sleeps, [2.0, 4.0, 8.0, 16.0])
        self.assertEqual(self.store.claims, 4)

    async def test_pause_ist_nach_oben_begrenzt_und_faellt_nach_erfolg_zurueck(self):
        sleeps, sleep = self.runner(9)
        self.make(store=FakeStore(), sleep=sleep, poll_secs=2.0)
        self.store.claim_error = httpx.ConnectError("kein Netz")
        with self.assertRaises(asyncio.CancelledError):
            await self.rdv.run()
        self.assertEqual(max(sleeps), 30.0)

    async def test_http_fehler_im_log_ohne_schluessel_und_url(self):
        meldungen = []
        sink = logger.add(lambda m: meldungen.append(str(m)), level="DEBUG")
        store = SignalStore("https://geheim-projekt.supabase.co", KEY, "s", OWNER, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        sleeps, sleep = self.runner(2)
        try:
            self.make(store=store, sleep=sleep)
            with self.assertRaises(asyncio.CancelledError):
                await self.rdv.run()
        finally:
            logger.remove(sink)
            await store.close()
        self.assertTrue([m for m in meldungen if "Abfrage fehlgeschlagen" in m or "Aufraeumen fehlgeschlagen" in m])
        self.assertFalse([m for m in meldungen if KEY in m or "geheim-projekt" in m])


class FakeRouter:
    def __init__(self):
        self.events = []

        @contextlib.asynccontextmanager
        async def lifespan(app):
            self.events.append("start")
            yield
            self.events.append("ende")

        self.lifespan_context = lifespan


class FakeApp:
    def __init__(self):
        self.router = FakeRouter()


class InstallTests(unittest.IsolatedAsyncioTestCase):
    def fake_store(self):
        store = FakeStore()
        store.enabled, store.owner_id = True, OWNER
        return store

    async def test_ausgeschaltet_aendert_nichts(self):
        app = FakeApp()
        original = app.router.lifespan_context
        for env in ({}, {"NIEEEKI_RENDEZVOUS": "0"}, {"NIEEEKI_RENDEZVOUS": ""}, {"NIEEEKI_RENDEZVOUS": "nein"}):
            with self.subTest(env=env):
                self.assertIsNone(install_rendezvous(app, lambda a: None, env=env, store=self.fake_store()))
                self.assertIs(app.router.lifespan_context, original)

    async def test_ohne_geraetebindung_nur_mit_ausdruecklicher_zustimmung(self):
        app = FakeApp()
        original = app.router.lifespan_context
        env = {"NIEEEKI_RENDEZVOUS": "1"}
        self.assertIsNone(install_rendezvous(app, lambda a: None, env=env, store=self.fake_store()))
        self.assertIs(app.router.lifespan_context, original)
        env["NIEEEKI_RENDEZVOUS_UNBOUND"] = "nein"
        self.assertIsNone(install_rendezvous(app, lambda a: None, env=env, store=self.fake_store()))

    async def test_mit_geraetebindung_braucht_es_keine_zustimmung(self):
        async def verifier(r, o):
            return True

        rdv = install_rendezvous(
            FakeApp(), lambda a: None, env={"NIEEEKI_RENDEZVOUS": "1"}, store=self.fake_store(),
            handler=FakeHandler(), verifier=verifier,
        )
        self.assertIsInstance(rdv, Rendezvous)

    async def test_schalter_erkennt_mehrere_schreibweisen(self):
        for wert in ("1", "true", "TRUE", "ja", "an", " 1 "):
            with self.subTest(wert=wert):
                rdv = install_rendezvous(
                    FakeApp(), lambda a: None, env={"NIEEEKI_RENDEZVOUS": wert, "NIEEEKI_RENDEZVOUS_UNBOUND": "1"},
                    store=self.fake_store(), handler=FakeHandler(),
                )
                self.assertIsInstance(rdv, Rendezvous)

    async def test_ohne_zugangsdaten_bleibt_es_aus(self):
        app = FakeApp()
        original = app.router.lifespan_context
        store = SignalStore(None, None, "s", None)
        env = {"NIEEEKI_RENDEZVOUS": "1", "NIEEEKI_RENDEZVOUS_UNBOUND": "1"}
        self.assertIsNone(install_rendezvous(app, lambda a: None, env=env, store=store))
        self.assertIs(app.router.lifespan_context, original)

    async def test_eingeschaltet_laeuft_im_lebenszyklus_der_app(self):
        app, store, handler = FakeApp(), self.fake_store(), FakeHandler()

        async def bot(args):
            pass

        rdv = install_rendezvous(
            app, bot, env={"NIEEEKI_RENDEZVOUS": "1", "NIEEEKI_RENDEZVOUS_UNBOUND": "1"}, store=store, handler=handler
        )
        self.assertIsInstance(rdv, Rendezvous)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
            self.assertIn("start", app.router.events)
            self.assertGreaterEqual(store.claims, 1)  # die Schleife laeuft
        self.assertEqual(app.router.events, ["start", "ende"])
        self.assertTrue(store.closed)  # beim Beenden aufgeraeumt
        self.assertTrue(handler.closed)  # Review: auch der Handler wird geschlossen

    async def test_zusammen_mit_dem_lebenszyklus_des_echten_runners(self):
        # Review: Der Runner haengt seinen Lebenszyklus erst nach install_rendezvous an (_add_lifespan_to_app).
        # Mit echtem FastAPI und der echten Funktion des Runners muss unsere Huelle erhalten bleiben.
        from fastapi import FastAPI
        from pipecat.runner.run import _add_lifespan_to_app

        app, store, events = FastAPI(), self.fake_store(), []
        install_rendezvous(
            app, lambda a: None, env={"NIEEEKI_RENDEZVOUS": "1", "NIEEEKI_RENDEZVOUS_UNBOUND": "1"},
            store=store, handler=FakeHandler(),
        )

        @contextlib.asynccontextmanager
        async def runner_lifespan(a):
            events.append("runner-start")
            yield
            events.append("runner-ende")

        _add_lifespan_to_app(app, runner_lifespan)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
            self.assertEqual(events, ["runner-start"])
            self.assertGreaterEqual(store.claims, 1)
        self.assertEqual(events, ["runner-start", "runner-ende"])
        self.assertTrue(store.closed)


class EchterHandlerTests(unittest.IsolatedAsyncioTestCase):
    """Mit der echten Pipecat-Schnittstelle: ein aiortc-Client erzeugt ein Offer, der echte
    SmallWebRTCRequestHandler antwortet. Das beweist, dass die Fakes oben die API nicht schoenen."""

    async def asyncSetUp(self):
        from aiortc import RTCPeerConnection
        from pipecat.runner.types import SmallWebRTCRunnerArguments
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler

        self.started, self.store = [], FakeStore()

        async def bot(args):
            self.started.append(args)

        self.rdv = Rendezvous(
            self.store, SmallWebRTCRequestHandler(), bot, OWNER, args_factory=SmallWebRTCRunnerArguments,
            connect_grace_secs=60,
        )
        self.client = RTCPeerConnection()
        self.client.addTransceiver("audio", direction="sendrecv")
        self.client.addTransceiver("video", direction="recvonly")

    async def asyncTearDown(self):
        logger.configure(patcher=lambda record: None)  # den Log-Filter aus den Tests wieder neutralisieren
        await self.client.close()
        await self.rdv.close()  # schliesst auch den echten Handler und damit die Verbindungen des Voice-Cores
        await asyncio.sleep(0.05)

    async def offer(self, pc_id=None):
        await self.client.setLocalDescription(await self.client.createOffer())
        return {"sdp": self.client.localDescription.sdp, "type": "offer", "pc_id": pc_id, "restart_pc": False}

    async def test_offer_wird_beantwortet_und_der_client_nimmt_die_antwort_an(self):
        from aiortc import RTCSessionDescription

        await self.rdv.process(row(await self.offer()))
        await asyncio.sleep(0.05)
        self.assertEqual(self.store.failures, [])
        self.assertEqual(len(self.store.answers), 1)
        antwort = self.store.answers[0][1]
        self.assertEqual(antwort["type"], "answer")
        self.assertTrue(antwort["sdp"].startswith("v=0"))
        self.assertTrue(antwort["pc_id"])
        await self.client.setRemoteDescription(RTCSessionDescription(sdp=antwort["sdp"], type=antwort["type"]))
        self.assertEqual(len(self.started), 1)  # bot() mit der echten Verbindung und unserem Body
        self.assertEqual(self.started[0].body, {"kanal": "phone"})
        self.assertEqual(type(self.started[0].webrtc_connection).__name__, "SmallWebRTCConnection")

    async def test_neuverhandlung_geht_an_dieselbe_verbindung_ohne_neues_bot(self):
        await self.rdv.process(row(await self.offer(), row_id="r1"))
        pc_id = self.store.answers[0][1]["pc_id"]
        await self.rdv.process(row(await self.offer(pc_id=pc_id), row_id="r2"))
        await asyncio.sleep(0.05)
        self.assertEqual(self.store.failures, [])
        self.assertEqual([a[0] for a in self.store.answers], ["r1", "r2"])
        self.assertEqual(len(self.started), 1)  # nur die erste Verbindung startet bot()

    async def test_unbekannte_pc_id_legt_eine_neue_verbindung_an(self):
        # Z. B. nach einem Neustart des Voice-Cores: Das Handy schickt eine pc_id, die es hier nicht mehr gibt.
        await self.rdv.process(row(await self.offer(pc_id="gibt-es-nicht")))
        await asyncio.sleep(0.05)
        self.assertEqual(self.store.failures, [])
        self.assertEqual(len(self.store.answers), 1)
        self.assertEqual(len(self.started), 1)
        self.assertNotEqual(self.store.answers[0][1]["pc_id"], "gibt-es-nicht")

    async def test_pipecat_loggt_das_sdp_der_filter_verhindert_es(self):
        # aiortc nimmt auch ein kaputtes SDP ohne Fehler an, darum wird der Handler-Fehler erzwungen: Dann laeuft
        # Pipecats echter Fehlerpfad (er loggt das ganze Request-Objekt samt SDP und den Fehlertext).
        from unittest import mock

        ohne, mit = [], []
        boom = mock.AsyncMock(side_effect=RuntimeError("GEHEIMNIS-FEHLERTEXT"))
        patch = mock.patch("pipecat.transports.smallwebrtc.connection.SmallWebRTCConnection.initialize", new=boom)
        with patch:
            sink = logger.add(lambda m: ohne.append(str(m)), level="DEBUG")
            try:
                logger.configure(patcher=lambda record: None)  # ohne Filter ...
                await self.rdv.process(row({"sdp": "v=0\r\nGEHEIMNIS-EINS", "type": "offer"}, row_id="a"))
            finally:
                logger.remove(sink)
            sink = logger.add(lambda m: mit.append(str(m)), level="DEBUG")
            try:
                install_log_filter()  # ... und mit Filter
                await self.rdv.process(row({"sdp": "v=0\r\nGEHEIMNIS-ZWEI", "type": "offer"}, row_id="b"))
            finally:
                logger.remove(sink)
        self.assertTrue([m for m in ohne if "GEHEIMNIS-EINS" in m], "ohne Filter muss Pipecat das SDP loggen, sonst prueft dieser Test nichts")
        self.assertTrue([m for m in ohne if "GEHEIMNIS-FEHLERTEXT" in m])
        self.assertTrue([m for m in mit if "unterdrueckt" in m], "der Filter hat nichts ersetzt")
        self.assertFalse([m for m in mit if "GEHEIMNIS-ZWEI" in m or "GEHEIMNIS-FEHLERTEXT" in m])
        self.assertEqual([f[1] for f in self.store.failures], ["handler", "handler"])

    async def test_install_baut_echten_handler_und_ice_server_aus_der_umgebung(self):
        from pipecat.runner.types import SmallWebRTCRunnerArguments
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler

        store = FakeStore()
        store.enabled, store.owner_id = True, OWNER
        rdv = install_rendezvous(
            FakeApp(),
            lambda args: None,
            env={
                "NIEEEKI_RENDEZVOUS": "1",
                "NIEEEKI_RENDEZVOUS_UNBOUND": "1",
                "NIEEEKI_ICE_SERVERS": '[{"urls": ["stun:stun.example.org:3478"], "user": "unbekannt"}]',
            },
            store=store,
        )
        self.assertIsInstance(rdv._handler, SmallWebRTCRequestHandler)
        self.assertEqual(len(rdv._handler._ice_servers), 1)  # der unbekannte Schluessel "user" brachte keinen Absturz
        args = rdv._args_factory(webrtc_connection="c", body={"kanal": "phone"}, session_id="s")
        self.assertIsInstance(args, SmallWebRTCRunnerArguments)
        self.assertEqual((args.webrtc_connection, args.body), ("c", {"kanal": "phone"}))


class IceTests(unittest.TestCase):
    def test_gueltige_liste(self):
        raw = '[{"urls": ["stun:stun.example.org:3478"]}, {"urls": ["turn:t.example.org"], "username": "u", "credential": "c"}]'
        self.assertEqual(len(ice_servers_from_env(raw)), 2)

    def test_unbekannte_schluessel_werden_verworfen(self):
        raw = '[{"urls": ["stun:a.example.org"], "user": "x", "pass": "y", "username": "u", "credential": "c"}]'
        self.assertEqual(ice_servers_from_env(raw), [{"urls": ["stun:a.example.org"], "username": "u", "credential": "c"}])

    def test_leer_oder_ungueltig_ergibt_keine(self):
        for raw in [None, "", "  ", "kein json", "{}", "[1]", '[{"username": "u"}]', '[{"urls": []}]']:
            with self.subTest(raw=raw):
                self.assertEqual(ice_servers_from_env(raw), [])


class ModulTests(unittest.TestCase):
    def test_modul_importiert_bot_nie(self):
        # Ein zweiter Import von bot haette eine eigene SingleSession: zwei Gespraeche liefen parallel.
        self.assertFalse(hasattr(rendezvous, "bot"))
        source = open(rendezvous.__file__, encoding="utf-8").read()
        self.assertNotIn("import bot", source)


if __name__ == "__main__":
    unittest.main()
