"""Tests fuer playback.py: Innehalten am WebRTC-Ausgang und die echte Abspielposition.

Ausfuehren:  uv run python -m unittest -v test_playback.py   (im Ordner voice-core/)
"""

import asyncio
import unittest

from pipecat.frames.frames import OutputAudioRawFrame, TTSAudioRawFrame

from playback import DRAIN_SECS, AudioHold, audio_ns

CHUNK = bytes(1920)  # 40 ms bei 24 kHz, 16 Bit, mono


def tts_chunk():
    return TTSAudioRawFrame(audio=CHUNK, sample_rate=24000, num_channels=1)


class FakeOutput:
    def __init__(self):
        self.written = []

    async def write_audio_frame(self, frame):
        self.written.append(frame)
        return True


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class AudioHoldTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.hold = AudioHold(clock_ns=lambda: 5_000_000_000, monotonic=self.clock)
        self.out = FakeOutput()
        self.assertTrue(self.hold.install(self.out))

    def test_dauer_eines_happens(self):
        self.assertEqual(audio_ns(tts_chunk()), 40_000_000)

    async def test_position_zaehlt_nur_sprachausgabe(self):
        self.assertIsNone(self.hold.played_until_ns())
        await self.out.write_audio_frame(tts_chunk())
        await self.out.write_audio_frame(tts_chunk())
        # Fuellwoerter (einfacher Ton) verschieben die Wort-Zeitstempel nicht
        await self.out.write_audio_frame(OutputAudioRawFrame(audio=CHUNK, sample_rate=24000, num_channels=1))
        self.assertEqual(self.hold.played_until_ns(), 5_000_000_000 + 80_000_000)
        self.assertEqual(len(self.out.written), 3)

    async def test_pause_haelt_den_naechsten_happen_zurueck(self):
        self.hold.pause()
        write = asyncio.create_task(self.out.write_audio_frame(tts_chunk()))
        await asyncio.sleep(0.05)
        self.assertFalse(write.done())
        self.assertEqual(self.out.written, [])
        self.hold.resume()
        self.assertTrue(await write)
        self.assertEqual(len(self.out.written), 1)

    async def test_nach_abbruch_wird_der_rest_verworfen(self):
        await self.out.write_audio_frame(tts_chunk())
        self.hold.pause()
        write = asyncio.create_task(self.out.write_audio_frame(tts_chunk()))
        await asyncio.sleep(0)
        self.hold.cut()
        self.assertFalse(await write)  # gehoert zur abgebrochenen Antwort
        self.assertIsNone(self.hold.played_until_ns())  # naechste Antwort zaehlt neu
        self.clock.t += DRAIN_SECS + 0.01
        self.assertTrue(await self.out.write_audio_frame(tts_chunk()))
        self.assertEqual(len(self.out.written), 2)


if __name__ == "__main__":
    unittest.main()
