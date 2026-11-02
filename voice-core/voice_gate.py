"""Stimmpruefung: nur Nikolajs Stimme darf Auftraege und PC-Aktionen ausloesen.

SpeakerVerifier sitzt direkt hinter dem Mikrofon. Er haelt die letzten Sekunden Audio vor, schneidet
jede Aeusserung anhand der VAD-Meldungen heraus und vergleicht die laufende Aeusserung (hoechstens
die letzten 8 s) mit dem Stimmabdruck (voiceprint.py). Ergebnis: nikolaj, fremd oder unklar (zu
kurz oder dazwischen). Rohaudio wird nie gespeichert.

VoiceGate merkt sich, wie die letzte Aeusserung eingestuft wurde. Jedes Werkzeug ausser OPEN_TOOLS
fragt vor dem Ausfuehren das Gate (install_tool_gate, gate_schemas): geprueft wird im Code, nicht im
Prompt. Direkte Funktionen (register_direct_function) sind nicht abgedeckt; nieeeki nutzt keine.
- nikolaj (genug Sprache, Wert hoch): erlaubt, bis trust_secs danach.
- kurz ("Ja", "mach"): zu wenig zum Urteilen, es gilt das Ergebnis davor. Klingt es klar fremd, fremd.
- fremd oder unklar (genug Sprache, aber nicht sicher; ebenso jede fehlgeschlagene Pruefung): gesperrt,
  bis Nikolaj wieder sicher erkannt ist oder tippt. Spricht nieeeki gleichzeitig (Echo), heisst ein
  niedriger Wert nur unklar.
- Getippter Text aus der Oberflaeche zaehlt wie Nikolaj: die Oberflaeche ist mit zweitem Faktor
  angemeldet (watch_typed_text). Pruefergebnisse zu frueheren Aeusserungen ueberschreiben ihn nicht.

Modi (NIEEEKI_VOICE_CHECK): aus | beobachten (Standard: messen, melden, ins Log schreiben, nichts
sperren) | streng. Streng sperrt auch, solange kein Abdruck eingelernt ist, Modell oder Abdruck nicht
geladen sind oder die Pruefung laenger als PENDING_WAIT_SECS dauert; getippter Text geht immer.

Einlernen nur per Klick in der Oberflaeche (Nachricht "stimmabdruck"), nie per Sprache: sonst
koennte eine fremde Stimme den Abdruck ersetzen. Gibt es schon einen, muss Nikolaj im Modus streng
vorher an der Stimme erkannt sein. Waehrend der Aufnahme hoeren Spracherkennung und VAD nur Stille,
nieeeki antwortet also nicht auf den Vorlesetext.
"""

from __future__ import annotations

import asyncio
import functools
import math
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import numpy as np
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame

from voiceprint import (
    MIN_CHUNKS,
    SPEECH_DBFS,
    VoiceModel,
    centroid,
    cosine,
    dbfs,
    enroll_chunks,
    speech_blocks,
)

NIKOLAJ, FREMD, UNKLAR, KURZ = "nikolaj", "fremd", "unklar", "kurz"
TYPED = "getippt"
MODES = ("aus", "beobachten", "streng")
# Werkzeuge, die jede Stimme ausloesen darf: nichts aendern, nichts Privates zeigen
OPEN_TOOLS = frozenset({"aktuelle_zeit", "websuche", "canvas_zeigen"})
PENDING_WAIT_SECS = 2.0  # so lange wartet ein Werkzeug hoechstens auf die laufende Pruefung
RING_SECS = 10.0
MAX_VERIFY_SECS = 8.0
TURN_GAP_SECS = 3.0  # laengere Stille: die naechste Aeusserung wird fuer sich geprueft
PRE_ROLL_SECS = 0.25  # VAD meldet den Beginn erst nach start_secs, etwas Luft davor
POST_ROLL_SECS = 0.1
ENROLL_SECS = 20.0  # so viel reine Sprache fuer den Abdruck
ENROLL_MAX_SECS = 90.0
ENROLL_WAIT_SECS = 8.0  # spricht nieeeki ihre Anleitung nicht, beginnt die Aufnahme trotzdem
ENROLL_MIN_SPREAD = 0.55  # Stuecke liegen im Schnitt so nah am Mittel, sonst war es zu unruhig
ENROLL_REPORT_SECS = 0.5
ENROLL_PROMPT = (
    "Okay, ich lerne jetzt deine Stimme. Lies den Text auf dem Bildschirm in Ruhe vor oder erzaehl "
    "einfach etwas, bis der Balken voll ist."
)
ENROLL_DONE = "Danke, jetzt kenne ich deine Stimme."


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name) or default)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):  # auch "nan" und "inf": sonst liefe das Vertrauen nie ab
        logger.warning("{} ist keine gueltige Zahl, nehme {}", name, default)
        return default
    return value


@dataclass
class VoiceGate:
    mode: str = "beobachten"
    owner_min: float = 0.5  # ab hier Nikolaj
    stranger_max: float = 0.3  # bis hier fremd, dazwischen unklar
    min_secs: float = 1.0  # kuerzere Aeusserungen (reine Sprache) reichen nicht fuer "Nikolaj"
    trust_secs: float = 120.0
    clock: Callable[[], float] = time.monotonic
    voiceprint: np.ndarray | None = None
    print_state: str = "laedt"  # laedt | da | fehlt | kaputt
    model_ok: bool | None = None
    _who: str | None = None
    _at: float = 0.0
    _typed_at: float = -math.inf
    _pending: asyncio.Task | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> VoiceGate:
        mode = (os.getenv("NIEEEKI_VOICE_CHECK") or "beobachten").strip().lower()
        if mode not in MODES:
            logger.warning("NIEEEKI_VOICE_CHECK={!r} unbekannt, pruefe streng", mode)
            mode = "streng"
        return cls(
            mode=mode,
            owner_min=_env_float("NIEEEKI_VOICE_OWNER_MIN", 0.5),
            stranger_max=_env_float("NIEEEKI_VOICE_STRANGER_MAX", 0.3),
            min_secs=_env_float("NIEEEKI_VOICE_MIN_SECS", 1.0),
            trust_secs=_env_float("NIEEEKI_VOICE_TRUST_SECS", 120.0),
        )

    @property
    def status(self) -> str:
        """aus | fehlt (nicht eingelernt) | laedt | kaputt | bereit"""
        if self.mode == "aus":
            return "aus"
        if self.print_state == "fehlt":
            return "fehlt"
        if self.print_state == "kaputt" or self.model_ok is False:
            return "kaputt"
        if self.print_state == "laedt" or self.model_ok is None:
            return "laedt"
        return "bereit"

    def set_voiceprint(self, voiceprint: np.ndarray | None, ok: bool = True) -> None:
        self.voiceprint = voiceprint
        self.print_state = "kaputt" if not ok else "da" if voiceprint is not None else "fehlt"

    def classify(self, score: float, secs: float, overlap: bool) -> str:
        """nikolaj: sicher er. fremd: sicher nicht. unklar: genug Sprache, aber nicht sicher (entzieht
        das Vertrauen). kurz: zu wenig Sprache zum Urteilen ("Ja"), aendert nichts."""
        if score >= self.owner_min:
            return NIKOLAJ if secs >= self.min_secs else KURZ
        if score <= self.stranger_max:
            return UNKLAR if overlap else FREMD  # sprach nieeeki gleichzeitig, kann es Echo sein
        return UNKLAR if secs >= self.min_secs else KURZ

    def record(self, verdict: str, heard_at: float | None = None) -> None:
        """heard_at: Ende der Aeusserung (gleiche Uhr). Was vor getipptem Text gesagt wurde, zaehlt nicht mehr."""
        if verdict == KURZ or (heard_at is not None and heard_at < self._typed_at):
            return
        self._who, self._at = verdict, self.clock()

    def typed(self) -> None:
        self._who = TYPED
        self._at = self._typed_at = self.clock()

    def reason(self) -> str | None:
        """None, wenn zuletzt Nikolaj sprach oder tippte, sonst warum nicht."""
        if self._who == FREMD:
            return "fremde Stimme"
        if self._who == UNKLAR:
            return "Stimme nicht sicher erkannt"
        if self._who is None:
            return "Stimme noch nicht erkannt"
        if self.clock() - self._at > self.trust_secs:
            return "Stimme zu lange nicht mehr erkannt"
        return None

    def voice_trusted(self) -> bool:
        """Zuletzt sicher an der Stimme erkannt (nicht nur getippt)."""
        return self._who == NIKOLAJ and self.reason() is None

    def track(self, task: asyncio.Task) -> None:
        self._pending = task

    def _grund(self) -> str | None:
        if self._pending and not self._pending.done():
            return "Stimmpruefung dauert zu lange"
        status = self.status
        if status == "bereit":
            return self.reason()
        if self._who == TYPED and self.reason() is None:
            return None  # getippt geht auch ohne Abdruck
        return "kein Stimmabdruck eingelernt" if status == "fehlt" else "Stimmpruefung nicht bereit"

    async def check(self, tool: str) -> dict | None:
        """Vor jedem Werkzeug: None = ausfuehren, sonst das Ergebnis fuer Claude."""
        if self.mode == "aus" or tool in OPEN_TOOLS:
            return None
        if self._pending and not self._pending.done():
            await asyncio.wait({self._pending}, timeout=PENDING_WAIT_SECS)
        grund = self._grund()
        if grund is None:
            return None
        if self.mode == "beobachten":
            logger.info("Stimmpruefung (beobachten): {} waere gesperrt ({})", tool, grund)
            return None
        logger.warning("Stimmpruefung: {} gesperrt ({})", tool, grund)
        return {
            "status": "blocked",
            "grund": f"Stimmpruefung: {grund}",
            "hinweis": "Nur Nikolaj darf das ausloesen. Nicht erneut versuchen.",
        }


def gated(name: str | None, handler, gate: VoiceGate):
    """Handler, der vorher das Gate fragt. Offene Werkzeuge und schon gepruefte bleiben, wie sie sind.
    name None (ein Handler fuer alle Werkzeuge): geprueft wird der Name des Aufrufs."""
    if name in OPEN_TOOLS or getattr(handler, "_nieeeki_gate", None) is gate:
        return handler

    @functools.wraps(handler)  # uebernimmt auch @tool_options (Zeitlimit usw.)
    async def guarded(params):
        blocked = await gate.check(name or params.function_name)
        if blocked is not None:
            await params.result_callback(blocked)
            return
        await handler(params)

    guarded._nieeeki_gate = gate
    return guarded


def install_tool_gate(llm, gate: VoiceGate) -> None:
    """Jedes Werkzeug, das ab jetzt am LLM registriert wird, laeuft durch das Gate.
    Direkt nach dem Anlegen des LLM aufrufen, vor allen register_function."""
    register = llm.register_function

    def register_function(function_name, handler, *args, **kwargs):
        return register(function_name, gated(function_name, handler, gate), *args, **kwargs)

    llm.register_function = register_function


def gate_schemas(tools: ToolsSchema, gate: VoiceGate) -> ToolsSchema:
    """Werkzeuge mit eigenem Handler (z. B. vom PC-Host per MCP) einmal einpacken. Pipecat registriert
    sie bei jedem Kontext neu; derselbe eingepackte Handler bleibt dabei derselbe."""
    return ToolsSchema(standard_tools=[
        FunctionSchema(s.name, s.description, s.properties, s.required, handler=gated(s.name, s.handler, gate))
        if s.handler is not None else s
        for s in tools.standard_tools
    ])


def watch_typed_text(rtvi, gate: VoiceGate) -> bool:
    """Getippter Text aus der Oberflaeche (RTVI send-text) zaehlt wie Nikolaj."""
    handle = getattr(rtvi, "_handle_send_text", None)
    if handle is None:
        logger.warning("Stimmpruefung: getippter Text nicht erkennbar (Pipecat geaendert?)")
        return False

    async def handle_send_text(data):
        gate.typed()
        await handle(data)

    rtvi._handle_send_text = handle_send_text
    return True


@dataclass
class _Enrollment:
    started: float
    waiting: bool = True  # bis nieeeki ihre Anleitung fertig gesprochen hat
    bot_started: bool = False
    audio: bytearray = field(default_factory=bytearray)
    speech_secs: float = 0.0
    reported: float = 0.0


class SpeakerVerifier(FrameProcessor):
    """Prueft jede Aeusserung gegen den Stimmabdruck und lernt ihn auf Klick ein (siehe oben)."""

    def __init__(
        self,
        gate: VoiceGate,
        model: VoiceModel,
        *,
        save: Callable[[np.ndarray], Awaitable[bool]],
        say: Callable[[str], Awaitable[None]],
        clock: Callable[[], float] = time.time,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._gate = gate
        self._model = model
        self._save = save
        self._say = say
        self._clock = clock  # Wanduhr wie VADUser...SpeakingFrame.timestamp
        self._ring: deque[tuple[float, bytes]] = deque()
        self._ring_secs = 0.0
        self._rate = 16000
        self._segment_start: float | None = None  # mit Luft davor (zum Ausschneiden)
        self._speech_start = 0.0  # wie VAD ihn meldet (zum Messen der reinen Sprache)
        self._segment_overlap = False
        self._turn = b""  # laufende Aeusserung, hoechstens MAX_VERIFY_SECS
        self._turn_secs = 0.0
        self._turn_overlap = False
        self._turn_closed = True
        self._last_stop = -math.inf
        self._bot_speaking = False
        self._task: asyncio.Task | None = None
        self._enroll: _Enrollment | None = None
        self._finishing = False  # Abdruck wird gerade berechnet und gespeichert
        self._cancelled = False
        self._tasks: set[asyncio.Task] = set()

    # ---------- Laden und Status ----------

    async def prepare(self, load_voiceprint: Callable[[], Awaitable[tuple[bool, np.ndarray | None]]]) -> None:
        """Modell und Abdruck laden (im Hintergrund, das Gespraech wartet nicht)."""
        if self._gate.mode == "aus":
            return
        ok, voiceprint = await load_voiceprint()
        self._gate.set_voiceprint(voiceprint, ok)
        self._gate.model_ok = await self._model.get() is not None
        logger.info("Stimmpruefung: {} ({})", self._gate.status, self._gate.mode)
        await self.report_status()

    async def report_status(self) -> None:
        await self._send({"type": "stimme", "status": self._gate.status, "modus": self._gate.mode})

    async def _send(self, data: dict) -> None:
        await self.push_frame(RTVIServerMessageFrame(data=data))

    # ---------- Frames ----------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            await self.on_audio(frame)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self.on_speech_start(frame.timestamp, frame.start_secs)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.on_speech_stop(frame.timestamp, frame.stop_secs)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.on_turn_end()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.on_bot_speaking(True)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.on_bot_speaking(False)
        await self.push_frame(frame, direction)

    def on_turn_end(self) -> None:
        self._turn_closed = True  # die naechste Aeusserung wird fuer sich geprueft

    def on_bot_speaking(self, speaking: bool) -> None:
        self._bot_speaking = speaking
        if speaking:
            self._turn_closed = True  # was danach kommt, ist eine neue Aeusserung
        if self._enroll:
            if speaking:
                self._enroll.bot_started = True
            elif self._enroll.bot_started:
                self._enroll.waiting = False  # ihre Anleitung ist vorbei, jetzt aufnehmen

    async def on_audio(self, frame: InputAudioRawFrame) -> None:
        pcm = frame.audio
        self._rate = frame.sample_rate
        self._ring.append((self._clock(), pcm))
        self._ring_secs += _secs(pcm, self._rate)
        while self._ring_secs > RING_SECS and len(self._ring) > 1:
            self._ring_secs -= _secs(self._ring.popleft()[1], self._rate)
        if self._enroll:
            await self._collect(pcm)
            frame.audio = bytes(len(pcm))  # Spracherkennung und VAD hoeren Stille

    def on_speech_start(self, timestamp: float, start_secs: float) -> None:
        if self._enroll:
            return
        self._speech_start = timestamp - start_secs
        if self._turn_closed or self._speech_start - self._last_stop > TURN_GAP_SECS:
            self._turn, self._turn_secs, self._turn_overlap, self._turn_closed = b"", 0.0, False, False
        self._segment_start = self._speech_start - PRE_ROLL_SECS
        self._segment_overlap = self._bot_speaking

    def on_speech_stop(self, timestamp: float, stop_secs: float) -> None:
        if self._enroll or self._segment_start is None:
            return
        speech_end = timestamp - stop_secs
        segment = self._slice(self._segment_start, speech_end + POST_ROLL_SECS)
        self._turn_overlap = self._turn_overlap or self._segment_overlap or self._bot_speaking
        self._segment_start = None
        self._last_stop = speech_end
        # Nur die letzten MAX_VERIFY_SECS behalten; gemessen wird die reine Sprache ohne die Luft drumherum
        self._turn = (self._turn + segment)[-int(MAX_VERIFY_SECS * self._rate) * 2:]
        self._turn_secs = min(self._turn_secs + max(0.0, speech_end - self._speech_start), MAX_VERIFY_SECS)
        previous = self._task
        self._task = self._spawn(
            self._verify(self._turn, self._rate, self._turn_secs, self._turn_overlap, self._gate.clock(), previous)
        )
        self._gate.track(self._task)

    def _slice(self, start: float, end: float) -> bytes:
        parts = []
        for t_end, pcm in self._ring:
            t_start = t_end - _secs(pcm, self._rate)
            if t_end > start and t_start < end:
                parts.append(pcm)
        return b"".join(parts)

    # ---------- Pruefen ----------

    async def _verify(
        self, pcm: bytes, rate: int, secs: float, overlap: bool, heard_at: float, previous: asyncio.Task | None
    ) -> None:
        """secs: reine Sprache. heard_at: Ende der Aeusserung (Uhr des Gates)."""
        if previous and not previous.done():
            await asyncio.wait({previous})  # der Reihe nach, sonst ueberholt ein altes Ergebnis
        if self._gate.voiceprint is None or self._gate.mode == "aus":
            return
        model = await self._model.get()
        self._gate.model_ok = model is not None
        try:
            if model is None or not pcm:
                raise RuntimeError("kein Modell" if model is None else "kein Ton ausgeschnitten")
            score = cosine(await asyncio.to_thread(model.embed, pcm, rate), self._gate.voiceprint)
        except Exception as e:  # nicht geprueft heisst nicht erkannt: nie das alte Ergebnis weiterverwenden
            logger.warning("Stimmpruefung fehlgeschlagen ({}) -> unklar", e)
            self._gate.record(UNKLAR, heard_at)
            await self._send({"type": "stimme", "ergebnis": UNKLAR, "modus": self._gate.mode})
            return
        verdict = self._gate.classify(score, secs, overlap)
        self._gate.record(verdict, heard_at)
        logger.info(
            "Stimme: {:.2f} bei {:.1f} s{} -> {}", score, secs, " (nieeeki sprach)" if overlap else "", verdict
        )
        shown = UNKLAR if verdict == KURZ else verdict
        await self._send({"type": "stimme", "ergebnis": shown, "wert": round(score, 2), "modus": self._gate.mode})

    # ---------- Einlernen ----------

    async def start_enrollment(self) -> None:
        if self._enroll or self._finishing:
            return
        if self._gate.voiceprint is not None:
            # Ersetzen nur, wenn er gerade an der Stimme erkannt ist: wer nur an der angemeldeten
            # Oberflaeche sitzt, soll den Abdruck nicht dauerhaft auf sich umschreiben koennen.
            if self._gate.mode == "streng" and not self._gate.voice_trusted():
                await self._fail("Sag erst kurz etwas in einem ganzen Satz, damit ich dich erkenne. Dann noch mal einlernen.")
                return
            logger.warning("Stimmabdruck: vorhandener Abdruck wird neu eingelernt")
        self._enroll = _Enrollment(started=self._clock())
        logger.info("Stimmabdruck: Einlernen beginnt")
        await self._send({"type": "stimmabdruck", "status": "start", "sekunden": 0, "ziel": ENROLL_SECS})
        await self._say(ENROLL_PROMPT)

    async def cancel_enrollment(self) -> None:
        if not (self._enroll or self._finishing):
            return
        self._enroll = None
        self._cancelled = self._finishing  # rechnet schon: nicht mehr speichern
        logger.info("Stimmabdruck: Einlernen abgebrochen")
        await self._send({"type": "stimmabdruck", "status": "abgebrochen"})

    async def _collect(self, pcm: bytes) -> None:
        e = self._enroll
        now = self._clock()
        if now - e.started > ENROLL_MAX_SECS:  # zuerst: auch wenn ihr Ende nie gemeldet wurde
            self._enroll = None
            await self._fail("Ich habe zu wenig von dir gehört. Versuch es bitte noch einmal.")
            return
        if e.waiting:
            # Ihre Anleitung soll nicht im Abdruck landen; kommt sie nicht, trotzdem anfangen
            if now - e.started < ENROLL_WAIT_SECS or self._bot_speaking:
                return
            e.waiting = False
        e.audio.extend(pcm)
        if dbfs(np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)) > SPEECH_DBFS:
            e.speech_secs += _secs(pcm, self._rate)
        if e.speech_secs >= ENROLL_SECS:
            self._enroll = None
            self._finishing = True
            await self._send({"type": "stimmabdruck", "status": "rechnet", "sekunden": ENROLL_SECS, "ziel": ENROLL_SECS})
            self._spawn(self._finish(bytes(e.audio), self._rate))
        elif now - e.reported >= ENROLL_REPORT_SECS:
            e.reported = now
            await self._send({
                "type": "stimmabdruck", "status": "aufnahme", "sekunden": round(e.speech_secs, 1), "ziel": ENROLL_SECS,
            })

    async def _finish(self, pcm: bytes, rate: int) -> None:
        try:
            await self._compute_and_save(pcm, rate)
        except Exception as e:
            logger.exception("Stimmabdruck: Einlernen fehlgeschlagen: {}", e)
            await self._fail("Das Einlernen hat nicht geklappt. Schau ins Log des Voice-Core.")
        finally:
            self._finishing = self._cancelled = False

    async def _compute_and_save(self, pcm: bytes, rate: int) -> None:
        model = await self._model.get()
        if model is None:
            await self._fail("Das Modell für die Stimmerkennung fehlt. Schau ins Log des Voice-Core.")
            return
        chunks = enroll_chunks(speech_blocks(pcm, rate), rate)
        if len(chunks) < MIN_CHUNKS:
            await self._fail("Ich habe zu wenig von dir gehört. Versuch es bitte noch einmal.")
            return
        embeddings = await asyncio.to_thread(lambda: [model.embed(c, rate) for c in chunks])
        voiceprint, spread = centroid(embeddings)
        logger.info("Stimmabdruck: {} Stuecke, Streuung {:.2f}", len(chunks), spread)
        if spread < ENROLL_MIN_SPREAD:
            await self._fail("Die Aufnahme war zu unruhig. Bitte noch einmal, am besten ohne andere Stimmen im Raum.")
            return
        if self._cancelled:
            logger.info("Stimmabdruck: abgebrochen, nichts gespeichert")
            return
        if not await self._save(voiceprint):
            await self._fail("Ich konnte den Stimmabdruck nicht speichern. Schau ins Log des Voice-Core.")
            return
        self._gate.set_voiceprint(voiceprint)
        self._gate.record(NIKOLAJ)  # er hat gerade per Klick eingelernt und gesprochen
        await self._send({"type": "stimmabdruck", "status": "fertig", "sekunden": ENROLL_SECS, "ziel": ENROLL_SECS})
        await self.report_status()
        await self._say(ENROLL_DONE)

    async def _fail(self, text: str) -> None:
        logger.warning("Stimmabdruck: {}", text)
        await self._send({"type": "stimmabdruck", "status": "fehler", "text": text})
        await self._say(text)

    # ---------- Aufraeumen ----------

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cleanup(self):
        await super().cleanup()
        for task in list(self._tasks):
            task.cancel()


def _secs(pcm: bytes, rate: int) -> float:
    return len(pcm) / 2 / rate if rate else 0.0
