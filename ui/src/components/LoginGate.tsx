"use client";

import Image from "next/image";
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { getSupabase, loginTime, maxLoginAgeMs, rememberChosen, setRememberLogin } from "@/lib/supabase";

/**
 * Zugang zu nieeeki: E-Mail und Passwort, danach der Code aus der Authenticator-App (aal2).
 * Beim ersten Mal richtet Nikolaj den zweiten Faktor per QR-Code ein. Der Voice-Core prueft
 * dasselbe Token noch einmal selbst (voice-core/auth_gate.py), die Oberflaeche ist nur die Tuer.
 */

// Build-Konstanten: auf Server und Client gleich, also ohne Hydration-Unterschied nutzbar
const CONFIGURED = Boolean(
  process.env.NEXT_PUBLIC_SUPABASE_URL && process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY,
);

type Step =
  | { kind: "laedt" }
  | { kind: "fehlt" }
  | { kind: "anmelden" }
  | { kind: "einrichten"; factorId: string; qr: string; secret: string }
  | { kind: "code"; factorId: string }
  | { kind: "fertig" };

/**
 * Supabase liefert den QR-Code als rohe, unkodierte SVG-Daten-URL mit Zeilenumbruch am Ende.
 * next/image bricht daran ab ("cannot end with a space or control character"), also sauber
 * kodiert neu aufbauen.
 */
export function qrDataUrl(raw: string) {
  const svg = raw.slice(raw.indexOf(",") + 1).trim();
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** "1 Woche" in der Desktop-App, sonst "1 Tag" (nur im Browser aufrufen). */
function maxAgeLabel() {
  return maxLoginAgeMs() > 24 * 3600 * 1000 ? "1 Woche" : "1 Tag";
}

/** Anmeldung zu alt (Desktop 1 Woche, Web 1 Tag)? Dann abmelden; true = abgemeldet. */
async function signOutIfExpired(): Promise<boolean> {
  const supabase = getSupabase();
  const { data } = (await supabase?.auth.getSession()) ?? { data: { session: null } };
  if (!supabase || !data.session) return false;
  const since = loginTime(data.session);
  if (since !== null && Date.now() - since <= maxLoginAgeMs()) return false;
  await supabase.auth.signOut();
  return true;
}

function CodeField() {
  return (
    <label>
      Sechsstelliger Code
      <input
        name="code"
        inputMode="numeric"
        autoComplete="one-time-code"
        pattern="[0-9 ]{6,7}"
        maxLength={7}
        required
        autoFocus
      />
    </label>
  );
}

export function LoginGate({ children }: { children: (signOut: () => void) => ReactNode }) {
  const [step, setStep] = useState<Step>(CONFIGURED ? { kind: "laedt" } : { kind: "fehlt" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Welcher Schritt ist dran? Ohne Sitzung anmelden, mit aal1 Code abfragen oder einrichten.
  const nextStep = useCallback(async () => {
    const supabase = getSupabase();
    if (!supabase) return setStep({ kind: "fehlt" });
    const { data: sessionData } = await supabase.auth.getSession();
    if (!sessionData.session) return setStep({ kind: "anmelden" });
    if (await signOutIfExpired()) {
      setError(`Deine Anmeldung ist abgelaufen (nach ${maxAgeLabel()}). Bitte neu anmelden.`);
      return setStep({ kind: "anmelden" });
    }

    const { data: aal, error: aalError } = await supabase.auth.mfa.getAuthenticatorAssuranceLevel();
    if (aalError) {
      setError(aalError.message);
      return setStep({ kind: "anmelden" });
    }
    if (aal.currentLevel === "aal2") return setStep({ kind: "fertig" });

    const { data: factors, error: listError } = await supabase.auth.mfa.listFactors();
    if (listError) {
      setError(listError.message);
      return setStep({ kind: "anmelden" });
    }
    const verified = factors.totp[0];
    if (verified) return setStep({ kind: "code", factorId: verified.id });

    // Ersteinrichtung: liegengebliebene, nie bestaetigte Faktoren zuerst entfernen
    for (const f of factors.all) {
      if (f.factor_type === "totp" && f.status === "unverified") {
        await supabase.auth.mfa.unenroll({ factorId: f.id });
      }
    }
    const { data: enrolled, error: enrollError } = await supabase.auth.mfa.enroll({
      factorType: "totp",
      issuer: "nieeeki",
      friendlyName: "nieeeki",
    });
    if (enrollError) {
      setError(enrollError.message);
      return setStep({ kind: "anmelden" });
    }
    setStep({
      kind: "einrichten",
      factorId: enrolled.id,
      qr: qrDataUrl(enrolled.totp.qr_code),
      secret: enrolled.totp.secret,
    });
  }, []);

  useEffect(() => {
    const supabase = getSupabase();
    if (!supabase) return;
    const { data } = supabase.auth.onAuthStateChange((event) => {
      if (event === "SIGNED_OUT") setStep({ kind: "anmelden" });
      // Weitere Supabase-Aufrufe erst nach dem Rueckruf, sonst blockiert sich supabase-js selbst
      if (event === "INITIAL_SESSION") setTimeout(() => void nextStep(), 0);
    });
    return () => data.subscription.unsubscribe();
  }, [nextStep]);

  // Auch bei offener Seite: nach 1 Tag (Web) bzw. 1 Woche (Desktop) automatisch abmelden
  const signedIn = step.kind === "fertig";
  useEffect(() => {
    if (!signedIn) return;
    const id = setInterval(() => {
      void signOutIfExpired().then((out) => {
        if (out) setError(`Deine Anmeldung ist abgelaufen (nach ${maxAgeLabel()}). Bitte neu anmelden.`);
      });
    }, 60_000);
    return () => clearInterval(id);
  }, [signedIn]);

  const run = async (action: () => Promise<string | null>) => {
    setBusy(true);
    setError(null);
    try {
      const problem = await action();
      if (problem) setError(problem);
      else await nextStep();
    } finally {
      setBusy(false);
    }
  };

  const signIn = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    // vor dem Anmelden festlegen: davon haengt ab, wohin supabase-js die Sitzung schreibt
    setRememberLogin(form.get("bleiben") === "on");
    void run(async () => {
      const { error } = await getSupabase()!.auth.signInWithPassword({
        email: String(form.get("email")).trim(),
        password: String(form.get("password")),
      });
      if (!error) return null;
      return error.status === 400 ? "E-Mail oder Passwort stimmt nicht." : error.message;
    });
  };

  const verify = (factorId: string) => (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const code = String(new FormData(e.currentTarget).get("code")).replace(/\s/g, "");
    void run(async () => {
      const { error } = await getSupabase()!.auth.mfa.challengeAndVerify({ factorId, code });
      return error ? "Der Code passt nicht. Nimm den aktuellen aus der App." : null;
    });
  };

  const signOut = useCallback(() => {
    void getSupabase()?.auth.signOut();
  }, []);

  if (step.kind === "fertig") return <>{children(signOut)}</>;

  return (
    <div className="gate">
      <div className="aurora" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <section className="gate-card" aria-labelledby="gate-title">
        <h1 className="wordmark" id="gate-title">
          nieeeki
        </h1>

        {step.kind === "laedt" && <p className="gate-hint">Einen Moment …</p>}

        {step.kind === "fehlt" && (
          <p className="error">
            Anmeldung nicht eingerichtet: NEXT_PUBLIC_SUPABASE_URL und
            NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY fehlen.
          </p>
        )}

        {step.kind === "anmelden" && (
          <form className="gate-form" onSubmit={signIn}>
            <p className="gate-hint">Melde dich an. Nur du sprichst mit nieeeki.</p>
            <label>
              E-Mail
              <input name="email" type="email" autoComplete="username" required autoFocus />
            </label>
            <label>
              Passwort
              <input name="password" type="password" autoComplete="current-password" required />
            </label>
            {/* Nur im Browser gerendert (Schritt "anmelden" gibt es erst nach dem Laden) */}
            <label className="gate-check">
              <input
                name="bleiben"
                type="checkbox"
                defaultChecked={Boolean(window.nieeekiDesktop?.isDesktop) || rememberChosen()}
              />
              Angemeldet bleiben ({maxAgeLabel()})
            </label>
            <button className="wake" disabled={busy}>
              {busy ? "Prüfe …" : "Anmelden"}
            </button>
          </form>
        )}

        {step.kind === "einrichten" && (
          <form className="gate-form" onSubmit={verify(step.factorId)}>
            <p className="gate-hint">
              Einmalig: Scanne den QR-Code mit deiner Authenticator-App (z. B. Google
              Authenticator, Microsoft Authenticator, 1Password) und gib die sechs Ziffern ein.
            </p>
            <Image
              className="gate-qr"
              src={step.qr}
              alt="QR-Code für die Authenticator-App"
              width={200}
              height={200}
              unoptimized
            />
            <details className="gate-secret">
              <summary>QR-Code lässt sich nicht scannen?</summary>
              <code>{step.secret}</code>
            </details>
            <CodeField />
            <button className="wake" disabled={busy}>
              {busy ? "Prüfe …" : "Bestätigen"}
            </button>
          </form>
        )}

        {step.kind === "code" && (
          <form className="gate-form" onSubmit={verify(step.factorId)}>
            <p className="gate-hint">Der Code aus deiner Authenticator-App.</p>
            <CodeField />
            <button className="wake" disabled={busy}>
              {busy ? "Prüfe …" : "Weiter"}
            </button>
          </form>
        )}

        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}

        {(step.kind === "einrichten" || step.kind === "code") && (
          <button type="button" className="gate-link" onClick={signOut}>
            Abmelden
          </button>
        )}
      </section>
    </div>
  );
}
