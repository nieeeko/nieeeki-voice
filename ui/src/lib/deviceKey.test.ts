// Tests fuer deviceKey.ts: Geraeteschluessel, Signatur, Kurzcode und das signierende fetch (Weg B, Schritt 6).
// Ausfuehren: npm test   (im Ordner ui/)
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CODE_ALPHABET,
  createDeviceKey,
  fromBase64Url,
  loadOrCreateKey,
  pairingCode,
  signOffer,
  signedMessage,
  signingFetch,
  toBase64Url,
  type DeviceKey,
  type KeyStore,
} from "./deviceKey.ts";

const enc = new TextEncoder();
const hex = (bytes: ArrayBuffer) => Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, "0")).join("");

function memoryStore(): KeyStore & { saves: number } {
  let value: DeviceKey | null = null;
  const store = {
    saves: 0,
    load: async () => value,
    save: async (key: DeviceKey) => {
      store.saves++;
      value = key;
    },
    clear: async () => {
      value = null;
    },
  };
  return store;
}

/** Prueft eine Signatur so, wie es der Voice-Core tut (mit dem oeffentlichen Schluessel aus base64url). */
async function verifies(publicKey: string, message: string, sig: string): Promise<boolean> {
  const key = await crypto.subtle.importKey("raw", fromBase64Url(publicKey) as BufferSource, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
  return crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, fromBase64Url(sig) as BufferSource, enc.encode(message));
}

test("base64url: Rundlauf ohne Padding, strenge Pruefung", () => {
  const bytes = Uint8Array.from([0, 250, 251, 252, 253, 254, 255, 62, 63]);
  const text = toBase64Url(bytes);
  assert.match(text, /^[A-Za-z0-9_-]+$/);
  assert.deepEqual(fromBase64Url(text), bytes);
  assert.throws(() => fromBase64Url("ab+/"));
  assert.throws(() => fromBase64Url("abc="));
  assert.throws(() => fromBase64Url("a"));
  assert.deepEqual(fromBase64Url("AAA"), Uint8Array.from([0, 0]));
  assert.throws(() => fromBase64Url("AAB")); // gleiche Bytes, aber Fuellbits gesetzt
});

test("Kurzcode: fester Vektor (derselbe steht in voice-core/test_devices.py)", async () => {
  const raw = Uint8Array.from({ length: 65 }, (_, i) => (i === 0 ? 4 : i));
  assert.equal(await pairingCode(toBase64Url(raw)), "B5K4-PL6X-R95R");
});

// Echter WebCrypto-Vektor (dieselbe Zeile steht in voice-core/test_devices.py): Der Voice-Core prueft ihn mit Python
const VEKTOR = {
  publicKey: "BG-o-TtCddif-uUWbkFc9pfmacnpCnHtHKyeO6VRedaZQoqcWikP2hTgLNs_lP3ih-Nw4F0yBNho2bqqNlVSzjc",
  code: "3RMK-EX87-8MPT",
  sdp: "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\n",
  pc_id: "pc-vektor",
  sig: "Iu7byXabGIRH4smREfZ7BD3qWAoIyw9iQOrr1TsQalHevwE4bRDOlzqCxEOrSWB2w7KkQLREGbBNeL1bth7olA",
  ts: 1800000000,
};

test("Vektor: Kurzcode und Signatur aus derselben Quelle wie im Voice-Core", async () => {
  assert.equal(await pairingCode(VEKTOR.publicKey), VEKTOR.code);
  const hash = hex(await crypto.subtle.digest("SHA-256", enc.encode(VEKTOR.sdp)));
  assert.ok(await verifies(VEKTOR.publicKey, signedMessage(VEKTOR.ts, VEKTOR.pc_id, hash), VEKTOR.sig));
  assert.ok(!(await verifies(VEKTOR.publicKey, signedMessage(VEKTOR.ts, null, hash), VEKTOR.sig)));
});

test("Kurzcode: nur Zeichen aus dem Alphabet, Form XXXX-XXXX-XXXX", async () => {
  const key = await createDeviceKey();
  const code = await pairingCode(key.publicKey);
  assert.match(code, /^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$/);
  for (const c of code.replaceAll("-", "")) assert.ok(CODE_ALPHABET.includes(c));
  assert.ok(!/[IO01]/.test(code));
});

test("Schluessel: oeffentlicher Teil roh (65 Bytes), privater Teil nicht exportierbar", async () => {
  const key = await createDeviceKey();
  const raw = fromBase64Url(key.publicKey);
  assert.equal(raw.length, 65);
  assert.equal(raw[0], 4);
  assert.equal(key.publicKey.length, 87); // die Datenbank verlangt genau 87 Zeichen
  assert.equal(key.privateKey.extractable, false);
  for (const format of ["pkcs8", "jwk", "raw"] as const) {
    await assert.rejects(() => crypto.subtle.exportKey(format, key.privateKey));
  }
});

test("loadOrCreateKey: legt nur einmal an", async () => {
  const store = memoryStore();
  const first = await loadOrCreateKey(store);
  const second = await loadOrCreateKey(store);
  assert.equal(first.publicKey, second.publicKey);
  assert.equal(store.saves, 1);
});

test("signOffer: Signatur gilt fuer Zeit, pc_id und SDP, sonst nicht", async () => {
  const key = await createDeviceKey();
  const sdp = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n";
  const { sig, ts } = await signOffer(key, { sdp, pc_id: "pc-1" }, 1_800_000_000.9);
  assert.equal(ts, 1_800_000_000);
  assert.equal(fromBase64Url(sig).length, 64); // roh r||s, nicht DER
  const hash = hex(await crypto.subtle.digest("SHA-256", enc.encode(sdp)));
  assert.ok(await verifies(key.publicKey, `nieeeki-rtc-v1|1800000000|pc-1|${hash}`, sig));
  assert.ok(!(await verifies(key.publicKey, `nieeeki-rtc-v1|1800000001|pc-1|${hash}`, sig)));
  assert.ok(!(await verifies(key.publicKey, `nieeeki-rtc-v1|1800000000||${hash}`, sig)));
  const otherHash = hex(await crypto.subtle.digest("SHA-256", enc.encode(sdp + "x")));
  assert.ok(!(await verifies(key.publicKey, `nieeeki-rtc-v1|1800000000|pc-1|${otherHash}`, sig)));
  const other = await createDeviceKey();
  assert.ok(!(await verifies(other.publicKey, signedMessage(ts, "pc-1", hash), sig)));
});

type Call = { input: unknown; init?: RequestInit };

function recorder() {
  const calls: Call[] = [];
  const base = async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ input, init });
    return new Response("{}");
  };
  return { calls, base };
}

test("signingFetch: signiert Offers an /api/rtc, der Rest des Bodies bleibt", async () => {
  const key = await createDeviceKey();
  const { calls, base } = recorder();
  const wrapped = signingFetch(base, async () => key, () => 1_800_000_000_000);
  const offer = { sdp: "v=0\r\n", type: "offer", pc_id: null, restart_pc: false, requestData: {} };
  await wrapped("/api/rtc", { method: "POST", headers: { "X-Test": "1" }, body: JSON.stringify(offer) });
  const sent = JSON.parse(calls[0].init?.body as string);
  assert.equal(sent.sdp, offer.sdp);
  assert.equal(sent.type, "offer");
  assert.equal(sent.restart_pc, false);
  assert.equal(sent.ts, 1_800_000_000);
  const hash = hex(await crypto.subtle.digest("SHA-256", enc.encode(offer.sdp)));
  assert.ok(await verifies(key.publicKey, `nieeeki-rtc-v1|1800000000||${hash}`, sent.sig));
  assert.deepEqual(calls[0].init?.headers, { "X-Test": "1" });
});

test("signingFetch: der echte Aufruf des Transports, fetch(new Request(...)) mit einem Argument, wird signiert", async () => {
  // client-js makeRequest: fetch(new Request(endpoint, { method: "POST", mode: "cors", headers, body, signal }))
  const key = await createDeviceKey();
  const { calls, base } = recorder();
  const wrapped = signingFetch(base, async () => key, () => 1_800_000_000_000);
  const controller = new AbortController();
  const offer = { sdp: "v=0\r\n", type: "offer", pc_id: "abc", restart_pc: false, requestData: {} };
  const request = new Request("https://nieeeki.example/api/rtc", {
    method: "POST",
    mode: "cors",
    headers: new Headers({ "Content-Type": "application/json", Authorization: "Bearer TOKEN" }),
    body: JSON.stringify(offer),
    signal: controller.signal,
  });
  await wrapped(request);
  const sent = calls[0].input as Request;
  assert.ok(sent instanceof Request);
  const body = JSON.parse(await sent.text());
  const hash = hex(await crypto.subtle.digest("SHA-256", enc.encode(offer.sdp)));
  assert.ok(await verifies(key.publicKey, `nieeeki-rtc-v1|1800000000|abc|${hash}`, body.sig));
  assert.equal(body.sdp, offer.sdp);
  assert.equal(body.restart_pc, false);
  assert.equal(sent.method, "POST");
  assert.equal(sent.headers.get("authorization"), "Bearer TOKEN"); // Header bleiben erhalten
  assert.equal(sent.headers.get("content-type"), "application/json");
  assert.equal(sent.mode, "cors");
  controller.abort();
  assert.equal(sent.signal.aborted, true); // das Abbruchsignal des Transports gilt weiter
  assert.equal(calls[0].init, undefined);
});

test("signingFetch: Request fuer andere Adressen oder ohne Offer bleibt unberuehrt", async () => {
  const key = await createDeviceKey();
  const { calls, base } = recorder();
  const wrapped = signingFetch(base, async () => key, () => 0);
  const fremd = new Request("https://elsewhere.example/start", { method: "POST", body: JSON.stringify({ sdp: "x", type: "offer" }) });
  const get = new Request("https://nieeeki.example/api/rtc");
  const leer = new Request("https://nieeeki.example/api/rtc", { method: "POST", body: JSON.stringify({ hallo: 1 }) });
  for (const request of [fremd, get, leer]) await wrapped(request);
  assert.deepEqual(calls.map((c) => c.input), [fremd, get, leer]); // dieselben Objekte, nichts neu gebaut
});

test("signingFetch: Neuverhandlung (pc_id) fliesst in die Signatur ein", async () => {
  const key = await createDeviceKey();
  const { calls, base } = recorder();
  const wrapped = signingFetch(base, async () => key, () => 1_800_000_000_000);
  await wrapped(new URL("https://nieeeki.example/api/rtc"), {
    method: "post",
    body: JSON.stringify({ sdp: "v=0\r\n", type: "offer", pc_id: "abc" }),
  });
  const sent = JSON.parse(calls[0].init?.body as string);
  const hash = hex(await crypto.subtle.digest("SHA-256", enc.encode("v=0\r\n")));
  assert.ok(await verifies(key.publicKey, `nieeeki-rtc-v1|1800000000|abc|${hash}`, sent.sig));
});

test("signingFetch: alles andere geht unveraendert durch", async () => {
  const key = await createDeviceKey();
  const { calls, base } = recorder();
  const wrapped = signingFetch(base, async () => key, () => 0);
  const body = JSON.stringify({ sdp: "v=0\r\n", type: "offer" });
  await wrapped("/api/rtc", { method: "PATCH", body }); // ICE-Kandidaten
  await wrapped("/api/rtc"); // GET
  await wrapped("/api/rtc", { method: "POST", body: "kein json" });
  await wrapped("/api/rtc", { method: "POST", body: JSON.stringify({ sdp: "x", type: "answer" }) });
  await wrapped("/api/rtc", { method: "POST", body: JSON.stringify([1, 2]) });
  await wrapped("/api/rtc", { method: "POST", body: JSON.stringify(null) });
  await wrapped("/api/rtc", { method: "POST", body: new Uint8Array([1]) });
  await wrapped("https://elsewhere.example/start", { method: "POST", body });
  await wrapped("/api/rtc/extra", { method: "POST", body });
  assert.equal(calls.length, 9);
  for (const call of calls) assert.ok(!String(call.init?.body ?? "").includes('"sig"'));
});

test("signingFetch: ohne Schluessel oder bei Fehler im Speicher ungeaendert", async () => {
  const { calls, base } = recorder();
  const body = JSON.stringify({ sdp: "v=0\r\n", type: "offer" });
  await signingFetch(base, async () => null, () => 0)("/api/rtc", { method: "POST", body });
  assert.equal(calls[0].init?.body, body);
});
