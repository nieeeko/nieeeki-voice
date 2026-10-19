"use client";

import type { Activity } from "@/lib/useNieeeki";

/** Was nieeeki gerade tut: Live-Zeitleiste der Werkzeugaufrufe (vom Voice-Core gemeldet). */

const LABEL: Record<string, string> = {
  webseite_oeffnen: "öffnet Webseite",
  programm_oeffnen: "startet Programm",
  oeffnen_mit: "öffnet",
  fenster_auflisten: "schaut, was offen ist",
  fenster_fokussieren: "holt Fenster nach vorne",
  tastatur_tippen: "tippt",
  tasten_druecken: "drückt",
  maus_scrollen: "scrollt",
  bildschirm_ansehen: "schaut auf den Bildschirm",
  bildschirm_teilen: "teilt den Bildschirm",
  auf_bildschirm_klicken: "klickt",
  powershell_lesen: "liest PC-Infos",
  websuche: "sucht im Web",
  canvas_zeigen: "legt aufs Canvas",
  merken: "merkt sich etwas",
  aktuelle_zeit: "schaut auf die Uhr",
  entwicklung_vorbereiten: "plant Entwicklung",
  entwicklung_starten: "startet Claude Code",
  entwicklung_status: "prüft Entwicklung",
  dark_mode: "schaltet Dark Mode",
  system_info: "liest Systeminfos",
};

const STATUS_TEXT: Record<Activity["status"], string> = {
  laeuft: "läuft",
  fertig: "erledigt",
  abgelehnt: "nicht ausgeführt",
  fehler: "Fehler",
};

export function ActivityFeed({ items }: { items: Activity[] }) {
  if (!items.length) return null;
  return (
    <section className="activity" aria-label="Was nieeeki gerade tut" aria-live="polite">
      <ol>
        {items.map((a) => (
          <li key={a.id} className="activity-item" data-status={a.status}>
            <span className="activity-dot" aria-hidden="true" />
            <span className="activity-what">
              {LABEL[a.name] ?? a.name}
              {a.detail && <span className="activity-detail"> {a.detail}</span>}
            </span>
            <span className="activity-status">{STATUS_TEXT[a.status]}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
