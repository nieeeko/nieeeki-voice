"use client";

import { useEffect, useState } from "react";
import { LABEL } from "@/components/ActivityFeed";
import { isOpen, missionDuration, missionState, type Mission as MissionData } from "@/lib/mission";

/**
 * Auftrag im Kommandozentralen-Stil: Schrittnummer gross, Dauer als laufende Uhr, jeder Werkzeugschritt
 * als Balken (erledigt = rot gefuellt, laeuft = Lauflicht, wartet = pulsiert, Fehler = gestrichen).
 * Alle Werte sind echt: Schritte und Zeiten kommen aus den Aktivitaetsmeldungen des Voice-Core.
 */

const STATE_TEXT = {
  laeuft: "Läuft",
  wartet: "Wartet auf Freigabe",
  fehler: "Mit Problem beendet",
  fertig: "Erledigt",
} as const;

const MARK = { laeuft: "", wartet: "!", fertig: "✓", abgelehnt: "✕", fehler: "✕" } as const;

function seconds(ms: number) {
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
}

function two(n: number) {
  return String(n).padStart(2, "0");
}

/** Laufende Dauer: zaehlt nur, solange ein Schritt offen ist (eigener Takt, die Seite rendert nicht mit). */
function Duration({ mission }: { mission: MissionData }) {
  // Startwert 0 statt Date.now(): gleich beim Server-Rendern und im Browser (keine Hydration-Abweichung)
  const [now, setNow] = useState(0);
  const running = mission.steps.some(isOpen); // abgeschlossen: Dauer bis zum letzten Schritt, ohne Uhr
  useEffect(() => {
    if (!running) return;
    const tick = () => setNow(Date.now());
    const first = setTimeout(tick, 0);
    const id = setInterval(tick, 100);
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, [running]);
  return <span className="mission-clock-value">{seconds(missionDuration(mission, now))}</span>;
}

export function Mission({ mission }: { mission: MissionData }) {
  const steps = mission.steps;
  if (!steps.length) return null;
  const state = missionState(mission);
  const done = steps.filter((s) => s.status === "fertig").length;
  const open = steps.findIndex(isOpen);
  const current = open >= 0 ? open + 1 : steps.length; // offener Schritt, sonst der letzte

  return (
    <section className="mission" data-state={state} aria-label="Aktueller Auftrag" aria-live="polite">
      <header className="mission-head">
        <div className="mission-title">
          <span className="hud-label">Auftrag</span>
          <span className="hud-badge" data-state={state}>
            {state === "fertig" ? "✓ " : "● "}
            {STATE_TEXT[state]}
          </span>
        </div>
        <div className="mission-clock">
          <span className="hud-label">Dauer</span>
          <Duration mission={mission} />
        </div>
      </header>

      <p className="mission-step" aria-label={`Schritt ${current} von ${steps.length}`}>
        <span className="mission-step-word">Schritt</span>
        <span className="mission-step-num">{two(current)}</span>
        <span className="mission-step-of">/ {two(steps.length)}</span>
      </p>

      <ol className="mission-steps">
        {steps.map((s, i) => (
          <li key={s.id} className="mission-row" data-status={s.status}>
            <span className="mission-idx">{two(i + 1)}</span>
            <span className="mission-bar">
              <span className="mission-fill" />
              <span className="mission-text">
                {LABEL[s.name] ?? s.name}
                {s.detail && <span className="mission-detail"> {s.detail}</span>}
              </span>
            </span>
            <span className="mission-mark" aria-label={s.status}>
              {MARK[s.status]}
            </span>
          </li>
        ))}
      </ol>

      <div className="mission-progress">
        <span className="hud-label">Fortschritt</span>
        <span className="mission-progress-value">
          {done}/{steps.length}
        </span>
        <span className="mission-progress-track">
          <span style={{ width: `${(done / steps.length) * 100}%` }} />
        </span>
      </div>
    </section>
  );
}
