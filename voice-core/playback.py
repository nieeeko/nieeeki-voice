"""Wiedergabe am WebRTC-Ausgang anhalten und fortsetzen, und wissen, wie weit der Ton wirklich ist.

Pipecat schreibt nieekis Ton in 40-ms-Happen in die WebRTC-Spur (write_audio_frame) und wartet jeweils,
bis die Spur den Happen abgeholt hat; alles Weitere wartet in der Warteschlange des Ausgangs. Genau an
dieser Stelle sitzt AudioHold:
  * pause(): der naechste Happen wartet, die Spur sendet Stille. Nichts geht verloren.
  * resume(): es geht an derselben Stelle weiter (falscher Alarm: Nikolaj hat nur kurz etwas gesagt).
  * cut(): nach einer echten Unterbrechung. Happen, die bis zum Abbruch noch durchrutschen, verwirft es.
  * played_until_ns(): bis zu welcher Uhrzeit der Pipeline-Uhr (gleiche Skala wie TTSTextFrame.pts)
    der Ton dieser Antwort hinausgegangen ist.

Warum die Uhrzeit: Pipecat rechnet die Wort-Zeitstempel ab dem ersten Ton der Antwort, ohne Luecken.
Liefert Soniox einmal langsamer, als gesprochen wird, oder steht der Ton in einer Pause, laeuft die Uhr
weiter, der Ton nicht. Daher eilten die Woerter dem Ton voraus (gemessen ~0,5 s, siehe soft_interrupt.py).
Gemessen am geschriebenen Ton meldet der PlaybackTracker jedes Wort, wenn es wirklich hinausgeht.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from loguru import logger

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Nach einem Abbruch noch so lange Happen verwerfen, bis Pipecat die alte Warteschlange geleert hat.
# Neuer Ton kommt fruehestens nach Nikolajs Satz und Claudes Antwort, also weit spaeter.
DRAIN_SECS = 0.15
# Sicherung: Pipecat haelt einen Schreibvorgang, der laenger als 10 s dauert, fuer eine tote Verbindung
# und beendet die Sitzung. Laenger als das bleibt keine Pause stehen.
MAX_PAUSE_SECS = 4.0

Write = Callable[[OutputAudioRawFrame], Awaitable[bool]]


def audio_ns(frame: OutputAudioRawFrame) -> int:
    """Spieldauer eines 16-Bit-PCM-Happens in Nanosekunden."""
    bytes_per_sec = frame.sample_rate * frame.num_channels * 2
    return len(frame.audio) * 1_000_000_000 // bytes_per_sec if bytes_per_sec else 0


class AudioHold:
    def __init__(
        self,
        *,
        clock_ns: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._open = asyncio.Event()
        self._open.set()
        self._clock_ns = clock_ns
        self._now = monotonic
        self._t0_ns: int | None = None  # Uhrzeit, zu der der erste Ton dieser Antwort ankam
        self._played_ns = 0  # so viel TTS-Ton dieser Antwort ist hinausgegangen
        self._drop_until = 0.0
        self._progress = asyncio.Event()

    @property
    def paused(self) -> bool:
        return not self._open.is_set()

    def install(self, output) -> bool:
        """Klinkt sich in write_audio_frame des Ausgangs ein (transport.output())."""
        write = getattr(output, "write_audio_frame", None)
        if write is None:
            logger.warning("AudioHold: Ausgang ohne write_audio_frame, Innehalten bleibt aus")
            return False
        if self._clock_ns is None:
            self._clock_ns = lambda: output.get_clock().get_time()

        async def gated(frame: OutputAudioRawFrame) -> bool:
            return await self.write(frame, write)

        output.write_audio_frame = gated
        return True

    async def write(self, frame: OutputAudioRawFrame, write: Write) -> bool:
        if not self._open.is_set():
            try:
                await asyncio.wait_for(self._open.wait(), MAX_PAUSE_SECS)
            except asyncio.TimeoutError:
                logger.warning("AudioHold: Pause zu lang, Ton laeuft weiter")
                self.resume()
        if self._now() < self._drop_until:
            return False  # Rest einer abgebrochenen Antwort
        if isinstance(frame, TTSAudioRawFrame):
            if self._t0_ns is None:
                self.mark_start()
            self._played_ns += audio_ns(frame)
            self._progress.set()
        return await write(frame)

    def mark_start(self):
        """Erster Ton einer Antwort ist am Ausgang angekommen (PlaybackStart, sonst erster Happen)."""
        if self._t0_ns is None and self._clock_ns is not None:
            self._t0_ns = self._clock_ns()

    def played_until_ns(self) -> int | None:
        if self._t0_ns is None:
            return None
        return self._t0_ns + self._played_ns

    def pause(self):
        self._open.clear()

    def resume(self):
        self._open.set()
        self._progress.set()

    def cut(self):
        """Echte Unterbrechung: Rest verwerfen, danach offen fuer die naechste Antwort."""
        self._drop_until = self._now() + DRAIN_SECS
        self.new_turn()
        self.resume()

    def new_turn(self):
        self._t0_ns = None
        self._played_ns = 0
        self._progress.set()

    async def wait_progress(self, timeout: float):
        """Wartet, bis wieder Ton hinausging oder sich der Zustand aenderte (hoechstens timeout)."""
        self._progress.clear()
        try:
            await asyncio.wait_for(self._progress.wait(), timeout)
        except asyncio.TimeoutError:
            pass


class PlaybackStart(FrameProcessor):
    """Direkt vor transport.output(): merkt, wann der erste Ton einer Antwort am Ausgang ankommt.

    Ab diesem Moment zaehlt Pipecat die Wort-Zeitstempel (start_word_timestamps). Laeuft vorher noch
    etwas anderes (ein Fuellwort, eine Pause), wartet der Ton, die Woerter aber nicht.
    """

    def __init__(self, hold: AudioHold, **kwargs):
        super().__init__(**kwargs)
        self._hold = hold

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            self._hold.mark_start()
        elif isinstance(frame, BotStoppedSpeakingFrame) and direction == FrameDirection.UPSTREAM:
            self._hold.new_turn()  # Antwort fertig gespielt; die naechste zaehlt neu
        elif isinstance(frame, InterruptionFrame):
            self._hold.new_turn()
        await self.push_frame(frame, direction)
