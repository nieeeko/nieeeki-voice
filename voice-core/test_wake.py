"""Tests fuer den Weckwort-Lauscher (wake.py): fortlaufende Merkmale wie im Training, Protokoll.

Ausfuehren:  uv run python -m unittest -v test_wake.py   (im Ordner voice-core/)
"""

import io
import json
import queue
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import wake
from persona import greeting_for


class BegruessungTests(unittest.TestCase):
    def test_per_weckwort_kurz(self):
        self.assertEqual(greeting_for({"anlass": "weckwort"}, "Hi Nikolaj, ich bin da.", "Ja?"), "Ja?")

    def test_sonst_normal(self):
        for body in [None, {}, {"anlass": "klick"}, "weckwort", ["weckwort"], {"anlass": ["weckwort"]}]:
            with self.subTest(body=body):
                self.assertEqual(greeting_for(body, "Hi", "Ja?"), "Hi")


class FortlaufendeMerkmaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.features = wake.StreamingFeatures()

    def test_gleich_wie_berechnung_am_stueck(self):
        rng = np.random.default_rng(1)
        n_blocks = 30
        audio = (rng.standard_normal(n_blocks * wake.BLOCK) * 3000).astype(np.int16)
        f = self.features
        f.reset()
        for k in range(n_blocks):
            out = f.feed(audio[k * wake.BLOCK:(k + 1) * wake.BLOCK])
        self.assertEqual(out.shape, (wake.N_EMBEDDINGS, 96))
        # Referenz: Mel ueber das ganze Audio (mit demselben Null-Vorlauf), Embeddings an denselben Stellen
        whole = np.concatenate([np.zeros(wake.MEL_CONTEXT, np.float32), audio.astype(np.float32) / 32768.0])
        mel = f.mel(whole)[0]
        self.assertEqual(mel.shape[0], 8 * n_blocks)
        ends = [8 * (k + 1) for k in range(n_blocks - wake.N_EMBEDDINGS, n_blocks)]
        expected = np.stack([f.embed(mel[e - wake.MEL_WINDOW:e][np.newaxis])[0] for e in ends])
        np.testing.assert_allclose(out, expected, atol=1e-4)

    def test_bewertet_erst_nach_zwei_sekunden(self):
        f = self.features
        f.reset()
        silence = np.zeros(wake.BLOCK, np.int16)
        results = [f.feed(silence) for _ in range(30)]
        first = next(i for i, r in enumerate(results) if r is not None)
        self.assertEqual(first, 24)  # 76 Mel-Zeilen nach 10 Bloecken, dann 16 Embeddings: 25 x 80 ms


class SchwelleTests(unittest.TestCase):
    def test_begleitdatei_und_ersatzwert(self):
        import tempfile

        d = Path(tempfile.mkdtemp())
        model = d / "m.onnx"
        self.assertEqual(wake.model_threshold(model), 0.5)  # keine Begleitdatei
        for content, expected in [('{"schwelle": 0.8}', 0.8), ('{"schwelle": 2}', 0.5), ('{"schwelle": "hoch"}', 0.5),
                                  ("kein json", 0.5), ("[0.8]", 0.5), ('{"anders": 1}', 0.5)]:
            with self.subTest(content=content):
                (d / "m.json").write_text(content, encoding="utf-8")
                self.assertEqual(wake.model_threshold(model), expected)

    def test_mitgeliefertes_modell(self):
        model = Path(wake.__file__).parent / "models" / "nieeeki.onnx"
        self.assertTrue(model.is_file())
        self.assertEqual(wake.model_threshold(model), 0.8)


class ProtokollTests(unittest.TestCase):
    def test_ohne_modell_dauerhafter_abbruch(self):
        out = io.StringIO()
        with mock.patch.object(wake, "MODEL_PATH", Path("gibt/es/nicht.onnx")), mock.patch("sys.stdout", out):
            self.assertEqual(wake.main(), 2)  # 2 = Desktop-App startet nicht neu
        self.assertIn("fehler", json.loads(out.getvalue()))

    def test_nur_bekannte_befehle_und_ende_bei_eof(self):
        commands = queue.Queue()
        with mock.patch("sys.stdin", io.StringIO("pause\nrm -rf /\nWEITER\n")):
            wake._read_commands(commands)
        self.assertEqual([commands.get_nowait() for _ in range(3)], ["pause", "weiter", "ende"])
        self.assertTrue(commands.empty())


if __name__ == "__main__":
    unittest.main()
