"use client";

import { useCallback, useEffect, useState } from "react";
import { deviceKeySupported, indexedDbKeyStore, loadOrCreateKey, pairingCode } from "@/lib/deviceKey";
import { deviceLabel, pairErrorText, pairState, type PairState } from "@/lib/devicePairing";
import { getSupabase } from "@/lib/supabase";

/*
 * Dieses Geraet koppeln (Weg B ohne Tailscale, plans/online-ohne-tailscale.md, Schritt 6). Das Geraet erzeugt
 * einen Schluessel, der es nie verlaesst, traegt dessen oeffentlichen Teil als wartende Zeile ein und zeigt
 * einen Kurzcode. Freigeben kann nur Nikolaj am Laptop (uv run devices.py koppeln): ein gestohlenes Token
 * reicht dafuer nicht. Ohne Kopplung lehnt der Voice-Core Sitzungen ueber das Internet ab.
 */

const SCHEMA = process.env.NEXT_PUBLIC_NIEEEKI_DB_SCHEMA ?? "nieeeki_voice_dev";
const POLL_MS = 3000;
const MAX_POLL_ERRORS = 3; // so viele Fehler hintereinander, dann steht "Wartet" nicht mehr endlos da
const UNSUPPORTED =
  "Dieser Browser kann den Geräteschlüssel hier nicht speichern (privater Modus, gesperrter Speicher oder keine HTTPS-Verbindung). Bitte in einem normalen Fenster über https://nieeeki.nieeeko.com öffnen.";

type Phase = "laden" | PairState | "fehler";

const BADGE: Record<Phase, { state: string; text: string }> = {
  laden: { state: "wartet", text: "Prüfe" },
  keine: { state: "aus", text: "Nicht gekoppelt" },
  wartet: { state: "wartet", text: "Wartet auf den Laptop" },
  gekoppelt: { state: "laeuft", text: "Gekoppelt" },
  fehler: { state: "fehler", text: "Fehler" },
};

/** Meine Zeile zu diesem Schluessel (RLS zeigt nur eigene). */
async function readState(publicKey: string): Promise<PairState> {
  const supabase = getSupabase();
  if (!supabase) throw new Error("kein Supabase");
  const { data, error } = await supabase
    .schema(SCHEMA)
    .from("voice_devices")
    .select("status,expires_at")
    .eq("public_key", publicKey)
    .maybeSingle();
  if (error) throw error;
  return pairState(data, Date.now());
}

export function DevicePair({ onClose }: { onClose: () => void }) {
  const [phase, setPhase] = useState<Phase>("laden");
  const [publicKey, setPublicKey] = useState<string | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Beim Oeffnen: gibt es hier schon einen Schluessel, und wie steht seine Zeile?
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        if (!deviceKeySupported()) {
          setPhase("fehler");
          setMessage(UNSUPPORTED);
          return;
        }
        const key = await indexedDbKeyStore.load();
        if (!alive) return;
        if (!key) return setPhase("keine");
        const [state, shortCode] = await Promise.all([readState(key.publicKey), pairingCode(key.publicKey)]);
        if (!alive) return;
        setPublicKey(key.publicKey);
        setCode(shortCode);
        setPhase(state);
      } catch {
        if (alive) {
          setPhase("fehler");
          setMessage("Der Stand der Kopplung ließ sich nicht lesen. Bitte noch einmal öffnen.");
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // Solange der Laptop noch nicht freigegeben hat, alle paar Sekunden nachsehen
  useEffect(() => {
    if (phase !== "wartet" || !publicKey) return;
    let alive = true;
    let failures = 0;
    const id = setInterval(() => {
      void readState(publicKey)
        .then((state) => {
          failures = 0;
          if (!alive || state === "wartet") return;
          setPhase(state);
          if (state === "keine") setMessage("Der Code ist abgelaufen. Bitte die Kopplung neu starten.");
        })
        .catch(() => {
          // Dauerhaft kein Zugriff (Anmeldung abgelaufen, offline): nicht endlos "Wartet" anzeigen
          failures += 1;
          if (alive && failures >= MAX_POLL_ERRORS) {
            setPhase("fehler");
            setMessage("Der Stand der Kopplung lässt sich nicht lesen (offline oder Anmeldung abgelaufen). Bitte ab- und wieder anmelden.");
          }
        });
    }, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [phase, publicKey]);

  const start = useCallback(async () => {
    setBusy(true);
    setMessage(null);
    try {
      if (!deviceKeySupported()) {
        setPhase("fehler");
        setMessage(UNSUPPORTED);
        return;
      }
      // Speicher nicht von selbst wegraeumen lassen (Safari loescht sonst nach 7 Tagen ohne Nutzung); darf scheitern
      void navigator.storage?.persist?.().catch(() => {});
      const supabase = getSupabase();
      if (!supabase) throw new Error("kein Supabase");
      const key = await loadOrCreateKey(indexedDbKeyStore);
      const shortCode = await pairingCode(key.publicKey);
      const { error, status } = await supabase
        .schema(SCHEMA)
        .from("voice_devices")
        .insert({ public_key: key.publicKey, label: deviceLabel(navigator.userAgent) });
      // 23505: dieser Schluessel hat schon eine Zeile (wartend oder freigegeben), das ist kein Fehler
      if (error && error.code !== "23505") {
        setPhase("fehler");
        setMessage(pairErrorText(error, status));
        return;
      }
      setPublicKey(key.publicKey);
      setCode(shortCode);
      setPhase(await readState(key.publicKey));
    } catch {
      setPhase("fehler");
      setMessage("Die Kopplung konnte nicht gestartet werden. Bitte noch einmal versuchen.");
    } finally {
      setBusy(false);
    }
  }, []);

  const forget = useCallback(async () => {
    setBusy(true);
    try {
      await indexedDbKeyStore.clear();
      setPublicKey(null);
      setCode(null);
      setPhase("keine");
      setMessage("Der Schlüssel auf diesem Gerät ist gelöscht. Am Laptop mit „uv run devices.py entfernen“ auch die Freigabe entfernen.");
    } catch {
      setMessage("Der Schlüssel ließ sich nicht löschen.");
    } finally {
      setBusy(false);
    }
  }, []);

  const badge = BADGE[phase];
  return (
    <section className="enroll" aria-label="Gerät koppeln">
      <div className="enroll-head">
        <span className="hud-label">Weg ohne Tailscale</span>
        <span className="hud-badge" data-state={badge.state} role="status">
          {badge.text}
        </span>
      </div>
      <h2>Dieses Gerät koppeln</h2>
      <p className="enroll-hint">
        Ohne Tailscale darf nur ein Gerät eine Sitzung starten, das du am Laptop freigegeben hast. Der Schlüssel dafür
        entsteht auf diesem Gerät und verlässt es nie. Ein gestohlenes Passwort oder Token reicht dann nicht mehr (gegen
        Code, der auf diesem Gerät selbst in die Seite eingeschleust wird, schützt er nicht). Ist
        der Schlüssel einmal weg (privates Fenster, „Websitedaten löschen“, Safari nach längerer Nichtnutzung), einfach
        neu koppeln.
      </p>
      {phase === "wartet" && code && (
        <>
          <p className="pair-code">
            <span className="visually-hidden">Kopplungscode: </span>
            {code}
          </p>
          <p className="enroll-hint">
            Am Laptop im Ordner <b>voice-core</b> eingeben: <code>uv run devices.py koppeln</code>, dann diesen Code. Er
            gilt zehn Minuten.
          </p>
        </>
      )}
      {phase === "gekoppelt" && (
        <p className="enroll-hint">
          Fertig. Dieses Gerät kann jetzt ohne Tailscale eine Sitzung starten
          {code ? ` (Code ${code})` : ""}.
        </p>
      )}
      <p className="enroll-status" aria-live="polite" data-state={phase === "fehler" ? "fehler" : phase === "gekoppelt" ? "fertig" : ""}>
        {message ?? ""}
      </p>
      <div className="approval-actions">
        {(phase === "keine" || phase === "fehler") && (
          <button type="button" className="approve" disabled={busy} onClick={() => void start()}>
            Kopplung starten
          </button>
        )}
        {publicKey && (
          <button type="button" className="reject" disabled={busy} onClick={() => void forget()}>
            Schlüssel auf diesem Gerät löschen
          </button>
        )}
        <button type="button" className="reject" onClick={onClose}>
          Schließen
        </button>
      </div>
    </section>
  );
}
