"""Tests fuer session_guard.py: nie zwei Gespraeche gleichzeitig.

Ausfuehren:  uv run python -m unittest -v test_session_guard.py   (im Ordner voice-core/)
"""

import unittest
from unittest import mock

from session_guard import SingleSession


def connection(fails=False):
    c = mock.Mock()
    c.disconnect = mock.AsyncMock(side_effect=RuntimeError("schon weg") if fails else None)
    return c


class SingleSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_neues_gespraech_trennt_das_alte(self):
        guard, a, b = SingleSession(), connection(), connection()
        await guard.take(a)
        await guard.take(b)
        a.disconnect.assert_awaited_once()
        b.disconnect.assert_not_awaited()
        self.assertIs(guard.current, b)

    async def test_erstes_gespraech_trennt_nichts(self):
        guard, a = SingleSession(), connection()
        await guard.take(a)
        a.disconnect.assert_not_awaited()

    async def test_altes_ende_gibt_neues_nicht_frei(self):
        guard, a, b = SingleSession(), connection(), connection()
        await guard.take(a)
        await guard.take(b)
        guard.release(a)  # das alte raeumt spaeter auf
        self.assertIs(guard.current, b)
        guard.release(b)
        self.assertIsNone(guard.current)

    async def test_kaputte_alte_verbindung_blockiert_nicht(self):
        guard, a, b = SingleSession(), connection(fails=True), connection()
        await guard.take(a)
        await guard.take(b)
        self.assertIs(guard.current, b)


if __name__ == "__main__":
    unittest.main()
