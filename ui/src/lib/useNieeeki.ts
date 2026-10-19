"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import type { CanvasItem } from "@/components/Canvas";
import { accessToken } from "@/lib/supabase";

export type Presence = "offline" | "connecting" | "idle" | "listening" | "thinking" | "speaking";

declare global {
  interface Window {
    /** Nur in der Desktop-App (desktop/preload.js) vorhanden. */
    nieeekiDesktop?: {
      isDesktop: boolean;
      levels: (sample: LevelSample) => void;
      /** Lichtrahmen im Schleier: wo nieeeki gerade handelt (Bildschirmpixel vom Host) */
      highlight?: (h: { rahmen?: number[]; punkt?: number[]; ok: boolean }) => void;
      /** Weckwort-Treffer vom lokalen Lauscher; gibt eine Abmelde-Funktion zurueck */
      onWake?: (callback: (hit: { score: number }) => void) => () => void;
      /** Laeuft ein Gespraech? Dann pausiert der Lauscher und gibt das Mikrofon frei. */
      conversation?: (active: boolean) => void;
    };
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
  status: "laeuft" | "fertig" | "abgelehnt" | "fehler" | "wartet";
  rahmen?: number[]; // Fensterrechteck [links, oben, rechts, unten] in Bildschirmpixeln
  punkt?: number[]; // Klickpunkt [x, y]
};

/** Ein Abtastwert der Stimmlinie: oben Nikolaj, unten nieeeki. */
export type LevelSample = { user: number; bot: number; cut: boolean; speaker: number };

/** Verlauf der Stimmlinie; seq zaehlt jeden neuen Abtastwert (VoiceLine zeichnet nur dann neu). */
export type LevelHistory = { samples: LevelSample[]; seq: number };

const VOICE_CORE_URL = process.env.NEXT_PUBLIC_VOICE_CORE_URL ?? "http://localhost:7860";
// Nur diese Adresse (und localhost) laesst der Voice-Core herein (PIPECAT_ALLOWED_ORIGINS in bot.py)
const HOME_URL = "https://nieeeki.nieeeko.com";
const HISTORY = 240; // ~12 s bei 20 Hz
// Per Weckwort begonnene Gespraeche enden nach so langer Stille von selbst; dann lauscht der PC wieder.
const WAKE_IDLE_MS = 45_000;

export function useNieeeki() {
  const clientRef = useRef<PipecatClient | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const levels = useRef<LevelSample>({ user: 0, bot: 0, cut: false, speaker: 0 });
  const presenceRef = useRef<Presence>("offline");
  const history = useRef<LevelHistory>({ samples: [], seq: 0 });
  // nieekis Pegel messen wir selbst: SmallWebRTC meldet onRemoteAudioLevel nie (kein Player).
  const botMeter = useRef<{ ctx: AudioContext; analyser: AnalyserNode; buf: Float32Array<ArrayBuffer> } | null>(null);

  const [presence, setPresenceState] = useState<Presence>("offline");
  const [userLine, setUserLine] = useState("");
  const [botLine, setBotLine] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [canvasItems, setCanvasItems] = useState<CanvasItem[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [activities, setActivities] = useState<Activity[]>([]);
  // Live-Bildschirm des Laptops (voice-core/screen_stream.py), nur auf Wunsch
  const [screenOn, setScreenOn] = useState(false);
  const [screenTrack, setScreenTrack] = useState<MediaStreamTrack | null>(null);

  // Weicher Stopp bei Unterbrechungen (Server schickt "leiser_stopp")
  const softStop = useRef<SoftStop>({ armedAt: null, fading: false });
  const restoreVolume = () => restoreAudio(audioRef.current, softStop.current);

  // Weckwort-Gespraech: seit wann ruhig, und ob gerade etwas laeuft, das nicht abbrechen darf
  const wakeSession = useRef(false);
  const lastChange = useRef(0);
  const busy = useRef(false);

  const setPresence = useCallback((p: Presence) => {
    presenceRef.current = p;
    lastChange.current = performance.now();
    setPresenceState(p);
  }, []);

  // 20 Hz Abtastung der Pegel fuer die Stimmlinie. Bewusst ohne React-State:
  // sonst rendert die ganze Seite (samt Canvas) 20-mal pro Sekunde neu.
  useEffect(() => {
    const id = setInterval(() => {
      const meter = botMeter.current;
      if (meter) {
        meter.analyser.getFloatTimeDomainData(meter.buf);
        let sum = 0;
        for (const v of meter.buf) sum += v * v;
        // RMS ungefaehr auf die Skala des Mikrofonpegels bringen (Sprache ~0,1-0,3)
        levels.current.bot = Math.min(1, Math.sqrt(sum / meter.buf.length) * 2);
      }
      // Weicher Stopp: in der naechsten Wortpause (spaetestens nach 600 ms) ausblenden. Hing frueher
      // an onRemoteAudioLevel, das bei SmallWebRTC nie feuert, und war deshalb nie aktiv.
      const stop = softStop.current;
      if (stop.armedAt && !stop.fading && (levels.current.bot < 0.015 || performance.now() - stop.armedAt > 600)) {
        fadeOutAudio(audioRef.current, stop);
      }
      const s = { ...levels.current };
      levels.current.cut = false;
      window.nieeekiDesktop?.levels(s); // Stimmen-Rand der Desktop-App (sonst nicht vorhanden)
      const h = history.current;
      h.samples.push(s);
      if (h.samples.length > HISTORY) h.samples.shift();
      h.seq++;
    }, 50);
    return () => clearInterval(id);
  }, []);

  const start = useCallback(async (anlass: "klick" | "weckwort") => {
    setError(null);
    wakeSession.current = anlass === "weckwort";
    setPresence("connecting");
    const client = new PipecatClient({
      transport: new SmallWebRTCTransport(),
      enableMic: true,
      enableCam: false,
      callbacks: {
        onBotReady: () => setPresence("idle"),
        onDisconnected: () => {
          setPresence("offline");
          setScreenOn(false);
          setScreenTrack(null);
        },
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
        onTrackStarted: (track, participant) => {
          if (participant?.local) return;
          if (track.kind === "video") {
            setScreenTrack(track); // Videospur von nieeeki = Bildschirm-Stream des Laptops
            return;
          }
          if (track.kind !== "audio") return;
          if (!audioRef.current) audioRef.current = new Audio();
          const stream = new MediaStream([track]);
          audioRef.current.srcObject = stream;
          void audioRef.current.play();
          // Pegel fuer Kugel, Stimmlinie und den Schleier der Desktop-App
          void botMeter.current?.ctx.close();
          const ctx = new AudioContext();
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 512;
          ctx.createMediaStreamSource(stream).connect(analyser);
          void ctx.resume();
          botMeter.current = { ctx, analyser, buf: new Float32Array(analyser.fftSize) };
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
          if (msg?.type === "bildschirm") setScreenOn(Boolean((msg as { aktiv?: unknown }).aktiv));
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
              // Desktop-App: Lichtrahmen ums Zielfenster und Welle am Klickpunkt
              if ((a.rahmen || a.punkt) && a.status !== "laeuft") {
                window.nieeekiDesktop?.highlight?.({ rahmen: a.rahmen, punkt: a.punkt, ok: a.status === "fertig" });
              }
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
      // Der Voice-Core laesst nur Nikolaj mit zweitem Faktor herein (voice-core/auth_gate.py).
      // Der Transport schickt diese Header auch beim WebRTC-Offer und den ICE-Updates mit.
      const token = await accessToken();
      await client.startBotAndConnect({
        endpoint: `${VOICE_CORE_URL}/start`,
        headers: new Headers(token ? { Authorization: `Bearer ${token}` } : {}),
        // Immer gueltiges JSON (leerer Body gab einen ERROR im Log); per Weckwort begruesst sie nur kurz
        requestData: anlass === "weckwort" ? { anlass } : {},
      });
    } catch (e) {
      setPresence("offline");
      // Vercel-Vorschauen weist der Voice-Core per CORS ab (Schutz vor fremden Seiten); der Browser
      // meldet das nur als Netzwerkfehler, darum hier ausdruecklich sagen, wo es geht.
      const preview = window.location.hostname.endsWith(".vercel.app");
      setError(
        (e as { status?: number }).status === 401
          ? "Der Voice-Core hat die Anmeldung abgelehnt. Bitte ab- und wieder anmelden."
          : preview
            ? `Vorschau-Adressen dürfen den Voice-Core nicht nutzen. Bitte ${HOME_URL} öffnen.`
            : `Voice-Core unter ${VOICE_CORE_URL} nicht erreichbar. Läuft bot.py?`,
      );
    }
  }, [setPresence]);

  const connect = useCallback(() => start("klick"), [start]);

  const disconnect = useCallback(async () => {
    await clientRef.current?.disconnect();
    clientRef.current = null;
    void botMeter.current?.ctx.close();
    botMeter.current = null;
    levels.current.bot = 0;
    setScreenOn(false);
    setScreenTrack(null);
    setPresence("offline");
  }, [setPresence]);

  // Desktop-App: "nieeeki" gesagt -> Gespraech starten (nur wenn keins laeuft)
  useEffect(() => {
    return window.nieeekiDesktop?.onWake?.(() => {
      if (presenceRef.current === "offline") void start("weckwort");
    });
  }, [start]);

  // Waehrend eines Gespraechs pausiert der Weckwort-Lauscher (das Mikrofon gehoert dem Gespraech)
  useEffect(() => {
    window.nieeekiDesktop?.conversation?.(presence !== "offline");
  }, [presence]);

  // Weckwort-Gespraech nach WAKE_IDLE_MS Stille beenden, aber nie mitten in einer Aktion, bei offener
  // Freigabe, laufendem Entwicklungsauftrag oder Live-Bild
  useEffect(() => {
    const id = setInterval(() => {
      if (!wakeSession.current || busy.current || presenceRef.current !== "idle") return;
      if (performance.now() - lastChange.current > WAKE_IDLE_MS) void disconnect();
    }, 1000);
    return () => clearInterval(id);
  }, [disconnect]);

  // Bildschirm-Stream an/aus; den Zustand bestaetigt der Voice-Core per Nachricht "bildschirm".
  const toggleScreen = useCallback(() => {
    clientRef.current?.sendClientMessage("bildschirm", { an: !screenOn });
  }, [screenOn]);

  // Tippen im Live-Bild (0..1 relativ zum Bild) = Klick am Laptop; der Host prueft das Ziel.
  const clickScreen = useCallback((x: number, y: number, taste: "links" | "rechts" = "links") => {
    clientRef.current?.sendClientMessage("bildschirm_klick", { x, y, taste });
  }, []);
  const scrollScreen = useCallback((x: number, y: number, richtung: "hoch" | "runter", schritte: number) => {
    clientRef.current?.sendClientMessage("bildschirm_scrollen", { x, y, richtung, schritte });
  }, []);
  const typeOnScreen = useCallback((text: string, enter: boolean) => {
    clientRef.current?.sendClientMessage("bildschirm_tippen", { text, enter });
  }, []);
  const pressOnScreen = useCallback((taste: string) => {
    clientRef.current?.sendClientMessage("bildschirm_taste", { taste });
  }, []);

  // Freigabe geht nur ueber die eigene WebRTC-Verbindung zum Voice-Core, nie per Sprache.
  const decide = useCallback((a: Approval, ja: boolean) => {
    clientRef.current?.sendClientMessage("freigabe", { job: a.job, id: a.id, entscheidung: ja ? "ja" : "nein" });
    setApprovals((list) => list.filter((x) => !(x.job === a.job && x.id === a.id)));
  }, []);

  const acting = activities.some((a) => a.status === "laeuft");
  const devRunning = canvasItems.some((it) => it.kind === "dokument" && it.laeuft === true);
  const hasApprovals = approvals.length > 0;

  useEffect(() => {
    busy.current = acting || devRunning || hasApprovals || screenOn;
    lastChange.current = performance.now(); // Stille zaehlt erst ab hier
  }, [acting, devRunning, hasApprovals, screenOn]);

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
    screenOn,
    screenTrack,
    toggleScreen,
    clickScreen,
    scrollScreen,
    typeOnScreen,
    pressOnScreen,
    decide,
    connect,
    disconnect,
  };
}
