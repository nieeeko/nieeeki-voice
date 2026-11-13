"""Geraetebindung fuer Weg B: nur gekoppelte Geraete duerfen eine Sitzung ueber Supabase starten.

Plan: plans/online-ohne-tailscale.md, Schritt 6. Ohne Tailscale reicht sonst ein gestohlenes Supabase-Token (aal2)
fuer eine Sitzung mit voller Fernsteuerung. Darum signiert das Geraet jedes Offer mit einem Schluessel, der es nie
verlaesst (ui/src/lib/deviceKey.ts: ECDSA P-256, nicht exportierbar). Dieses Modul ist der `verifier` von
rendezvous.Rendezvous und der Zugang zur Geraeteliste (Tabelle voice_devices, Migration 0003).

Koppeln (nur am Laptop, Eingabe am Terminal; kein Netzweg, den ein Token-Dieb erreichen koennte):
  1. Das Handy erzeugt seinen Schluessel, traegt den oeffentlichen Teil als wartende Zeile ein (mit seinem Token,
     die Datenbank erlaubt nur das) und zeigt einen Kurzcode "XXXX-XXXX-XXXX" (erste 60 Bit von SHA-256 des Schluessels).
  2. Am Laptop:  uv run devices.py koppeln   fragt den Code ab, zeigt das passende wartende Geraet und gibt es
     nach "j" frei. Ein Dieb mit Token kann nur wartende Zeilen anlegen (und sieht die Schluessel des echten Handys);
     freigegeben wird nur, was zu dem Code passt, den Nikolaj von SEINEM Handy abliest. Eine zweite Vorlage mit
     gleichem 60-Bit-Code zu suchen ist nicht machbar (bei 40 Bit waere es mit einer GPU in Minuten gegangen, darum
     60); bei mehr als einem Treffer lehnt das Werkzeug ab. Das Etikett kommt vom Handy und ist ungeprueft: allein der
     Code zaehlt.
  3. Entfernen:  uv run devices.py entfernen <Nummer>   (Nummern zeigt  uv run devices.py liste).

Vertrag mit deviceKey.ts (derselbe Testvektor steht in test_devices.py und deviceKey.test.ts):
  * oeffentlicher Schluessel: base64url ohne Padding, roher Punkt (65 Bytes, 0x04 || X || Y)
  * signiert wird "nieeeki-rtc-v1|<ts>|<pc_id oder leer>|<SHA-256 des SDP in Hex>" (UTF-8)
  * Signatur: ECDSA/SHA-256, roh r||s (64 Bytes), base64url
  * ts muss hoechstens MAX_SKEW_SECS von created_at der Zeile abweichen (Uhr der Datenbank, nicht des Laptops)

Alles faellt zu: fehlt etwas oder schlaegt etwas fehl (Zeit, Signatur, Datenbank), gilt das Geraet als nicht gebunden.
Aus: NIEEEKI_DEVICE_BINDING=aus (dann gilt wieder NIEEEKI_RENDEZVOUS_UNBOUND, siehe rendezvous.py).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from loguru import logger

SIGN_PREFIX = "nieeeki-rtc-v1"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne I, O, 0, 1
CODE_CHARS = 12  # 60 Bit: so viele, dass sich keine zweite Vorlage fuer einen mitgelesenen Schluessel suchen laesst
MAX_SKEW_SECS = 60
OFF_VALUES = frozenset({"aus", "0", "false", "nein", "off"})
_B64URL = re.compile(r"[A-Za-z0-9_-]+")  # mit fullmatch benutzen ($ wuerde auch vor einem nachgestellten \n passen)


# ---------- Kleine, reine Funktionen ----------


def b64url_decode(text: object) -> bytes | None:
    """Strikt wie fromBase64Url in deviceKey.ts: nur das URL-sichere Alphabet, kein Padding."""
    if not isinstance(text, str) or not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        return None
    # Nur die kanonische Schreibweise: sonst gaebe es mehrere Texte (Fuellbits) fuer dieselben Bytes
    return raw if base64.urlsafe_b64encode(raw).decode().rstrip("=") == text else None


def load_public_key(text: object) -> ec.EllipticCurvePublicKey | None:
    """Der rohe Punkt (65 Bytes) als P-256-Schluessel, None bei allem, was kein gueltiger Punkt ist."""
    raw = b64url_decode(text)
    if raw is None or len(raw) != 65 or raw[0] != 4:
        return None
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        return None


def pairing_code(public_key: object) -> str | None:
    """Kurzcode "XXXX-XXXX-XXXX" aus dem rohen Schluessel (die ersten 60 Bit von SHA-256, je 5 Bit ein Zeichen,
    hoechstwertige zuerst); None, wenn der Schluessel ungueltig ist."""
    raw = b64url_decode(public_key)
    if raw is None:
        return None
    bits = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") >> (64 - 5 * CODE_CHARS)
    chars = "".join(CODE_ALPHABET[(bits >> shift) & 31] for shift in range(5 * (CODE_CHARS - 1), -1, -5))
    return "-".join(chars[i : i + 4] for i in range(0, CODE_CHARS, 4))


def normalize_code(text: str) -> str:
    """Eingabe am Terminal: Gross-/Kleinschreibung, Bindestrich und Leerzeichen egal. Leer bei falscher Laenge."""
    chars = "".join(c for c in text.upper() if c not in "- \t")
    return chars if len(chars) == CODE_CHARS and all(c in CODE_ALPHABET for c in chars) else ""


def signed_message(ts: int, pc_id: str | None, sdp: str) -> bytes:
    digest = hashlib.sha256(sdp.encode()).hexdigest()
    return f"{SIGN_PREFIX}|{ts}|{pc_id or ''}|{digest}".encode()


def verify_signature(public_key: object, message: bytes, signature: object) -> bool:
    key = load_public_key(public_key)
    raw = b64url_decode(signature)
    if key is None or raw is None or len(raw) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        key.verify(der, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def parse_timestamp(value: object) -> float | None:
    """ISO-Zeitstempel aus PostgREST (z. B. 2026-11-13T10:00:00.123456+00:00) als Sekunden seit 1970."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


# ---------- Geraeteliste (Tabelle voice_devices, service_role) ----------


class DeviceStore:
    """Zugriff auf voice_devices ueber PostgREST, wie SignalStore in rendezvous.py."""

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
    def from_env(cls) -> DeviceStore:
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

    async def _list(self, status: str) -> list[dict]:
        r = await self._http().get(
            "/voice_devices",
            params={
                "user_id": f"eq.{self.owner_id}",
                "status": f"eq.{status}",
                "select": "id,public_key,label,created_at,approved_at",
                "order": "created_at.asc,id.asc",  # feste Reihenfolge: "entfernen <Nummer>" muss zur "liste" passen
            },
        )
        r.raise_for_status()
        rows = r.json()
        return [x for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []

    async def active(self) -> list[dict]:
        return await self._list("active")

    async def pending(self) -> list[dict]:
        return await self._list("pending")

    async def purge(self) -> None:
        """Abgelaufene wartende Zeilen entfernen (nach der Uhr der Datenbank)."""
        r = await self._http().post("/rpc/purge_voice_devices", json={})
        r.raise_for_status()

    async def activate(self, device_id: str) -> bool:
        """Eine wartende Zeile freigeben. False, wenn sie nicht mehr wartet (abgelaufen oder schon freigegeben)."""
        r = await self._http().patch(
            "/voice_devices",
            params={"id": f"eq.{device_id}", "user_id": f"eq.{self.owner_id}", "status": "eq.pending", "select": "id"},
            json={"status": "active"},  # den Zeitpunkt setzt die Datenbank, und sie prueft auch die Frist (Trigger)
        )
        r.raise_for_status()
        return bool(r.json())

    async def remove(self, device_id: str) -> bool:
        r = await self._http().delete(
            "/voice_devices",
            params={"id": f"eq.{device_id}", "user_id": f"eq.{self.owner_id}", "select": "id"},
        )
        r.raise_for_status()
        return bool(r.json())

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------- Pruefung im Rendezvous ----------


class DeviceVerifier:
    """verifier fuer rendezvous.Rendezvous: (Zeile, geprueftes Offer) -> hat ein gekoppeltes Geraet es signiert?"""

    def __init__(self, store: Any, *, max_skew_secs: float = MAX_SKEW_SECS):
        self._store = store
        self._max_skew = max_skew_secs

    async def __call__(self, row: dict, offer: dict) -> bool:
        sig, ts = offer.get("sig"), offer.get("ts")
        if not isinstance(sig, str) or not isinstance(ts, int) or isinstance(ts, bool):
            return False
        created = parse_timestamp(row.get("created_at"))
        if created is None or abs(created - ts) > self._max_skew:
            return False  # fehlt created_at, gilt das Offer als nicht frisch (kein Vergleich mit der Laptop-Uhr)
        message = signed_message(ts, offer.get("pc_id"), offer["sdp"])
        for device in await self._store.active():  # Fehler hier fallen als "nicht gebunden" zurueck (rendezvous.py)
            if verify_signature(device.get("public_key"), message, sig):
                logger.info("Geraetebindung: Offer von Geraet {} signiert", str(device.get("id", "?"))[:8])
                return True
        return False

    async def close(self) -> None:
        await self._store.close()


def binding_enabled(env: dict | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("NIEEEKI_DEVICE_BINDING") or "an").strip().lower() not in OFF_VALUES


def verifier_from_env(env: dict | None = None, store: Any = None) -> DeviceVerifier | None:
    """Der Pruefer fuer bot.py. None nur, wenn die Bindung mit NIEEEKI_DEVICE_BINDING=aus abgeschaltet ist."""
    if not binding_enabled(env):
        logger.warning("NIEEEKI_DEVICE_BINDING=aus: Weg B ohne Geraetebindung (nur mit NIEEEKI_RENDEZVOUS_UNBOUND=1)")
        return None
    return DeviceVerifier(store or DeviceStore.from_env())


# ---------- Werkzeug am Laptop ----------


def _label(device: dict) -> str:
    """Das Etikett kommt vom Handy: Steuerzeichen (Escape-Folgen im Terminal) werden nie ausgegeben."""
    text = "".join(c for c in str(device.get("label") or "") if c.isprintable())[:40].strip()
    return text or "ohne Namen"


async def pair(store: Any, *, input_fn: Callable[[str], str] = input, print_fn: Callable[..., None] = print) -> int:
    """Ein wartendes Handy nach seinem Kurzcode freigeben. Rueckgabe: Exit-Code."""
    await store.purge()
    waiting = [d for d in await store.pending() if pairing_code(d.get("public_key")) and load_public_key(d.get("public_key"))]
    if not waiting:
        print_fn("Kein Handy wartet. Am Handy 'Gerät koppeln' öffnen (der Code gilt 10 Minuten) und es noch einmal versuchen.")
        return 1
    typed = normalize_code(input_fn("Code vom Handy (z. B. ABCD-EFGH-JKLM): "))
    if not typed:
        print_fn("Das ist kein gültiger Code (12 Zeichen aus Buchstaben und Ziffern, ohne I, O, 0 und 1).")
        return 1
    matches = [d for d in waiting if (pairing_code(d["public_key"]) or "").replace("-", "") == typed]
    if not matches:
        print_fn("Zu diesem Code wartet kein Handy. Code noch einmal vom Handy ablesen.")
        return 1
    if len(matches) > 1:
        print_fn("Mehrere wartende Geräte haben diesen Code. Nichts freigegeben. Bitte neu koppeln.")
        return 1
    device = matches[0]
    print_fn(f"Gefunden: {_label(device)}, Code {pairing_code(device['public_key'])}, angelegt {device.get('created_at')}.")
    print_fn("(Der Name kommt vom Handy und ist ungeprüft. Entscheidend ist, dass der Code auf DEINEM Handy steht.)")
    if input_fn("Dieses Handy koppeln? (j/N) ").strip().lower() not in ("j", "ja", "y", "yes"):
        print_fn("Abgebrochen, nichts geändert.")
        return 1
    # Zwischen dem ersten Lesen und der Bestaetigung ist Zeit vergangen: noch einmal lesen und nur freigeben, wenn
    # genau dieses eine Geraet zum Code passt (kein neuer Zwilling, nicht inzwischen abgelaufen)
    still_waiting = [d for d in await store.pending() if (pairing_code(d.get("public_key")) or "").replace("-", "") == typed]
    if len(still_waiting) != 1 or str(still_waiting[0].get("id")) != str(device["id"]):
        print_fn("Die wartenden Geräte haben sich inzwischen geändert. Nichts freigegeben, bitte neu starten.")
        return 1
    if not await store.activate(str(device["id"])):
        print_fn("Die Kopplung ist inzwischen abgelaufen. Am Handy neu starten.")
        return 1
    print_fn("Gekoppelt. Das Handy kann jetzt ohne Tailscale eine Sitzung starten.")
    return 0


async def list_devices(store: Any, *, print_fn: Callable[..., None] = print) -> int:
    devices = await store.active()
    if not devices:
        print_fn("Keine gekoppelten Geräte.")
        return 0
    for number, device in enumerate(devices, 1):
        print_fn(f"{number}. {_label(device)}  Code {pairing_code(device.get('public_key')) or '?'}  gekoppelt {device.get('approved_at') or '?'}")
    return 0


async def remove_device(
    store: Any,
    selector: str,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
) -> int:
    devices = await store.active()
    if not (selector.isascii() and selector.isdecimal()) or not 1 <= int(selector) <= len(devices):
        print_fn("Nummer aus  uv run devices.py liste  angeben.")
        return 1
    device = devices[int(selector) - 1]
    # Die Nummern folgen der Reihenfolge der Liste und koennen sich verschieben: erst zeigen, welches Geraet gemeint ist
    code = pairing_code(device.get("public_key")) or "?"
    if input_fn(f"Entfernen: {_label(device)}, Code {code}? (j/N) ").strip().lower() not in ("j", "ja", "y", "yes"):
        print_fn("Abgebrochen, nichts geändert.")
        return 1
    if not await store.remove(str(device["id"])):
        print_fn("Das Gerät war schon entfernt.")
        return 1
    print_fn(f"Entfernt: {_label(device)}. Es kann keine Sitzung mehr starten.")
    return 0


async def _run(args: argparse.Namespace) -> int:
    store = DeviceStore.from_env()
    if not store.enabled:
        print("SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY oder NIEEEKI_OWNER_ID fehlt (.env des Voice-Cores).")
        return 2
    try:
        if args.command == "koppeln":
            return await pair(store)
        if args.command == "liste":
            return await list_devices(store)
        return await remove_device(store, args.nummer)
    except (httpx.HTTPError, ValueError) as e:  # ValueError: Antwort der Datenbank ist kein JSON
        print(f"Datenbank nicht erreichbar oder Migration 0003 fehlt ({type(e).__name__}).")
        return 2
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(override=True)
    parser = argparse.ArgumentParser(description="Geräte für 'online ohne Tailscale' koppeln (nur am Laptop)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("koppeln", help="ein wartendes Handy mit dem Kurzcode freigeben")
    sub.add_parser("liste", help="gekoppelte Geräte zeigen")
    remove = sub.add_parser("entfernen", help="ein Gerät entfernen")
    remove.add_argument("nummer", help="Nummer aus 'liste'")
    try:
        return asyncio.run(_run(parser.parse_args(argv)))
    except (EOFError, KeyboardInterrupt):  # Eingabe abgebrochen: nichts wurde geaendert
        print("\nAbgebrochen, nichts geändert.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
