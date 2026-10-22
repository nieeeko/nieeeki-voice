"""Tests fuer consent.py: Ein Ja auf nieekis Rueckfrage wird markiert, damit sie ausfuehrt statt nachzufragen.

Ausfuehren:  uv run python -m unittest -v test_consent.py   (im Ordner voice-core/)
"""

import unittest

from consent import CONSENT_NOTE, consent_index, mark_consent

FRAGE = {"role": "assistant", "content": "● Also: ein Repository namens Test anlegen und klonen. Soll ich das so machen?"}


class Context:
    def __init__(self, messages):
        self.messages = messages

    def set_messages(self, messages):
        self.messages = messages


class ConsentTests(unittest.TestCase):
    def test_ja_auf_frage_wird_markiert(self):
        for antwort in ["Ja.", "Ja, mach das.", "Jo", "Okay, leg los", "Genau so", "Starte.", "Ja. Ja, klone es dahin."]:
            with self.subTest(antwort=antwort):
                ctx = Context([FRAGE, {"role": "user", "content": antwort}])
                self.assertTrue(mark_consent(ctx))
                self.assertIn(CONSENT_NOTE, ctx.messages[-1]["content"])
                self.assertTrue(ctx.messages[-1]["content"].startswith(antwort))

    def test_nein_und_neue_auftraege_nicht(self):
        for antwort in ["Nein, ich habe irgendwo einen Ordner.", "Ja, aber nicht dort.", "Warte mal",
                        "Leg ein Repository an und klone es in VS Code und öffne es danach gleich mit Claude",
                        "Wie geht's dir?", ""]:
            with self.subTest(antwort=antwort):
                self.assertIsNone(consent_index([FRAGE, {"role": "user", "content": antwort}]))

    def test_nur_nach_einer_frage(self):
        aussage = {"role": "assistant", "content": "● Erledigt, das Repository liegt im Projekte-Ordner."}
        self.assertIsNone(consent_index([aussage, {"role": "user", "content": "Ja."}]))

    def test_systemhinweise_sind_kein_ja(self):
        hinweis = "[PC-Freigabe: Nikolaj hat die Aktion freigegeben. Ja ...]"
        self.assertIsNone(consent_index([FRAGE, {"role": "user", "content": hinweis}]))

    def test_nicht_doppelt(self):
        ctx = Context([FRAGE, {"role": "user", "content": "Ja."}])
        mark_consent(ctx)
        self.assertFalse(mark_consent(ctx))
        self.assertEqual(ctx.messages[-1]["content"].count(CONSENT_NOTE), 1)

    def test_inhaltsbloecke_und_werkzeugaufruf_ohne_text(self):
        werkzeug = {"role": "assistant", "content": [{"type": "tool_use", "name": "x", "input": {}}]}
        ctx = Context([FRAGE, werkzeug, {"role": "user", "content": [{"type": "text", "text": "Ja klar"}]}])
        self.assertTrue(mark_consent(ctx))
        self.assertEqual(ctx.messages[-1]["content"][-1]["text"], CONSENT_NOTE)

    def test_werkzeugergebnis_dazwischen_ist_keine_offene_frage(self):
        ergebnis = {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}
        self.assertIsNone(consent_index([FRAGE, ergebnis, {"role": "assistant", "content": ""},
                                         {"role": "user", "content": "Ja"}]))


if __name__ == "__main__":
    unittest.main()
