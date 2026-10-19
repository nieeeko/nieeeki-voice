"""Tests fuer screen_stream.py: Aufnahme in fester Groesse, Platzhalter bei gesperrtem Laptop.

Ausfuehren:  uv run python -m unittest -v test_screen_stream.py   (im Ordner voice-core/)
Nimmt einmal echt den Bildschirm auf (nur im Speicher, nichts wird gespeichert oder gesendet).
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from pipecat.frames.frames import OutputImageRawFrame
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.tests.utils import SleepFrame, run_test

import screen_stream as s

FRAME_BYTES = s.WIDTH * s.HEIGHT * 3


class GrabberTests(unittest.TestCase):
    def test_echte_aufnahme_hat_feste_groesse(self):
        self.assertEqual(len(s._Grabber().grab()), FRAME_BYTES)

    def test_gesperrt_zeigt_platzhalter_ohne_aufnahme(self):
        grabber = s._Grabber()
        with mock.patch.object(s, "_desktop_locked", return_value=True):
            frame = grabber.grab()
        self.assertEqual(len(frame), FRAME_BYTES)
        self.assertIsNone(grabber._sct)  # mss wurde gar nicht erst angelegt

    def test_hochformat_wird_eingepasst(self):
        # Ein hoher, schmaler Monitor (z. B. gedreht) muss mittig mit Rand landen, nicht verzerrt.
        grabber = s._Grabber()
        shot = SimpleNamespace(size=(1080, 1920), bgra=b"\xff" * (1080 * 1920 * 4))
        grabber._sct = SimpleNamespace(grab=lambda monitor: shot, monitors=[{}, {}])
        with mock.patch.object(s, "_desktop_locked", return_value=False), \
             mock.patch.object(s, "_foreground_monitor", return_value={}):
            frame = grabber.grab()
        self.assertEqual(len(frame), FRAME_BYTES)
        self.assertEqual(frame[:3], b"\x00\x00\x00")  # links oben: schwarzer Rand
        mid = (s.HEIGHT // 2 * s.WIDTH + s.WIDTH // 2) * 3
        self.assertEqual(frame[mid:mid + 3], b"\xff\xff\xff")  # Mitte: das Bild


class StreamerPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_liefert_bilder_aus_liefert_keine(self):
        streamer = s.ScreenStreamer()
        log: list[str] = []

        async def bedienen():
            while not streamer._running:
                await asyncio.sleep(0.02)
            await streamer.set_active(True, "test")
            await asyncio.sleep(0.8)
            await streamer.set_active(False, "test")
            log.append("aus")

        knopf = asyncio.create_task(bedienen())
        down, _ = await run_test(streamer, frames_to_send=[SleepFrame(sleep=1.6)])
        await knopf

        bilder = [f for f in down if isinstance(f, OutputImageRawFrame)]
        meldungen = [f.data["aktiv"] for f in down if isinstance(f, RTVIServerMessageFrame)]
        self.assertGreaterEqual(len(bilder), 2)
        self.assertTrue(all(f.size == (s.WIDTH, s.HEIGHT) and len(f.image) == FRAME_BYTES for f in bilder))
        self.assertEqual(meldungen, [True, False])
        # Nach "aus" kommt kein Bild mehr: das letzte Bild liegt vor der Aus-Meldung
        aus_index = max(i for i, f in enumerate(down) if isinstance(f, RTVIServerMessageFrame))
        self.assertFalse(any(isinstance(f, OutputImageRawFrame) for f in down[aus_index:]))
        self.assertFalse(streamer.active)


def _geo_links():
    """2560x1440-Monitor links vom Hauptmonitor, auf 1280x720 verkleinert, ohne Rand."""
    return s.FrameGeometry(left=-2560, top=0, scale=0.5, off_x=0, off_y=0, width=1280, height=720)


def _laufender_streamer(antwort='{"status": "success", "summary": "Klick in Chrome"}'):
    streamer = s.ScreenStreamer()
    streamer._task = object()  # Stream laeuft
    streamer._grabber.geometry = _geo_links()
    streamer.push_frame = mock.AsyncMock()
    host = SimpleNamespace(_call_tool_text=mock.AsyncMock(return_value=antwort))
    return streamer, host


class FernklickTests(unittest.IsolatedAsyncioTestCase):
    def test_umrechnung(self):
        self.assertEqual(s.frame_to_screen(_geo_links(), 0.5, 0.5), (-1280, 720))
        hoch = s.FrameGeometry(0, 0, 720 / 1920, 437, 0, 405, 720)  # Hochformat mittig mit Rand
        self.assertEqual(s.frame_to_screen(hoch, 0.5, 0.5), (541, 960))
        self.assertIsNone(s.frame_to_screen(hoch, 0.05, 0.5))  # schwarzer Rand
        self.assertIsNone(s.frame_to_screen(None, 0.5, 0.5))  # noch kein Bild / gesperrt
        self.assertIsNone(s.frame_to_screen(_geo_links(), 1.5, 0.5))

    async def test_tippen_klickt_ueber_den_host(self):
        streamer, host = _laufender_streamer()
        result = await streamer.click(0.25, 0.5, host)
        host._call_tool_text.assert_awaited_once_with("maus_klicken", {"x": -1920, "y": 720})
        self.assertEqual(result["status"], "success")
        meldung = streamer.push_frame.await_args.args[0].data
        self.assertEqual((meldung["type"], meldung["name"], meldung["status"]), ("aktivitaet", "fernklick", "fertig"))
        # Doppeltippen direkt danach wird kein zweiter Klick
        self.assertEqual((await streamer.click(0.25, 0.5, host))["status"], "ignored")
        host._call_tool_text.assert_awaited_once()

    async def test_host_lehnt_ab(self):
        streamer, host = _laufender_streamer('{"status": "locked", "summary": "Der Laptop ist gesperrt."}')
        await streamer.click(0.5, 0.5, host)
        self.assertEqual(streamer.push_frame.await_args.args[0].data["status"], "abgelehnt")

    async def test_ohne_stream_kein_klick(self):
        streamer, host = _laufender_streamer()
        streamer._task = None
        result = await streamer.click(0.5, 0.5, host)
        host._call_tool_text.assert_not_awaited()
        self.assertEqual(result["status"], "rejected")

    async def test_unsinnige_stellen_klicken_nicht(self):
        for fx, fy in [("x", 0.5), (None, 0.5), (float("nan"), 0.5), (2, 0.5), (0.5, -1)]:
            with self.subTest(fx=fx, fy=fy):
                streamer, host = _laufender_streamer()
                result = await streamer.click(fx, fy, host)
                host._call_tool_text.assert_not_awaited()
                self.assertEqual(result["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
