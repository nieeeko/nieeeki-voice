"""Tests fuer soft_interrupt.py: wann nieeeki sich unterbrechen laesst und wo sie aufhoert.

Ausfuehren:  uv run python -m unittest -v test_soft_interrupt.py   (im Ordner voice-core/)
21.10.: Ein einziges Wortstueck (" Al", " Du") unterbrach sie, und dann sprach sie noch bis zum
Satzende oder drei Woerter weiter (bis 2,5 s ueber Nikolaj hinweg).
"""

import unittest
from unittest import mock

from pipecat.frames.frames import BotStartedSpeakingFrame, InterimTranscriptionFrame

from soft_interrupt import (
    MeaningfulWordsUserTurnStartStrategy,
    SoftInterruptGate,
    is_stop_request,
    meaningful_word_count,
)


def interim(text):
    return InterimTranscriptionFrame(text=text, user_id="u", timestamp="")


class WortTests(unittest.TestCase):
    def test_rueckmeldungen_zaehlen_nicht(self):
        self.assertEqual(meaningful_word_count("mhm ja genau okay"), 0)
        self.assertEqual(meaningful_word_count(" Al"), 1)
        self.assertEqual(meaningful_word_count("Leg das Repository an"), 4)

    def test_sofort_woerter(self):
        for text in ["Stopp!", "warte mal", "Nein.", "Moment", "halt"]:
            with self.subTest(text=text):
                self.assertTrue(is_stop_request(text))
        for text in ["ja genau", "Leg an", "mhm", ""]:
            with self.subTest(text=text):
                self.assertFalse(is_stop_request(text))


class StartTests(unittest.IsolatedAsyncioTestCase):
    async def strategy(self):
        s = MeaningfulWordsUserTurnStartStrategy(min_words=2, enable_interruptions=False)
        await s.process_frame(BotStartedSpeakingFrame())
        return s

    async def triggered(self, text):
        s = await self.strategy()
        with mock.patch.object(s, "trigger_user_turn_started") as started, \
             mock.patch.object(s, "trigger_reset_aggregation"):
            await s.process_frame(interim(text))
        return started.await_count > 0

    async def test_ein_wortstueck_unterbricht_nicht(self):
        for text in [" Al", " Du", " Leg", "mhm", "ja genau", "okay okay"]:
            with self.subTest(text=text):
                self.assertFalse(await self.triggered(text))

    async def test_zwei_echte_woerter_oder_stopp(self):
        for text in [" Leg das", "Du hast", "Stopp", " warte", "Nein"]:
            with self.subTest(text=text):
                self.assertTrue(await self.triggered(text))


class GrenzeTests(unittest.TestCase):
    def test_stoppt_an_der_naechsten_wortgrenze(self):
        gate = SoftInterruptGate(playback_lag_secs=0)  # Standard: max_words=0
        gate.on_word_played("Ich")
        with mock.patch.object(gate, "_start_fire") as fire:
            gate._arm()
            cut = gate.on_word_played("lege")  # das naechste Wort beginnt: hier ist Schluss
        fire.assert_called_once()
        self.assertTrue(cut)


if __name__ == "__main__":
    unittest.main()
