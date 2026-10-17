"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

export type Presence = "offline" | "connecting" | "idle" | "listening" | "thinking" | "speaking";

/** Ein Abtastwert der Stimmlinie: oben Nikolaj, unten nieeeki. */
export type LevelSample = { user: number; bot: number; cut: boolean };

const VOICE_CORE_URL = process.env.NEXT_PUBLIC_VOICE_CORE_URL ?? "http://localhost:7860";
const HISTORY = 240; // ~12 s bei 20 Hz

export function useNieeeki() {
  const clientRef = useRef<PipecatClient | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const levels = useRef({ user: 0, bot: 0, cut: false });
  const presenceRef = useRef<Presence>("offline");

  const [presence, setPresenceState] = useState<Presence>("offline");
  const [history, setHistory] = useState<LevelSample[]>([]);
  const [userLine, setUserLine] = useState("");
  const [botLine, setBotLine] = useState("");
  const [error, setError] = useState<string | null>(null);

  const setPresence = useCallback((p: Presence) => {
    presenceRef.current = p;
    setPresenceState(p);
  }, []);

  // 20 Hz Abtastung der Pegel fuer die Stimmlinie
  useEffect(() => {
    const id = setInterval(() => {
      const s = { ...levels.current };
      levels.current.cut = false;
      setHistory((h) => [...h.slice(-(HISTORY - 1)), s]);
    }, 50);
    return () => clearInterval(id);
  }, []);

  const connect = useCallback(async () => {
    setError(null);
    setPresence("connecting");
    const client = new PipecatClient({
      transport: new SmallWebRTCTransport(),
      enableMic: true,
      enableCam: false,
      callbacks: {
        onBotReady: () => setPresence("idle"),
        onDisconnected: () => setPresence("offline"),
        onUserStartedSpeaking: () => {
          // Spricht nieeeki gerade, ist das eine Unterbrechung
          if (presenceRef.current === "speaking") levels.current.cut = true;
          setPresence("listening");
        },
        onUserStoppedSpeaking: () => setPresence("thinking"),
        onBotStartedSpeaking: () => {
          setBotLine("");
          setPresence("speaking");
        },
        onBotStoppedSpeaking: () => setPresence("idle"),
        onUserTranscript: (d) => setUserLine(d.text),
        onBotTranscript: (d) => setBotLine((l) => (l ? `${l} ${d.text}` : d.text)),
        onLocalAudioLevel: (level) => {
          levels.current.user = level;
        },
        onRemoteAudioLevel: (level) => {
          levels.current.bot = level;
        },
        onTrackStarted: (track, participant) => {
          if (participant?.local || track.kind !== "audio") return;
          if (!audioRef.current) audioRef.current = new Audio();
          audioRef.current.srcObject = new MediaStream([track]);
          void audioRef.current.play();
        },
        onError: (msg) => {
          // data ist meist ein Objekt ({ message } / { error }); String() ergab "[object Object]".
          const data = (msg as { data?: unknown }).data as
            | string
            | { message?: string; error?: string }
            | undefined;
          const text =
            typeof data === "string" ? data : (data?.message ?? data?.error ?? JSON.stringify(data));
          setError(text || "Verbindungsfehler");
        },
      },
    });
    clientRef.current = client;
    try {
      await client.startBotAndConnect({ endpoint: `${VOICE_CORE_URL}/start` });
    } catch {
      setPresence("offline");
      setError(`Voice-Core unter ${VOICE_CORE_URL} nicht erreichbar. Läuft bot.py?`);
    }
  }, [setPresence]);

  const disconnect = useCallback(async () => {
    await clientRef.current?.disconnect();
    clientRef.current = null;
    setPresence("offline");
  }, [setPresence]);

  return { presence, history, userLine, botLine, error, connect, disconnect };
}
