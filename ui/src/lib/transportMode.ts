/**
 * Welchen Weg nimmt die Oberflaeche zum Voice-Core? (Weg B, plans/online-ohne-tailscale.md)
 *   "direkt":     wie bisher ueber NEXT_PUBLIC_VOICE_CORE_URL (Tailscale oder localhost)
 *   "rendezvous": ueber /api/rtc und Supabase, ohne Tailscale
 *
 * Der Standard kommt aus NEXT_PUBLIC_TRANSPORT (beim Bauen eingesetzt). Damit man bei Standard "rendezvous" nicht
 * ausgesperrt ist, falls dieser Weg scheitert (z. B. ohne TURN hinter Mobilfunk), gilt zuerst, was der Nutzer
 * selbst waehlt: "?weg=direkt" oder "?weg=rendezvous" in der Adresse (wird im Browser gemerkt), "?weg=standard"
 * vergisst die Wahl wieder. Reine Funktion (kein window, kein localStorage), damit sie ohne Browser testbar ist.
 */

export type Transport = "direkt" | "rendezvous";
export type TransportChoice = { mode: Transport; store?: Transport | "clear" };

const isTransport = (value: unknown): value is Transport => value === "direkt" || value === "rendezvous";

export function resolveTransport(input: {
  envDefault: string | undefined;
  search: string;
  stored: string | null;
}): TransportChoice {
  const standard: Transport = input.envDefault === "rendezvous" ? "rendezvous" : "direkt";
  const wanted = new URLSearchParams(input.search.startsWith("?") ? input.search : "").get("weg");
  if (isTransport(wanted)) return { mode: wanted, store: wanted };
  if (wanted === "standard") return { mode: standard, store: "clear" };
  if (isTransport(input.stored)) return { mode: input.stored };
  return { mode: standard };
}
