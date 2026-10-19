"""Tests fuer auth_gate.py: Token-Pruefung und Middleware (ohne Netz, eigene Schluessel).

Ausfuehren:  uv run python -m unittest -v test_auth_gate.py   (im Ordner voice-core/)
"""

import time
import unittest
from types import SimpleNamespace

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from auth_gate import OwnerOnlyMiddleware, TokenVerifier

OWNER = "219cc195-1471-4bf6-a8b5-83b6a2fd8e8d"
URL = "https://projekt.supabase.co"
ISS = f"{URL}/auth/v1"
KEY = ec.generate_private_key(ec.SECP256R1())
FREMD = ec.generate_private_key(ec.SECP256R1())
HS_SECRET = "nur-ein-testgeheimnis-fuer-hs256-tokens"


class FakeJwks:
    """Steht fuer PyJWKClient: liefert immer den oeffentlichen Testschluessel."""

    def __init__(self):
        self.calls = 0

    def get_signing_key_from_jwt(self, token):
        self.calls += 1
        return SimpleNamespace(key=KEY.public_key())


def token(key=KEY, alg="ES256", **override):
    now = int(time.time())
    claims = {
        "sub": OWNER,
        "aal": "aal2",
        "aud": "authenticated",
        "iss": ISS,
        "role": "authenticated",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(override)
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "test"})


def verifier(transport=None):
    return TokenVerifier(URL, OWNER, "publishable", jwks_client=FakeJwks(), transport=transport)


class TokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_gueltiges_token(self):
        self.assertIsNone(await verifier().check(token()))

    async def test_ohne_token(self):
        self.assertEqual(await verifier().check(""), "nicht angemeldet")

    async def test_abgelaufen(self):
        self.assertEqual(await verifier().check(token(exp=int(time.time()) - 120)), "Anmeldung abgelaufen")

    async def test_fremdes_konto(self):
        self.assertEqual(await verifier().check(token(sub="jemand-anders")), "falsches Konto")

    async def test_ohne_zweiten_faktor(self):
        self.assertEqual(await verifier().check(token(aal="aal1")), "zweiter Faktor fehlt")

    async def test_falsches_projekt(self):
        reason = await verifier().check(token(iss="https://anderes.supabase.co/auth/v1"))
        self.assertTrue(reason.startswith("Token ungueltig"), reason)

    async def test_falsche_zielgruppe(self):
        reason = await verifier().check(token(aud="anon"))
        self.assertTrue(reason.startswith("Token ungueltig"), reason)

    async def test_fremder_schluessel(self):
        reason = await verifier().check(token(key=FREMD))
        self.assertTrue(reason.startswith("Token ungueltig"), reason)

    async def test_unsigniert_abgelehnt(self):
        unsigned = jwt.encode({"sub": OWNER, "aal": "aal2"}, None, algorithm="none")
        self.assertEqual(await verifier().check(unsigned), "Signaturverfahren none nicht erlaubt")

    async def test_kaputtes_token(self):
        self.assertTrue((await verifier().check("kein.jwt")).startswith("Token ungueltig"))

    async def test_ohne_konfiguration_alles_zu(self):
        v = TokenVerifier(None, None, jwks_client=FakeJwks())
        self.assertIn("ohne", await v.check(token()))

    async def test_cache_spart_schluesselabfrage(self):
        v = verifier()
        t = token()
        await v.check(t)
        await v.check(t)
        self.assertEqual(v._jwks.calls, 1)

    async def test_hs256_prueft_supabase(self):
        def antwort(request):
            self.assertEqual(request.url.path, "/auth/v1/user")
            return httpx.Response(200, json={"id": OWNER})

        v = verifier(httpx.MockTransport(antwort))
        self.assertIsNone(await v.check(token(key=HS_SECRET, alg="HS256")))

    async def test_hs256_von_supabase_abgelehnt(self):
        v = verifier(httpx.MockTransport(lambda r: httpx.Response(401, json={"msg": "invalid"})))
        reason = await v.check(token(key=HS_SECRET, alg="HS256"))
        self.assertTrue(reason.startswith("Token ungueltig"), reason)

    async def test_hs256_abgelaufen_trotz_supabase_ok(self):
        v = verifier(httpx.MockTransport(lambda r: httpx.Response(200, json={"id": OWNER})))
        reason = await v.check(token(key=HS_SECRET, alg="HS256", exp=int(time.time()) - 120))
        self.assertEqual(reason, "Anmeldung abgelaufen")


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def run_mw(self, scope):
        called, sent = [], []

        async def app(scope, receive, send):
            called.append(scope["type"])

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent.append(message)

        await OwnerOnlyMiddleware(app, verifier())(scope, receive, send)
        return called, sent

    def http(self, method="POST", auth=None):
        headers = [(b"authorization", auth.encode())] if auth else []
        return {"type": "http", "method": method, "path": "/start", "headers": headers}

    async def test_websocket_abgelehnt(self):
        called, sent = await self.run_mw({"type": "websocket", "path": "/ws-client", "headers": []})
        self.assertEqual(called, [])
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])

    async def test_preflight_offen(self):
        called, _ = await self.run_mw(self.http("OPTIONS"))
        self.assertEqual(called, ["http"])

    async def test_ohne_token_401(self):
        called, sent = await self.run_mw(self.http())
        self.assertEqual(called, [])
        self.assertEqual(sent[0]["status"], 401)

    async def test_mit_token_durch(self):
        called, _ = await self.run_mw(self.http(auth=f"Bearer {token()}"))
        self.assertEqual(called, ["http"])


if __name__ == "__main__":
    unittest.main()
