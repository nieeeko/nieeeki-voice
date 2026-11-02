"""Tests fuer voiceprint.py: Rechenwege rund um den Stimmabdruck.

Ausfuehren:  uv run python -m unittest -v test_voiceprint.py   (im Ordner voice-core/)

Der letzte Test braucht das echte Modell (.cache/sprecher/, laedt der Voice-Core beim ersten Start)
und wird sonst uebersprungen.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from voiceprint import (
    DIM,
    MODEL_PATH,
    RETRY_SECS,
    VoiceModel,
    centroid,
    cosine,
    enroll_chunks,
    from_vector,
    normalize,
    speech_blocks,
    to_vector,
)

RATE = 16000


def tone(secs: float, amplitude: int = 8000) -> bytes:
    t = np.arange(int(RATE * secs)) / RATE
    return (np.sin(2 * np.pi * 220 * t) * amplitude).astype(np.int16).tobytes()


def silence(secs: float) -> bytes:
    return bytes(int(RATE * secs) * 2)


class VectorTests(unittest.TestCase):
    def test_hin_und_zurueck_ueber_die_datenbank(self):
        v = normalize(np.random.default_rng(1).normal(size=DIM).astype(np.float32))
        back = from_vector(to_vector(v))
        self.assertIsNotNone(back)
        self.assertAlmostEqual(cosine(v, back), 1.0, places=5)

    def test_liste_wird_auch_gelesen(self):
        self.assertIsNotNone(from_vector([0.1] * DIM))

    def test_falsche_laenge_wird_abgelehnt(self):
        self.assertIsNone(from_vector("[0.1,0.2]"))
        self.assertIsNone(from_vector(None))

    def test_mittelwert_ist_normiert_und_misst_die_streuung(self):
        a = np.zeros(DIM, dtype=np.float32)
        a[0] = 1
        center, spread = centroid([a, a, a])
        self.assertAlmostEqual(float(np.linalg.norm(center)), 1.0, places=5)
        self.assertAlmostEqual(spread, 1.0, places=5)
        b = np.zeros(DIM, dtype=np.float32)
        b[1] = 1
        self.assertLess(centroid([a, b])[1], 0.8)


class AudioTests(unittest.TestCase):
    def test_pausen_fallen_weg(self):
        pcm = tone(1.0) + silence(2.0) + tone(1.0)
        self.assertAlmostEqual(len(speech_blocks(pcm, RATE)) / 2 / RATE, 2.0, places=1)

    def test_nur_stille_ergibt_nichts(self):
        self.assertEqual(speech_blocks(silence(1.0), RATE), b"")

    def test_stuecke_gleich_lang_rest_faellt_weg(self):
        chunks = enroll_chunks(tone(10.0), RATE)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) == RATE * 3 * 2 for c in chunks))


class ModelLoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_falsche_datei_wird_geloescht_und_spaeter_neu_versucht(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "modell.onnx"
            path.write_bytes(b"ausgetauscht")
            now = [100.0]
            model = VoiceModel(path, sha256="0" * 64, clock=lambda: now[0])
            self.assertIsNone(await model.get())
            self.assertFalse(path.exists())
            with mock.patch("voiceprint.download", mock.AsyncMock()) as download:
                self.assertIsNone(await model.get())  # gleich danach: kein neuer Versuch
                download.assert_not_awaited()
                now[0] += RETRY_SECS + 1
                await model.get()
                download.assert_awaited_once()


@unittest.skipUnless(MODEL_PATH.is_file(), "Modell fehlt (.cache/sprecher/)")
class ModelTests(unittest.TestCase):
    def test_abdruck_hat_192_werte_und_ist_stabil(self):
        from voiceprint import Embedder

        model = Embedder()
        pcm = tone(3.0) + bytes(np.random.default_rng(2).integers(-800, 800, RATE * 2, dtype=np.int16).tobytes())
        a, b = model.embed(pcm, RATE), model.embed(pcm, RATE)
        self.assertEqual(a.shape, (DIM,))
        self.assertAlmostEqual(float(np.linalg.norm(a)), 1.0, places=4)
        self.assertAlmostEqual(cosine(a, b), 1.0, places=4)


if __name__ == "__main__":
    unittest.main()
