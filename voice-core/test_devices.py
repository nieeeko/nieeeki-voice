"""Tests fuer devices.py: Geraetebindung fuer Weg B (Plan, Schritt 6).

Ausfuehren:  uv run python -m unittest -v test_devices.py   (im Ordner voice-core/)
Kein Netz, keine Datenbank: Fakes und httpx.MockTransport. Der VEKTOR stammt aus echtem WebCrypto (Node, deviceKey.ts):
Er belegt, dass Browser und Voice-Core dieselbe Nachricht, dasselbe Signaturformat und denselben Kurzcode meinen.
"""

import asyncio
import base64
import hashlib
import json
import unittest

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import devices
from devices import (
    DeviceStore,
    DeviceVerifier,
    b64url_decode,
    binding_enabled,
    list_devices,
    load_public_key,
    normalize_code,
    pair,
    pairing_code,
    parse_timestamp,
    remove_device,
    signed_message,
    verifier_from_env,
    verify_signature,
)

OWNER = "11111111-1111-1111-1111-111111111111"

# Erzeugt von ui/src/lib/deviceKey.ts in Node (WebCrypto, ECDSA P-256), ts fest, Signatur einmal gezogen
VEKTOR = {
    "publicKey": "BG-o-TtCddif-uUWbkFc9pfmacnpCnHtHKyeO6VRedaZQoqcWikP2hTgLNs_lP3ih-Nw4F0yBNho2bqqNlVSzjc",
    "code": "3RMK-EX87-8MPT",
    "sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\n",
    "pc_id": "pc-vektor",
    "sig": "Iu7byXabGIRH4smREfZ7BD3qWAoIyw9iQOrr1TsQalHevwE4bRDOlzqCxEOrSWB2w7KkQLREGbBNeL1bth7olA",
    "ts": 1800000000,
    "ohne_pc_id_sig": "q3jZQn2tk0m6mVK9jOrCQw9zobNFObkiHcve3VCfSBM3YrAB5tm6CBGmmBxBSrApBSeMG8EiSz0G_ULt01eDpA",
}
# 1800000000 = 2027-01-15T08:00:00Z
CREATED = "2027-01-15T08:00:00.123456+00:00"
CREATED_TS = 1800000000.123456


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def fresh_key() -> str:
    """Ein anderer, gueltiger oeffentlicher Schluessel (roher Punkt)."""
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return b64u(point)


def offer(**kw):
    base = {"sdp": VEKTOR["sdp"], "type": "offer", "pc_id": VEKTOR["pc_id"], "restart_pc": False,
            "sig": VEKTOR["sig"], "ts": VEKTOR["ts"]}
    base.update(kw)
    return base


def row(created_at=CREATED):
    return {"id": "row-1", "user_id": OWNER, "created_at": created_at, "request": {}}


class FakeDeviceStore:
    def __init__(self, active=None, pending=None, fail=None):
        self._active, self._pending, self.fail = list(active or []), list(pending or []), fail
        self.activated, self.removed, self.purged, self.closed = [], [], 0, False

    def _check(self):
        if self.fail:
            raise self.fail

    async def active(self):
        self._check()
        return list(self._active)

    async def pending(self):
        self._check()
        return list(self._pending)

    async def purge(self):
        self.purged += 1

    async def activate(self, device_id):
        self.activated.append(device_id)
        return device_id != "weg"

    async def remove(self, device_id):
        self.removed.append(device_id)
        return device_id != "weg"

    async def close(self):
        self.closed = True


class KryptoTests(unittest.TestCase):
    def test_vektor_aus_dem_browser_gilt(self):
        message = signed_message(VEKTOR["ts"], VEKTOR["pc_id"], VEKTOR["sdp"])
        self.assertTrue(verify_signature(VEKTOR["publicKey"], message, VEKTOR["sig"]))

    def test_ohne_pc_id_ist_eine_andere_nachricht(self):
        ohne = signed_message(VEKTOR["ts"], None, VEKTOR["sdp"])
        self.assertTrue(verify_signature(VEKTOR["publicKey"], ohne, VEKTOR["ohne_pc_id_sig"]))
        self.assertFalse(verify_signature(VEKTOR["publicKey"], ohne, VEKTOR["sig"]))

    def test_jede_veraenderung_bricht_die_signatur(self):
        pub, sig = VEKTOR["publicKey"], VEKTOR["sig"]
        self.assertFalse(verify_signature(pub, signed_message(VEKTOR["ts"] + 1, VEKTOR["pc_id"], VEKTOR["sdp"]), sig))
        self.assertFalse(verify_signature(pub, signed_message(VEKTOR["ts"], "anderes", VEKTOR["sdp"]), sig))
        self.assertFalse(verify_signature(pub, signed_message(VEKTOR["ts"], VEKTOR["pc_id"], VEKTOR["sdp"] + "x"), sig))
        self.assertFalse(verify_signature(fresh_key(), signed_message(VEKTOR["ts"], VEKTOR["pc_id"], VEKTOR["sdp"]), sig))

    def test_kaputte_eingaben_werden_abgewiesen_statt_zu_werfen(self):
        message = signed_message(VEKTOR["ts"], VEKTOR["pc_id"], VEKTOR["sdp"])
        raw_sig = b64url_decode(VEKTOR["sig"])
        laenge_65 = (b64u(raw_sig[:32] + b"\x00" + raw_sig[32:]), b64u(b"\x00" + raw_sig), b64u(raw_sig + b"\x00"))
        for bad_sig in (None, 5, "", "abc", "!" * 86, VEKTOR["sig"][:-4], VEKTOR["sig"] + "AAAA", VEKTOR["sig"] + "\n",
                        *laenge_65):
            with self.subTest(sig=bad_sig):
                self.assertFalse(verify_signature(VEKTOR["publicKey"], message, bad_sig))
        for bad_key in (None, "", "AAAA", b64u(b"\x04" + b"\x00" * 64), b64u(b"\x02" + b"\x01" * 64), VEKTOR["publicKey"][:-1]):
            with self.subTest(key=bad_key):
                self.assertFalse(verify_signature(bad_key, message, VEKTOR["sig"]))

    def test_base64url_streng(self):
        self.assertEqual(b64url_decode("AAEC"), b"\x00\x01\x02")
        for bad in ("AAE=", "A", "a+b/", "a b", "", None, 5):
            with self.subTest(bad=bad):
                self.assertIsNone(b64url_decode(bad))

    def test_nur_kanonische_schreibweise(self):
        self.assertEqual(b64url_decode("AAA"), b"\x00\x00")
        self.assertIsNone(b64url_decode("AAB"))  # gleiche Bytes, aber Fuellbits gesetzt
        kanonisch = VEKTOR["publicKey"]
        self.assertIsNotNone(load_public_key(kanonisch))
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        i = alphabet.index(kanonisch[-1])
        variante = kanonisch[:-1] + alphabet[i ^ 1]  # nur ein Fuellbit anders
        self.assertEqual(len(variante), len(kanonisch))
        self.assertIsNone(load_public_key(variante))
        self.assertIsNone(pairing_code(variante))

    def test_oeffentlicher_schluessel_muss_auf_der_kurve_liegen(self):
        self.assertIsNotNone(load_public_key(VEKTOR["publicKey"]))
        self.assertIsNone(load_public_key(b64u(b"\x04" + b"\x01" * 64)))  # richtige Form, kein Punkt der Kurve
        self.assertIsNone(load_public_key(b64u(b"\x04" + b"\x01" * 63)))  # zu kurz


class KurzcodeTests(unittest.TestCase):
    def test_vektor_aus_dem_browser(self):
        self.assertEqual(pairing_code(VEKTOR["publicKey"]), VEKTOR["code"])

    def test_fester_vektor_wie_in_deviceKey_test_ts(self):
        raw = bytes([4] + list(range(1, 65)))
        self.assertEqual(pairing_code(b64u(raw)), "B5K4-PL6X-R95R")

    def test_unabhaengige_berechnung(self):
        digest = hashlib.sha256(b64url_decode(VEKTOR["publicKey"])).digest()[:8]
        bits = "".join(f"{b:08b}" for b in digest)[:60]  # die ersten 60 Bit, je 5 Bit ein Zeichen
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        chars = "".join(alphabet[int(bits[i:i + 5], 2)] for i in range(0, 60, 5))
        self.assertEqual(pairing_code(VEKTOR["publicKey"]), f"{chars[:4]}-{chars[4:8]}-{chars[8:]}")

    def test_ungueltiger_schluessel_hat_keinen_code(self):
        self.assertIsNone(pairing_code("a b"))
        self.assertIsNone(pairing_code(None))

    def test_eingabe_ist_nachsichtig_aber_nicht_beliebig(self):
        self.assertEqual(normalize_code("3rmk-ex87-8mpt"), "3RMKEX878MPT")
        self.assertEqual(normalize_code(" 3RMK EX87 8MPT "), "3RMKEX878MPT")
        for bad in ("", "3RMK", "3RMK-EX87", "3RMK-EX87-8MP", "3RMK-EX87-8MPTX", "3RMK-EX87-8MPO", "3RMK-EX87-8MP1",
                    "3RMK-EX87-8MP0", "3RMK_EX87_8MPT"):
            with self.subTest(bad=bad):
                self.assertEqual(normalize_code(bad), "")


class ZeitTests(unittest.TestCase):
    def test_postgrest_formate(self):
        self.assertAlmostEqual(parse_timestamp(CREATED), CREATED_TS, places=3)
        self.assertEqual(parse_timestamp("2027-01-15T08:00:00Z"), 1800000000)
        self.assertEqual(parse_timestamp("2027-01-15T09:00:00+01:00"), 1800000000)
        self.assertEqual(parse_timestamp("2027-01-15T08:00:00"), 1800000000)  # ohne Zone gilt UTC

    def test_unbrauchbares(self):
        for bad in (None, 5, "", "gestern", {}):
            self.assertIsNone(parse_timestamp(bad))


class PrueferTests(unittest.IsolatedAsyncioTestCase):
    def verifier(self, **store_kw):
        self.store = FakeDeviceStore(**store_kw)
        return DeviceVerifier(self.store)

    async def test_gekoppeltes_geraet_mit_frischer_signatur(self):
        v = self.verifier(active=[{"id": "d1", "public_key": VEKTOR["publicKey"]}])
        self.assertTrue(await v(row(), offer()))

    async def test_zweites_geraet_in_der_liste_genuegt(self):
        v = self.verifier(active=[{"id": "d0", "public_key": fresh_key()}, {"id": "d1", "public_key": VEKTOR["publicKey"]}])
        self.assertTrue(await v(row(), offer()))

    async def test_ohne_signatur_oder_zeit_nein(self):
        v = self.verifier(active=[{"id": "d1", "public_key": VEKTOR["publicKey"]}])
        self.assertFalse(await v(row(), {k: x for k, x in offer().items() if k != "sig"}))
        self.assertFalse(await v(row(), {k: x for k, x in offer().items() if k != "ts"}))
        self.assertFalse(await v(row(), offer(ts=True)))
        self.assertFalse(await v(row(), offer(sig=5)))

    async def test_nicht_gekoppeltes_oder_entferntes_geraet_nein(self):
        for active in ([], [{"id": "x", "public_key": fresh_key()}], [{"id": "x", "public_key": "kaputt!"}], [{"id": "x"}]):
            with self.subTest(active=len(active)):
                self.assertFalse(await self.verifier(active=active)(row(), offer()))

    async def test_zeitfenster_gegen_created_at_der_datenbank(self):
        v = self.verifier(active=[{"id": "d1", "public_key": VEKTOR["publicKey"]}])
        self.assertTrue(await v(row("2027-01-15T08:01:00+00:00"), offer()))   # genau 60 s spaeter: noch gueltig
        self.assertTrue(await v(row("2027-01-15T07:59:00+00:00"), offer()))   # genau 60 s frueher (Uhr des Handys geht vor)
        self.assertTrue(await v(row("2027-01-15T08:00:59+00:00"), offer()))   # 59 s spaeter
        self.assertTrue(await v(row("2027-01-15T07:59:01+00:00"), offer()))   # 59 s frueher
        self.assertFalse(await v(row("2027-01-15T08:01:01+00:00"), offer()))  # zu alt
        self.assertFalse(await v(row("2027-01-15T07:58:59+00:00"), offer()))  # zu weit in der Zukunft

    async def test_ohne_created_at_nein(self):
        v = self.verifier(active=[{"id": "d1", "public_key": VEKTOR["publicKey"]}])
        self.assertFalse(await v({"id": "r"}, offer()))
        self.assertFalse(await v(row("gestern"), offer()))

    async def test_signatur_gilt_nur_fuer_dieses_offer(self):
        v = self.verifier(active=[{"id": "d1", "public_key": VEKTOR["publicKey"]}])
        self.assertFalse(await v(row(), offer(sdp=VEKTOR["sdp"] + "a=x\r\n")))       # anderes SDP, gleiche Signatur
        self.assertFalse(await v(row(), offer(pc_id="fremd")))                       # andere Verbindung
        self.assertFalse(await v(row(), offer(pc_id=None)))                          # pc_id weggelassen
        self.assertTrue(await v(row(), offer(pc_id=None, sig=VEKTOR["ohne_pc_id_sig"])))

    async def test_datenbankfehler_wirft_und_laesst_so_nie_durch(self):
        v = self.verifier(fail=httpx.ConnectError("weg"))
        with self.assertRaises(httpx.ConnectError):  # rendezvous.py faengt das ab und lehnt mit "geraet" ab
            await v(row(), offer())

    async def test_close_schliesst_den_store(self):
        v = self.verifier()
        await v.close()
        self.assertTrue(self.store.closed)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def make(self, handler):
        return DeviceStore("https://x.supabase.co", "SERVICE-KEY", "nieeeki_voice_test", OWNER, httpx.MockTransport(handler))

    async def test_active_filtert_auf_besitzer_und_status(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=[{"id": "d1", "public_key": "k"}, "unsinn"])

        store = self.make(handler)
        self.assertEqual(await store.active(), [{"id": "d1", "public_key": "k"}])
        request = seen[0]
        self.assertEqual(request.url.path, "/rest/v1/voice_devices")
        self.assertEqual(request.url.params["user_id"], f"eq.{OWNER}")
        self.assertEqual(request.url.params["status"], "eq.active")
        self.assertEqual(request.headers["accept-profile"], "nieeeki_voice_test")
        self.assertEqual(request.headers["authorization"], "Bearer SERVICE-KEY")
        await store.close()

    async def test_pending_und_fehler(self):
        store = self.make(lambda r: httpx.Response(200, json=[]))
        self.assertEqual(await store.pending(), [])
        failing = self.make(lambda r: httpx.Response(500, json={}))
        with self.assertRaises(httpx.HTTPStatusError):
            await failing.active()
        weird = self.make(lambda r: httpx.Response(200, json={"nicht": "liste"}))
        self.assertEqual(await weird.active(), [])

    async def test_activate_nur_wartende_zeilen_des_besitzers(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=[{"id": "d1"}])

        store = self.make(handler)
        self.assertTrue(await store.activate("d1"))
        request = seen[0]
        self.assertEqual(request.method, "PATCH")
        self.assertEqual(request.url.params["id"], "eq.d1")
        self.assertEqual(request.url.params["status"], "eq.pending")
        self.assertEqual(request.url.params["user_id"], f"eq.{OWNER}")
        body = json.loads(request.content)
        self.assertEqual(body, {"status": "active"})  # Zeitpunkt und Frist liegen bei der Datenbank
        self.assertFalse(await self.make(lambda r: httpx.Response(200, json=[])).activate("d1"))  # nicht mehr wartend

    async def test_remove_und_purge(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=[{"id": "d1"}] if request.method == "DELETE" else [])

        store = self.make(handler)
        self.assertTrue(await store.remove("d1"))
        await store.purge()
        self.assertEqual([r.method for r in seen], ["DELETE", "POST"])
        self.assertEqual(seen[0].url.params["user_id"], f"eq.{OWNER}")
        self.assertEqual(seen[1].url.path, "/rest/v1/rpc/purge_voice_devices")

    def test_enabled_braucht_alles(self):
        self.assertTrue(DeviceStore("u", "k", "s", "o").enabled)
        self.assertFalse(DeviceStore("u", None, "s", "o").enabled)
        self.assertFalse(DeviceStore("u", "k", "s", None).enabled)


class KoppelnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = []
        self.waiting = {"id": "w1", "public_key": VEKTOR["publicKey"], "label": "iPhone", "created_at": CREATED}

    def run_pair(self, store, *answers):
        queue = list(answers)
        return pair(store, input_fn=lambda prompt: queue.pop(0), print_fn=self.out.append)

    async def test_code_passt_und_wird_bestaetigt(self):
        store = FakeDeviceStore(pending=[self.waiting])
        self.assertEqual(await self.run_pair(store, "3rmk ex87 8mpt", "j"), 0)
        self.assertEqual(store.activated, ["w1"])
        self.assertEqual(store.purged, 1)
        self.assertTrue(any("iPhone" in line and "3RMK-EX87-8MPT" in line for line in self.out))
        self.assertTrue(any("ungeprüft" in line for line in self.out))  # Hinweis: das Etikett ist nicht vertrauenswuerdig

    async def test_ohne_bestaetigung_bleibt_alles_wie_es_ist(self):
        store = FakeDeviceStore(pending=[self.waiting])
        for answer in ("", "n", "nein", "x"):
            self.assertEqual(await self.run_pair(store, "3RMK-EX87-8MPT", answer), 1)
        self.assertEqual(store.activated, [])

    async def test_falscher_oder_kaputter_code(self):
        store = FakeDeviceStore(pending=[self.waiting])
        self.assertEqual(await self.run_pair(store, "AAAA-BBBB-CCCC"), 1)
        self.assertEqual(await self.run_pair(store, "3RMK-EX87"), 1)  # nur die ersten 8 Zeichen genuegen nicht mehr
        self.assertEqual(await self.run_pair(store, "3RMK"), 1)
        self.assertEqual(await self.run_pair(store, ""), 1)
        self.assertEqual(store.activated, [])

    async def test_nichts_wartet(self):
        store = FakeDeviceStore()
        self.assertEqual(await self.run_pair(store), 1)
        self.assertTrue(any("Kein Handy wartet" in line for line in self.out))

    async def test_wartende_mit_ungueltigem_schluessel_zaehlen_nicht(self):
        store = FakeDeviceStore(pending=[{"id": "w9", "public_key": b64u(b"\x04" + b"\x01" * 64), "label": "x"}])
        self.assertEqual(await self.run_pair(store), 1)

    async def test_mehrdeutiger_code_gibt_nichts_frei(self):
        twin = {**self.waiting, "id": "w2"}
        store = FakeDeviceStore(pending=[self.waiting, twin])
        self.assertEqual(await self.run_pair(store, "3RMK-EX87-8MPT", "j"), 1)
        self.assertEqual(store.activated, [])

    async def test_wartende_aendern_sich_zwischen_eingabe_und_freigabe(self):
        twin = {**self.waiting, "id": "w2"}

        class Wechselt(FakeDeviceStore):
            async def pending(self_inner):
                self_inner.calls = getattr(self_inner, "calls", 0) + 1
                return [self.waiting] if self_inner.calls == 1 else [self.waiting, twin]  # beim zweiten Lesen ein Zwilling

        store = Wechselt()
        self.assertEqual(await self.run_pair(store, "3RMK-EX87-8MPT", "j"), 1)
        self.assertEqual(store.activated, [])
        self.assertTrue(any("inzwischen geändert" in line for line in self.out))

    async def test_zwischenzeitlich_abgelaufen(self):
        store = FakeDeviceStore(pending=[{**self.waiting, "id": "weg"}])
        self.assertEqual(await self.run_pair(store, "3RMK-EX87-8MPT", "j"), 1)
        self.assertTrue(any("abgelaufen" in line for line in self.out))

    async def test_etikett_vom_handy_gibt_nie_steuerzeichen_aus(self):
        evil = {**self.waiting, "label": "\x1b[2Jiphone\x07‮\u0085"}
        store = FakeDeviceStore(pending=[evil])
        self.assertEqual(await self.run_pair(store, "3RMK-EX87-8MPT", "j"), 0)
        text = "\n".join(self.out)
        for forbidden in ("\x1b", "\x07", "‮", "\u0085"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(devices._label({"label": None}), "ohne Namen")
        self.assertEqual(devices._label({"label": "x" * 100}), "x" * 40)

    async def test_liste_und_entfernen(self):
        store = FakeDeviceStore(active=[{"id": "d1", "public_key": VEKTOR["publicKey"], "label": "iPhone", "approved_at": "heute"}])
        self.assertEqual(await list_devices(store, print_fn=self.out.append), 0)
        self.assertIn("1. iPhone", self.out[0])
        self.assertIn("3RMK-EX87-8MPT", self.out[0])
        prompts = []

        def ask(answer):
            def fn(prompt):
                prompts.append(prompt)
                return answer
            return fn

        for nein in ("", "n", "x"):  # ohne ausdrueckliches "j" bleibt das Geraet
            self.assertEqual(await remove_device(store, "1", input_fn=ask(nein), print_fn=self.out.append), 1)
        self.assertEqual(store.removed, [])
        self.assertTrue(all("iPhone" in p and "3RMK-EX87-8MPT" in p for p in prompts))  # zeigt, welches Geraet gemeint ist
        self.assertEqual(await remove_device(store, "1", input_fn=ask("j"), print_fn=self.out.append), 0)
        self.assertEqual(store.removed, ["d1"])
        for bad in ("0", "2", "abc", "-1", "", "²", "١"):  # auch Ziffern, die isdigit() kennt, int() aber nicht
            self.assertEqual(await remove_device(store, bad, input_fn=ask("j"), print_fn=self.out.append), 1)
        self.assertEqual(store.removed, ["d1"])
        self.assertEqual(await list_devices(FakeDeviceStore(), print_fn=self.out.append), 0)


class RendezvousMitBindungTests(unittest.IsolatedAsyncioTestCase):
    """Zusammenspiel: validate_offer reicht sig/ts durch, der Pruefer entscheidet, die Zeile wird beantwortet oder abgelehnt."""

    class Store:
        def __init__(self):
            self.answers, self.failures = [], []

        async def answer(self, row_id, response):
            self.answers.append(row_id)
            return True

        async def fail(self, row_id, code):
            self.failures.append((row_id, code))
            return True

        async def close(self):
            pass

    class Handler:
        def __init__(self):
            self.requests = []

        async def handle_web_request(self, request, callback):
            self.requests.append(request)
            return {"sdp": "v=0 antwort", "type": "answer", "pc_id": "pc-1"}

    def make(self, **store_kw):
        from rendezvous import Rendezvous

        self.store, self.handler = self.Store(), self.Handler()
        self.verifier_store = FakeDeviceStore(**store_kw)

        async def bot(args):
            return None

        self.rdv = Rendezvous(
            self.store, self.handler, bot, OWNER, args_factory=lambda **kw: kw, verifier=DeviceVerifier(self.verifier_store)
        )

    def zeile(self, **request):
        body = {"sdp": VEKTOR["sdp"], "type": "offer", "pc_id": VEKTOR["pc_id"], "restart_pc": False,
                "sig": VEKTOR["sig"], "ts": VEKTOR["ts"]}
        body.update(request)
        return {"id": "r1", "user_id": OWNER, "created_at": CREATED, "request": body, "status": "claimed"}

    async def test_gekoppeltes_geraet_wird_beantwortet(self):
        self.make(active=[{"id": "d1", "public_key": VEKTOR["publicKey"]}])
        await self.rdv.process(self.zeile())
        self.assertEqual((self.store.answers, self.store.failures), (["r1"], []))
        self.assertEqual(len(self.handler.requests), 1)
        self.assertEqual(self.handler.requests[0].pc_id, VEKTOR["pc_id"])
        self.assertFalse(hasattr(self.handler.requests[0], "sig"))  # die Signatur erreicht den WebRTC-Handler nie

    async def test_alles_andere_wird_mit_geraet_abgelehnt_und_der_handler_bleibt_unberuehrt(self):
        faelle = {
            "ohne Signatur": (dict(sig=None, ts=None), {"id": "d1", "public_key": VEKTOR["publicKey"]}),
            "fremdes SDP": (dict(sdp=VEKTOR["sdp"] + "a=x\r\n"), {"id": "d1", "public_key": VEKTOR["publicKey"]}),
            "anderes Geraet": ({}, {"id": "d2", "public_key": fresh_key()}),
        }
        for name, (request, device) in faelle.items():
            with self.subTest(fall=name):
                self.make(active=[device])
                zeile = self.zeile(**request)
                zeile["request"] = {k: v for k, v in zeile["request"].items() if v is not None}
                await self.rdv.process(zeile)
                self.assertEqual(self.store.failures, [("r1", "geraet")])
                self.assertEqual((self.store.answers, self.handler.requests), ([], []))

    async def test_ausfall_der_geraeteliste_laesst_nichts_durch_und_ist_kein_urteil_ueber_das_geraet(self):
        self.make(fail=httpx.ConnectError("weg"))
        await self.rdv.process(self.zeile())
        self.assertEqual(self.store.failures, [("r1", "abbruch")])  # 5xx: der Client versucht es nach 2 s noch einmal
        self.assertEqual(self.handler.requests, [])
        # Der Wiederholversuch (gleiches Offer) gilt nicht als "wiederholt" und klappt, sobald die Liste wieder da ist
        self.verifier_store.fail = None
        self.verifier_store._active = [{"id": "d1", "public_key": VEKTOR["publicKey"]}]
        await self.rdv.process(self.zeile())
        self.assertEqual(self.store.answers, ["r1"])

    async def test_haengende_geraeteliste_wird_nach_dem_zeitbudget_abgebrochen(self):
        import rendezvous

        class Haengt(FakeDeviceStore):
            async def active(self):
                await asyncio.sleep(30)

        self.make()
        self.rdv._verifier = DeviceVerifier(Haengt())
        original = rendezvous.VERIFY_TIMEOUT_SECS
        rendezvous.VERIFY_TIMEOUT_SECS = 0.05
        try:
            await self.rdv.process(self.zeile())
        finally:
            rendezvous.VERIFY_TIMEOUT_SECS = original
        self.assertEqual(self.store.failures, [("r1", "abbruch")])
        self.assertEqual(self.handler.requests, [])

    async def test_wiederholungsfrist_ist_laenger_als_das_zeitfenster(self):
        import rendezvous

        # Sonst koennte ein beobachtetes, signiertes Offer nach dem Vergessen (Neustart ausgenommen) noch einmal gelten
        self.assertGreater(rendezvous.SEEN_TTL_SECS, 2 * devices.MAX_SKEW_SECS)

    async def test_close_schliesst_auch_die_geraeteliste(self):
        self.make()
        await self.rdv.close()
        self.assertTrue(self.verifier_store.closed)


class EinstellungTests(unittest.TestCase):
    def test_bindung_ist_standardmaessig_an(self):
        self.assertTrue(binding_enabled({}))
        self.assertTrue(binding_enabled({"NIEEEKI_DEVICE_BINDING": "an"}))
        self.assertTrue(binding_enabled({"NIEEEKI_DEVICE_BINDING": "irgendwas"}))
        for off in ("aus", "AUS", " 0 ", "false", "nein", "off"):
            self.assertFalse(binding_enabled({"NIEEEKI_DEVICE_BINDING": off}))

    def test_verifier_from_env(self):
        store = FakeDeviceStore()
        self.assertIsInstance(verifier_from_env({}, store=store), DeviceVerifier)
        self.assertIsNone(verifier_from_env({"NIEEEKI_DEVICE_BINDING": "aus"}, store=store))

    def test_zeitfenster_und_praefix_sind_festgelegt(self):
        self.assertEqual(devices.MAX_SKEW_SECS, 60)
        self.assertEqual(devices.SIGN_PREFIX, "nieeeki-rtc-v1")


if __name__ == "__main__":
    unittest.main()
