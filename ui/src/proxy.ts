import { NextResponse, type NextRequest } from "next/server";
import { cspHeaders, cspMode, cspModeIsKnown, newNonce, requestHeadersForNext } from "@/lib/csp";

// Setzt je Seitenaufruf die strenge Content-Security-Policy mit frischer Nonce (lib/csp.ts erklaert Stufen und
// Inhalt). Next liest die Nonce aus den Headern der Anfrage und haengt sie an seine eigenen Skripte; dafuer muss die
// Seite je Anfrage gerendert werden (app/layout.tsx: connection()). Achtung: Next kopiert auch die Antwort-Header von
// hier auf die Anfrage. Darum setzt der Proxy nie einen scharfen Header ohne Nonce (siehe BASELINE_POLICY).

let warnedMode = false;

export function proxy(request: NextRequest) {
  const raw = process.env.NIEEEKI_CSP;
  if (!warnedMode && !cspModeIsKnown(raw)) {
    warnedMode = true;
    console.warn(`NIEEEKI_CSP: unbekannter Wert ${JSON.stringify((raw ?? "").slice(0, 40))}, es gilt "beobachten"`);
  }
  const headers = cspHeaders(cspMode(raw, process.env.NODE_ENV === "production"), {
    nonce: newNonce(),
    supabaseUrl: process.env.NEXT_PUBLIC_SUPABASE_URL,
    // derselbe Standard wie in lib/useNieeeki.ts
    voiceCoreUrl: process.env.NEXT_PUBLIC_VOICE_CORE_URL ?? "http://localhost:7860",
  });

  const response = NextResponse.next({ request: { headers: requestHeadersForNext(request.headers, headers.forNext) } });
  for (const [name, value] of headers.response) response.headers.set(name, value);
  return response;
}

export const config = {
  // Seiten ja (auch Vorab-Abrufe: Sonst kaeme eine Policy samt Nonce des Absenders ungeprueft bei Next an); API-Routen
  // und statische Dateien brauchen keine Policy. Pfadgrenzen, damit z. B. /apixyz nicht ausgenommen ist.
  matcher: ["/((?!api(?:/|$)|_next/static/|_next/image(?:/|$)|icons/|favicon\\.ico$|manifest\\.webmanifest$).*)"],
};
