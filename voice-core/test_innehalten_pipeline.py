"""Zusammenspiel in einer echten Pipecat-Pipeline: Gate, Ausgang (MediaSender), AudioHold, Tracker.

Ausfuehren:  uv run python -m unittest -v test_innehalten_pipeline.py   (im Ordner voice-core/, ~6 s)

Der Ausgang taktet wie die WebRTC-Spur: jeder 40-ms-Happen braucht seine Spielzeit. Die Sprachausgabe
ist nachgebaut wie bei Pipecat/Soniox: Ton schneller als Echtzeit, Woerter mit pts = erster Ton + Versatz.
"""

import asyncio
import time
import unittest

from pipecat.frames.frames import (
    EndFrame,
    Frame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

from playback import AudioHold, PlaybackStart
from soft_interrupt import PlaybackTracker, SoftInterruptGate

RATE = 24000
CHUNK = b"\x10\x27" * 960  # 40 ms Ton (nicht still)
CHUNKS_PER_WORD = 5  # jedes Wort 0,2 s


class PacedOutput(BaseOutputTransport):
    def __init__(self):
        super().__init__(TransportParams(audio_out_enabled=True, audio_out_sample_rate=RATE))
        self.log: list[float] = []  # Zeitpunkt jedes geschriebenen Happens

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame) -> bool:
        self.log.append(time.monotonic())
        await asyncio.sleep(len(frame.audio) / (RATE * 2))
        return True


class FakeTTS(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    async def speak(self, words: list[str], gap_after: dict[int, float] | None = None):
        gap_after = gap_after or {}
        base = None
        for i, text in enumerate(words):
            for _ in range(CHUNKS_PER_WORD):
                await self.push_frame(TTSAudioRawFrame(audio=CHUNK, sample_rate=RATE, num_channels=1))
                if base is None:
                    base = self.get_clock().get_time()
            word = TTSTextFrame(text=text, aggregated_by="word")
            word.pts = base + i * CHUNKS_PER_WORD * 40_000_000
            await self.push_frame(word)
            if i in gap_after:
                await asyncio.sleep(gap_after[i])  # Soniox liefert langsamer, als gesprochen wird
        await self.push_frame(TTSStoppedFrame())


class Collector(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.words: list[tuple[float, str]] = []
        self.started = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.started.set()
        if isinstance(frame, TTSTextFrame):
            self.words.append((time.monotonic(), frame.text))
        await self.push_frame(frame, direction)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.output = PacedOutput()
        self.hold = AudioHold()
        self.hold.install(self.output)
        self.gate = SoftInterruptGate(hold=self.hold, resume_after_secs=0.5, max_pause_secs=2.0)
        self.tts = FakeTTS()
        self.collector = Collector()
        self.task = PipelineTask(
            Pipeline(
                [
                    self.gate,
                    self.tts,
                    PlaybackStart(self.hold),
                    self.output,
                    PlaybackTracker(self.gate, self.hold),
                    self.collector,
                ]
            ),
            params=PipelineParams(audio_out_sample_rate=RATE),
            enable_rtvi=False,
        )
        self.runner = asyncio.create_task(PipelineRunner(handle_sigint=False).run(self.task))
        await asyncio.wait_for(self.collector.started.wait(), 5)

    async def asyncTearDown(self):
        await self.task.queue_frame(EndFrame())
        await asyncio.wait_for(self.runner, 10)

    def word_time(self, text):
        return next(t for t, w in self.collector.words if w == text)

    async def test_woerter_kommen_mit_ihrem_ton_auch_wenn_soniox_stockt(self):
        words = ["Eins", "zwei", "drei", "vier"]
        # Nach "zwei" 0,8 s nichts von Soniox: der Ton steht 0,4 s still, die Uhr laeuft weiter.
        await self.tts.speak(words, gap_after={1: 0.8})
        await asyncio.sleep(1.4)
        self.assertEqual([w for _, w in self.collector.words], words)
        vier_ton = self.output.log[3 * CHUNKS_PER_WORD]  # erster Happen von "vier"
        # Nach der Uhr kaeme "vier" 0,2 s vor seinem Ton (ohne Abgleich gemessen: -0,216 s);
        # gemessen am geschriebenen Ton nur Takt-Ungenauigkeit (Windows-Timer ~16 ms)
        self.assertLess(abs(self.word_time("vier") - vier_ton), 0.1)

    async def test_innehalten_und_an_derselben_stelle_weiter(self):
        words = ["Ich", "lege", "das", "Repository", "jetzt", "an."]
        speaking = asyncio.create_task(self.tts.speak(words))
        await asyncio.sleep(0.3)
        self.gate.hint(" Danke")  # ein Wort von Nikolaj, sonst nichts
        await speaking
        await asyncio.sleep(2.2)
        self.assertEqual([w for _, w in self.collector.words], words)  # nichts verloren
        self.assertEqual(len(self.output.log), len(words) * CHUNKS_PER_WORD)
        gaps = [(b - a, i + 1) for i, (a, b) in enumerate(zip(self.output.log, self.output.log[1:]))]
        longest, at = max(gaps)
        self.assertGreater(longest, 0.35)  # sie hat innegehalten ...
        self.assertEqual(at % CHUNKS_PER_WORD, 0)  # ... genau an einer Wortgrenze
        pause_end = self.output.log[at]
        resumed_word = words[at // CHUNKS_PER_WORD]
        self.assertGreaterEqual(self.word_time(resumed_word), pause_end - 0.01)

    async def test_zweites_wort_waehrend_sie_innehaelt_bricht_ab(self):
        words = ["Ich", "lege", "das", "Repository", "jetzt", "an."]
        speaking = asyncio.create_task(self.tts.speak(words))
        await asyncio.sleep(0.3)
        self.gate.hint(" Das")
        await asyncio.sleep(0.25)
        self.assertTrue(self.hold.paused)
        await self.task.queue_frame(UserStartedSpeakingFrame())  # "Das ist falsch": Einwurf
        await speaking
        await asyncio.sleep(1.5)
        spoken = [w for _, w in self.collector.words]
        self.assertEqual(spoken, words[: len(spoken)])
        self.assertLessEqual(len(spoken), 3)  # nur, was vor dem Innehalten hinausging
        self.assertLessEqual(len(self.output.log), len(spoken) * CHUNKS_PER_WORD + 1)
        self.assertFalse(self.hold.paused)  # offen fuer die naechste Antwort


if __name__ == "__main__":
    unittest.main()
