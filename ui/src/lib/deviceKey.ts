/**
 * Geraeteschluessel fuer Weg B (plans/online-ohne-tailscale.md, Schritt 6).
 *
 * Ohne Tailscale reicht sonst ein gestohlenes Supabase-Token fuer eine Sitzung. Darum signiert dieses Geraet jedes
 * Offer mit einem eigenen Schluessel, der es nie verlaesst: ECDSA P-256, per WebCrypto erzeugt und NICHT
 * exportierbar (extractable: false), im IndexedDB des Browsers. Der Voice-Core (voice-core/devices.py) kennt nur
 * den oeffentlichen Teil der Geraete, die Nikolaj am Laptop mit dem Kurzcode freigegeben hat. Der Schluessel laesst
 * sich nicht kopieren oder mitnehmen; Skripte der eigenen Seite koennten ihn allerdings zum Signieren benutzen (wie
 * bei jedem WebCrypto-Schluessel): Schutz davor ist die Seite selbst (kein fremder Code, kein XSS).
 *
 * Vertrag mit devices.py (beide Seiten haben denselben Testvektor, bei Aenderungen gemeinsam anpassen):
 *   - oeffentlicher Schluessel: base64url (ohne Padding) des rohen Punkts, 65 Bytes (0x04 || X || Y)
 *   - signiert wird der Text "nieeeki-rtc-v1|<ts>|<pc_id oder leer>|<SHA-256 des SDP in Hex>" (UTF-8)
 *   - Signatur: ECDSA mit SHA-256, roh r||s (64 Bytes, so liefert sie WebCrypto), base64url
 *   - ts: ganze Sekunden seit 1970 (Uhr des Geraets); der Voice-Core erlaubt 60 s Abweichung zur Datenbankuhr
 *   - Kurzcode: erste 60 Bit von SHA-256(roher Schluessel), je 5 Bit als Zeichen aus CODE_ALPHABET, "XXXX-XXXX-XXXX"
 *
 * Nur TypeScript-Syntax, die Node beim Testen selbst entfernen kann (kein enum, keine Parameter-Properties).
 */

export const SIGN_PREFIX = "nieeeki-rtc-v1";
// Ohne I, O, 0 und 1: beim Abtippen am Laptop nicht zu verwechseln
export const CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
export const CODE_CHARS = 12; // 60 Bit: zu viel, um fuer einen mitgelesenen Schluessel eine zweite Vorlage zu suchen
export const RTC_PATH = "/api/rtc";

export type DeviceKey = {
  privateKey: CryptoKey; // nicht exportierbar
  publicKey: string; // base64url, roh
};

/** Wo der Schluessel liegt (im Browser IndexedDB, im Test ein Speicher). */
export type KeyStore = {
  load: () => Promise<DeviceKey | null>;
  save: (key: DeviceKey) => Promise<void>;
  clear: () => Promise<void>;
};

const encoder = new TextEncoder();

export function toBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Strikt: nur das URL-sichere Alphabet, kein Padding. Wirft bei allem anderen. */
export function fromBase64Url(text: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) throw new Error("kein base64url");
  const binary = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  // Nur die kanonische Schreibweise (wie b64url_decode in devices.py): Fuellbits muessen 0 sein
  if (toBase64Url(bytes) !== text) throw new Error("kein kanonisches base64url");
  return bytes;
}

async function sha256(data: Uint8Array): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", data as BufferSource));
}

const toHex = (bytes: Uint8Array) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");

/** Kurzcode aus dem rohen oeffentlichen Schluessel (base64url): was das Handy zeigt und der Laptop abfragt. */
export async function pairingCode(publicKey: string): Promise<string> {
  const digest = await sha256(fromBase64Url(publicKey));
  // Die ersten 60 Bit, je 5 Bit ein Zeichen, hoechstwertige zuerst (wie pairing_code in devices.py); bitweise gelesen,
  // weil 60 Bit nicht mehr in eine Zahl passen und BigInt das Ziel der tsconfig nicht kennt
  let code = "";
  for (let i = 0; i < CODE_CHARS; i++) {
    let value = 0;
    for (let bit = i * 5; bit < i * 5 + 5; bit++) value = (value << 1) | ((digest[bit >> 3] >> (7 - (bit & 7))) & 1);
    code += CODE_ALPHABET[value];
  }
  return `${code.slice(0, 4)}-${code.slice(4, 8)}-${code.slice(8)}`;
}

export function signedMessage(ts: number, pcId: string | null | undefined, sdpHashHex: string): string {
  return `${SIGN_PREFIX}|${ts}|${pcId ?? ""}|${sdpHashHex}`;
}

/** Neues Schluesselpaar. Der private Teil ist nicht exportierbar, auch nicht von eigenem Code oder Erweiterungen. */
export async function createDeviceKey(): Promise<DeviceKey> {
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  // Der oeffentliche Teil ist immer exportierbar (WebCrypto), auch wenn das Paar es nicht ist
  const raw = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
  return { privateKey: pair.privateKey, publicKey: toBase64Url(raw) };
}

export async function loadOrCreateKey(store: KeyStore): Promise<DeviceKey> {
  const create = async () => {
    const existing = await store.load();
    if (existing) return existing;
    const key = await createDeviceKey();
    await store.save(key);
    return key;
  };
  // Zwei Tabs gleichzeitig: nur einer legt an, der andere findet danach den Schluessel (Web Locks, wo vorhanden)
  const locks = typeof navigator !== "undefined" ? navigator.locks : undefined;
  return locks ? locks.request("nieeeki-geraet-schluessel", create) : create();
}

/** Signatur und Zeitstempel fuer ein Offer. */
export async function signOffer(
  key: DeviceKey,
  offer: { sdp: string; pc_id?: string | null },
  nowSecs: number,
): Promise<{ sig: string; ts: number }> {
  const ts = Math.floor(nowSecs);
  const hash = toHex(await sha256(encoder.encode(offer.sdp)));
  const message = encoder.encode(signedMessage(ts, offer.pc_id, hash));
  const sig = new Uint8Array(
    await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key.privateKey, message as BufferSource),
  );
  return { sig: toBase64Url(sig), ts };
}

type FetchFn = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

/**
 * fetch, das Offers an /api/rtc signiert. Der Transport (SmallWebRTCTransport) bietet keinen Haken dafuer: Er
 * schickt Offer, Neuverhandlung und ICE-Neustart per fetch an den Endpunkt, Header sind dort fest. Alles andere
 * (andere Adressen, andere Methoden, fremde Bodies) geht unveraendert durch. Ohne Schluessel auch: Dann lehnt
 * der Voice-Core ab (Kurzcode "geraet"), falls er die Bindung verlangt.
 */
export function signingFetch(base: FetchFn, getKey: () => Promise<DeviceKey | null>, now: () => number): FetchFn {
  return async (input, init) => {
    // Der Transport (client-js makeRequest) ruft fetch(new Request(endpoint, { method, headers, body })) mit EINEM
    // Argument auf: Der Body steckt dann im Request, nicht in init. Beide Formen werden gesignt.
    const req = typeof Request !== "undefined" && input instanceof Request ? input : null;
    const method = (init?.method ?? req?.method ?? "GET").toUpperCase();
    if (method !== "POST") return base(input, init);
    let path: string;
    try {
      path = new URL(requestUrl(input), "http://localhost").pathname;
    } catch {
      return base(input, init);
    }
    if (path !== RTC_PATH) return base(input, init);

    const bodyInRequest = req !== null && init?.body == null;
    let text: string;
    if (typeof init?.body === "string") {
      text = init.body;
    } else if (bodyInRequest) {
      try {
        text = await req.clone().text(); // der Klon, damit das Original unberuehrt bleibt
      } catch {
        return base(input, init);
      }
    } else {
      return base(input, init);
    }

    let body: unknown;
    try {
      body = JSON.parse(text);
    } catch {
      return base(input, init);
    }
    const offer = body as { sdp?: unknown; type?: unknown; pc_id?: unknown };
    if (typeof body !== "object" || body === null || typeof offer.sdp !== "string" || offer.type !== "offer") {
      return base(input, init);
    }
    const key = await getKey();
    if (!key) return base(input, init);
    const pcId = typeof offer.pc_id === "string" ? offer.pc_id : null;
    const signed = await signOffer(key, { sdp: offer.sdp, pc_id: pcId }, now() / 1000);
    const signedBody = JSON.stringify({ ...body, ...signed });
    // new Request(req, { body }) behaelt Methode, Header (Authorization, Content-Type), mode und signal des Originals
    return bodyInRequest ? base(new Request(req, { body: signedBody })) : base(input, { ...init, body: signedBody });
  };
}

// ---------- Browser: IndexedDB ----------

const DB_NAME = "nieeeki-geraet";
const STORE = "schluessel";
const RECORD = "geraet";

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB nicht verfuegbar"));
  });
}

// Fertig ist ein Zugriff erst mit dem Abschluss der Transaktion (oncomplete), nicht schon mit dem Erfolg der
// Anfrage: Bricht die Transaktion ab (Speicher voll, Browser beendet), muss das als Fehler ankommen, sonst zeigt die
// Oberflaeche einen Kurzcode fuer einen Schluessel, der nie gespeichert wurde.
function run<T>(mode: IDBTransactionMode, action: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return openDb().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const tx = db.transaction(STORE, mode);
        let result: T | undefined;
        const request = action(tx.objectStore(STORE));
        request.onsuccess = () => {
          result = request.result;
        };
        request.onerror = () => reject(request.error ?? new Error("IndexedDB-Fehler"));
        tx.oncomplete = () => resolve(result as T);
        tx.onabort = () => reject(tx.error ?? new Error("IndexedDB-Transaktion abgebrochen"));
        tx.onerror = () => reject(tx.error ?? new Error("IndexedDB-Fehler"));
      }).finally(() => db.close()),
  );
}

/** Kann dieser Browser den Geraeteschluessel erzeugen und speichern? (Secure Context, WebCrypto, IndexedDB) */
export function deviceKeySupported(): boolean {
  return (
    typeof window !== "undefined" &&
    window.isSecureContext === true &&
    typeof crypto !== "undefined" &&
    !!crypto.subtle &&
    typeof indexedDB !== "undefined"
  );
}

/** Ein CryptoKey laesst sich per Strukturklon in IndexedDB ablegen, ohne dass sein Material je sichtbar wird. */
export const indexedDbKeyStore: KeyStore = {
  async load() {
    const value = (await run("readonly", (s) => s.get(RECORD))) as Partial<DeviceKey> | undefined;
    return value && value.privateKey instanceof CryptoKey && typeof value.publicKey === "string"
      ? { privateKey: value.privateKey, publicKey: value.publicKey }
      : null;
  },
  async save(key) {
    await run("readwrite", (s) => s.put(key, RECORD));
  },
  async clear() {
    await run("readwrite", (s) => s.delete(RECORD));
  },
};

const INSTALLED = Symbol.for("nieeeki.rtcSigning");

/**
 * Haengt das signierende fetch einmal in window.fetch ein. Es fasst nur POST-Anfragen an /api/rtc an; ohne
 * gespeicherten Schluessel aendert es nichts. Mehrfaches Aufrufen (auch nach einem Neuladen im Entwicklungsmodus)
 * schadet nicht: Die Markierung sitzt auf der Funktion selbst.
 */
export function installRtcSigning(): void {
  if (typeof window === "undefined") return;
  if ((window.fetch as unknown as Record<symbol, unknown>)[INSTALLED]) return;
  const original = window.fetch.bind(window);
  const getKey = () =>
    indexedDbKeyStore.load().catch((e) => {
      // Kein Schluessel lesbar (privater Modus o. Ae.): Das Offer geht unsigniert raus, der Voice-Core lehnt es ab
      console.warn("Geraeteschluessel nicht lesbar:", e instanceof Error ? e.name : "Fehler");
      return null;
    });
  const wrapped = signingFetch(original, getKey, () => Date.now());
  (wrapped as unknown as Record<symbol, unknown>)[INSTALLED] = true;
  window.fetch = wrapped;
}
