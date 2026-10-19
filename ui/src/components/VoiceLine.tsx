"use client";

import { useEffect, useRef, type RefObject } from "react";
import type { LevelHistory, Presence } from "@/lib/useNieeeki";

const SLOTS = 240; // ~12 s bei 20 Hz, wie HISTORY in useNieeeki

/**
 * Die Stimmlinie: ein laufendes Band ueber die letzten Sekunden.
 * Oberhalb der Mittellinie Nikolajs Stimme, unterhalb die von nieeeki.
 * Eine Unterbrechung erscheint als senkrechter Schnitt.
 * Gezeichnet wird im eigenen Animationsframe aus einem Ref, nur bei neuen Abtastwerten.
 */
export function VoiceLine({
  history,
  presence,
}: {
  history: RefObject<LevelHistory>;
  presence: Presence;
}) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    let width = 0;
    let height = 0;
    let drawn = -1;
    let raf = 0;
    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      ({ width, height } = canvas.getBoundingClientRect());
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawn = -1;
    };
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();
    const css = getComputedStyle(canvas); // live: folgt auch dem Wechsel hell/dunkel

    const draw = () => {
      raf = requestAnimationFrame(draw);
      const h = history.current;
      if (!h || h.seq === drawn) return;
      drawn = h.seq;

      const userColor = css.getPropertyValue("--voice-user").trim();
      const botColor = css.getPropertyValue("--voice-bot").trim();
      const cutColor = css.getPropertyValue("--cut").trim();
      const mid = height / 2;
      const step = width / SLOTS;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = css.getPropertyValue("--rule").trim();
      ctx.fillRect(0, mid - 0.5, width, 1);

      const offset = SLOTS - h.samples.length;
      h.samples.forEach((s, i) => {
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
    };
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
    };
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
