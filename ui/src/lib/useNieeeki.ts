"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import type { CanvasItem } from "@/components/Canvas";

export type Presence = "offline" | "connecting" | "idle" | "listening" | "thinking" | "speaking";

declare global {
  interface Window {
    /** Nur in der Desktop-App (desktop/preload.js) vorhanden. */
    nieeekiDesktop?: { isDesktop: boolean; levels: (sample: LevelSample) => void };
  }
}

/** Aktion eines Entwicklungsauftrags, die Nikolaj per Klick freigeben muss. */
export type Approval = { job: string; projekt: string; id: string; grund: string; detail: string };

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
  const [canvasItems, setCanvasItems] = useState<CanvasItem[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);

  const setPresence = useCallback((p: Presence) => {
    presenceRef.current = p;
    setPresenceState(p);
  }, []);

  // 20 Hz Abtastung der Pegel fuer die Stimmlinie
  useEffect(() => {
    const id = setInterval(() => {
      const s = { ...levels.current };
      levels.current.cut = false;
      window.nieeekiDesktop?.levels(s); // Stimmen-Rand der Desktop-App (sonst nicht vorhanden)
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
        onServerMessage: (data) => {
          // canvas_zeigen im Voice-Core -> neues Element aufs Canvas
          const msg = data as {
            type?: string;
            item?: CanvasItem;
            job?: string;
            projekt?: string;
            items?: { id: string; grund: string; detail: string }[];
          };
          if (msg?.type === "canvas" && msg.item) {
            const item = msg.item;
            // Gleiche id = Aktualisierung (z. B. Fortschritt eines Entwicklungsauftrags)
            setCanvasItems((items) => {
              const i = item.id ? items.findIndex((it) => it.id === item.id) : -1;
              if (i < 0) return [...items, item];
              const next = [...items];
              next[i] = item;
              return next;
            });
          }
          if (msg?.type === "freigaben" && msg.job && msg.items) {
            const { job, projekt = "" } = msg;
            const fresh = msg.items.map((it) => ({ ...it, job, projekt }));
            setApprovals((list) => [
              ...list.filter((a) => !fresh.some((f) => f.job === a.job && f.id === a.id)),
              ...fresh,
            ]);
          }
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

  // Freigabe geht nur ueber die eigene WebRTC-Verbindung zum Voice-Core, nie per Sprache.
  const decide = useCallback((a: Approval, ja: boolean) => {
    clientRef.current?.sendClientMessage("freigabe", { job: a.job, id: a.id, entscheidung: ja ? "ja" : "nein" });
    setApprovals((list) => list.filter((x) => !(x.job === a.job && x.id === a.id)));
  }, []);

  return { presence, history, userLine, botLine, error, canvasItems, approvals, decide, connect, disconnect };
}
