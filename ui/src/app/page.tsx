"use client";

import { VoiceLine } from "@/components/VoiceLine";
import { useNieeeki, type Presence } from "@/lib/useNieeeki";

const PRESENCE_TEXT: Record<Presence, string> = {
  offline: "schläft",
  connecting: "wacht auf",
  idle: "ist da",
  listening: "hört zu",
  thinking: "denkt nach",
  speaking: "spricht",
};

const CANVAS_KINDS = [
  { name: "Tabellen", hint: "„Zeig mir die Projektliste als Tabelle“" },
  { name: "Dokumente", hint: "„Entwirf ein Angebot für …“" },
  { name: "BPMN-Prozesse", hint: "„Modellier den Freigabeprozess“" },
  { name: "Architektur", hint: "„Skizzier die Systemlandschaft von …“" },
  { name: "Pipelines", hint: "„Wie läuft das Deployment gerade?“" },
  { name: "Code", hint: "„Schreib das Migrationsskript“" },
];

export default function Home() {
  const { presence, history, userLine, botLine, error, connect, disconnect } = useNieeeki();
  const online = presence !== "offline" && presence !== "connecting";

  return (
    <div className="shell">
      <section className="presence" data-presence={presence}>
        <header className="presence-head">
          <h1 className="wordmark">nieeeki</h1>
          <p className="state" aria-live="polite">
            {PRESENCE_TEXT[presence]}
          </p>
        </header>

        <VoiceLine history={history} presence={presence} />
        <div className="legend">
          <span className="legend-user">du</span>
          <span className="legend-bot">nieeeki</span>
        </div>

        <div className="lines" aria-live="polite">
          <p className="line-user">{userLine || (online ? "Sag einfach, was du brauchst." : "")}</p>
          <p className="line-bot">{botLine}</p>
        </div>

        {error && <p className="error">{error}</p>}

        <button
          className="wake"
          onClick={online ? disconnect : connect}
          disabled={presence === "connecting"}
        >
          {online ? "Gespräch beenden" : presence === "connecting" ? "Verbinde …" : "Gespräch starten"}
        </button>
        <p className="fine">Später reicht „nieeeki“ als Weckwort.</p>
      </section>

      <section className="canvas" aria-label="Canvas">
        <div className="canvas-empty">
          <h2>Was nieeeki dir hier zeigt</h2>
          <p>Ergebnisse, die man sehen muss, landen auf dieser Fläche statt in der Sprache.</p>
          <ul className="kinds">
            {CANVAS_KINDS.map((k) => (
              <li key={k.name}>
                <strong>{k.name}</strong>
                <span>{k.hint}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>
    </div>
  );
}
