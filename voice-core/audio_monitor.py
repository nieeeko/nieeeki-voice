"""Diagnose: wie gleichmaessig und wie laut kommt das Mikrofon-Audio vom Browser an?

Sitzt direkt hinter transport.input() und schreibt alle REPORT_SECS eine Zeile ins Log:
Pegel (dBFS, Median/Spitze), Anzahl Luecken > GAP_MS zwischen Audio-Paketen und die laengste
Luecke. Damit lassen sich Browser vergleichen (Chrome gegen VS-Code-Vorschau): Luecken deuten auf
gedrosselte Tabs, Netz oder CPU; sehr leise Pegel auf Mikrofon- oder Verstaerkungsprobleme.
"""

from __future__ import annotations

import math
import statistics
import time
from array import array

from loguru import logger

from pipecat.frames.frames import Frame, InputAudioRawFrame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame

REPORT_SECS = 10.0
GAP_MS = 60.0


class AudioInputMonitor(FrameProcessor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._reset(time.monotonic())

    def _reset(self, now: float):
        self._window_start = now
        self._last = None
        self._gaps = 0
        self._max_gap_ms = 0.0
        self._levels: list[float] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            now = time.monotonic()
            if self._last is not None:
                gap_ms = (now - self._last) * 1000
                expected_ms = len(frame.audio) / 2 / frame.sample_rate * 1000  # 16-bit mono
                if gap_ms > max(GAP_MS, expected_ms * 3):
                    self._gaps += 1
                    self._max_gap_ms = max(self._max_gap_ms, gap_ms)
            self._last = now
            self._levels.append(_dbfs(frame.audio))
            if now - self._window_start >= REPORT_SECS:
                self._report(now)
        await self.push_frame(frame, direction)

    def _report(self, now: float):
        if self._levels:
            logger.info(
                "Mikrofon: Pegel Median {:.0f} dBFS, Spitze {:.0f} dBFS | Luecken >{:.0f} ms: {} "
                "(laengste {:.0f} ms) | {} Pakete in {:.0f} s",
                statistics.median(self._levels), max(self._levels), GAP_MS, self._gaps,
                self._max_gap_ms, len(self._levels), now - self._window_start,
            )
        last = self._last
        self._reset(now)
        self._last = last


class SpeakerTracker(FrameProcessor):
    """Sitzt hinter der Spracherkennung und meldet der Oberflaeche, wer gerade spricht.

    Soniox liefert pro Wort eine Sprecher-Kennung ("1", "2", ...). Ohne Stimmabdruck gilt: wer in der
    Sitzung zuerst spricht, ist Nikolaj (Index 0), weitere Stimmen bekommen Index 1, 2, ...
    Der Stimmen-Rand der Desktop-App faerbt sich danach.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._order: dict[str, int] = {}
        self._current: int | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)):
            speaker = _last_speaker(frame.result)
            if speaker is not None:
                index = self._order.setdefault(speaker, len(self._order))
                if index != self._current:
                    self._current = index
                    await self.push_frame(RTVIServerMessageFrame(data={"type": "sprecher", "index": index}))
        await self.push_frame(frame, direction)


def _last_speaker(result) -> str | None:
    if not isinstance(result, list):
        return None
    for token in reversed(result):
        if isinstance(token, dict) and token.get("speaker"):
            return str(token["speaker"])
    return None


def _dbfs(pcm: bytes) -> float:
    samples = array("h", pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return -120.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return 20 * math.log10(rms / 32768) if rms > 0 else -120.0
