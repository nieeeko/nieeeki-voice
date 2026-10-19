"use client";

import { createClient, type Session, type SupabaseClient } from "@supabase/supabase-js";

/** Browser-Client nur fuer die Anmeldung: oeffentlicher Schluessel, nie der service_role-Key. */
let client: SupabaseClient | null = null;

// "Angemeldet bleiben": an = Sitzung im dauerhaften Speicher, aus = nur bis Fenster/App geschlossen.
// Ohne Eintrag (Anmeldungen von vor der Checkbox) gilt "an".
const REMEMBER_KEY = "nieeeki-angemeldet-bleiben";
const DAY_MS = 24 * 3600 * 1000;

export function rememberLogin(): boolean {
  try {
    return localStorage.getItem(REMEMBER_KEY) !== "0";
  } catch {
    return false;
  }
}

/** Hat Nikolaj den Haken bei der letzten Anmeldung selbst gesetzt? (Vorauswahl im Browser) */
export function rememberChosen(): boolean {
  try {
    return localStorage.getItem(REMEMBER_KEY) === "1";
  } catch {
    return false;
  }
}

export function setRememberLogin(on: boolean) {
  try {
    localStorage.setItem(REMEMBER_KEY, on ? "1" : "0");
  } catch {
    // privater Modus o. Ae.: dann eben nur fuer diese Sitzung
  }
}

/** Je nach Haken localStorage oder sessionStorage; Abmelden raeumt beide. */
const authStorage = {
  getItem(key: string) {
    try {
      return (rememberLogin() ? localStorage : sessionStorage).getItem(key);
    } catch {
      return null;
    }
  },
  setItem(key: string, value: string) {
    try {
      (rememberLogin() ? localStorage : sessionStorage).setItem(key, value);
    } catch {
      // Speicher nicht verfuegbar: Sitzung lebt nur im Speicher der Seite
    }
  },
  removeItem(key: string) {
    try {
      localStorage.removeItem(key);
      sessionStorage.removeItem(key);
    } catch {
      // nichts zu tun
    }
  },
};

export function getSupabase(): SupabaseClient | null {
  if (typeof window === "undefined") return null;
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (!url || !key) return null;
  client ??= createClient(url, key, {
    auth: { persistSession: true, autoRefreshToken: true, storage: authStorage },
  });
  return client;
}

/** Desktop-App (desktop/preload.js) 1 Woche, Web-UI 1 Tag; der Voice-Core prueft dasselbe. */
export function maxLoginAgeMs(): number {
  return window.nieeekiDesktop?.isDesktop ? 7 * DAY_MS : DAY_MS;
}

/**
 * Zeitpunkt der Anmeldung (Passwort, danach Code) aus dem Token: Supabase schreibt ihn als "amr" hinein
 * und behaelt ihn beim Erneuern bei. Ohne Angabe: null (dann gilt die Anmeldung als abgelaufen).
 */
export function loginTime(session: Session): number | null {
  try {
    const part = session.access_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(atob(part)) as { amr?: { timestamp?: unknown }[] };
    const stamps = (claims.amr ?? []).map((e) => e?.timestamp).filter((t): t is number => typeof t === "number");
    return stamps.length ? Math.min(...stamps) * 1000 : null;
  } catch {
    return null;
  }
}

/** Access-Token fuer den Voice-Core (getSession erneuert es, wenn es abgelaufen ist). */
export async function accessToken(): Promise<string | null> {
  const supabase = getSupabase();
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}
