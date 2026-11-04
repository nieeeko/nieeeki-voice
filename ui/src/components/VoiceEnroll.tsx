"use client";

import type { Enrollment } from "@/lib/useNieeeki";

/*
 * Stimmabdruck einlernen (voice-core/voice_gate.py): Vorlesetext und Fortschritt. Der Voice-Core zaehlt
 * nur Sekunden mit Sprache; Pausen fuellen den Balken nicht. Gespeichert wird nur der Abdruck.
 */

// "Der Nordwind und die Sonne" (Aesop), der uebliche Text fuer Sprachaufnahmen: viele deutsche Laute.
const TEXT =
  "Einst stritten sich Nordwind und Sonne, wer von ihnen beiden wohl der Stärkere wäre, als ein Wanderer, " +
  "der in einen warmen Mantel gehüllt war, des Weges daherkam. Sie wurden einig, dass derjenige für den " +
  "Stärkeren gelten sollte, der den Wanderer zwingen würde, seinen Mantel abzunehmen. Der Nordwind blies mit " +
  "aller Macht, aber je mehr er blies, desto fester hüllte sich der Wanderer in seinen Mantel ein. Endlich " +
  "gab der Nordwind den Kampf auf. Nun erwärmte die Sonne die Luft mit ihren freundlichen Strahlen, und schon " +
  "nach wenigen Augenblicken zog der Wanderer seinen Mantel aus. Da musste der Nordwind zugeben, dass die " +
  "Sonne von ihnen beiden der Stärkere war.";

const STATUS_TEXT: Record<Enrollment["status"], string> = {
  start: "nieeeki sagt kurz, worum es geht. Danach einfach loslesen.",
  aufnahme: "Ich höre zu. Lies einfach weiter, Pausen sind kein Problem.",
  rechnet: "Fast fertig, ich rechne den Abdruck aus.",
  fertig: "Fertig. nieeeki erkennt jetzt deine Stimme.",
  fehler: "Das hat nicht geklappt.",
};

function badge(e: Enrollment): { state: string; text: string } {
  if (e.status === "fehler") return { state: "fehler", text: "Fehler" };
  if (e.status === "fertig") return { state: "laeuft", text: "Fertig" };
  if (e.status === "rechnet") return { state: "wartet", text: "Rechnet" };
  if (e.status === "aufnahme") return { state: "laeuft", text: `${Math.floor(e.sekunden)} / ${e.ziel} s` };
  return { state: "wartet", text: "Gleich" };
}

export function VoiceEnroll({ enrollment, onClose }: { enrollment: Enrollment; onClose: () => void }) {
  const done = enrollment.status === "fertig" || enrollment.status === "fehler";
  const pct = Math.min(100, Math.round((enrollment.sekunden / enrollment.ziel) * 100));
  const b = badge(enrollment);
  return (
    <section className="enroll" aria-label="Stimme einlernen">
      <div className="enroll-head">
        <span className="hud-label">Stimmabdruck</span>
        <span className="hud-badge" data-state={b.state}>
          {b.text}
        </span>
      </div>
      <h2>Stimme einlernen</h2>
      <p className="enroll-hint">
        Lies den Text in normaler Lautstärke vor oder erzähl einfach frei, bis der Balken voll ist. Am besten
        ohne andere Stimmen im Raum. Gespeichert wird nur der Abdruck, keine Aufnahme.
      </p>
      <blockquote className="enroll-text">{TEXT}</blockquote>
      <div
        className="enroll-bar"
        role="progressbar"
        aria-label="Aufgenommene Sprache in Sekunden"
        aria-valuemin={0}
        aria-valuemax={enrollment.ziel}
        aria-valuenow={Math.floor(enrollment.sekunden)}
      >
        <span style={{ width: `${pct}%` }} />
      </div>
      <p className="enroll-status" aria-live="polite" data-state={enrollment.status}>
        {enrollment.status === "fehler" && enrollment.text ? enrollment.text : STATUS_TEXT[enrollment.status]}
      </p>
      <div className="approval-actions">
        <button type="button" className={done ? "approve" : "reject"} onClick={onClose}>
          {done ? "Schließen" : "Abbrechen"}
        </button>
      </div>
    </section>
  );
}
