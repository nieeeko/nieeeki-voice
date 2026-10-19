"use client";

import { useEffect, useRef } from "react";

/**
 * Live-Bildschirm des Laptops (voice-core/screen_stream.py): Nikolaj sieht unterwegs mit, was
 * nieeeki am PC tut. Kommt als Videospur ueber dieselbe angemeldete WebRTC-Verbindung.
 */
export function ScreenView({ track }: { track: MediaStreamTrack | null }) {
  const ref = useRef<HTMLVideoElement>(null);

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

  return (
    <section className="screen" aria-label="Laptop-Bildschirm live">
      <video ref={ref} className="screen-video" autoPlay playsInline muted onDoubleClick={fullscreen} />
      <div className="screen-bar">
        <span className="screen-live">
          <span className="screen-dot" aria-hidden="true" />
          Live vom Laptop
        </span>
        <button type="button" className="gate-link" onClick={fullscreen}>
          Vollbild
        </button>
      </div>
      {!track && <p className="screen-hint">Verbinde Video …</p>}
    </section>
  );
}
