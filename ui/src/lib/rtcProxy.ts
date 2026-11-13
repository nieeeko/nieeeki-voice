/**
 * Signalisierungs-Proxy fuer "online ohne Tailscale" (Weg B, plans/online-ohne-tailscale.md, Schritt 5).
 *
 * Der Browser-Transport (SmallWebRTCTransport) kann nur per HTTP signalisieren. Statt den Voice-Core im Netz
 * zu erreichen, legt diese Logik sein Offer als Zeile in voice_signals ab (als der angemeldete Nutzer: die
 * Datenbank prueft aal2, Anmeldealter, Profil und Rate-Limit) und wartet, bis der Voice-Core (der nur
 * ausgehend nachfragt, voice-core/rendezvous.py) die Antwort zurueckgeschrieben hat.
 *
 * Kein Geheimnis noetig: nur der oeffentliche Publishable Key und das Token des Nutzers.
 * Reine Funktionen mit eingereichten Abhaengigkeiten (fetch, Uhr), damit sie ohne Netz testbar sind.
 * Nur TypeScript-Syntax, die Node beim Testen selbst entfernen kann (kein enum, keine Parameter-Properties).
 */

export const MAX_BODY_CHARS = 40_000;
// sig und ts: Signatur des Geraeteschluessels (deviceKey.ts), geprueft wird sie nur im Voice-Core (devices.py)
const ALLOWED_KEYS = new Set(["sdp", "type", "pc_id", "restart_pc", "requestData", "request_data", "sig", "ts"]);
const MAX_SIG_CHARS = 200;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Kurzcodes, die der Voice-Core in die Zeile schreibt (rendezvous.py), und was der Client daraus machen soll:
// 4xx beendet ihn sofort (deterministisch), 5xx laesst ihn nach 2 s neu versuchen.
// "abbruch": der Voice-Core wurde mitten in der Zeile beendet, ein neuer Versuch hilft (5xx).
const FAILED_STATUS: Record<string, number> = {
  owner: 403, geraet: 403, form: 400, wiederholt: 400, handler: 502, abbruch: 503,
};

export type RtcEnv = { url: string; key: string; schema: string };
export type Deps = {
  fetch: (url: string, init?: RequestInit) => Promise<Response>;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
};
export type ProxyResult = { status: number; body: Record<string, unknown> };

const fail = (status: number, error: string): ProxyResult => ({ status, body: { error } });

/** Das Token aus "Authorization: Bearer ...", sonst null. */
export function bearerToken(header: string | null | undefined): string | null {
  const m = /^Bearer\s+(\S+)$/i.exec(header ?? "");
  return m ? m[1] : null;
}

/**
 * Das Offer so, wie die Tabelle es will (nur sdp, type "offer", pc_id, restart_pc), oder null bei jeder
 * Abweichung. requestData aus dem Client wird verworfen: Der Voice-Core erzeugt den Body fuer bot() selbst.
 */
export function parseOffer(text: string): Record<string, unknown> | null {
  if (text.length === 0 || text.length > MAX_BODY_CHARS) return null;
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) return null;
  const offer = data as Record<string, unknown>;
  for (const key of Object.keys(offer)) if (!ALLOWED_KEYS.has(key)) return null;
  if (typeof offer.sdp !== "string" || offer.sdp.length === 0) return null;
  if (offer.type !== "offer") return null;
  const pcId = offer.pc_id;
  if (pcId != null && (typeof pcId !== "string" || pcId.length === 0 || pcId.length > 100)) return null;
  if (offer.restart_pc != null && typeof offer.restart_pc !== "boolean") return null;
  const parsed: Record<string, unknown> = {
    sdp: offer.sdp,
    type: "offer",
    pc_id: pcId ?? null,
    restart_pc: offer.restart_pc ?? false,
  };
  // Signatur und Zeit des Geraets: nur durchreichen, wenn sie die Form haben, die der Voice-Core erwartet
  const { sig, ts } = offer;
  if (sig != null) {
    if (typeof sig !== "string" || sig.length === 0 || sig.length > MAX_SIG_CHARS || !/^[A-Za-z0-9_-]+$/.test(sig)) return null;
    parsed.sig = sig;
  }
  if (ts != null) {
    if (typeof ts !== "number" || !Number.isInteger(ts) || ts <= 0 || ts >= 1e11) return null;
    parsed.ts = ts;
  }
  return parsed;
}

/**
 * STUN-Adressen aus NEXT_PUBLIC_ICE_SERVERS (JSON-Liste mit "urls"). Die Variable ist im Browser fuer jeden
 * lesbar, darum zaehlen nur stun:/stuns:-Adressen; TURN und Zugangsdaten werden verworfen (die kommen spaeter
 * kurzlebig vom Voice-Core, Plan Schritt 3). Ungueltig oder leer ergibt eine leere Liste.
 */
export function parseIceServers(raw: string | undefined): Array<{ urls: string[] }> {
  if (!raw || raw.trim() === "") return [];
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(data)) return [];
  const servers: Array<{ urls: string[] }> = [];
  for (const entry of data) {
    if (typeof entry !== "object" || entry === null) continue;
    const urls = (entry as { urls?: unknown }).urls;
    const list = typeof urls === "string" ? [urls] : Array.isArray(urls) ? urls : [];
    const stun = list.filter((u): u is string => typeof u === "string" && /^stuns?:/i.test(u));
    if (stun.length > 0 && stun.length === list.length) servers.push({ urls: stun });
  }
  return servers;
}

function insertFailure(status: number): ProxyResult {
  if (status === 401) return fail(401, "anmeldung");
  if (status === 403) return fail(403, "verboten");
  if (status === 429) return fail(429, "zu_viele_anfragen");
  if (status === 400 || status === 413 || status === 422) return fail(400, "form");
  return fail(status >= 500 ? 503 : 502, "datenbank");
}

/**
 * Legt das Offer ab und wartet auf die Antwort des Voice-Cores.
 * Erfolg: 200 mit { sdp, type, pc_id }. Fehler: nur ein Kurzcode, nie Zeileninhalt oder Token.
 */
export async function relayOffer(
  offer: Record<string, unknown>,
  token: string,
  env: RtcEnv,
  deps: Deps,
  opts: { timeoutMs?: number; pollMs?: number } = {},
): Promise<ProxyResult> {
  const timeoutMs = opts.timeoutMs ?? 20_000;
  const pollMs = opts.pollMs ?? 500;
  const base = `${env.url.replace(/\/$/, "")}/rest/v1/voice_signals`;
  const auth = { apikey: env.key, Authorization: `Bearer ${token}` };

  let id: string;
  try {
    const res = await deps.fetch(base, {
      method: "POST",
      headers: { ...auth, "Content-Type": "application/json", "Content-Profile": env.schema, Prefer: "return=representation" },
      body: JSON.stringify({ request: offer }),
    });
    if (!res.ok) return insertFailure(res.status);
    const rows = (await res.json()) as Array<{ id?: unknown }>;
    const rowId = Array.isArray(rows) ? rows[0]?.id : undefined;
    if (typeof rowId !== "string" || !UUID.test(rowId)) return fail(502, "unerwartet");
    id = rowId;
  } catch {
    return fail(503, "datenbank");
  }

  const deadline = deps.now() + timeoutMs;
  while (deps.now() < deadline) {
    await deps.sleep(pollMs); // die Antwort kommt nie sofort: der Voice-Core fragt alle 2 s nach
    try {
      const res = await deps.fetch(`${base}?id=eq.${id}&select=status,response`, {
        headers: { ...auth, "Accept-Profile": env.schema },
      });
      if (res.status === 401 || res.status === 403) return fail(res.status, "anmeldung");
      if (!res.ok) continue; // kurzer Ausfall der Datenbank: weiter versuchen, bis die Zeit um ist
      const rows = (await res.json()) as Array<{ status?: string; response?: Record<string, unknown> }>;
      const row = Array.isArray(rows) ? rows[0] : undefined;
      if (!row) break; // Zeile ist weg (aufgeraeumt): keine Antwort mehr zu erwarten
      if (row.status === "answered" && row.response && typeof row.response.sdp === "string") {
        return {
          status: 200,
          body: { sdp: row.response.sdp, type: row.response.type ?? "answer", pc_id: row.response.pc_id ?? null },
        };
      }
      if (row.status === "failed") {
        const code = typeof row.response?.error === "string" ? row.response.error : "";
        return fail(FAILED_STATUS[code] ?? 502, code in FAILED_STATUS ? code : "voice_core");
      }
    } catch {
      // Netzfehler beim Nachfragen: weiter versuchen, bis die Zeit um ist
    }
  }
  return fail(504, "keine_antwort");
}
