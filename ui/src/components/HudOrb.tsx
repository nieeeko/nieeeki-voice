"use client";

import { useEffect, useRef, type RefObject } from "react";
import { Orb } from "@/components/Orb";
import type { LevelSample, Presence } from "@/lib/useNieeeki";

/**
 * Der Kern im Kommandozentralen-Stil: die lebendige Lichtkugel (Orb, WebGL) in HUD-Ringen (SVG).
 * - Skalenring aussen, vier Ringsegmente, Punktering und Fassung um den Kern
 * - Pegelbogen: waechst mit der Stimme (nieeeki rot, Nikolaj hell)
 * - denkt nach: kurzer Lichtbogen laeuft um
 * - handelt: roter Strichring und zwei Umlaufbahnen
 * Der Kern ist ein Knopf: Tippen startet oder beendet das Gespraech.
 */

const C = 400; // viewBox
const MID = C / 2;
const R_TICKS = 192;
const R_SEGMENTS = 172;
const LEVEL_SHARE = 0.38; // so viel vom Ring fuellt der Pegelbogen bei voller Stimme

function ticksPath() {
  let d = "";
  for (let i = 0; i < 120; i++) {
    const a = (i / 120) * Math.PI * 2;
    const len = i % 10 === 0 ? 10 : i % 5 === 0 ? 6 : 3;
    const x1 = MID + Math.cos(a) * R_TICKS;
    const y1 = MID + Math.sin(a) * R_TICKS;
    const x2 = MID + Math.cos(a) * (R_TICKS - len);
    const y2 = MID + Math.sin(a) * (R_TICKS - len);
    d += `M${x1.toFixed(1)} ${y1.toFixed(1)}L${x2.toFixed(1)} ${y2.toFixed(1)}`;
  }
  return d;
}

const TICKS = ticksPath(); // fest, einmal beim Laden

export function HudOrb({
  presence,
  levels,
  acting,
  onToggle,
}: {
  presence: Presence;
  levels: RefObject<LevelSample>;
  acting: boolean;
  onToggle: () => void;
}) {
  const levelRef = useRef<SVGCircleElement>(null);
  const online = presence !== "offline" && presence !== "connecting";
  const circumference = 2 * Math.PI * R_SEGMENTS;

  // Pegelbogen im eigenen Animationsframe, ohne React neu zu rendern
  useEffect(() => {
    const arc = levelRef.current;
    if (!arc) return;
    let raf = 0;
    let shown = 0;
    let speaker = "";
    const frame = () => {
      raf = requestAnimationFrame(frame);
      const lv = levels.current;
      const user = Math.min(1, (lv?.user ?? 0) * 3.2);
      const bot = Math.min(1, (lv?.bot ?? 0) * 3.2);
      const target = Math.max(user, bot);
      shown += (target - shown) * (target > shown ? 0.35 : 0.08);
      const len = Math.max(0, shown) * circumference * LEVEL_SHARE;
      arc.style.strokeDasharray = `${len.toFixed(1)} ${circumference.toFixed(1)}`;
      const who = user > bot ? "user" : "bot";
      if (who !== speaker) {
        speaker = who;
        arc.dataset.who = who;
      }
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [levels, circumference]);

  return (
    <button
      type="button"
      className="hud-orb"
      data-presence={presence}
      data-acting={acting}
      onClick={onToggle}
      disabled={presence === "connecting"}
      aria-label={online ? "Gespräch beenden" : "Gespräch starten"}
    >
      <Orb presence={presence} levels={levels} acting={acting} />
      <svg className="hud-rings" viewBox={`0 0 ${C} ${C}`} aria-hidden="true">
        <g className="hud-spin-slow">
          <path className="hud-ticks" d={TICKS} />
        </g>
        <circle className="hud-command" cx={MID} cy={MID} r={184} />
        <g className="hud-spin-rev">
          <circle className="hud-segments" cx={MID} cy={MID} r={R_SEGMENTS} />
        </g>
        <circle
          ref={levelRef}
          className="hud-level"
          cx={MID}
          cy={MID}
          r={R_SEGMENTS}
          transform={`rotate(112 ${MID} ${MID})`}
        />
        <circle className="hud-dots" cx={MID} cy={MID} r={150} />
        <g className="hud-orbits">
          <ellipse cx={MID} cy={MID} rx={156} ry={58} transform={`rotate(-28 ${MID} ${MID})`} />
          <ellipse cx={MID} cy={MID} rx={156} ry={58} transform={`rotate(32 ${MID} ${MID})`} />
        </g>
        <g className="hud-comet">
          <circle cx={MID} cy={MID} r={160} />
        </g>
        <circle className="hud-bezel" cx={MID} cy={MID} r={118} />
      </svg>
    </button>
  );
}
