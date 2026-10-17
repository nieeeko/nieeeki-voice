"use client";

import { useEffect, useRef } from "react";
import type { LevelSample, Presence } from "@/lib/useNieeeki";

/**
 * Die Stimmlinie: ein laufendes Band ueber die letzten Sekunden.
 * Oberhalb der Mittellinie Nikolajs Stimme, unterhalb die von nieeeki.
 * Eine Unterbrechung erscheint als senkrechter Schnitt.
 */
export function VoiceLine({ history, presence }: { history: LevelSample[]; presence: Presence }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const { width, height } = canvas.getBoundingClientRect();
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);

    const css = getComputedStyle(canvas);
    const userColor = css.getPropertyValue("--voice-user").trim();
    const botColor = css.getPropertyValue("--voice-bot").trim();
    const rule = css.getPropertyValue("--rule").trim();
    const cutColor = css.getPropertyValue("--cut").trim();

    const mid = height / 2;
    const step = width / 240;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = rule;
    ctx.fillRect(0, mid - 0.5, width, 1);

    const offset = 240 - history.length;
    history.forEach((s, i) => {
      const x = (offset + i) * step;
      const u = Math.min(1, s.user * 2.2) * (mid - 6);
      const b = Math.min(1, s.bot * 2.2) * (mid - 6);
      if (u > 0.5) {
        ctx.fillStyle = userColor;
        ctx.fillRect(x, mid - u - 1, Math.max(1, step - 1), u);
      }
      if (b > 0.5) {
        ctx.fillStyle = botColor;
        ctx.fillRect(x, mid + 1, Math.max(1, step - 1), b);
      }
      if (s.cut) {
        ctx.fillStyle = cutColor;
        ctx.fillRect(x, 4, 2, height - 8);
      }
    });
  }, [history]);

  return (
    <canvas
      ref={ref}
      className="voice-line"
      data-presence={presence}
      role="img"
      aria-label="Stimmlinie: oben deine Stimme, unten nieeeki"
    />
  );
}
