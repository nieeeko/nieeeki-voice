"""Zugangskontrolle fuer den Voice-Core: nur Nikolaj mit zweitem Faktor startet Sitzungen.

Jede HTTP-Anfrage an den Pipecat-Runner (/start, /sessions/.../api/offer, ICE-Updates, /client)
braucht das Supabase-Access-Token der Oberflaeche im Header "Authorization: Bearer ...".
Geprueft wird: Signatur, Ablauf, Projekt (iss), Zielgruppe (aud), sub = NIEEEKI_OWNER_ID und
aal2 (Passwort plus Code aus der Authenticator-App). Der CORS-Preflight (OPTIONS) bleibt offen,
er traegt keine Daten. WebSockets lehnt der Voice-Core ganz ab: nieeeki nutzt nur WebRTC.

Signatur: ES256/RS256 lokal ueber die oeffentlichen Schluessel des Projekts (JWKS, gecacht).
Stellt das Projekt noch HS256-Tokens aus, prueft Supabase selbst (GET /auth/v1/user).
"""

import asyncio
import hashlib
import json
import os
import time

import httpx
import jwt
from loguru import logger

CACHE_SECS = 60  # gepruefte Tokens so lange merken: ICE-Updates kommen im Sekundentakt
LEEWAY_SECS = 30  # Uhren von Laptop und Supabase duerfen etwas auseinanderlaufen


class TokenVerifier:
    def __init__(
        self,
        supabase_url: str | None,
        owner_id: str | None,
        api_key: str | None = None,
        *,
        jwks_client=None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.configured = bool(supabase_url and owner_id)
        self.issuer = f"{(supabase_url or '').rstrip('/')}/auth/v1"
        self.owner_id = owner_id
        self.api_key = api_key
        self._jwks = jwks_client
        if self._jwks is None and self.configured:
            self._jwks = jwt.PyJWKClient(
                f"{self.issuer}/.well-known/jwks.json", cache_keys=True, lifespan=600
            )
        self._transport = transport  # nur fuer Tests
        self._ok_until: dict[str, float] = {}

    async def check(self, token: str) -> str | None:
        """None, wenn das Token Nikolaj mit zweitem Faktor gehoert, sonst der Ablehnungsgrund."""
        if not self.configured:
            return "Voice-Core ohne SUPABASE_URL oder NIEEEKI_OWNER_ID"
        if not token:
            return "nicht angemeldet"
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        if self._ok_until.get(key, 0) > now:
            return None
        try:
            alg = jwt.get_unverified_header(token).get("alg")
            if alg in ("ES256", "RS256"):
                signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
                claims = jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=[alg],
                    audience="authenticated",
                    issuer=self.issuer,
                    leeway=LEEWAY_SECS,
                    options={"require": ["exp", "sub", "iss", "aud"]},
                )
            elif alg == "HS256":
                claims = await self._check_remote(token)
            else:
                return f"Signaturverfahren {alg} nicht erlaubt"
        except jwt.ExpiredSignatureError:
            return "Anmeldung abgelaufen"
        except (jwt.PyJWTError, httpx.HTTPError) as e:
            return f"Token ungueltig ({type(e).__name__})"

        if claims.get("sub") != self.owner_id:
            return "falsches Konto"
        if claims.get("aal") != "aal2":
            return "zweiter Faktor fehlt"
        self._ok_until = {k: v for k, v in self._ok_until.items() if v > now}
        self._ok_until[key] = min(float(claims["exp"]), now + CACHE_SECS)
        return None

    async def _check_remote(self, token: str) -> dict:
        async with httpx.AsyncClient(timeout=5, transport=self._transport) as client:
            response = await client.get(
                f"{self.issuer}/user",
                headers={"Authorization": f"Bearer {token}", "apikey": self.api_key or ""},
            )
        if response.status_code != 200:
            raise jwt.InvalidTokenError(f"Supabase lehnt ab ({response.status_code})")
        # Die Signatur hat Supabase geprueft; Ablauf, Projekt und Zielgruppe pruefen wir selbst.
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
                "require": ["exp", "sub", "iss", "aud"],
            },
            audience="authenticated",
            issuer=self.issuer,
            leeway=LEEWAY_SECS,
        )
        if response.json().get("id") != claims.get("sub"):
            raise jwt.InvalidTokenError("Token und Supabase-Nutzer passen nicht zusammen")
        return claims


class OwnerOnlyMiddleware:
    """ASGI-Middleware vor dem Pipecat-Runner (eingehaengt ueber install_owner_gate)."""

    def __init__(self, app, verifier: TokenVerifier):
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http" or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return

        auth = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"), "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        reason = await self.verifier.check(token)
        if reason is None:
            await self.app(scope, receive, send)
            return

        logger.warning(f"Zugang abgelehnt: {scope['method']} {scope['path']} ({reason})")
        body = json.dumps({"error": "nicht berechtigt", "detail": reason}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
            }
        )
        await send({"type": "http.response.body", "body": body})


# Vercel-Vorschauen dieses Projekts (z. B. nieeeki-voice-git-<branch>-nieeeko.vercel.app). Sie sind nur mit
# Vercel-Anmeldung im Team erreichbar (Projekt-Schutz "all_except_custom_domains", geprueft am 19.10.2026);
# herein kommt trotzdem nur, wer Nikolajs Login mit zweitem Faktor hat (OwnerOnlyMiddleware).
DEFAULT_PREVIEW_ORIGIN_REGEX = r"https://nieeeki-voice-[a-z0-9-]+-nieeeko\.vercel\.app"


def allow_preview_origins(runner) -> str | None:
    """Laesst im CORS des Pipecat-Runners zusaetzlich die Vercel-Vorschauen zu (Muster statt fester Liste).

    Der Runner kennt nur PIPECAT_ALLOWED_ORIGINS als Liste; er baut seine CORSMiddleware erst in main().
    Hier wird die Klasse im Runner-Modul durch eine Variante mit allow_origin_regex ersetzt.
    PIPECAT_ALLOWED_ORIGIN_REGEX="" schaltet das ab.
    """
    pattern = os.getenv("PIPECAT_ALLOWED_ORIGIN_REGEX", DEFAULT_PREVIEW_ORIGIN_REGEX).strip()
    if not pattern:
        return None
    base = runner.CORSMiddleware

    class PreviewCORSMiddleware(base):
        def __init__(self, app, **kwargs):
            kwargs.setdefault("allow_origin_regex", pattern)
            super().__init__(app, **kwargs)

    runner.CORSMiddleware = PreviewCORSMiddleware
    return pattern


def install_owner_gate(app) -> TokenVerifier:
    """Haengt die Zugangskontrolle vor alle Routen des Runners. Ohne Konfiguration: alles zu."""
    verifier = TokenVerifier(
        os.getenv("SUPABASE_URL"),
        os.getenv("NIEEEKI_OWNER_ID"),
        os.getenv("SUPABASE_PUBLISHABLE_KEY"),
    )
    if not verifier.configured:
        logger.error("Zugangskontrolle: SUPABASE_URL oder NIEEEKI_OWNER_ID fehlt, alles wird abgelehnt")
    app.add_middleware(OwnerOnlyMiddleware, verifier=verifier)
    return verifier
