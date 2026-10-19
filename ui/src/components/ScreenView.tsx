"use client";

import { useEffect, useRef, useState, type FormEvent, type PointerEvent, type WheelEvent } from "react";

/**
 * Live-Bildschirm des Laptops (voice-core/screen_stream.py): Nikolaj sieht unterwegs mit, was
 * nieeeki am PC tut. Kommt als Videospur ueber dieselbe angemeldete WebRTC-Verbindung.
 * Mit "Steuern": Tippen = Klick, Wischen = Scrollen, darunter Text und Tasten. Ausgefuehrt wird
 * alles vom Host, mit denselben Pruefungen wie nieekis eigene Aktionen.
 */
type Ripple = { id: number; x: number; y: number };
export type RemoteKey = "enter" | "esc" | "backspace" | "tab";
export type ScreenControl = {
  tap: (x: number, y: number) => void;
  scroll: (x: number, y: number, richtung: "hoch" | "runter", schritte: number) => void;
  type: (text: string, enter: boolean) => void;
  key: (taste: RemoteKey) => void;
};

const SWIPE_PX = 24; // ab so viel senkrechter Bewegung ist es Wischen statt Tippen
const KEYS: { taste: RemoteKey; label: string; name: string }[] = [
  { taste: "enter", label: "↵", name: "Enter" },
  { taste: "backspace", label: "⌫", name: "Rücktaste" },
  { taste: "tab", label: "Tab", name: "Tab" },
  { taste: "esc", label: "Esc", name: "Escape" },
];

export function ScreenView({ track, control }: { track: MediaStreamTrack | null; control: ScreenControl }) {
  const ref = useRef<HTMLVideoElement>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  const wheelAt = useRef(0);
  const [steering, setSteering] = useState(false);
  const [ripples, setRipples] = useState<Ripple[]>([]);
  const [text, setText] = useState("");

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

  const ripple = (x: number, y: number) => {
    const id = performance.now();
    setRipples((list) => [...list, { id, x, y }]);
    setTimeout(() => setRipples((list) => list.filter((r) => r.id !== id)), 650);
  };

  const down = (e: PointerEvent<HTMLVideoElement>) => {
    if (steering) start.current = { x: e.clientX, y: e.clientY };
  };

  const up = (e: PointerEvent<HTMLVideoElement>) => {
    const from = start.current;
    start.current = null;
    if (!steering || !track || !from) return;
    const box = e.currentTarget.getBoundingClientRect();
    const dx = e.clientX - from.x;
    const dy = e.clientY - from.y;
    if (Math.abs(dy) > SWIPE_PX && Math.abs(dy) > Math.abs(dx)) {
      // Wischen nach oben = weiter nach unten scrollen, wie am Handy
      const steps = Math.min(10, Math.max(1, Math.round(Math.abs(dy) / 40)));
      control.scroll((from.x - box.left) / box.width, (from.y - box.top) / box.height, dy < 0 ? "runter" : "hoch", steps);
      return;
    }
    const x = (e.clientX - box.left) / box.width;
    const y = (e.clientY - box.top) / box.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return;
    control.tap(x, y);
    ripple(x, y);
  };

  const wheel = (e: WheelEvent<HTMLVideoElement>) => {
    if (!steering || !track) return;
    const now = performance.now();
    if (now - wheelAt.current < 150) return; // Mausrad am Desktop: hoechstens gut sechs Mal pro Sekunde
    wheelAt.current = now;
    const box = e.currentTarget.getBoundingClientRect();
    control.scroll((e.clientX - box.left) / box.width, (e.clientY - box.top) / box.height, e.deltaY > 0 ? "runter" : "hoch", 3);
  };

  const send = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!text.trim()) return;
    control.type(text, false);
    setText("");
  };

  return (
    <section className="screen" aria-label="Laptop-Bildschirm live">
      <div className="screen-stage" data-control={steering}>
        <video
          ref={ref}
          className="screen-video"
          autoPlay
          playsInline
          muted
          onPointerDown={down}
          onPointerUp={up}
          onPointerCancel={() => (start.current = null)}
          onWheel={wheel}
        />
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
          {steering ? "Tippen = Klick, Wischen = Scrollen" : "Live vom Laptop"}
        </span>
        <span className="screen-actions">
          <button
            type="button"
            className="screen-control"
            aria-pressed={steering}
            onClick={() => setSteering((s) => !s)}
          >
            Steuern
          </button>
          <button type="button" className="gate-link" onClick={fullscreen}>
            Vollbild
          </button>
        </span>
      </div>
      {steering && (
        <form className="screen-type" onSubmit={send}>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={500}
            enterKeyHint="send"
            placeholder="Text für das Fenster vorne"
            aria-label="Text, der am Laptop getippt wird"
          />
          <button type="submit" className="screen-control">
            Tippen
          </button>
          <span className="screen-keys">
            {KEYS.map((k) => (
              <button
                key={k.taste}
                type="button"
                className="screen-key"
                aria-label={k.name}
                onClick={() => control.key(k.taste)}
              >
                {k.label}
              </button>
            ))}
          </span>
        </form>
      )}
      {!track && <p className="screen-hint">Verbinde Video …</p>}
    </section>
  );
}
