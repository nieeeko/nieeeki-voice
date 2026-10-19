"use client";

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

/** Browser-Client nur fuer die Anmeldung: oeffentlicher Schluessel, nie der service_role-Key. */
let client: SupabaseClient | null = null;

export function getSupabase(): SupabaseClient | null {
  if (typeof window === "undefined") return null;
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (!url || !key) return null;
  client ??= createClient(url, key, { auth: { persistSession: true, autoRefreshToken: true } });
  return client;
}

/** Access-Token fuer den Voice-Core (getSession erneuert es, wenn es abgelaufen ist). */
export async function accessToken(): Promise<string | null> {
  const supabase = getSupabase();
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}
