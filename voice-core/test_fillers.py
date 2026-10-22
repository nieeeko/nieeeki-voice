"""Tests fuer fillers.py: "Moment." statt Stille, wenn ein Werkzeug laeuft, und das optionale "Mhm".

Ausfuehren:  uv run python -m unittest -v test_fillers.py   (im Ordner voice-core/)
"""

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMFullResponseStartFrame,
    LLMMarkerFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from fillers import (
    CHUNK_BYTES,
    LONG_PHRASES,
    MHM_PHRASES,
    TOOL_PHRASES,
    FillerBank,
    FillerPlayer,
    fit_chunks,
    trim_silence,
)


def tone(ms_quiet_before=200, ms_loud=300, ms_quiet_after=200, rate=24000):
    quiet = lambda ms: np.zeros(rate * ms // 1000, dtype=np.int16)  # noqa: E731
    loud = np.full(rate * ms_loud // 1000, 8000, dtype=np.int16)
    return np.concatenate([quiet(ms_quiet_before), loud, quiet(ms_quiet_after)]).tobytes()


class AufnahmeTests(unittest.IsolatedAsyncioTestCase):
    def test_stille_vorn_und_hinten_weg(self):
        trimmed = trim_silence(tone(), head_ms=30, tail_ms=80)
        self.assertEqual(len(trimmed) // 2, 24000 * (30 + 300 + 80) // 1000)
        self.assertEqual(trim_silence(bytes(4800)), b"")

    def test_ganze_happen(self):
        self.assertEqual(len(fit_chunks(bytes(100))), CHUNK_BYTES)
        self.assertEqual(len(fit_chunks(bytes(CHUNK_BYTES))), CHUNK_BYTES)

    async def test_einmal_aufnehmen_dann_aus_dem_cache(self):
        calls = []

        async def synth(text):
            calls.append(text)
            if text == "Sekunde.":
                raise RuntimeError("Netz weg")
            return tone()

        with tempfile.TemporaryDirectory() as tmp:
            bank = FillerBank("Lena", {"speed": 1.1}, cache_dir=Path(tmp), phrases=TOOL_PHRASES)
            await bank.prepare(synth)
            self.assertTrue(bank.ready)
            self.assertEqual(sorted(calls), sorted(TOOL_PHRASES))
            # Neuer Prozess: alles Gespeicherte kommt aus dem Cache, nur das Fehlende neu
            calls.clear()
            again = FillerBank("Lena", {"speed": 1.1}, cache_dir=Path(tmp), phrases=TOOL_PHRASES)
            await again.prepare(synth)
            self.assertEqual(calls, ["Sekunde."])
            # Andere Stimme: eigene Aufnahmen
            other = FillerBank("Jonas", cache_dir=Path(tmp), phrases=TOOL_PHRASES)
            self.assertEqual(len(other.load_cached()), len(TOOL_PHRASES))

    def test_nicht_zweimal_hintereinander_dasselbe(self):
        bank = FillerBank("Lena", phrases=TOOL_PHRASES)
        bank._audio = {p: b"x" for p in TOOL_PHRASES}
        picks = [bank.pick(TOOL_PHRASES)[0] for _ in range(20)]
        self.assertTrue(all(a != b for a, b in zip(picks, picks[1:])))


def ready_bank():
    bank = FillerBank("Lena")
    bank._audio = {p: bytes(CHUNK_BYTES) for p in TOOL_PHRASES + LONG_PHRASES + MHM_PHRASES}
    return bank


def tool_start(i="1"):
    return FunctionCallInProgressFrame(function_name="programm_oeffnen", tool_call_id=i, arguments={})


def tool_result(i="1"):
    return FunctionCallResultFrame(function_name="programm_oeffnen", tool_call_id=i, arguments={}, result="ok")


class PlayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        p = mock.patch.object(FrameProcessor, "process_frame", new=mock.AsyncMock())
        p.start()
        self.addCleanup(p.stop)
        self.player = self.make()

    def make(self, **kwargs):
        options = dict(tool_after_secs=0.02, long_wait_secs=0.1, rng=lambda: 0.0)
        player = FillerPlayer(ready_bank(), **{**options, **kwargs})
        player.played = []

        async def push(frame, direction=FrameDirection.DOWNSTREAM):
            if isinstance(frame, OutputAudioRawFrame):
                player.played.append(frame)

        async def cancel(task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        player.push_frame = push
        player.create_task = lambda coro, name=None: asyncio.get_running_loop().create_task(coro)
        player.cancel_task = cancel
        return player

    async def send(self, *frames, player=None):
        for frame in frames:
            direction = FrameDirection.UPSTREAM if isinstance(frame, BotStartedSpeakingFrame) else FrameDirection.DOWNSTREAM
            await (player or self.player).process_frame(frame, direction)

    async def test_werkzeug_ohne_ansage_bekommt_ein_moment(self):
        await self.send(UserStartedSpeakingFrame(), LLMFullResponseStartFrame(), tool_start())
        await asyncio.sleep(0.06)
        self.assertEqual(len(self.player.played), 1)
        await self.send(tool_result())

    async def test_hat_sie_schon_etwas_gesagt_kein_fuellwort(self):
        await self.send(UserStartedSpeakingFrame(), LLMFullResponseStartFrame(), LLMTextFrame("Klar, mach ich."), tool_start())
        await asyncio.sleep(0.06)
        self.assertEqual(self.player.played, [])
        await self.send(tool_result())

    async def test_spricht_sie_gerade_kein_fuellwort(self):
        await self.send(UserStartedSpeakingFrame(), tool_start(), BotStartedSpeakingFrame())
        await asyncio.sleep(0.06)
        self.assertEqual(self.player.played, [])
        await self.send(tool_result())

    async def test_neuer_satz_von_nikolaj_bricht_das_warten_ab(self):
        await self.send(UserStartedSpeakingFrame(), tool_start(), UserStartedSpeakingFrame())
        await asyncio.sleep(0.06)
        self.assertEqual(self.player.played, [])

    async def test_langes_werkzeug_bekommt_einmal_dauert_noch(self):
        await self.send(UserStartedSpeakingFrame(), tool_start())
        await asyncio.sleep(0.7)
        self.assertEqual(len(self.player.played), 2)  # "Moment." und einmal "Dauert noch ..."
        await self.send(tool_result())

    async def test_ohne_schalter_kein_werkzeug_fuellwort(self):
        player = self.make(tool_fill=False)
        await self.send(UserStartedSpeakingFrame(), tool_start(), player=player)
        await asyncio.sleep(0.06)
        self.assertEqual(player.played, [])

    async def test_mhm_nur_wenn_eingeschaltet_selten_und_nie_ins_wort(self):
        await self.send(LLMMarkerFrame("◐"))
        self.assertEqual(self.player.played, [])  # Standard: aus
        player = self.make(mhm=True, mhm_cooldown_secs=10)
        await self.send(LLMMarkerFrame("◐"), LLMMarkerFrame("◐"), player=player)
        self.assertEqual(len(player.played), 1)  # Abklingzeit
        talking = self.make(mhm=True)
        await self.send(VADUserStartedSpeakingFrame(), LLMMarkerFrame("◐"), player=talking)
        self.assertEqual(talking.played, [])  # er spricht schon weiter


if __name__ == "__main__":
    unittest.main()
