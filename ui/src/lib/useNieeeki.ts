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

type SoftStop = { armedAt: number | null; fading: boolean };

/** nieekis Stimme in 140 ms ausblenden (weicher Stopp bei Unterbrechung). */
function fadeOutAudio(audio: HTMLAudioElement | null, state: SoftStop) {
  state.fading = true;
  if (!audio) return;
  const start = performance.now();
  const step = () => {
    const t = Math.min(1, (performance.now() - start) / 140);
    audio.volume = 1 - t;
    if (t < 1 && state.fading) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  // Sicherheitsnetz: nie dauerhaft stumm bleiben
  setTimeout(() => {
    if (state.fading) restoreAudio(audio, state);
  }, 3000);
}

function restoreAudio(audio: HTMLAudioElement | null, state: SoftStop) {
  state.armedAt = null;
  state.fading = false;
  if (audio) audio.volume = 1;
}

/** Aktion eines Entwicklungsauftrags, die Nikolaj per Klick freigeben muss. */
export type Approval = { job: string; projekt: string; id: string; grund: string; detail: string };

/** Ein Werkzeugaufruf von nieeeki fuer die Aktivitaets-Zeitleiste (activity.py im Voice-Core). */
export type Activity = {
  id: string;
  name: string;
  detail: string;
  status: "laeuft" | "fertig" | "abgelehnt" | "fehler";
};

/** Ein Abtastwert der Stimmlinie: oben Nikolaj, unten nieeeki. */
export type LevelSample = { user: number; bot: number; cut: boolean; speaker: number };

const VOICE_CORE_URL = process.env.NEXT_PUBLIC_VOICE_CORE_URL ?? "http://localhost:7860";
const HISTORY = 240; // ~12 s bei 20 Hz

export function useNieeeki() {
  const clientRef = useRef<PipecatClient | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const levels = useRef<LevelSample>({ user: 0, bot: 0, cut: false, speaker: 0 });
  const presenceRef = useRef<Presence>("offline");

  const [presence, setPresenceState] = useState<Presence>("offline");
  const [history, setHistory] = useState<LevelSample[]>([]);
  const [userLine, setUserLine] = useState("");
  const [botLine, setBotLine] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [canvasItems, setCanvasItems] = useState<CanvasItem[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [activities, setActivities] = useState<Activity[]>([]);

  // Weicher Stopp bei Unterbrechungen (Server schickt "leiser_stopp")
  const softStop = useRef<SoftStop>({ armedAt: null, fading: false });
  const fadeOut = () => fadeOutAudio(audioRef.current, softStop.current);
  const restoreVolume = () => restoreAudio(audioRef.current, softStop.current);

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
          restoreVolume(); // neue Antwort nach einem weichen Stopp wieder hoerbar
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
          // Weicher Stopp: in der naechsten Wortpause (oder spaetestens nach 600 ms) ausblenden
          const armed = softStop.current.armedAt;
          if (armed && !softStop.current.fading && (level < 0.015 || performance.now() - armed > 600)) {
            fadeOut();
          }
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
          if (msg?.type === "leiser_stopp") softStop.current.armedAt = performance.now();
          // Wer spricht (0 = Nikolaj, 1.. = weitere Stimmen) -> Farbe im Stimmen-Rand
          if (msg?.type === "sprecher" && typeof (msg as { index?: unknown }).index === "number")
            levels.current.speaker = (msg as { index: number }).index;
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
          if (msg?.type === "aktivitaet") {
            const a = msg as unknown as Activity;
            if (a.id && a.name) {
              // gleiche id = Statuswechsel; neueste oben, hoechstens 6 Eintraege
              setActivities((list) => [a, ...list.filter((x) => x.id !== a.id)].slice(0, 6));
            }
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

  const acting = activities.some((a) => a.status === "laeuft");

  return {
    presence,
    history,
    levels,
    userLine,
    botLine,
    error,
    canvasItems,
    approvals,
    activities,
    acting,
    decide,
    connect,
    disconnect,
  };
}
