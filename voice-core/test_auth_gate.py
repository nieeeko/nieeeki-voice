"""Tests fuer auth_gate.py: Token-Pruefung und Middleware (ohne Netz, eigene Schluessel).

Ausfuehren:  uv run python -m unittest -v test_auth_gate.py   (im Ordner voice-core/)
"""

import time
import unittest
from types import SimpleNamespace

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from auth_gate import (
    MAX_LOGIN_AGE_DESKTOP,
    MAX_LOGIN_AGE_WEB,
    OwnerOnlyMiddleware,
    TokenVerifier,
    allow_preview_origins,
    max_login_age,
)

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


def token(key=KEY, alg="ES256", login_ago=60, **override):
    now = int(time.time())
    claims = {
        "sub": OWNER,
        "aal": "aal2",
        "aud": "authenticated",
        "iss": ISS,
        "role": "authenticated",
        "iat": now,
        "exp": now + 3600,
        # wie Supabase: Passwort, danach Code; beim Erneuern bleiben die Zeitstempel gleich
        "amr": [{"method": "totp", "timestamp": now - login_ago + 20}, {"method": "password", "timestamp": now - login_ago}],
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


class HoechstalterTests(unittest.IsolatedAsyncioTestCase):
    """Desktop (localhost) 1 Woche, Web-UI 1 Tag ab Anmeldung, auch wenn das Token frisch erneuert ist."""

    async def test_desktop_eine_woche(self):
        v = verifier()
        self.assertIsNone(await v.check(token(login_ago=6 * 86400), MAX_LOGIN_AGE_DESKTOP))
        self.assertIn("7 Tage", await v.check(token(login_ago=8 * 86400), MAX_LOGIN_AGE_DESKTOP))

    async def test_web_ein_tag(self):
        v = verifier()
        self.assertIsNone(await v.check(token(login_ago=23 * 3600), MAX_LOGIN_AGE_WEB))
        self.assertIn("1 Tag", await v.check(token(login_ago=25 * 3600), MAX_LOGIN_AGE_WEB))

    async def test_cache_umgeht_das_alter_nicht(self):
        v = verifier()
        t = token(login_ago=2 * 86400)
        self.assertIsNone(await v.check(t, MAX_LOGIN_AGE_DESKTOP))
        self.assertIsNotNone(await v.check(t, MAX_LOGIN_AGE_WEB))  # gleiches Token aus dem Netz

    async def test_ohne_anmeldezeit_abgelehnt(self):
        for amr in [None, [], "password", [{"method": "password"}], [{"method": "password", "timestamp": True}]]:
            with self.subTest(amr=amr):
                self.assertEqual(await verifier().check(token(amr=amr)), "Anmeldezeitpunkt fehlt im Token")

    def test_herkunft_bestimmt_das_alter(self):
        self.assertEqual(max_login_age("http://localhost:3000"), MAX_LOGIN_AGE_DESKTOP)
        self.assertEqual(max_login_age("http://127.0.0.1:3000"), MAX_LOGIN_AGE_DESKTOP)
        for origin in ["https://nieeeki.nieeeko.com", "https://nieeeki-voice-git-x-nieeeko.vercel.app", "", "http://localhost:3001"]:
            with self.subTest(origin=origin):
                self.assertEqual(max_login_age(origin), MAX_LOGIN_AGE_WEB)


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


class VorschauHerkunftTests(unittest.TestCase):
    """CORS wie im Pipecat-Runner (feste Liste), ergaenzt um die Vercel-Vorschauen des Projekts."""

    def client(self, env_regex=None):
        from starlette.applications import Starlette
        from starlette.middleware.cors import CORSMiddleware
        from starlette.testclient import TestClient
        from unittest import mock

        runner = SimpleNamespace(CORSMiddleware=CORSMiddleware)
        env = {} if env_regex is None else {"PIPECAT_ALLOWED_ORIGIN_REGEX": env_regex}
        with mock.patch.dict("os.environ", env):
            allow_preview_origins(runner)
        app = Starlette()
        app.add_middleware(
            runner.CORSMiddleware, allow_origins=["https://nieeeki.nieeeko.com"],
            allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
        )
        return TestClient(app)

    def preflight(self, client, origin):
        return client.options("/start", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        })

    def test_vorschauen_und_produktion_erlaubt(self):
        c = self.client()
        for origin in [
            "https://nieeeki.nieeeko.com",
            "https://nieeeki-voice-git-feat-lichtrahmen-nieeeko.vercel.app",
            "https://nieeeki-voice-a1b2c3d4e-nieeeko.vercel.app",
        ]:
            with self.subTest(origin=origin):
                r = self.preflight(c, origin)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers["access-control-allow-origin"], origin)

    def test_fremde_herkunft_abgelehnt(self):
        c = self.client()
        for origin in [
            "https://evil.vercel.app",
            "https://nieeeki-voice-git-x-andere.vercel.app",
            "https://nieeeki-voice-git-x-nieeeko.vercel.app.evil.com",
            "http://nieeeki-voice-git-x-nieeeko.vercel.app",
            "https://andere-app-nieeeko.vercel.app",
        ]:
            with self.subTest(origin=origin):
                self.assertEqual(self.preflight(c, origin).status_code, 400)

    def test_leeres_muster_schaltet_ab(self):
        c = self.client(env_regex="")
        r = self.preflight(c, "https://nieeeki-voice-git-feat-lichtrahmen-nieeeko.vercel.app")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
