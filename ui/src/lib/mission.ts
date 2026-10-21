import type { Activity } from "@/lib/useNieeeki";

/**
 * Ein Auftrag: alle Werkzeugschritte, die nieeeki fuer eine Bitte von Nikolaj ausfuehrt.
 * Ein neuer Auftrag beginnt mit dem ersten Schritt, nachdem Nikolaj etwas gesagt oder getippt hat
 * (newTask). Laeuft da noch ein Schritt des alten Auftrags, gehoert der neue Schritt noch dazu.
 */
export type Mission = { start: number; steps: Activity[] };

export const EMPTY_MISSION: Mission = { start: 0, steps: [] };

const OPEN: Activity["status"][] = ["laeuft", "wartet"];

export function isOpen(step: Activity) {
  return OPEN.includes(step.status);
}

/** Naechster Stand nach einer Aktivitaetsmeldung (gleiche id = Statuswechsel eines Schritts). */
export function nextMission(m: Mission, a: Activity, now: number, newTask: boolean): Mission {
  const i = m.steps.findIndex((s) => s.id === a.id);
  if (i >= 0) {
    const steps = [...m.steps];
    steps[i] = { ...steps[i], ...a, seit: steps[i].seit, bis: now };
    return { ...m, steps };
  }
  const step = { ...a, seit: now, bis: now };
  if (newTask && !m.steps.some(isOpen)) return { start: now, steps: [step] };
  return { start: m.steps.length ? m.start : now, steps: [...m.steps, step] };
}

export type MissionState = "laeuft" | "wartet" | "fehler" | "fertig";

/** Gesamtzustand: wartet eine Freigabe, laeuft etwas, ging etwas schief oder ist alles erledigt? */
export function missionState(m: Mission): MissionState {
  if (m.steps.some((s) => s.status === "wartet")) return "wartet";
  if (m.steps.some((s) => s.status === "laeuft")) return "laeuft";
  if (m.steps.some((s) => s.status === "fehler" || s.status === "abgelehnt")) return "fehler";
  return "fertig";
}

/** Dauer bis jetzt (laeuft noch) oder bis zum letzten Statuswechsel (abgeschlossen), in ms. */
export function missionDuration(m: Mission, now: number) {
  if (!m.steps.length) return 0;
  const end = m.steps.some(isOpen) ? now : Math.max(...m.steps.map((s) => s.bis ?? m.start));
  return Math.max(0, end - m.start);
}
