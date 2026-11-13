"""Rendezvous: WebRTC-Offers aus Supabase abholen, ohne dass der Laptop etwas annimmt.

Weg B aus plans/online-ohne-tailscale.md, Schritt 4. Der Browser schickt sein Offer an /api/rtc (Vercel),
die Route legt eine Zeile in voice_signals an. Dieses Modul fragt nur ausgehend nach (alle POLL_SECS),
uebernimmt die Zeile atomar (claim_voice_signal), gibt das Offer dem WebRTC-Handler im Prozess und schreibt
die Antwort zurueck. Am Laptop lauscht dafuer nichts im Netz.

Aus: NIEEEKI_RENDEZVOUS=1 schaltet es ein. Ohne den Schalter aendert sich nichts. Ohne Geraetebindung
(verifier, Plan Schritt 6) laeuft es nur mit zusaetzlich NIEEEKI_RENDEZVOUS_UNBOUND=1: Dann schuetzt allein das
Supabase-Token den Weg (Tailscale schuetzt nur den direkten), das soll eine bewusste Entscheidung sein.

Sicherheit (die Datenbank prueft schon aal2, Anmeldealter, Profil, Form und Rate-Limit; hier gilt es nochmal,
weil dieser Prozess PC-Werkzeuge ausloest):
  * Nur Zeilen des Eigentuemers (NIEEEKI_OWNER_ID).
  * Der Body wird streng geprueft (nur bekannte Schluessel). Was der Client unter requestData schickt, geht
    NIE an bot(): den Body erzeugt dieses Modul selbst (bot() liest daraus nur "anlass" und "kanal").
  * Dasselbe Offer (gleicher SDP-Hash) wird nicht zweimal angenommen. Das ist keine Sicherheitsgrenze (wer das
    Token hat, erzeugt ein frisches Offer), sondern schuetzt vor Dopplern; ein Client erzeugt bei jedem Versuch
    ein neues SDP.
  * Optional prueft ein verifier die Geraetebindung (Plan, Schritt 6).
  * Es wird immer nur ein Offer zugleich bearbeitet (die Schleife laeuft der Reihe nach), auch wenn die
    Datenbank bis zu 6 Zeilen je Minute zulaesst.
  * Offers mit bekannter pc_id (Neuverhandlung, ICE-Neustart) gehen an dieselbe Verbindung und starten kein
    neues bot(). Eine unbekannte pc_id (z. B. nach einem Neustart) legt eine neue Verbindung an.
  * bot() uebernimmt ein laufendes Gespraech erst, wenn die Verbindung wirklich steht (bot.py), nicht schon
    beim Offer. Kommt innerhalb von CONNECT_GRACE_SECS keine Verbindung zustande, wird sie verworfen.
  * Scheitert etwas nach dem Anlegen der Verbindung (Handler, Antwort schreiben), wird sie samt bot()-Task
    wieder verworfen: keine Geister-Sitzungen.
  * Die Offer-Zeile wird mit der Antwort durch einen Platzhalter ersetzt (SDP enthaelt IP-Adressen). Die Antwort
    bleibt, bis der Proxy sie gelesen hat, und geht mit purge_voice_signals (nie juenger als 2 Minuten).
  * Nie SDP oder Zeileninhalt loggen: Fehler nur als Kurzcode in die Zeile, im Log nur die Fehlerart. Pipecat
    loggt bei Handler-Fehlern selbst SDP; dafuer filtert install_log_filter (aktiv, sobald das Rendezvous laeuft).

Kurzcodes in der Zeile (Vertrag mit ui/src/lib/rtcProxy.ts; 4xx beendet den Client, 5xx laesst ihn neu versuchen):
  form, owner, wiederholt, geraet -> 4xx;  handler, abbruch -> 5xx.
  "geraet" nur, wenn der Pruefer das Geraet verneint; faellt der Pruefer aus (Datenbank, Zeitbudget), ist es "abbruch".
Kein Vergleich von created_at mit der Laptop-Uhr: eine falsch gehende Uhr wuerde sonst jede Anfrage ablehnen.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

POLL_SECS = 2.0
POLL_MAX_SECS = 30.0  # bei Dauerfehlern waechst die Pause bis hierher
PURGE_EVERY_SECS = 60.0
PURGE_OLDER_SECS = 180
HANDLER_TIMEOUT_SECS = 10.0  # der Proxy wartet 20 s auf die Antwort, eine spaetere waere nutzlos
VERIFY_TIMEOUT_SECS = 5.0  # Gesamtbudget der Geraetepruefung (eine haengende Datenbank darf die Schleife nicht blockieren)
CONNECT_GRACE_SECS = 30.0  # so lange darf eine beantwortete Verbindung brauchen, bis sie steht
CLOSE_WAIT_SECS = 3.0  # beim Beenden dürfen laufende Gespraeche selbst aufraeumen (Sitzung beenden, Lernen)
MAX_SDP_CHARS = 30000
SEEN_TTL_SECS = 600.0
SEEN_MAX = 200
CHANNEL = "phone"  # Kanal der Sitzungen, die ueber das Rendezvous kommen (memory.start_session)
PLACEHOLDER = {"sdp": "", "type": "offer"}  # ersetzt das Offer nach der Antwort (der CHECK der Tabelle verlangt sdp und type)
ENV_ON = frozenset({"1", "true", "yes", "ja", "an"})

ALLOWED_KEYS = frozenset({"sdp", "type", "pc_id", "restart_pc", "requestData", "request_data", "sig", "ts"})
MAX_SIG_CHARS = 200  # Signatur des Geraets (devices.py): 64 Bytes als base64url sind 86 Zeichen
ICE_KEYS = ("urls", "username", "credential")

Verifier = Callable[[dict, dict], Awaitable[bool]]  # (Zeile, geprueftes Offer) -> Geraet ist gebunden


def _env_on(value: str | None) -> bool:
    return (value or "").strip().lower() in ENV_ON


def validate_offer(request: object) -> dict | None:
    """Das Offer so, wie SmallWebRTCRequest es braucht, oder None bei jeder Abweichung.

    requestData/request_data aus dem Client wird bewusst verworfen (siehe Modul-Docstring)."""
    if not isinstance(request, dict) or set(request) - ALLOWED_KEYS:
        return None
    sdp = request.get("sdp")
    if not isinstance(sdp, str) or not sdp or len(sdp) > MAX_SDP_CHARS:
        return None
    if request.get("type") != "offer":
        return None
    pc_id = request.get("pc_id")
    if pc_id is not None and not (isinstance(pc_id, str) and 0 < len(pc_id) <= 100):
        return None
    restart = request.get("restart_pc")
    if restart is not None and not isinstance(restart, bool):
        return None
    for key in ("requestData", "request_data"):
        extra = request.get(key)
        if extra is not None and not isinstance(extra, dict):
            return None
    # Signatur und Zeit des Geraets (devices.py); nur der verifier wertet sie aus, der Handler sieht sie nie
    sig, ts = request.get("sig"), request.get("ts")
    if sig is not None and not (isinstance(sig, str) and 0 < len(sig) <= MAX_SIG_CHARS):
        return None
    if ts is not None and (isinstance(ts, bool) or not isinstance(ts, int) or not 0 < ts < 10**11):
        return None
    offer = {"sdp": sdp, "type": "offer", "pc_id": pc_id, "restart_pc": restart}
    if sig is not None:
        offer["sig"] = sig
    if ts is not None:
        offer["ts"] = ts
    return offer


def _hide_sdp(record: dict) -> None:
    """loguru-Patcher: Pipecat loggt bei einem Handler-Fehler das ganze Request-Objekt (mit SDP) und den
    Fehlertext von aiortc (kann SDP-Teile enthalten). Beides wird ersetzt."""
    message = record["message"]
    if "SmallWebRTC request details" in message:
        record["message"] = "SmallWebRTC request details (unterdrueckt)"
    elif "Error processing SmallWebRTC request" in message:
        record["message"] = "Error processing SmallWebRTC request (Text unterdrueckt)"


def install_log_filter() -> None:
    logger.configure(patcher=_hide_sdp)


class SignalStore:
    """Zugriff auf voice_signals ueber PostgREST (service_role), wie MemoryStore."""

    def __init__(
        self,
        url: str | None,
        key: str | None,
        schema: str,
        owner_id: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url, self.key, self.schema, self.owner_id = url, key, schema, owner_id
        self._transport = transport  # nur fuer Tests (httpx.MockTransport)
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_env(cls) -> SignalStore:
        return cls(
            os.getenv("SUPABASE_URL"),
            os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
            os.getenv("NIEEEKI_DB_SCHEMA", "nieeeki_voice_dev"),
            os.getenv("NIEEEKI_OWNER_ID"),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key and self.owner_id)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.url}/rest/v1",
                headers={
                    "apikey": self.key or "",
                    "Authorization": f"Bearer {self.key}",
                    "Accept-Profile": self.schema,
                    "Content-Profile": self.schema,
                    "Prefer": "return=representation",
                },
                timeout=10.0,
                transport=self._transport,
            )
        return self._client

    async def claim(self, owner_id: str) -> dict | None:
        r = await self._http().post("/rpc/claim_voice_signal", json={"p_user_id": owner_id})
        r.raise_for_status()
        rows = r.json()
        return rows[0] if isinstance(rows, list) and rows else None

    async def answer(self, row_id: str, response: dict) -> bool:
        """Antwort schreiben und das Offer durch den Platzhalter ersetzen. False: Zeile war nicht mehr 'claimed'."""
        r = await self._http().patch(
            f"/voice_signals?id=eq.{row_id}&status=eq.claimed&select=id",
            json={"status": "answered", "response": response, "request": PLACEHOLDER},
        )
        r.raise_for_status()
        return bool(r.json())

    async def fail(self, row_id: str, code: str) -> bool:
        r = await self._http().patch(
            f"/voice_signals?id=eq.{row_id}&status=eq.claimed&select=id",
            json={"status": "failed", "response": {"error": code}, "request": PLACEHOLDER},
        )
        r.raise_for_status()
        return bool(r.json())

    async def purge(self, older_than_secs: int) -> None:
        r = await self._http().post(
            "/rpc/purge_voice_signals", json={"p_older_than": f"{int(older_than_secs)} seconds"}
        )
        r.raise_for_status()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class Rendezvous:
    """Holt Offers ab, uebergibt sie dem WebRTC-Handler und schreibt die Antworten zurueck (siehe Modul)."""

    def __init__(
        self,
        store: Any,
        handler: Any,
        bot: Callable[[Any], Awaitable[None]],
        owner_id: str,
        *,
        args_factory: Callable[..., Any],
        verifier: Verifier | None = None,
        poll_secs: float = POLL_SECS,
        purge_every_secs: float = PURGE_EVERY_SECS,
        purge_older_secs: int = PURGE_OLDER_SECS,
        handler_timeout_secs: float = HANDLER_TIMEOUT_SECS,
        connect_grace_secs: float = CONNECT_GRACE_SECS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._store, self._handler, self._bot = store, handler, bot
        self._owner = str(owner_id).lower()
        self._args_factory = args_factory
        self._verifier = verifier
        self._poll, self._purge_every, self._purge_older = poll_secs, purge_every_secs, purge_older_secs
        self._timeout, self._grace = handler_timeout_secs, connect_grace_secs
        self._clock, self._sleep = clock, sleep
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._last_purge = -float("inf")
        self._tasks: set[asyncio.Task] = set()  # laufende bot()-Tasks (feste Referenzen, sonst sammelt sie die Muellabfuhr ein)
        self._watchers: set[asyncio.Task] = set()  # Wachtasks, die eine nicht stehende Verbindung verwerfen
        self._fresh: list[tuple[Any, asyncio.Task]] = []  # waehrend der laufenden Zeile angelegt (Verbindung, bot()-Task)
        if verifier is None:
            logger.warning("Rendezvous ohne Geraetebindung: ein gestohlenes Token reicht fuer eine Sitzung")

    # ---------- Schleife ----------

    async def run(self) -> None:
        errors = 0
        while True:
            try:
                await self._maybe_purge()
                row = await self._store.claim(self._owner)
                errors = 0
                if row:
                    await self.process(row)
                    continue  # gleich nachsehen, ob noch eine wartet (Neuverhandlung folgt oft sofort)
            except Exception as e:  # nie Zeileninhalt loggen, nur die Art des Fehlers
                errors += 1
                logger.warning("Rendezvous: Abfrage fehlgeschlagen ({}), Versuch {}", type(e).__name__, errors)
            await self._sleep(min(self._poll * 2 ** (min(errors, 6) - 1), POLL_MAX_SECS) if errors else self._poll)

    async def _maybe_purge(self) -> None:
        now = self._clock()
        if now - self._last_purge < self._purge_every:
            return
        self._last_purge = now  # auch bei einem Fehler: nicht jede Runde neu versuchen
        try:
            await self._store.purge(self._purge_older)
        except Exception as e:  # Aufraeumen darf das Abholen nie blockieren
            logger.warning("Rendezvous: Aufraeumen fehlgeschlagen ({})", type(e).__name__)

    # ---------- Eine Zeile ----------

    async def process(self, row: dict) -> None:
        row_id = row.get("id")
        if not row_id:
            return
        try:
            await self._process(row_id, row)
        except asyncio.CancelledError:
            # Abbruch mitten in der Zeile (z. B. der Voice-Core wird beendet): dem Proxy gleich einen 5xx geben,
            # statt ihn 20 s warten zu lassen, und nichts Halbes zuruecklassen.
            await self._abandon_fresh()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._store.fail(row_id, "abbruch"), 2.0)
            raise

    async def _process(self, row_id: str, row: dict) -> None:
        if str(row.get("user_id", "")).lower() != self._owner:
            return await self._reject(row_id, "owner")
        offer = validate_offer(row.get("request"))
        if offer is None:
            return await self._reject(row_id, "form")
        digest = hashlib.sha256(offer["sdp"].encode()).hexdigest()
        if self._seen_before(digest):
            return await self._reject(row_id, "wiederholt")
        if self._verifier is not None:
            try:
                bound = await asyncio.wait_for(self._verifier(row, offer), VERIFY_TIMEOUT_SECS)
            except Exception as e:
                # Datenbank weg oder zu langsam: nie durchlassen, aber auch keine Aussage ueber das Geraet. "abbruch"
                # (5xx) laesst den Client nach 2 s neu versuchen; das Offer zaehlt dabei nicht als Wiederholung.
                logger.warning("Rendezvous: Geraetepruefung fehlgeschlagen ({})", type(e).__name__)
                self._seen.pop(digest, None)
                return await self._reject(row_id, "abbruch")
            if not bound:
                return await self._reject(row_id, "geraet")
        started = self._clock()
        self._fresh = []
        try:
            answer = await asyncio.wait_for(self._dispatch(offer), self._timeout)
        except Exception as e:
            logger.warning("Rendezvous: Handler fehlgeschlagen ({})", type(e).__name__)
            await self._abandon_fresh()  # eine schon angelegte Verbindung samt bot() nicht als Geist stehen lassen
            return await self._reject(row_id, "handler")
        response = {"sdp": answer["sdp"], "type": answer["type"], "pc_id": answer["pc_id"]}
        if not await self._write_answer(row_id, response):
            logger.warning("Rendezvous: Antwort nicht geschrieben, die Verbindung wird verworfen")
            return await self._abandon_fresh()
        # Eine unbekannte pc_id (z. B. nach einem Neustart) legt eine neue Verbindung an: massgeblich ist, ob
        # der Handler wirklich eine angelegt hat, nicht ob das Offer eine pc_id trug.
        kind = "neue Verbindung" if self._fresh else "Neuverhandlung"
        for connection, task in self._fresh:
            self._watch(connection, task)
        self._fresh = []
        logger.info("Rendezvous: {} beantwortet in {:.1f} s", kind, self._clock() - started)

    async def _dispatch(self, offer: dict) -> dict:
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest

        request = SmallWebRTCRequest(
            sdp=offer["sdp"],
            type=offer["type"],
            pc_id=offer["pc_id"],
            restart_pc=offer["restart_pc"],
            request_data=None,
        )
        answer = await self._handler.handle_web_request(request, self._on_connection)
        if not answer or not all(answer.get(k) for k in ("sdp", "type", "pc_id")):
            raise RuntimeError("unvollstaendige Antwort")
        return answer

    async def _on_connection(self, connection: Any) -> None:
        """Neue Verbindung: bot() starten, mit einem Body, den nur dieses Modul bestimmt."""
        runner_args = self._args_factory(
            webrtc_connection=connection, body={"kanal": CHANNEL}, session_id=str(uuid.uuid4())
        )
        task = asyncio.create_task(self._bot(runner_args), name="rendezvous-bot")
        self._tasks.add(task)
        task.add_done_callback(self._bot_done)
        self._fresh.append((connection, task))

    def _bot_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Rendezvous: bot() beendet mit Fehler ({})", type(task.exception()).__name__)

    async def _write_answer(self, row_id: str, response: dict) -> bool:
        """Die Antwort schreiben, bei einem Fehler einmal kurz wiederholen. True nur, wenn die Zeile sie nahm."""
        for attempt in (1, 2):
            try:
                return bool(await self._store.answer(row_id, response))
            except Exception as e:
                logger.warning("Rendezvous: Antwort schreiben fehlgeschlagen ({}), Versuch {}", type(e).__name__, attempt)
                if attempt == 1:
                    await self._sleep(0.2)
        return False

    async def _abandon_fresh(self) -> None:
        """Verbindungen und bot()-Tasks verwerfen, die waehrend der laufenden Zeile angelegt wurden."""
        fresh, self._fresh = self._fresh, []
        for connection, task in fresh:
            task.cancel()
            with contextlib.suppress(Exception):
                await connection.disconnect()

    def _watch(self, connection: Any, task: asyncio.Task) -> None:
        """Steht die beantwortete Verbindung nach CONNECT_GRACE_SECS nicht, wird sie verworfen: Die Antwort kam nie
        beim Handy an oder das Handy gab auf. Sonst lebt die Pipeline im Leerlauf, bis ihr Timeout greift."""

        async def watch() -> None:
            await asyncio.sleep(self._grace)
            check = getattr(connection, "is_connected", None)
            if check is None or task.done() or check():
                return
            logger.warning("Rendezvous: Verbindung kam nicht zustande, wird verworfen")
            task.cancel()
            with contextlib.suppress(Exception):
                await connection.disconnect()

        watcher = asyncio.create_task(watch(), name="rendezvous-watch")
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def _reject(self, row_id: str, code: str) -> None:
        logger.warning("Rendezvous: Anfrage abgelehnt ({})", code)
        try:
            await self._store.fail(row_id, code)
        except Exception as e:
            logger.warning("Rendezvous: Ablehnung nicht geschrieben ({})", type(e).__name__)

    def _seen_before(self, digest: str) -> bool:
        now = self._clock()
        for key in [k for k, t in self._seen.items() if now - t > SEEN_TTL_SECS]:
            del self._seen[key]
        if digest in self._seen:
            return True
        self._seen[digest] = now
        while len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)
        return False

    async def close(self) -> None:
        """Beenden: Wachtasks sofort weg, dann den Handler (trennt die Verbindungen, die Gespraeche raeumen dabei
        selbst auf), dann den Gespraechen kurz Zeit geben, dann den Rest abbrechen. Nichts davon darf das Beenden
        des Voice-Cores laenger als CLOSE_WAIT_SECS je Schritt aufhalten."""
        watchers = list(self._watchers)
        for watcher in watchers:
            watcher.cancel()
        close_handler = getattr(self._handler, "close", None)
        if close_handler is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(close_handler(), CLOSE_WAIT_SECS)
        tasks = list(self._tasks)
        if tasks:
            await asyncio.wait(tasks, timeout=CLOSE_WAIT_SECS)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *watchers, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._store.close()
        close_verifier = getattr(self._verifier, "close", None)  # der Pruefer haelt eine eigene Datenbankverbindung
        if close_verifier is not None:
            with contextlib.suppress(Exception):
                await close_verifier()


def ice_servers_from_env(raw: str | None) -> list[dict]:
    """NIEEEKI_ICE_SERVERS: JSON-Liste mit {"urls": [...], "username": ..., "credential": ...}. Ungueltig = keine.
    Andere Schluessel werden verworfen (IceServer kennt nur diese drei)."""
    if not raw or not raw.strip():
        return []
    try:
        servers = json.loads(raw)
        if not isinstance(servers, list) or not all(isinstance(s, dict) and s.get("urls") for s in servers):
            raise ValueError("Form")
        return [{k: s[k] for k in ICE_KEYS if k in s} for s in servers]
    except (ValueError, TypeError):
        logger.warning("NIEEEKI_ICE_SERVERS ist kein gueltiges JSON mit urls, ignoriert")
        return []


def install_rendezvous(
    app: Any,
    bot: Callable[[Any], Awaitable[None]],
    *,
    env: dict | None = None,
    store: Any = None,
    handler: Any = None,
    verifier: Verifier | None = None,
) -> Rendezvous | None:
    """Haengt das Rendezvous in den Lebenszyklus des Pipecat-Runners. None, wenn es nicht eingeschaltet ist.

    bot muss die Funktion aus dem laufenden Modul (__main__) sein, nicht aus einem zweiten Import: sonst haette
    sie eine eigene SingleSession und zwei Gespraeche liefen parallel."""
    env = os.environ if env is None else env
    value = env.get("NIEEEKI_RENDEZVOUS")
    if not _env_on(value):
        if value and value.strip() not in ("", "0"):
            logger.warning("NIEEEKI_RENDEZVOUS={!r} nicht erkannt (1, true, ja, an), Rendezvous bleibt aus", value)
        return None
    if verifier is None and not _env_on(env.get("NIEEEKI_RENDEZVOUS_UNBOUND")):
        logger.warning(
            "NIEEEKI_RENDEZVOUS=1 ohne Geraetebindung: bleibt aus. Mit Bindung (Plan, Schritt 6) oder bewusst ohne "
            "mit NIEEEKI_RENDEZVOUS_UNBOUND=1 (dann reicht ein gestohlenes Supabase-Token fuer eine Sitzung)."
        )
        return None
    store = store or SignalStore.from_env()
    if not store.enabled:
        logger.warning(
            "NIEEEKI_RENDEZVOUS=1, aber SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY oder NIEEEKI_OWNER_ID fehlt: aus"
        )
        return None
    if handler is None:
        from pipecat.transports.smallwebrtc.connection import IceServer
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler

        servers = [IceServer(**s) for s in ice_servers_from_env(env.get("NIEEEKI_ICE_SERVERS"))]
        handler = SmallWebRTCRequestHandler(ice_servers=servers or None)
    from pipecat.runner.types import SmallWebRTCRunnerArguments

    install_log_filter()
    rendezvous = Rendezvous(
        store, handler, bot, store.owner_id, args_factory=SmallWebRTCRunnerArguments, verifier=verifier
    )
    existing = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with existing(a):
            task = asyncio.create_task(rendezvous.run(), name="rendezvous")
            logger.info("Rendezvous laeuft (Abfrage alle {:.0f} s)", rendezvous._poll)
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                await rendezvous.close()

    app.router.lifespan_context = lifespan
    return rendezvous
