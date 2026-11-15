// Content-Security-Policy der Web-Oberflaeche (plans/online-ohne-tailscale.md, Folgepaket zu Schritt 6).
//
// Warum: Der Geraeteschluessel schuetzt vor einem gestohlenen Token, aber nicht vor fremdem Code in der Seite selbst
// (der koennte ueber das Signieren Sitzungen starten). Die strenge Policy laesst nur Skripte zu, die die Nonce dieser
// Antwort tragen oder von einem solchen Skript nachgeladen werden ('strict-dynamic'); kein eval, keine Inline-Handler.
//
// Stufen (Umgebungsvariable NIEEEKI_CSP; nur im Produktions-Build, in der Entwicklung immer aus):
//   beobachten (Standard): die strenge Policy nur als Report-Only (Meldungen an /api/csp-report)
//   streng:                die strenge Policy scharf
//   aus:                   keine strenge Policy
// Der Grundschutz (BASELINE_POLICY) ist davon unabhaengig immer scharf.
// Erst nach einem sauberen Live-Lauf ohne Meldungen auf "streng" stellen (wie bei der Stimmpruefung: erst beobachten).
//
// Reine Funktionen ohne Next-Importe, damit `npm test` sie direkt prueft (csp.test.ts).

export type CspMode = "aus" | "beobachten" | "streng";

export type CspInput = {
  nonce: string;
  /** NEXT_PUBLIC_SUPABASE_URL: Anmeldung, Geraeteliste (https) und Realtime (wss) */
  supabaseUrl?: string;
  /** NEXT_PUBLIC_VOICE_CORE_URL: der direkte Weg (?weg=direkt) spricht den Voice-Core selbst an */
  voiceCoreUrl?: string;
};

export type CspHeaders = {
  /** Fuer Next (Request-Header): daraus liest es die Nonce und setzt sie an seine Skripte. null = keine Policy */
  forNext: string | null;
  /** Fuer den Browser (Response-Header) */
  response: Array<[string, string]>;
};

export const REPORT_PATH = "/api/csp-report";
export const MAX_REPORT_CHARS = 8000;
/** Hoechstens so viele Bytes liest /api/csp-report vom Body (gestreamt, auch ohne Content-Length). Gleich der
 * Zeichengrenze: Ein Text bis MAX_REPORT_BYTES Bytes hat nie mehr als MAX_REPORT_CHARS Zeichen. */
export const MAX_REPORT_BYTES = MAX_REPORT_CHARS;
// Je Anfrage hoechstens 2 Logzeilen: Der Endpunkt ist ohne Anmeldung erreichbar (der Browser schickt kein Token).
const MAX_REPORTS = 2;
const MAX_FIELD_CHARS = 200;
const KNOWN_MODES = new Set(["", "aus", "beobachten", "streng"]);

// Grundschutz: kann nichts kaputt machen (kein <object>, kein <base>, keine Einbettung, keine fremden Formularziele).
// Er ist im Produktions-Build immer scharf und kommt als fester Header aus next.config.ts, NICHT aus dem Proxy:
// Next kopiert die Antwort-Header des Proxys auf die Anfrage und liest daraus die Nonce. Ein scharfer Header ohne
// script-src aus dem Proxy verdeckte beim Beobachten die Nonce der Report-Only-Policy (gemessen am 14.11.2026:
// kein Skript trug eine Nonce, jedes eigene Skript wurde gemeldet).
export const BASELINE_POLICY = "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'";

/** Feste Header fuer jede Antwort (next.config.ts); der Grundschutz nur im Produktions-Build. */
export function securityHeaders(production: boolean): Array<{ key: string; value: string }> {
  const headers = [
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "Referrer-Policy", value: "no-referrer" },
    { key: "X-Frame-Options", value: "DENY" },
    // Die Oberflaeche braucht nur das Mikrofon (nur die eigene Seite); alles andere bleibt fuer Seite und Einbettungen zu.
    {
      key: "Permissions-Policy",
      value:
        "microphone=(self), camera=(), display-capture=(), geolocation=(), payment=(), usb=(), serial=(), hid=(), " +
        "midi=(), browsing-topics=()",
    },
  ];
  if (production) headers.push({ key: "Content-Security-Policy", value: BASELINE_POLICY });
  return headers;
}

export function cspMode(raw: string | undefined, production: boolean): CspMode {
  if (!production) return "aus";
  const value = (raw ?? "").trim().toLowerCase();
  return value === "aus" || value === "streng" ? value : "beobachten";
}

/** false bei einem Tippfehler in NIEEEKI_CSP (dann gilt still "beobachten"; der Proxy warnt einmal im Log). */
export function cspModeIsKnown(raw: string | undefined): boolean {
  return KNOWN_MODES.has((raw ?? "").trim().toLowerCase());
}

/**
 * Header der Anfrage fuer Next: Next liest die Nonce aus content-security-policy(-report-only) der Anfrage. Darum nie
 * einen solchen Header des Clients uebernehmen (sonst waehlte der Absender die Nonce), nur die eigene Policy setzen.
 */
export function requestHeadersForNext(incoming: Headers, forNext: string | null): Headers {
  const headers = new Headers(incoming);
  headers.delete("content-security-policy");
  headers.delete("content-security-policy-report-only");
  if (forNext) headers.set("content-security-policy", forNext);
  return headers;
}

/** Herkunft (Schema, Host, Port) einer http(s)-Adresse, sonst null. */
export function httpOrigin(url: string | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.origin : null;
  } catch {
    return null;
  }
}

/** Frische Nonce je Antwort (144 Bit, base64 ohne Fuellzeichen). */
export function newNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(18));
  return btoa(String.fromCharCode(...bytes));
}

export function strictPolicy({ nonce, supabaseUrl, voiceCoreUrl }: CspInput): string {
  const connect = ["'self'"]; // /api/rtc und /api/csp-report
  const supabase = httpOrigin(supabaseUrl);
  if (supabase) connect.push(supabase, supabase.replace(/^http/, "ws"));
  const voiceCore = httpOrigin(voiceCoreUrl);
  if (voiceCore) connect.push(voiceCore);
  return [
    "default-src 'self'",
    // 'self' gilt nur fuer Browser ohne 'strict-dynamic'; Daily laedt sein Skript per Skript-Element (dailyPreload.ts)
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    // React-style-Attribute und das <style> im Mermaid-SVG (Canvas.tsx) brauchen Inline-Stile
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:", // data: fuer den QR-Code beim Einrichten des zweiten Faktors
    "font-src 'self' data:",
    "media-src 'self' blob: mediastream:",
    `connect-src ${connect.join(" ")}`,
    "worker-src 'self' blob:",
    "manifest-src 'self'",
    BASELINE_POLICY,
    // Bewusst nur report-uri: Steht zusaetzlich report-to da, nimmt Chrome nur die Reporting API, und die lieferte
    // in der Messung (14.11.2026) nichts aus. report-uri schickt jeden Verstoss sofort, in allen Browsern.
    `report-uri ${REPORT_PATH}`,
  ].join("; ");
}

export function cspHeaders(mode: CspMode, input: CspInput): CspHeaders {
  if (mode === "aus") return { forNext: null, response: [] };
  const strict = strictPolicy(input);
  if (mode === "streng") {
    return { forNext: strict, response: [["Content-Security-Policy", strict]] };
  }
  // Beim Beobachten nur Report-Only (der scharfe Grundschutz kommt aus next.config.ts, siehe BASELINE_POLICY)
  return { forNext: strict, response: [["Content-Security-Policy-Report-Only", strict]] };
}

// ---------- Meldungen des Browsers (/api/csp-report) ----------

/** Eine Meldung, bereinigt fuers Log: nur feste Felder, nur druckbares ASCII, Adressen ohne Query. */
export type CspReport = {
  directive: string;
  blocked: string;
  document: string;
  source: string;
  line: number | null;
  disposition: string;
};

function clean(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.replace(/[^\x20-\x7e]/g, "?").slice(0, MAX_FIELD_CHARS);
}

/**
 * Adressen ohne Query und Fragment (dort koennten Daten stehen), andere Schemata (data:, blob:, javascript:) nur als
 * Schema; Schluesselwoerter wie "inline" oder "eval" bleiben.
 */
function cleanUrl(value: unknown): string {
  if (typeof value !== "string") return "";
  try {
    const parsed = new URL(value);
    const web = parsed.protocol === "http:" || parsed.protocol === "https:";
    return clean(web ? parsed.origin + parsed.pathname : parsed.protocol);
  } catch {
    return clean(value);
  }
}

/** Body hoechstens bis maxBytes lesen (auch ohne Content-Length); laenger = null, der Rest wird verworfen. */
export async function readLimitedText(body: ReadableStream<Uint8Array> | null, maxBytes: number): Promise<string | null> {
  if (!body) return "";
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  let bytes = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    bytes += value.byteLength;
    if (bytes > maxBytes) {
      await reader.cancel();
      return null;
    }
    text += decoder.decode(value, { stream: true });
  }
  return text + decoder.decode();
}

function toReport(body: unknown): CspReport | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  const b = body as Record<string, unknown>;
  const directive = clean(b.effectiveDirective ?? b["effective-directive"] ?? b["violated-directive"]);
  if (!directive) return null;
  const line = b.lineNumber ?? b["line-number"];
  return {
    directive,
    blocked: cleanUrl(b.blockedURL ?? b["blocked-uri"]),
    document: cleanUrl(b.documentURL ?? b["document-uri"]),
    source: cleanUrl(b.sourceFile ?? b["source-file"]),
    line: typeof line === "number" && Number.isSafeInteger(line) && line >= 0 ? line : null,
    disposition: clean(b.disposition),
  };
}

/**
 * Liest beide Formate: das alte ({"csp-report": {...}}, report-uri) und die Reporting API
 * ([{type: "csp-violation", body: {...}}], report-to). Alles andere ergibt eine leere Liste.
 */
export function parseCspReports(text: string): CspReport[] {
  if (text.length > MAX_REPORT_CHARS) return [];
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return [];
  }
  const bodies: unknown[] = [];
  if (Array.isArray(data)) {
    for (const entry of data) {
      if (typeof entry === "object" && entry !== null && (entry as { type?: unknown }).type === "csp-violation") {
        bodies.push((entry as { body?: unknown }).body);
      }
    }
  } else if (typeof data === "object" && data !== null) {
    bodies.push((data as Record<string, unknown>)["csp-report"]);
  }
  const reports: CspReport[] = [];
  for (const body of bodies.slice(0, MAX_REPORTS)) {
    const report = toReport(body);
    if (report) reports.push(report);
  }
  return reports;
}
