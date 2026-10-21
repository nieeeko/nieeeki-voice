"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import type { CanvasItem } from "@/components/Canvas";
import { EMPTY_MISSION, nextMission, type Mission } from "@/lib/mission";
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

/**
 * Chromes Erlaubnis "Lokales Netzwerk" (Local Network Access): Ohne sie darf eine oeffentliche Seite
 * (Vercel) keine private Adresse wie die Tailscale-Adresse des Laptops (100.x) aufrufen. Am Laptop
 * selbst (localhost) und in Browsern ohne diese Sperre: null.
 */
async function localNetworkPermission(): Promise<PermissionState | null> {
  if (/^https?:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/.test(VOICE_CORE_URL)) return null;
  try {
    const status = await navigator.permissions.query({ name: "local-network-access" as PermissionName });
    return status.state;
  } catch {
    return null; // Browser kennt die Erlaubnis nicht
  }
}

function restoreAudio(audio: HTMLAudioElement | null, state: SoftStop) {
  state.armedAt = null;
  state.fading = false;
  if (audio) audio.volume = 1;
}

/** Aktion, die Nikolaj per Klick freigeben muss (Entwicklungsauftrag, PC, PC als Admin). */
export type Approval = {
  job: string;
  projekt: string;
  id: string;
  grund: string;
  detail: string;
  admin?: boolean;
  seit?: number; // wann die Anfrage ankam (im Browser gesetzt)
};

/** Ein Werkzeugaufruf von nieeeki fuer die Aktivitaets-Zeitleiste (activity.py im Voice-Core). */
export type Activity = {
  id: string;
  name: string;
  detail: string;
  status: "laeuft" | "fertig" | "abgelehnt" | "fehler" | "wartet";
  rahmen?: number[]; // Fensterrechteck [links, oben, rechts, unten] in Bildschirmpixeln
  punkt?: number[]; // Klickpunkt [x, y]
  seit?: number; // erste Meldung (Date.now(), im Browser gesetzt)
  bis?: number; // letzter Statuswechsel
};

/** Ein Abtastwert der Stimmlinie: oben Nikolaj, unten nieeeki. */
export type LevelSample = { user: number; bot: number; cut: boolean; speaker: number };

/** Verlauf der Stimmlinie; seq zaehlt jeden neuen Abtastwert (VoiceLine zeichnet nur dann neu). */
export type LevelHistory = { samples: LevelSample[]; seq: number };

/** Alles, was die Kommandozentrale (components/CommandCenter.tsx) anzeigt und bedient. */
export type NieeekiState = ReturnType<typeof useNieeeki>;

const VOICE_CORE_URL = process.env.NEXT_PUBLIC_VOICE_CORE_URL ?? "http://localhost:7860";
// Der Voice-Core laesst diese Adresse, localhost und die Vercel-Vorschauen herein (bot.py, auth_gate.py)
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
  // Der aktuelle Auftrag (Schritte seit Nikolajs letzter Bitte) und seit wann das Gespraech laeuft
  const [mission, setMission] = useState<Mission>(EMPTY_MISSION);
  const missionRef = useRef<Mission>(EMPTY_MISSION);
  const newTask = useRef(false);
  const [connectedAt, setConnectedAt] = useState<number | null>(null);
  // Getippter Text, waehrend das Gespraech erst aufgebaut wird
  const pendingText = useRef<string | null>(null);
  // Live-Bildschirm des Laptops (voice-core/screen_stream.py), nur auf Wunsch
  const [screenOn, setScreenOn] = useState(false);
  // Welcher Monitor im Live-Bild (0 = automatisch, der mit dem Fenster vorne) und wie viele es gibt
  const [screenMonitor, setScreenMonitor] = useState(0);
  const [screenMonitors, setScreenMonitors] = useState(1);
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
      // 350 statt 600 ms: der Server stoppt jetzt schon an der Wortgrenze, laenger warten wirkte hakelig
      if (stop.armedAt && !stop.fading && (levels.current.bot < 0.015 || performance.now() - stop.armedAt > 350)) {
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
        onBotReady: () => {
          setPresence("idle");
          setConnectedAt(Date.now());
          const text = pendingText.current;
          pendingText.current = null;
          if (text) {
            newTask.current = true;
            void client.sendText(text, { run_immediately: true, audio_response: true });
          }
        },
        onDisconnected: () => {
          // Auch wenn der Voice-Core trennt (z. B. ein neueres Gespraech uebernimmt): Platz frei machen
          if (clientRef.current === client) clientRef.current = null;
          setConnectedAt(null);
          setPresence("offline");
          setScreenOn(false);
          setScreenTrack(null);
        },
        onUserStartedSpeaking: () => {
          // Spricht nieeeki gerade, ist das eine Unterbrechung
          if (presenceRef.current === "speaking") levels.current.cut = true;
          setPresence("listening");
        },
        onUserStoppedSpeaking: () => {
          newTask.current = true; // der naechste Werkzeugschritt beginnt einen neuen Auftrag
          setPresence("thinking");
        },
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
            items?: { id: string; grund: string; detail: string; admin?: boolean }[];
          };
          if (msg?.type === "leiser_stopp") softStop.current.armedAt = performance.now();
          if (msg?.type === "bildschirm") {
            const m = msg as { aktiv?: unknown; monitor?: unknown; monitore?: unknown };
            setScreenOn(Boolean(m.aktiv));
            if (typeof m.monitor === "number") setScreenMonitor(m.monitor);
            if (typeof m.monitore === "number") setScreenMonitors(m.monitore);
          }
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
              const now = Date.now();
              // gleiche id = Statuswechsel; neueste oben, hoechstens 30 Eintraege
              setActivities((list) => {
                const prev = list.find((x) => x.id === a.id);
                const item = { ...a, seit: prev?.seit ?? now, bis: now };
                return [item, ...list.filter((x) => x.id !== a.id)].slice(0, 30);
              });
              const known = missionRef.current.steps.some((s) => s.id === a.id);
              missionRef.current = nextMission(missionRef.current, a, now, newTask.current);
              if (!known) newTask.current = false;
              setMission(missionRef.current);
              // Desktop-App: Lichtrahmen ums Zielfenster und Welle am Klickpunkt
              if ((a.rahmen || a.punkt) && a.status !== "laeuft") {
                window.nieeekiDesktop?.highlight?.({ rahmen: a.rahmen, punkt: a.punkt, ok: a.status === "fertig" });
              }
            }
          }
          if (msg?.type === "freigaben" && msg.job && msg.items) {
            const { job, projekt = "", items } = msg;
            const now = Date.now();
            setApprovals((list) => {
              const fresh = items.map((it) => ({
                ...it,
                job,
                projekt,
                seit: list.find((a) => a.job === job && a.id === it.id)?.seit ?? now,
              }));
              return [...list.filter((a) => !fresh.some((f) => f.job === a.job && f.id === a.id)), ...fresh];
            });
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
        // Immer gueltiges JSON (leerer Body gab einen ERROR im Log). Beim Bot kommt nur "body" an
        // (Pipecat-Runner); per Weckwort begruesst sie damit nur kurz ("Ja?").
        requestData: anlass === "weckwort" ? { body: { anlass } } : {},
      });
    } catch (e) {
      pendingText.current = null; // nicht beim naechsten Verbinden unerwartet nachschicken
      setPresence("offline");
      // Fremde Herkunft weist der Voice-Core per CORS ab; der Browser meldet das nur als Netzwerkfehler.
      // Chrome laesst eine Webseite ausserdem nur mit Erlaubnis ins lokale Netz (Tailscale-Adresse 100.x).
      const lna = (e as { status?: number }).status === 401 ? null : await localNetworkPermission();
      setError(
        (e as { status?: number }).status === 401
          ? "Der Voice-Core hat die Anmeldung abgelehnt. Bitte ab- und wieder anmelden."
          : lna === "denied" || lna === "prompt"
            ? "Chrome lässt diese Seite nicht ins lokale Netzwerk, darum kommt sie nicht zum Voice-Core. Links neben der Adresse auf das Symbol klicken, bei „Lokales Netzwerk“ „Zulassen“ wählen und neu laden."
            : `Voice-Core unter ${VOICE_CORE_URL} nicht erreichbar (läuft bot.py, ist die Adresse ${HOME_URL} oder eine Vorschau?).`,
      );
    }
  }, [setPresence]);

  const connect = useCallback(() => start("klick"), [start]);

  // Getippte Bitte: geht wie gesprochen an nieeeki, sie antwortet mit Stimme. Laeuft noch kein
  // Gespraech, wird es gestartet und der Text nach dem Verbindungsaufbau geschickt.
  const sendText = useCallback(
    (text: string) => {
      const t = text.trim();
      if (!t) return;
      setUserLine(t);
      const client = clientRef.current;
      const p = presenceRef.current;
      if (client && p !== "offline" && p !== "connecting") {
        newTask.current = true;
        void client.sendText(t, { run_immediately: true, audio_response: true });
        return;
      }
      pendingText.current = t;
      if (!client && p === "offline") void start("klick");
    },
    [start],
  );

  const disconnect = useCallback(async () => {
    await clientRef.current?.disconnect();
    clientRef.current = null;
    pendingText.current = null;
    setConnectedAt(null);
    void botMeter.current?.ctx.close();
    botMeter.current = null;
    levels.current.bot = 0;
    setScreenOn(false);
    setScreenTrack(null);
    setPresence("offline");
  }, [setPresence]);

  // Desktop-App: "nieeeki" gesagt -> Gespraech starten (nur wenn keins laeuft, auch kein halb offenes)
  useEffect(() => {
    return window.nieeekiDesktop?.onWake?.(() => {
      if (presenceRef.current === "offline" && !clientRef.current) void start("weckwort");
    });
  }, [start]);

  // Wird die Oberflaeche ausgehaengt (Abmelden, automatisches Abmelden, Neuladen), endet auch ihr
  // Gespraech. Sonst lief die Verbindung unsichtbar weiter (19.10.: das Weckwort startete daneben ein
  // zweites, beide sprachen gleichzeitig).
  useEffect(() => {
    const audio = audioRef;
    const meter = botMeter;
    const client = clientRef;
    return () => {
      const running = client.current;
      client.current = null;
      void running?.disconnect();
      audio.current?.pause();
      void meter.current?.ctx.close();
      meter.current = null;
      window.nieeekiDesktop?.conversation?.(false);
    };
  }, []);

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
  // Monitor fuer das Live-Bild waehlen; der Voice-Core bestaetigt per Nachricht "bildschirm".
  const chooseMonitor = useCallback((monitor: number) => {
    setScreenMonitor(monitor);
    clientRef.current?.sendClientMessage("bildschirm_monitor", { monitor });
  }, []);

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
  // Admin-Freigaben tragen Nikolajs Login; der Admin-Helfer prueft es selbst. immer = Erlaubnisliste.
  const decide = useCallback(async (a: Approval, ja: boolean, immer = false) => {
    setApprovals((list) => list.filter((x) => !(x.job === a.job && x.id === a.id)));
    const admin = a.admin && ja ? { token: (await accessToken()) ?? "", immer } : {};
    clientRef.current?.sendClientMessage("freigabe", {
      job: a.job, id: a.id, entscheidung: ja ? "ja" : "nein", ...admin,
    });
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
    mission,
    connectedAt,
    acting,
    screenOn,
    screenTrack,
    sendText,
    toggleScreen,
    screenMonitor,
    screenMonitors,
    chooseMonitor,
    clickScreen,
    scrollScreen,
    typeOnScreen,
    pressOnScreen,
    decide,
    connect,
    disconnect,
  };
}
