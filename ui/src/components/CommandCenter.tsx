"use client";

import { useEffect, useState } from "react";
import { ActivityFeed, clockTime } from "@/components/ActivityFeed";
import { Canvas } from "@/components/Canvas";
import { Composer } from "@/components/Composer";
import { HudOrb } from "@/components/HudOrb";
import { Mission } from "@/components/Mission";
import { ScreenView } from "@/components/ScreenView";
import { VoiceLine } from "@/components/VoiceLine";
import type { NieeekiState, Presence } from "@/lib/useNieeeki";

/*
 * Kommandozentrale: links das Gespraech, in der Mitte der Kern (oder Canvas / Live-Bildschirm),
 * rechts Freigaben und Verlauf. Laeuft ein Auftrag, rueckt die Schrittliste neben den Kern.
 * Reine Darstellung: den Zustand liefert useNieeeki (page.tsx).
 */

const PRESENCE_TEXT: Record<Presence, string> = {
  offline: "Schläft",
  connecting: "Wacht auf",
  idle: "Ist da",
  listening: "Hört zu",
  thinking: "Denkt nach",
  speaking: "Spricht",
};

const CANVAS_KINDS = [
  { name: "Tabellen", hint: "„Zeig mir die Projektliste als Tabelle“" },
  { name: "Dokumente", hint: "„Entwirf ein Angebot für …“" },
  { name: "Diagramme", hint: "„Skizzier die Systemlandschaft von …“" },
  { name: "Code", hint: "„Zeig mir ein SQL-Statement mit JOIN“" },
  { name: "Websuche", hint: "„Wie wird das Wetter morgen?“" },
  { name: "Am PC", hint: "„Öffne WhatsApp“ · „Klick auf Senden“" },
];

type View = "kern" | "canvas" | "bildschirm";
type Side = "freigaben" | "verlauf";

/** Sitzungsdauer im eigenen Takt (die Seite rendert dafuer nicht jede Sekunde neu). */
function SessionClock({ since }: { since: number | null }) {
  const [now, setNow] = useState(0); // 0 statt Date.now(): Server und Browser rendern gleich
  useEffect(() => {
    if (!since) return;
    const tick = () => setNow(Date.now());
    const first = setTimeout(tick, 0);
    const id = setInterval(tick, 1000);
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, [since]);
  if (!since || !now) return <span className="session-value">--:--</span>;
  const s = Math.max(0, Math.floor((now - since) / 1000));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return <span className="session-value">{h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`}</span>;
}

export function CommandCenter({ n, signOut }: { n: NieeekiState; signOut: () => void }) {
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
    mission,
    connectedAt,
    acting,
    screenOn,
    screenTrack,
    sendText,
    toggleScreen,
    screenMonitor,
    screenMonitors,
    chooseMonitor,
    clickScreen,
    scrollScreen,
    typeOnScreen,
    pressOnScreen,
    decide,
    connect,
    disconnect,
  } = n;
  const online = presence !== "offline" && presence !== "connecting";
  const state = acting && online ? "Handelt" : PRESENCE_TEXT[presence];
  const toggleConversation = () => void (online ? disconnect() : connect());

  // Ansicht in der Mitte: Nikolajs Wahl, aber Neues drängt nach vorn (neues Canvas-Element,
  // Bildschirm eingeschaltet). Abgeleitet statt per Effekt, damit nichts nachflackert.
  const [pick, setPick] = useState<{ view: View; canvas: number; screen: boolean }>({
    view: "kern",
    canvas: 0,
    screen: false,
  });
  let view = pick.view;
  if (canvasItems.length > pick.canvas) view = "canvas";
  if (screenOn && !pick.screen) view = "bildschirm";
  if (view === "bildschirm" && !screenOn) view = "kern";
  const choose = (v: View) => setPick({ view: v, canvas: canvasItems.length, screen: screenOn });

  // Rechte Spalte: eine neue Freigabe holt den Reiter "Freigaben" nach vorn
  const [sidePick, setSidePick] = useState<{ tab: Side; approvals: number }>({ tab: "verlauf", approvals: 0 });
  const side: Side = approvals.length > sidePick.approvals ? "freigaben" : sidePick.tab;
  const chooseSide = (tab: Side) => setSidePick({ tab, approvals: approvals.length });

  const hasMission = mission.steps.length > 0;

  return (
    <div className="hud" data-presence={presence} data-acting={acting} data-screen={screenOn}>
      <div className="hud-bg" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>

      {error && (
        <p className="error-banner" role="alert">
          {error}
        </p>
      )}
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">nieeeki</span>
          <span className="hud-label">Kommandozentrale</span>
        </div>
        <ul className="modules" aria-label="Status">
          <li data-on={online}>
            Voice-Core <b>{online ? "Online" : presence === "connecting" ? "Verbindet" : "Aus"}</b>
          </li>
          {online && (
            <li data-on={screenOn}>
              Bildschirm <b>{screenOn ? "Live" : "Aus"}</b>
            </li>
          )}
          {approvals.length > 0 && (
            <li data-on="warn">
              Freigaben <b>{approvals.length}</b>
            </li>
          )}
        </ul>
        <div className="session">
          <span className="hud-label">Sitzung</span>
          <SessionClock since={connectedAt} />
        </div>
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
      </header>

      <aside className="col-talk" aria-label="Gespräch">
        <h2 className="panel-title">
          Gespräch
          <span className="hud-badge" data-state={online ? "laeuft" : "aus"}>
            {online ? "● Online" : "Offline"}
          </span>
        </h2>
        <VoiceLine history={history} presence={presence} />
        <div className="legend" aria-hidden="true">
          <span className="legend-user">Du</span>
          <span className="legend-bot">nieeeki</span>
        </div>
        <div className="lines" aria-live="polite">
          <p className="line-user" key={`u-${userLine}`}>
            <span className="who">Du</span>
            {userLine || (online ? "Sag einfach, was du brauchst." : "Tippe auf den Kern oder schreib unten.")}
          </p>
          {botLine && (
            <p className="line-bot" key={`b-${botLine.slice(0, 24)}`}>
              <span className="who">nieeeki</span>
              {botLine}
            </p>
          )}
        </div>
      </aside>

      <main className="col-core">
        <nav className="views" aria-label="Ansicht">
          <button type="button" aria-pressed={view === "kern"} onClick={() => choose("kern")}>
            Kern
          </button>
          <button type="button" aria-pressed={view === "canvas"} onClick={() => choose("canvas")}>
            Canvas{canvasItems.length > 0 && <span className="count">{canvasItems.length}</span>}
          </button>
          {(online || screenOn) && (
            <button type="button" aria-pressed={view === "bildschirm"} onClick={() => choose("bildschirm")}>
              Bildschirm{screenOn && <span className="count live">Live</span>}
            </button>
          )}
        </nav>

        {view === "kern" && (
          <div className="core-view" data-mission={hasMission}>
            <div className="core-stage">
              <HudOrb presence={presence} levels={levels} acting={acting} onToggle={toggleConversation} />
              <p className="core-state" aria-live="polite">
                <span className="core-state-name">nieeeki</span>
                <span className="core-state-text">{state}</span>
              </p>
              <p className="core-hint">
                {online
                  ? "Sprich einfach los oder schreib unten. Tippen auf den Kern beendet das Gespräch."
                  : "Sag „nieeeki“ oder tippe auf den Kern."}
              </p>
            </div>
            {hasMission && <Mission mission={mission} />}
            <Composer
              presence={presence}
              screenOn={screenOn}
              onSend={sendText}
              onToggleConversation={toggleConversation}
              onToggleScreen={toggleScreen}
            />
          </div>
        )}

        {view === "canvas" &&
          (canvasItems.length > 0 ? (
            <Canvas items={canvasItems} />
          ) : (
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
          ))}

        {view === "bildschirm" && screenOn && (
          <ScreenView
            track={screenTrack}
            control={{ tap: clickScreen, scroll: scrollScreen, type: typeOnScreen, key: pressOnScreen }}
            monitors={{ count: screenMonitors, current: screenMonitor, choose: chooseMonitor }}
          />
        )}
      </main>

      <aside className="col-tasks" aria-label="Freigaben und Verlauf">
        <nav className="side-tabs" aria-label="Rechte Spalte">
          <button type="button" aria-pressed={side === "freigaben"} onClick={() => chooseSide("freigaben")}>
            Freigaben{approvals.length > 0 && <span className="count warn">{approvals.length}</span>}
          </button>
          <button type="button" aria-pressed={side === "verlauf"} onClick={() => chooseSide("verlauf")}>
            Verlauf
          </button>
        </nav>

        {side === "freigaben" &&
          (approvals.length > 0 ? (
            <section className="approvals" aria-label="Wartet auf deine Freigabe">
              {approvals.map((a) => (
                <article key={`${a.job}-${a.id}`} className="approval" data-admin={a.admin ? "true" : undefined}>
                  <p className="approval-meta">
                    <span>{clockTime(a.seit)}</span>
                    <span className="approval-kind">{a.admin ? "Admin-Freigabe" : "Freigabe"}</span>
                    {a.projekt && <span>{a.projekt}</span>}
                  </p>
                  <p className="approval-what">{a.grund}</p>
                  <code className="approval-detail">{a.detail}</code>
                  <div className="approval-actions">
                    <button className="approve" onClick={() => void decide(a, true)}>
                      Freigeben
                    </button>
                    <button className="reject" onClick={() => void decide(a, false)}>
                      Ablehnen
                    </button>
                    {a.admin && (
                      <button
                        className="always"
                        onClick={() => void decide(a, true, true)}
                        title="Freigeben und diese Aktion künftig ohne Rückfrage erlauben"
                      >
                        Immer erlauben
                      </button>
                    )}
                  </div>
                </article>
              ))}
            </section>
          ) : (
            <p className="panel-empty">
              Keine offenen Freigaben. Will nieeeki etwas tun, das du erlauben musst, erscheint es hier mit
              Freigeben und Ablehnen.
            </p>
          ))}

        {side === "verlauf" && <ActivityFeed items={activities} />}
      </aside>
    </div>
  );
}
