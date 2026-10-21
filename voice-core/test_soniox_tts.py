"""Tests fuer soniox_tts.py: jeder Satz geht mit Leerzeichen am Ende an Soniox.

Ausfuehren:  uv run python -m unittest -v test_soniox_tts.py   (im Ordner voice-core/)
Ohne Trenner las Soniox "Nutzung.Ich" oft als "Nutzung Punkt Ich" (Test 21.10.: 20 von 30 Saetzen).
"""

import unittest

from pipecat.services.soniox.tts import SonioxTTSService

from soniox_tts import spoken_text, voice_tuning


class LeerzeichenTests(unittest.TestCase):
    def test_satz_bekommt_leerzeichen(self):
        self.assertEqual(spoken_text("Ich klicke auf Nutzung."), "Ich klicke auf Nutzung. ")
        self.assertEqual(spoken_text("Soll ich?"), "Soll ich? ")

    def test_nichts_doppelt(self):
        for text in ["Fertig. ", "Fertig.\n", ""]:
            with self.subTest(text=text):
                self.assertEqual(spoken_text(text), text)

    def test_zusammengesetzt_immer_getrennt(self):
        joined = "".join(spoken_text(s) for s in ["Ich klicke auf Nutzung.", "Ich bin dran."])
        self.assertNotIn(".I", joined)


class StellschraubenTests(unittest.TestCase):
    def test_ohne_eintrag_soniox_standard(self):
        self.assertEqual(voice_tuning(None, None), {})
        self.assertEqual(voice_tuning("", ""), {})

    def test_tempo_im_bereich(self):
        for raw, expected in [("1.1", 1.1), ("1,1", 1.1), ("0.7", 0.7), ("1.3", 1.3)]:
            with self.subTest(raw=raw):
                self.assertEqual(voice_tuning(raw, None), {"speed": expected})

    def test_tempo_ausserhalb_oder_kaputt_wird_ignoriert(self):
        for raw in ["1.5", "0.5", "schnell", "nan", "inf"]:
            with self.subTest(raw=raw):
                self.assertEqual(voice_tuning(raw, None), {})

    def test_pausen_schalter(self):
        for raw, expected in [("1", {"reduce_silence": True}), ("ja", {"reduce_silence": True}),
                              ("0", {"reduce_silence": False}), ("vielleicht", {})]:
            with self.subTest(raw=raw):
                self.assertEqual(voice_tuning(None, raw), expected)

    def test_passt_in_pipecat_settings(self):
        settings = SonioxTTSService.Settings(voice="Annika", **voice_tuning("1.1", "1"))
        self.assertEqual((settings.speed, settings.reduce_silence), (1.1, True))


if __name__ == "__main__":
    unittest.main()
