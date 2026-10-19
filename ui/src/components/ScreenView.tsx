"use client";

import { useEffect, useRef, useState, type FormEvent, type PointerEvent, type WheelEvent } from "react";

/**
 * Live-Bildschirm des Laptops (voice-core/screen_stream.py): Nikolaj sieht unterwegs mit, was
 * nieeeki am PC tut. Kommt als Videospur ueber dieselbe angemeldete WebRTC-Verbindung.
 * Mit "Steuern": Tippen = Klick, langes Druecken = Rechtsklick, Wischen = Scrollen, darunter Text
 * und Tasten. Ausgefuehrt wird alles vom Host, mit denselben Pruefungen wie nieekis eigene Aktionen.
 * Die Lupe vergroessert das Bild; ohne "Steuern" setzt ein Tippen ihren Mittelpunkt.
 */
type Ripple = { id: number; x: number; y: number; rechts: boolean };
export type RemoteKey = "enter" | "esc" | "backspace" | "tab";
export type ScreenControl = {
  tap: (x: number, y: number, taste: "links" | "rechts") => void;
  scroll: (x: number, y: number, richtung: "hoch" | "runter", schritte: number) => void;
  type: (text: string, enter: boolean) => void;
  key: (taste: RemoteKey) => void;
};

const SWIPE_PX = 24; // ab so viel senkrechter Bewegung ist es Wischen statt Tippen
const MOVE_PX = 10; // mehr Bewegung bricht das lange Druecken ab
const LONG_PRESS_MS = 550;
const ZOOMS = [1, 2, 3];
const KEYS: { taste: RemoteKey; label: string; name: string }[] = [
  { taste: "enter", label: "↵", name: "Enter" },
  { taste: "backspace", label: "⌫", name: "Rücktaste" },
  { taste: "tab", label: "Tab", name: "Tab" },
  { taste: "esc", label: "Esc", name: "Escape" },
];

type Press = { x: number; y: number; moved: boolean; long: boolean; timer: ReturnType<typeof setTimeout> };

/** Stelle im (ggf. vergroesserten) Video relativ zum ganzen Bild, 0..1 */
function relative(video: HTMLVideoElement, clientX: number, clientY: number) {
  const box = video.getBoundingClientRect(); // enthaelt die Lupen-Skalierung
  return { x: (clientX - box.left) / box.width, y: (clientY - box.top) / box.height };
}

export function ScreenView({ track, control }: { track: MediaStreamTrack | null; control: ScreenControl }) {
  const ref = useRef<HTMLVideoElement>(null);
  const press = useRef<Press | null>(null);
  const wheelAt = useRef(0);
  const [steering, setSteering] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [focus, setFocus] = useState({ x: 0.5, y: 0.5 });
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

  // Echo an der Fingerstelle, in Prozent der sichtbaren Buehne (nicht des vergroesserten Videos)
  const ripple = (stage: Element | null, clientX: number, clientY: number, rechts: boolean) => {
    if (!stage) return;
    const box = stage.getBoundingClientRect();
    const id = performance.now();
    const r = { id, x: (clientX - box.left) / box.width, y: (clientY - box.top) / box.height, rechts };
    setRipples((list) => [...list, r]);
    setTimeout(() => setRipples((list) => list.filter((x) => x.id !== id)), 650);
  };

  const down = (e: PointerEvent<HTMLVideoElement>) => {
    if (!steering || !track) return;
    const video = e.currentTarget;
    const stage = video.parentElement;
    const { clientX, clientY } = e;
    const timer = setTimeout(() => {
      const p = press.current;
      if (!p || p.moved) return;
      p.long = true; // langes Druecken = Rechtsklick
      const at = relative(video, clientX, clientY);
      control.tap(at.x, at.y, "rechts");
      ripple(stage, clientX, clientY, true);
    }, LONG_PRESS_MS);
    press.current = { x: clientX, y: clientY, moved: false, long: false, timer };
  };

  const move = (e: PointerEvent<HTMLVideoElement>) => {
    const p = press.current;
    if (p && Math.hypot(e.clientX - p.x, e.clientY - p.y) > MOVE_PX) p.moved = true;
  };

  const up = (e: PointerEvent<HTMLVideoElement>) => {
    const video = e.currentTarget;
    if (!steering) {
      // Ansehen: Tippen setzt den Mittelpunkt der Lupe
      if (zoom > 1) setFocus(relative(video, e.clientX, e.clientY));
      return;
    }
    const p = press.current;
    press.current = null;
    if (!p || !track) return;
    clearTimeout(p.timer);
    if (p.long) return; // Rechtsklick ist schon passiert
    const dx = e.clientX - p.x;
    const dy = e.clientY - p.y;
    if (Math.abs(dy) > SWIPE_PX && Math.abs(dy) > Math.abs(dx)) {
      // Wischen nach oben = weiter nach unten scrollen, wie am Handy
      const from = relative(video, p.x, p.y);
      const steps = Math.min(10, Math.max(1, Math.round(Math.abs(dy) / 40)));
      control.scroll(from.x, from.y, dy < 0 ? "runter" : "hoch", steps);
      return;
    }
    const at = relative(video, e.clientX, e.clientY);
    if (at.x < 0 || at.x > 1 || at.y < 0 || at.y > 1) return;
    control.tap(at.x, at.y, "links");
    ripple(video.parentElement, e.clientX, e.clientY, false);
  };

  const cancel = () => {
    if (press.current) clearTimeout(press.current.timer);
    press.current = null;
  };

  const wheel = (e: WheelEvent<HTMLVideoElement>) => {
    if (!steering || !track) return;
    const now = performance.now();
    if (now - wheelAt.current < 150) return; // Mausrad am Desktop: hoechstens gut sechs Mal pro Sekunde
    wheelAt.current = now;
    const at = relative(e.currentTarget, e.clientX, e.clientY);
    control.scroll(at.x, at.y, e.deltaY > 0 ? "runter" : "hoch", 3);
  };

  const send = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!text.trim()) return;
    control.type(text, false);
    setText("");
  };

  const hint = steering
    ? "Tippen = Klick, lang drücken = Rechtsklick, wischen = scrollen"
    : zoom > 1
      ? "Tippen setzt die Lupe"
      : "Live vom Laptop";

  return (
    <section className="screen" aria-label="Laptop-Bildschirm live">
      <div className="screen-stage" data-control={steering}>
        <video
          ref={ref}
          className="screen-video"
          autoPlay
          playsInline
          muted
          style={{ transform: `scale(${zoom})`, transformOrigin: `${focus.x * 100}% ${focus.y * 100}%` }}
          onPointerDown={down}
          onPointerMove={move}
          onPointerUp={up}
          onPointerCancel={cancel}
          onContextMenu={(e) => e.preventDefault()}
          onWheel={wheel}
        />
        {ripples.map((r) => (
          <span
            key={r.id}
            className="screen-ripple"
            data-rechts={r.rechts}
            style={{ left: `${r.x * 100}%`, top: `${r.y * 100}%` }}
            aria-hidden="true"
          />
        ))}
      </div>
      <div className="screen-bar">
        <span className="screen-live">
          <span className="screen-dot" aria-hidden="true" />
          {hint}
        </span>
        <span className="screen-actions">
          <button
            type="button"
            className="screen-control"
            aria-label={`Lupe, jetzt ${zoom}-fach`}
            onClick={() => setZoom((z) => ZOOMS[(ZOOMS.indexOf(z) + 1) % ZOOMS.length])}
          >
            Lupe {zoom}×
          </button>
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
