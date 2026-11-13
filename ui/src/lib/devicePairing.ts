/**
 * Kleine, reine Helfer fuer das Koppeln dieses Geraets (components/DevicePair.tsx, Weg B Schritt 6):
 * Stand der Zeile in voice_devices, Etikett aus dem Browser und verstaendliche Fehlertexte.
 * Kein window, kein Netz: ohne Browser testbar (devicePairing.test.ts).
 */

export type PairState = "gekoppelt" | "wartet" | "keine";

/** Stand der eigenen Zeile: freigegeben, wartet noch auf den Laptop (und ist nicht abgelaufen) oder nichts. */
export function pairState(row: { status?: unknown; expires_at?: unknown } | null | undefined, nowMs: number): PairState {
  if (!row) return "keine";
  if (row.status === "active") return "gekoppelt";
  if (row.status === "pending" && typeof row.expires_at === "string") {
    const until = Date.parse(row.expires_at);
    if (Number.isFinite(until) && until > nowMs) return "wartet";
  }
  return "keine";
}

/** Name zur Anzeige am Laptop (die Datenbank erlaubt 1 bis 40 Zeichen ohne Steuerzeichen). Kein Beweis, nur Orientierung. */
export function deviceLabel(userAgent: string): string {
  if (/iPhone/i.test(userAgent)) return "iPhone";
  if (/iPad/i.test(userAgent)) return "iPad";
  if (/Android/i.test(userAgent)) return "Android";
  if (/Windows/i.test(userAgent)) return "Windows-PC";
  if (/Macintosh|Mac OS/i.test(userAgent)) return "Mac";
  if (/Linux/i.test(userAgent)) return "Linux";
  return "Browser";
}

/** Was bei einem fehlgeschlagenen Eintragen zu sagen ist (Fehler von PostgREST: code, HTTP-Status). */
export function pairErrorText(error: { code?: string } | null, status: number): string {
  if (error?.code === "PT429" || status === 429) return "Zu viele offene Kopplungen. Bitte zehn Minuten warten und noch einmal versuchen.";
  if (status === 401 || status === 403 || error?.code === "42501") {
    return "Die Anmeldung reicht dafür nicht mehr. Bitte ab- und wieder anmelden (mit Code).";
  }
  return "Die Kopplung konnte nicht gestartet werden. Ist die Datenbank auf dem neuesten Stand (Migration 0003)?";
}
