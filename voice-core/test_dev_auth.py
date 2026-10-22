"""Tests fuer den Claude-Abo-Token der Entwicklungsauftraege (dev_agent.take_oauth_token).

Ausfuehren:  uv run python -m unittest -v test_dev_auth.py   (im Ordner voice-core/)
"""

import os
import unittest
from unittest import mock

import dev_agent


class OAuthTokenTests(unittest.TestCase):
    def setUp(self):
        dev_agent._oauth_token = None

    def tearDown(self):
        dev_agent._oauth_token = None

    def test_token_verlaesst_die_umgebung_und_geht_nur_an_claude_code(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": " abc "}):
            self.assertEqual(dev_agent.take_oauth_token(), "abc")
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", os.environ)  # Unterprozesse erben ihn nicht
            self.assertEqual(dev_agent.claude_auth_env(), {"CLAUDE_CODE_OAUTH_TOKEN": "abc"})

    def test_ohne_token_gilt_der_login_am_pc(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            self.assertEqual(dev_agent.claude_auth_env(), {})


if __name__ == "__main__":
    unittest.main()
