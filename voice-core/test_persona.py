"""Tests fuer persona.py: feste PC-Fakten im System-Prompt (Benutzer- und Projekte-Ordner).

Ausfuehren:  uv run python -m unittest -v test_persona.py   (im Ordner voice-core/)
"""

import tempfile
import unittest
from pathlib import Path

from persona import BASE_PROMPT, build_system_prompt, pc_section


class PcTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_projekte_mit_unterordnern(self):
        projects = self.home / "Projekte"
        for name in ["radar", "nieeeki-voice", "Test", ".cache"]:
            (projects / name).mkdir(parents=True)
        (projects / "notiz.txt").write_text("x", encoding="utf-8")
        text = pc_section(self.home)
        self.assertIn(f"Benutzerordner: {self.home}", text)
        self.assertIn(f"Projekte-Ordner: {projects} mit nieeeki-voice, radar, Test", text)
        self.assertNotIn(".cache", text)
        self.assertNotIn("notiz", text)

    def test_ohne_projekte_ordner(self):
        self.assertIn("gibt es noch nicht", pc_section(self.home))

    def test_im_prompt_nach_dem_festen_teil(self):
        prompt = build_system_prompt(["Mag Kaffee"], pc_section(self.home))
        self.assertTrue(prompt.startswith(BASE_PROMPT))  # fester Teil vorne: Prompt-Caching bleibt
        self.assertLess(prompt.index("Dein PC"), prompt.index("Mag Kaffee"))
        self.assertEqual(build_system_prompt([]), BASE_PROMPT)

    def test_neue_werkzeuge_im_prompt(self):
        for tool in ["ordner_anlegen", "github_repo_anlegen", "git_klonen"]:
            self.assertIn(tool, BASE_PROMPT)
        self.assertNotIn("kurze Saetze", BASE_PROMPT)


if __name__ == "__main__":
    unittest.main()
