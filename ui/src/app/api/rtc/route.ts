import { NextResponse, type NextRequest } from "next/server";
import { MAX_BODY_CHARS, bearerToken, parseOffer, relayOffer } from "@/lib/rtcProxy";

// Signalisierung fuer "online ohne Tailscale" (Weg B, plans/online-ohne-tailscale.md, Schritt 5).
// Der Browser-Transport schickt Offer (POST) und ICE-Kandidaten (PATCH) immer per HTTP an einen Endpunkt.
// Hier landet das Offer als Zeile in voice_signals (als der angemeldete Nutzer, die Datenbank prueft aal2,
// Anmeldealter, Profil und Rate-Limit), der Voice-Core holt es ausgehend ab und antwortet.
// Kein Geheimnis noetig: nur NEXT_PUBLIC_*-Werte und das Token des Nutzers.

// (Kein "dynamic": mit cacheComponents nicht erlaubt; POST und PATCH werden ohnehin nie vorab erzeugt.)
export const maxDuration = 60;

const NO_STORE = { "Cache-Control": "no-store" };
const reply = (body: Record<string, unknown>, status: number) => NextResponse.json(body, { status, headers: NO_STORE });

export async function POST(req: NextRequest) {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  // Dasselbe Schema wie der Voice-Core (NIEEEKI_DB_SCHEMA, Standard nieeeki_voice_dev)
  const schema = process.env.NEXT_PUBLIC_NIEEEKI_DB_SCHEMA ?? "nieeeki_voice_dev";
  if (!url || !key) return reply({ error: "nicht_eingerichtet" }, 503);

  const token = bearerToken(req.headers.get("authorization"));
  if (!token) return reply({ error: "anmeldung" }, 401);

  const text = await req.text();
  if (text.length > MAX_BODY_CHARS) return reply({ error: "form" }, 413);
  const offer = parseOffer(text);
  if (!offer) return reply({ error: "form" }, 400);

  const result = await relayOffer(
    offer,
    token,
    { url, key, schema },
    {
      fetch: (u, init) => fetch(u, { ...init, cache: "no-store" }),
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      now: () => Date.now(),
    },
    { timeoutMs: 20_000 },
  );
  return reply(result.body, result.status);
}

// ICE-Kandidaten: Der Transport baut das Offer mit allen Kandidaten (waitForICEGathering), schickt sie aber
// trotzdem danach noch einzeln. Fehler dabei loggt er nur. Der Voice-Core bekommt sie schon im Offer,
// darum nichts zu tun und keine Datenbank noetig.
export async function PATCH() {
  return reply({ status: "success" }, 200);
}
