"""Tests fuer soniox_tts.py: jeder Satz geht mit Leerzeichen am Ende an Soniox.

Ausfuehren:  uv run python -m unittest -v test_soniox_tts.py   (im Ordner voice-core/)
Ohne Trenner las Soniox "Nutzung.Ich" oft als "Nutzung Punkt Ich" (Test 21.10.: 20 von 30 Saetzen).
"""

import unittest

from soniox_tts import spoken_text


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


if __name__ == "__main__":
    unittest.main()
