"use client";

import { useRef, useState, type FormEvent } from "react";
import type { Presence } from "@/lib/useNieeeki";

/**
 * Eingabe unter dem Kern: tippen statt sprechen (nieeeki antwortet trotzdem mit Stimme), dazu
 * Schnellbefehle. Das Mikrofon links startet oder beendet das Gespraech.
 */

const QUICK = [
  { label: "Wetter morgen", text: "Wie wird das Wetter morgen?" },
  { label: "Was ist offen?", text: "Welche Programme und Fenster sind gerade offen?" },
  { label: "Letzter Stand", text: "Woran haben wir zuletzt gearbeitet?" },
];

export function Composer({
  presence,
  screenOn,
  onSend,
  onToggleConversation,
  onToggleScreen,
}: {
  presence: Presence;
  screenOn: boolean;
  onSend: (text: string) => void;
  onToggleConversation: () => void;
  onToggleScreen: () => void;
}) {
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const online = presence !== "offline" && presence !== "connecting";

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    onSend(text);
    setText("");
  };

  return (
    <div className="composer">
      <form className="composer-bar" onSubmit={submit}>
        <button
          type="button"
          className="composer-mic"
          data-on={online}
          onClick={onToggleConversation}
          disabled={presence === "connecting"}
          aria-label={online ? "Gespräch beenden" : "Gespräch starten"}
          title={online ? "Gespräch beenden" : "Gespräch starten"}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="9" y="3" width="6" height="11" rx="3" />
            <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
          </svg>
        </button>
        <input
          ref={inputRef}
          id="nieeeki-eingabe"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Schreib nieeeki … z. B. „Zeig mir die Projektliste als Tabelle“"
          aria-label="Nachricht an nieeeki"
          autoComplete="off"
          enterKeyHint="send"
        />
        <button type="submit" className="composer-send" aria-label="Senden" disabled={!text.trim()}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5 12h13M13 6l6 6-6 6" />
          </svg>
        </button>
      </form>
      <p className="composer-hint">Enter sendet · nieeeki antwortet mit Stimme</p>
      <div className="chips" role="group" aria-label="Schnellbefehle">
        {QUICK.map((q) => (
          <button key={q.label} type="button" className="chip" onClick={() => onSend(q.text)}>
            {q.label}
          </button>
        ))}
        <button
          type="button"
          className="chip"
          onClick={() => {
            setText("Merk dir: ");
            inputRef.current?.focus();
          }}
        >
          Merk dir …
        </button>
        {online && (
          <button type="button" className="chip" aria-pressed={screenOn} onClick={onToggleScreen}>
            {screenOn ? "Bildschirm aus" : "Bildschirm zeigen"}
          </button>
        )}
      </div>
    </div>
  );
}
