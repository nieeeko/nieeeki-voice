"use client";

import { ActivityFeed } from "@/components/ActivityFeed";
import { Canvas } from "@/components/Canvas";
import { LoginGate } from "@/components/LoginGate";
import { Orb } from "@/components/Orb";
import { ScreenView } from "@/components/ScreenView";
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
  { name: "Diagramme", hint: "„Skizzier die Systemlandschaft von …“" },
  { name: "Code", hint: "„Zeig mir ein SQL-Statement mit JOIN“" },
  { name: "Websuche", hint: "„Wie wird das Wetter morgen?“" },
  { name: "Am PC", hint: "„Öffne WhatsApp“ · „Klick auf Senden“" },
];

export default function Home() {
  return <LoginGate>{(signOut) => <Nieeeki signOut={signOut} />}</LoginGate>;
}

function Nieeeki({ signOut }: { signOut: () => void }) {
  const {
    presence,
    history,
    levels,
    userLine,
    botLine,
    error,
    canvasItems,
    approvals,
    activities,
    acting,
    screenOn,
    screenTrack,
    toggleScreen,
    clickScreen,
    decide,
    connect,
    disconnect,
  } = useNieeeki();
  const online = presence !== "offline" && presence !== "connecting";
  const state = acting && online ? "handelt" : PRESENCE_TEXT[presence];

  return (
    <div className="shell" data-presence={presence} data-acting={acting} data-screen={screenOn}>
      <div className="aurora" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>

      <section className="presence" data-presence={presence}>
        <header className="presence-head">
          <h1 className="wordmark">nieeeki</h1>
          <p className="state" aria-live="polite">
            <span className="state-dot" aria-hidden="true" />
            {state}
          </p>
        </header>

        <div className="orb-wrap">
          <Orb presence={presence} levels={levels} acting={acting} />
        </div>

        <VoiceLine history={history} presence={presence} />

        <div className="lines" aria-live="polite">
          <p className="line-user" key={`u-${userLine}`}>
            {userLine || (online ? "Sag einfach, was du brauchst." : "Tippe auf „Gespräch starten“.")}
          </p>
          {botLine && (
            <p className="line-bot" key={`b-${botLine.slice(0, 24)}`}>
              {botLine}
            </p>
          )}
        </div>

        <ActivityFeed items={activities} />

        {error && <p className="error">{error}</p>}

        {approvals.length > 0 && (
          <section className="approvals" aria-label="Freigaben">
            <h2>Wartet auf deine Freigabe</h2>
            {approvals.map((a) => (
              <article key={`${a.job}-${a.id}`} className="approval">
                <p className="approval-what">
                  <strong>{a.grund}</strong> <span>· {a.projekt}</span>
                </p>
                <code className="approval-detail">{a.detail}</code>
                <div className="approval-actions">
                  <button className="approve" onClick={() => decide(a, true)}>
                    Freigeben
                  </button>
                  <button className="reject" onClick={() => decide(a, false)}>
                    Ablehnen
                  </button>
                </div>
              </article>
            ))}
          </section>
        )}

        {online && (
          <button type="button" className="screen-toggle" onClick={toggleScreen} aria-pressed={screenOn}>
            {screenOn ? "Bildschirm ausblenden" : "Bildschirm zeigen"}
          </button>
        )}

        <button
          className="wake"
          onClick={online ? disconnect : connect}
          disabled={presence === "connecting"}
        >
          {online ? "Gespräch beenden" : presence === "connecting" ? "Verbinde …" : "Gespräch starten"}
        </button>
        <button
          type="button"
          className="gate-link"
          onClick={() => {
            void disconnect();
            signOut();
          }}
        >
          Abmelden
        </button>
      </section>

      <section className="canvas" aria-label="Canvas">
        {screenOn && <ScreenView track={screenTrack} onTap={clickScreen} />}
        {canvasItems.length > 0 ? (
          <Canvas items={canvasItems} />
        ) : screenOn ? null : (
          <div className="canvas-empty">
            <h2>Was nieeeki dir hier zeigt</h2>
            <p>Ergebnisse, die man sehen muss, landen auf dieser Fläche statt in der Sprache.</p>
            <ul className="kinds">
              {CANVAS_KINDS.map((k, i) => (
                <li key={k.name} style={{ animationDelay: `${i * 0.9}s` }}>
                  <strong>{k.name}</strong>
                  <span>{k.hint}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </div>
  );
}
