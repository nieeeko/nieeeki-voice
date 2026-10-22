"""Kurze, vorab gesprochene Fuellwoerter: keine tote Stille, waehrend nieeeki arbeitet.

Menschen sagen "Moment." und fangen dann an; sie schweigen nicht drei Sekunden. Gemessen 20./21.10.:
Startet Claude ein Werkzeug, ohne vorher etwas zu sagen, kam der erste Ton erst nach Claude (~0,9 s)
+ Werkzeug (0,1 bis 10 s) + Claude (~0,9 s) + Sprachausgabe.

  * Werkzeug: hat Claude ein Werkzeug gestartet, ohne vorher etwas zu sagen, und ist nach
    tool_after_secs noch kein Ton da, sagt sie "Moment." (vorab aufgenommen, also sofort hoerbar,
    ohne auf Soniox zu warten). Laeuft das Werkzeug dann noch long_wait_secs, einmal "Dauert noch
    einen Moment."
  * Rueckmeldung (NIEEEKI_MHM=1): haelt Claude Nikolajs Satz fuer unfertig (◐), ein kurzes "Mhm" wie
    beim Zuhoeren. Selten (Abklingzeit, Zufall) und nie, wenn er schon weiterspricht.

Die Fuellwoerter gehen als einfacher Ton hinaus (OutputAudioRawFrame, kein TTSAudioRawFrame): der
Ausgang meldet dafuer kein "nieeeki spricht", nichts davon landet im Verlauf, und Unterbrechungen
laufen wie immer. Die Aufnahmen macht Soniox einmal in derselben Stimme und Einstellung wie im
Gespraech; danach liegen sie in voice-core/.cache/fuellwoerter/.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import time
from pathlib import Path
from typing import Awaitable, Callable, Sequence

import numpy as np
from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMMarkerFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

TOOL_PHRASES = ("Moment.", "Einen Moment.", "Sekunde.")
LONG_PHRASES = ("Dauert noch einen Moment.", "Gleich hab ich's.")
MHM_PHRASES = ("Mhm.", "Hm-hm.")
ALL_PHRASES = TOOL_PHRASES + LONG_PHRASES + MHM_PHRASES
INCOMPLETE_SHORT_MARKER = "◐"

SAMPLE_RATE = 24000  # wie der Ausgang (PipelineParams.audio_out_sample_rate): kein Umrechnen
CHUNK_BYTES = SAMPLE_RATE * 40 // 1000 * 2  # Pipecat schreibt 40-ms-Happen (audio_out_10ms_chunks=4)
MODEL = "tts-rt-v2"  # wie pipecat SonioxTTSService
CACHE_DIR = Path(__file__).parent / ".cache" / "fuellwoerter"
RENDER_TIMEOUT_SECS = 15.0

Synth = Callable[[str], Awaitable[bytes]]


def trim_silence(pcm: bytes, sample_rate: int = SAMPLE_RATE, *, threshold: int = 500,
                 head_ms: int = 30, tail_ms: int = 80) -> bytes:
    """Stille vorn und hinten abschneiden; Soniox setzt vor und nach jedem Satz eine Pause."""
    samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16)
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > threshold)
    if loud.size == 0:
        return b""
    start = max(0, int(loud[0]) - sample_rate * head_ms // 1000)
    end = min(samples.size, int(loud[-1]) + 1 + sample_rate * tail_ms // 1000)
    return samples[start:end].tobytes()


def fit_chunks(pcm: bytes, chunk_bytes: int = CHUNK_BYTES) -> bytes:
    """Mit Stille auf ganze 40-ms-Happen auffuellen. Ein angefangener Happen bliebe sonst im Ausgang
    liegen, bis der naechste Ton kommt, und klebte dann vor nieekis Antwort."""
    rest = len(pcm) % chunk_bytes
    return pcm + bytes(chunk_bytes - rest) if rest else pcm


async def soniox_synth(api_key: str, url: str, voice: str, tuning: dict, text: str) -> bytes:
    """Ein kurzer Satz als 16-Bit-PCM (24 kHz, mono) ueber die Soniox-TTS-Websocket."""
    import websockets

    stream_id = "fuellwort-" + hashlib.sha1(text.encode()).hexdigest()[:8]
    config = {
        "api_key": api_key,
        "stream_id": stream_id,
        "model": MODEL,
        "voice": voice,
        "language": "de",
        "audio_format": "pcm_s16le",
        "sample_rate": SAMPLE_RATE,
        **tuning,
    }
    audio = bytearray()
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps(config))
        await ws.send(json.dumps({"text": text + " ", "text_end": False, "stream_id": stream_id}))
        await ws.send(json.dumps({"text": "", "text_end": True, "stream_id": stream_id}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("error_code"):
                raise RuntimeError(f"Soniox {msg.get('error_code')}: {msg.get('error_message')}")
            if msg.get("audio"):
                audio += base64.b64decode(msg["audio"])
            if msg.get("terminated"):
                break
    return bytes(audio)


class FillerBank:
    """Die Aufnahmen der Fuellwoerter, je Stimme und Einstellung zwischengespeichert."""

    def __init__(self, voice: str, tuning: dict | None = None, *, cache_dir: Path = CACHE_DIR,
                 phrases: Sequence[str] = ALL_PHRASES):
        self.sample_rate = SAMPLE_RATE
        self._voice = voice
        self._tuning = dict(tuning or {})
        self._cache_dir = cache_dir
        self._phrases = tuple(phrases)
        self._audio: dict[str, bytes] = {}
        self._last: dict[tuple[str, ...], str] = {}

    @property
    def ready(self) -> bool:
        return bool(self._audio)

    def _path(self, text: str) -> Path:
        key = json.dumps(
            {"voice": self._voice, "tuning": self._tuning, "text": text, "model": MODEL, "rate": SAMPLE_RATE},
            sort_keys=True,
        )
        return self._cache_dir / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.pcm"

    def load_cached(self) -> list[str]:
        """Laedt, was schon aufgenommen ist; gibt zurueck, was fehlt."""
        missing = []
        for text in self._phrases:
            path = self._path(text)
            if path.is_file() and path.stat().st_size > 0:
                self._audio[text] = path.read_bytes()
            else:
                missing.append(text)
        return missing

    async def prepare(self, synth: Synth):
        """Fehlende Aufnahmen einmal bei Soniox holen. Fehler kosten nur das Fuellwort, nie das Gespraech."""
        missing = self.load_cached()
        if not missing:
            return

        async def one(text: str):
            try:
                pcm = fit_chunks(trim_silence(await asyncio.wait_for(synth(text), RENDER_TIMEOUT_SECS)))
            except Exception as e:  # Netz, Soniox, Zeitlimit
                logger.warning(f"Fuellwort {text!r} nicht aufgenommen: {e}")
                return
            if not pcm:
                logger.warning(f"Fuellwort {text!r}: Soniox lieferte nur Stille")
                return
            self._audio[text] = pcm
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                self._path(text).write_bytes(pcm)
            except OSError as e:
                logger.warning(f"Fuellwort {text!r} nicht gespeichert: {e}")

        # Nacheinander, nie parallel: Soniox begrenzt gleichzeitige TTS-Verbindungen (22.10.: ab der
        # vierten "429 Concurrent requests limit"), und das laufende Gespraech braucht auch eine.
        for text in missing:
            await one(text)
        logger.info(f"Fuellwoerter bereit: {len(self._audio)} von {len(self._phrases)}")

    def pick(self, phrases: Sequence[str]) -> tuple[str, bytes] | None:
        """Ein vorhandenes Fuellwort der Gruppe, moeglichst nicht dasselbe wie beim letzten Mal."""
        group = tuple(phrases)
        available = [p for p in group if p in self._audio]
        if not available:
            return None
        fresh = [p for p in available if p != self._last.get(group)] or available
        text = random.choice(fresh)
        self._last[group] = text
        return text, self._audio[text]


class FillerPlayer(FrameProcessor):
    """Zwischen Claude und der Sprachausgabe: spielt Fuellwoerter, wenn sonst Stille waere."""

    def __init__(
        self,
        bank: FillerBank,
        *,
        tool_fill: bool = True,
        tool_after_secs: float = 0.4,
        long_wait_secs: float = 6.0,
        mhm: bool = False,
        mhm_cooldown_secs: float = 20.0,
        mhm_chance: float = 0.6,
        rng: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._bank = bank
        self._tool_fill = tool_fill
        self._tool_after = tool_after_secs
        self._long_wait = long_wait_secs
        self._mhm = mhm
        self._mhm_cooldown = mhm_cooldown_secs
        self._mhm_chance = mhm_chance
        self._rng = rng
        self._now = monotonic
        self._bot_speaking = False
        self._user_speaking = False
        self._text_in_response = False  # Claude hat in dieser Antwort schon etwas zu sagen
        self._spoke_in_cycle = False  # seit Nikolajs letztem Satz hat sie schon gesprochen
        self._tools: set[str] = set()
        self._tool_started_at: float | None = None
        self._played_tool = False
        self._played_long = False
        self._sound_until = 0.0  # bis dann klingt Ton von ihr (Fuellwort oder Antwort)
        self._last_mhm = float("-inf")
        self._timer: asyncio.Task | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._spoke_in_cycle = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._sound_until = max(self._sound_until, self._now())
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._user_speaking = True
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_speaking = False
        elif direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, (UserStartedSpeakingFrame, InterruptionFrame)):
                await self._new_cycle()
            elif isinstance(frame, LLMFullResponseStartFrame):
                self._text_in_response = False
            elif isinstance(frame, LLMTextFrame) and frame.text.strip():
                self._text_in_response = True
            elif isinstance(frame, FunctionCallsStartedFrame):
                self._tools_started([c.tool_call_id for c in frame.function_calls])
            elif isinstance(frame, FunctionCallInProgressFrame):
                self._tools_started([frame.tool_call_id])
            elif isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
                self._tools.discard(frame.tool_call_id)
            elif isinstance(frame, LLMMarkerFrame) and frame.marker == INCOMPLETE_SHORT_MARKER:
                await self.maybe_mhm()

        await self.push_frame(frame, direction)

    async def _new_cycle(self):
        await self._cancel_timer()
        self._spoke_in_cycle = False
        self._text_in_response = False
        self._tools.clear()
        self._tool_started_at = None
        self._played_tool = False
        self._played_long = False

    def _tools_started(self, ids: list[str]):
        self._tools.update(i for i in ids if i)
        if self._tool_fill and self._tool_started_at is None:
            self._tool_started_at = self._now()
            self._timer = self.create_task(self._watch_tools(), name="fuellwort")

    def should_fill(self) -> bool:
        """Erstes Fuellwort: noch nichts gesagt, nichts unterwegs, niemand spricht."""
        return (
            self._bank.ready
            and not self._played_tool
            and not self._bot_speaking
            and not self._user_speaking
            and not self._text_in_response
            and not self._spoke_in_cycle
        )

    def should_say_long(self) -> bool:
        """Zweites Fuellwort: das Werkzeug laeuft noch und es ist seit long_wait_secs still."""
        started = self._tool_started_at or self._now()
        quiet_since = max(self._sound_until, started)
        return (
            self._bank.ready
            and not self._played_long
            and bool(self._tools)
            and not self._bot_speaking
            and not self._user_speaking
            and self._now() - quiet_since >= self._long_wait
        )

    async def _watch_tools(self):
        try:
            await asyncio.sleep(self._tool_after)
            if self.should_fill():
                self._played_tool = await self._play(TOOL_PHRASES, "Werkzeug")
            while self._tools and not self._played_long:
                await asyncio.sleep(0.25)
                if self.should_say_long():
                    self._played_long = await self._play(LONG_PHRASES, "Werkzeug dauert")
        finally:
            self._timer = None

    async def maybe_mhm(self):
        if not self._mhm or not self._bank.ready:
            return
        now = self._now()
        if self._user_speaking or self._bot_speaking or now - self._last_mhm < self._mhm_cooldown:
            return
        if self._rng() > self._mhm_chance:
            return
        self._last_mhm = now
        await self._play(MHM_PHRASES, "Rueckmeldung")

    async def _play(self, phrases: Sequence[str], reason: str) -> bool:
        picked = self._bank.pick(phrases)
        if not picked:
            return False
        text, audio = picked
        await self.push_frame(OutputAudioRawFrame(audio=audio, sample_rate=self._bank.sample_rate, num_channels=1))
        self._sound_until = self._now() + len(audio) / (2 * self._bank.sample_rate)
        logger.info(f"{self}: Fuellwort ({reason}): {text!r}")
        return True

    async def _cancel_timer(self):
        if self._timer:
            await self.cancel_task(self._timer)
            self._timer = None

    async def cleanup(self):
        await super().cleanup()
        await self._cancel_timer()
