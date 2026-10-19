"use client";

import { useEffect, useRef, useState, type PointerEvent } from "react";

/**
 * Live-Bildschirm des Laptops (voice-core/screen_stream.py): Nikolaj sieht unterwegs mit, was
 * nieeeki am PC tut. Kommt als Videospur ueber dieselbe angemeldete WebRTC-Verbindung.
 * Mit "Steuern" wird Tippen zum Klick am Laptop (der Host prueft das Ziel wie jeden Klick).
 */
type Ripple = { id: number; x: number; y: number };

export function ScreenView({
  track,
  onTap,
}: {
  track: MediaStreamTrack | null;
  onTap: (x: number, y: number) => void;
}) {
  const ref = useRef<HTMLVideoElement>(null);
  const [control, setControl] = useState(false);
  const [ripples, setRipples] = useState<Ripple[]>([]);

  useEffect(() => {
    const video = ref.current;
    if (!video || !track) return;
    video.srcObject = new MediaStream([track]);
    void video.play().catch(() => {}); // stumm, startet auch ohne Klick
    return () => {
      video.srcObject = null;
    };
  }, [track]);

  const fullscreen = () => {
    const video = ref.current as (HTMLVideoElement & { webkitEnterFullscreen?: () => void }) | null;
    if (!video) return;
    if (video.requestFullscreen) void video.requestFullscreen().catch(() => video.webkitEnterFullscreen?.());
    else video.webkitEnterFullscreen?.(); // iPhone-Safari kennt nur die Video-Variante
  };

  const tap = (e: PointerEvent<HTMLVideoElement>) => {
    if (!control || !track) return;
    const box = e.currentTarget.getBoundingClientRect();
    const x = (e.clientX - box.left) / box.width;
    const y = (e.clientY - box.top) / box.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return;
    onTap(x, y);
    const id = performance.now();
    setRipples((list) => [...list, { id, x, y }]);
    setTimeout(() => setRipples((list) => list.filter((r) => r.id !== id)), 650);
  };

  return (
    <section className="screen" aria-label="Laptop-Bildschirm live">
      <div className="screen-stage" data-control={control}>
        <video ref={ref} className="screen-video" autoPlay playsInline muted onPointerUp={tap} />
        {ripples.map((r) => (
          <span
            key={r.id}
            className="screen-ripple"
            style={{ left: `${r.x * 100}%`, top: `${r.y * 100}%` }}
            aria-hidden="true"
          />
        ))}
      </div>
      <div className="screen-bar">
        <span className="screen-live">
          <span className="screen-dot" aria-hidden="true" />
          {control ? "Tippen = Klick am Laptop" : "Live vom Laptop"}
        </span>
        <span className="screen-actions">
          <button
            type="button"
            className="screen-control"
            aria-pressed={control}
            onClick={() => setControl((c) => !c)}
          >
            Steuern
          </button>
          <button type="button" className="gate-link" onClick={fullscreen}>
            Vollbild
          </button>
        </span>
      </div>
      {!track && <p className="screen-hint">Verbinde Video …</p>}
    </section>
  );
}
