"""Sicherheitstests fuer den Host: Validatoren direkt vor der Aktion.

Ausfuehren:  uv run python -m unittest -v test_security.py   (im Ordner host/)
Keiner dieser Tests startet ein Programm oder eine Webseite.
"""

import unittest
from unittest import mock

import nieeeki_host as host


class UrlTests(unittest.TestCase):
    def test_accepts_public_https(self):
        url = "https://www.amazon.de/s?k=wohnzimmer+lampe"
        self.assertEqual(host.validate_url(url), url)

    def test_rejects_bad_urls(self):
        bad = [
            "", "   ", None, 42,
            "javascript:alert(1)", "file:///C:/Windows/System32", "ftp://example.com",
            "ms-settings:", "https://", "https://user:pass@example.com",
            "http://localhost:3000", "http://127.0.0.1", "http://192.168.0.1/admin",
            "http://10.0.0.5", "http://[::1]/", "http://169.254.169.254/latest/meta-data",
            "http://printer.local", 'https://example.com/" & calc.exe',
            "https://example.com/a b", "https://example.com/" + "a" * 3000,
        ]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(ValueError):
                host.validate_url(url)


class AppTests(unittest.TestCase):
    def test_rejects_unknown_or_path(self):
        for name in ["cmd", "powershell", r"C:\Windows\System32\cmd.exe", "chrome; calc", "", None]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                host.resolve_app(name)

    def test_uri_apps_resolve(self):
        self.assertEqual(host.resolve_app("einstellungen"), "ms-settings:")


class PowershellTests(unittest.TestCase):
    def test_read_only(self):
        for cmd in [
            "Get-Process",
            "Get-Process | Sort-Object CPU -Descending | Select-Object -First 5",
            "Get-ChildItem C:\\Users | Measure-Object",
            "Test-Connection google.com -Count 1",
        ]:
            with self.subTest(cmd=cmd):
                self.assertEqual(host.classify(cmd), "read")

    def test_needs_approval(self):
        for cmd in [
            "Remove-Item C:\\temp -Recurse", "ri C:\\temp", "Stop-Process -Name chrome",
            "Get-Process; Remove-Item x", "Get-ChildItem | Remove-Item", "Get-Date & calc",
            "Get-Process `; calc", "Get-Item $(calc)", "iex (Get-Content x)",
            "Invoke-Expression 'calc'", "powershell -enc ZQBjAGgAbwA=", "Start-Process calc",
            "Get-Process > C:\\x.txt", "Set-ItemProperty HKCU:\\x -Name a -Value 1",
            "winget install vlc", "Invoke-WebRequest https://evil.example",
        ]:
            with self.subTest(cmd=cmd):
                self.assertEqual(host.classify(cmd), "critical")

    def test_blocked(self):
        for cmd in ["bcdedit /set x", "net user hacker /add", "Get-Credential",
                    "Set-MpPreference -DisableRealtimeMonitoring $true", "", "x" * 2000]:
            with self.subTest(cmd=cmd):
                self.assertEqual(host.classify(cmd), "blocked")

    def test_critical_never_executes(self):
        with mock.patch.object(host, "_powershell") as run:
            result = host.powershell_lesen("Remove-Item C:\\wichtig -Recurse", "Test")
        run.assert_not_called()
        self.assertEqual(result["status"], "needs_approval")

    def test_no_approve_tool_for_model(self):
        import asyncio
        names = {t.name for t in asyncio.run(host.mcp.list_tools())}
        self.assertNotIn("approve", names)
        self.assertNotIn("execute_approved", names)
        self.assertNotIn("run_powershell", names)
        # freigabe_entscheiden gibt es, aber nur mit Schluessel und nie in pc_host.LLM_TOOLS (Voice-Core-Test)
        self.assertIn("freigabe_entscheiden", names)


class HighlightTests(unittest.TestCase):
    def tearDown(self):
        host._recent_actions.clear()

    def test_klick_liefert_rahmen_und_punkt(self):
        chrome = {"hwnd": 2, "titel": "Amazon - Chrome", "programm": "chrome.exe"}
        with mock.patch.object(host.wc, "is_locked", return_value=False), \
             mock.patch.object(host.wc, "screen_bounds", return_value=(0, 0, 2560, 1440)), \
             mock.patch.object(host.wc, "window_at", return_value=chrome), \
             mock.patch.object(host.wc, "window_rect", return_value=[100, 50, 1900, 1000]), \
             mock.patch.object(host.wc, "click"):
            result = host.maus_klicken(400, 300)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["rahmen"], [100, 50, 1900, 1000])
        self.assertEqual(result["punkt"], [400, 300])

    def test_ohne_fensterrechteck_kein_rahmen(self):
        notepad = {"hwnd": 3, "titel": "Unbenannt - Editor", "programm": "notepad.exe"}
        with mock.patch.object(host.wc, "is_locked", return_value=False), \
             mock.patch.object(host.wc, "foreground", return_value=notepad), \
             mock.patch.object(host.wc, "window_rect", return_value=None), \
             mock.patch.object(host.wc, "type_text"):
            result = host.tastatur_tippen("hallo rahmen")
        self.assertEqual(result["status"], "success")
        self.assertNotIn("rahmen", result)
        self.assertNotIn("punkt", result)

    def test_echtes_vordergrundfenster(self):
        rect = host.wc.window_rect(host.wc.foreground()["hwnd"])
        self.assertTrue(rect is None or (len(rect) == 4 and rect[2] > rect[0] and rect[3] > rect[1]))


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        host.PENDING.clear()
        self.key = mock.patch.object(host, "APPROVAL_KEY", "geheimer-test-schluessel")
        self.key.start()

    def tearDown(self):
        self.key.stop()
        host.PENDING.clear()

    def _anfrage(self):
        with mock.patch.object(host, "_powershell") as run:
            result = host.powershell_lesen("Stop-Process -Name notepad", "Notepad schliessen")
        run.assert_not_called()
        self.assertEqual(result["status"], "needs_approval")
        return result["approval_id"]

    def test_ja_fuehrt_genau_einmal_aus(self):
        aid = self._anfrage()
        with mock.patch.object(host, "_powershell", return_value={"status": "success", "summary": "ok"}) as run:
            first = host.freigabe_entscheiden(aid, True, "geheimer-test-schluessel")
            second = host.freigabe_entscheiden(aid, True, "geheimer-test-schluessel")
        run.assert_called_once_with("Stop-Process -Name notepad", timeout=600)
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "error")

    def test_nein_fuehrt_nichts_aus(self):
        aid = self._anfrage()
        with mock.patch.object(host, "_powershell") as run:
            result = host.freigabe_entscheiden(aid, False, "geheimer-test-schluessel")
            again = host.freigabe_entscheiden(aid, True, "geheimer-test-schluessel")
        run.assert_not_called()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(again["status"], "error")

    def test_ohne_schluessel_nichts(self):
        aid = self._anfrage()
        with mock.patch.object(host, "_powershell") as run:
            for key in ["", "falsch", None]:
                self.assertEqual(host.freigabe_entscheiden(aid, True, key)["status"], "blocked")
        run.assert_not_called()
        self.assertIn(aid, host.PENDING)  # die Anfrage bleibt offen

    def test_unbekannte_freigabe(self):
        with mock.patch.object(host, "_powershell") as run, \
                mock.patch.object(host, "_admin_call", return_value={"status": "error", "summary": "unbekannt"}):
            self.assertEqual(host.freigabe_entscheiden("deadbeef", True, "geheimer-test-schluessel")["status"], "error")
        run.assert_not_called()


class AdminTests(unittest.TestCase):
    """als_admin entscheidet nichts selbst: es reicht nur an den Admin-Helfer weiter."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        host.PENDING.clear()
        self.audit = Path(tempfile.mkdtemp()) / "audit.jsonl"
        self.patches = [mock.patch.object(host, "APPROVAL_KEY", "geheimer-test-schluessel"),
                        mock.patch.object(host, "AUDIT_LOG", self.audit)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_gesperrtes_geht_gar_nicht_erst_raus(self):
        with mock.patch.object(host, "_admin_call") as call:
            r = host.als_admin("powershell", "test", befehl="bcdedit /set testsigning on")
        self.assertEqual(r["status"], "blocked")
        call.assert_not_called()

    def test_vorschlag_geht_an_den_helfer(self):
        reply = {"status": "needs_approval", "admin": True, "approval_id": "a1b2c3d4"}
        with mock.patch.object(host, "_admin_call", return_value=reply) as call:
            r = host.als_admin("powershell", "Drucker", befehl="Restart-Service Spooler")
        self.assertEqual(r, reply)
        call.assert_called_once_with({"op": "vorschlagen", "aktion": "powershell", "grund": "Drucker",
                                      "befehl": "Restart-Service Spooler"})

    def test_unbekannte_aktion_und_kaputte_koordinaten(self):
        with mock.patch.object(host, "_admin_call") as call, mock.patch.object(host, "_locked", return_value=None):
            self.assertEqual(host.als_admin("cmd", "x", befehl="dir")["status"], "rejected")
            self.assertEqual(host.als_admin("klicken", "x", x="10", y=5)["status"], "rejected")
        call.assert_not_called()

    def test_klick_nicht_bei_gesperrtem_laptop(self):
        with mock.patch.object(host, "_admin_call") as call, \
                mock.patch.object(host, "_locked", return_value={"status": "locked"}):
            self.assertEqual(host.als_admin("klicken", "Turbo", x=10, y=20)["status"], "locked")
        call.assert_not_called()

    def test_admin_freigabe_mit_token_an_den_helfer_aber_nie_ins_log(self):
        with mock.patch.object(host, "_admin_call", return_value={"status": "success"}) as call:
            r = host.freigabe_entscheiden("a1b2c3d4", True, "geheimer-test-schluessel", token="TOKEN-SENTINEL", immer=True)
        self.assertEqual(r["status"], "success")
        call.assert_called_once_with({"op": "entscheiden", "approval_id": "a1b2c3d4", "ja": True,
                                      "token": "TOKEN-SENTINEL", "immer": True})
        self.assertNotIn("TOKEN-SENTINEL", self.audit.read_text(encoding="utf-8"))

    def test_ohne_schluessel_auch_keine_admin_freigabe(self):
        with mock.patch.object(host, "_admin_call") as call:
            r = host.freigabe_entscheiden("a1b2c3d4", True, "falsch", token="TOKEN-SENTINEL")
        self.assertEqual(r["status"], "blocked")
        call.assert_not_called()

    def test_ohne_einrichtung_klare_meldung(self):
        with mock.patch.object(host, "ADMIN_DIR", host.Path("C:/gibt/es/nicht")):
            r = host._admin_call({"op": "liste"})
        self.assertEqual(r["status"], "error")
        self.assertIn("admin-einrichten", r["summary"])


class KeyboardAndWindowTests(unittest.TestCase):
    def test_combos(self):
        self.assertEqual(host.wc.parse_combo("ctrl+t"), [0x11, ord("T")])
        self.assertEqual(host.wc.parse_combo("Enter"), [0x0D])
        for bad in ["win+r", "windows+x", "ctrl+alt", "", "ctrl+t+w", "rm -rf", None, "x" * 50]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                host.wc.parse_combo(bad)

    def test_blocked_targets(self):
        for exe, title in [("powershell.exe", "Windows PowerShell"), ("cmd.exe", "x"),
                           ("WindowsTerminal.exe", "x"), ("regedit.exe", "Registrierungs-Editor"),
                           ("consent.exe", "x"), ("chrome.exe", "Anmelden – Google-Konten"),
                           ("explorer.exe", "Ausführen"), ("code.exe", "Administrator: x")]:
            with self.subTest(exe=exe, title=title), self.assertRaises(ValueError):
                host.wc.check_target(exe, title)
        host.wc.check_target("WhatsApp.exe", "WhatsApp")  # erlaubt
        host.wc.check_target("Code.exe", "nieeeki-voice - Visual Studio Code")  # erlaubt

    def test_start_app_pick(self):
        apps = [{"Name": "WhatsApp", "AppID": "wa"}, {"Name": "Windows PowerShell", "AppID": "ps"},
                {"Name": "Visual Studio Code", "AppID": "code"}, {"Name": "Registrierungs-Editor", "AppID": "reg"}]
        self.assertEqual(host.wc.pick_start_app("whatsapp", apps)["AppID"], "wa")
        self.assertEqual(host.wc.pick_start_app("visual studio", apps)["AppID"], "code")
        for bad in ["powershell", "registrierungs-editor", "gibtsnicht", ""]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                host.wc.pick_start_app(bad, apps)

    def test_typing_into_console_never_types(self):
        with mock.patch.object(host.wc, "foreground", return_value={"hwnd": 1, "titel": "PowerShell", "programm": "powershell.exe"}), \
             mock.patch.object(host.wc, "type_text") as typed:
            result = host.tastatur_tippen("Remove-Item C:\\ -Recurse\n")
        typed.assert_not_called()
        self.assertEqual(result["status"], "rejected")


class OpenWithTests(unittest.TestCase):
    def test_paths(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(dir=host.HOME) as tmp:
            ok = Path(tmp)
            self.assertEqual(host.validate_local_path(str(ok)), ok.resolve())
            (ok / ".ssh").mkdir()
            for bad in ["", None, "Projekte\\radar", "C:\\Windows\\System32", "C:\\",
                        str(ok / ".ssh"), str(ok / "gibtsnicht"), str(host.HOME / ".." / ".." / "Windows")]:
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    host.validate_local_path(bad)

    def test_rejected_path_launches_nothing(self):
        with mock.patch.object(host, "_launch") as launch:
            result = host.oeffnen_mit("C:\\Windows\\System32", "explorer")
        launch.assert_not_called()
        self.assertEqual(result["status"], "rejected")


class DuplicateTests(unittest.TestCase):
    def test_same_action_twice_runs_once(self):
        host._recent_actions.clear()
        with mock.patch.object(host, "_launch") as launch,              mock.patch.object(host, "resolve_app", return_value="chrome.exe"),              mock.patch.object(host.wc, "list_windows", return_value=[]):
            first = host.webseite_oeffnen("https://www.amazon.de/s?k=e-bike")
            second = host.webseite_oeffnen("https://www.amazon.de/s?k=e-bike")
            third = host.webseite_oeffnen("https://www.amazon.de/s?k=lampe")
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(third["status"], "success")
        self.assertEqual(launch.call_count, 2)

    def tearDown(self):
        host._recent_actions.clear()  # andere Tests duerfen dieselben Aktionen ausfuehren


class OpenTests(unittest.TestCase):
    def test_rejected_url_launches_nothing(self):
        with mock.patch.object(host, "_launch") as launch:
            result = host.webseite_oeffnen("file:///C:/Windows/System32/cmd.exe")
        launch.assert_not_called()
        self.assertEqual(result["status"], "rejected")

    def test_url_passed_as_single_argv(self):
        with mock.patch.object(host, "_launch") as launch, \
             mock.patch.object(host, "resolve_app", return_value="chrome.exe"):
            host.webseite_oeffnen("https://www.amazon.de/s?k=lampe")
        launch.assert_called_once_with("chrome.exe", ["https://www.amazon.de/s?k=lampe"])


class LockTests(unittest.TestCase):
    def test_locked_laptop_runs_no_input(self):
        with mock.patch.object(host.wc, "is_locked", return_value=True), \
             mock.patch.object(host.wc, "type_text") as typed, \
             mock.patch.object(host.wc, "press") as pressed, \
             mock.patch.object(host.wc, "click") as clicked, \
             mock.patch.object(host.wc, "scroll") as scrolled, \
             mock.patch.object(host.wc, "focus") as focused, \
             mock.patch.object(host.wc, "screenshot") as shot:
            results = [host.tastatur_tippen("hallo"), host.tasten_druecken("enter"), host.maus_klicken(10, 10),
                       host.maus_scrollen(), host.bildschirmfoto(), host.fenster_fokussieren("Chrome")]
        for result in results:
            self.assertEqual(result["status"], "locked")
        for action in (typed, pressed, clicked, scrolled, focused, shot):
            action.assert_not_called()

    def test_scroll_at_point_checks_target(self):
        chrome = {"hwnd": 2, "titel": "Amazon - Chrome", "programm": "chrome.exe"}
        konsole = {"hwnd": 3, "titel": "PowerShell", "programm": "powershell.exe"}
        with mock.patch.object(host.wc, "is_locked", return_value=False), \
             mock.patch.object(host.wc, "screen_bounds", return_value=(0, 0, 2560, 1440)), \
             mock.patch.object(host.wc, "window_at", return_value=chrome), \
             mock.patch.object(host.wc, "scroll") as scrolled:
            ok = host.maus_scrollen("runter", 3, x=1200, y=700)
            outside = host.maus_scrollen("runter", 3, x=5000, y=700)
        self.assertEqual(ok["status"], "success")
        scrolled.assert_called_once_with(1200, 700, -3)
        self.assertEqual(outside["status"], "rejected")
        with mock.patch.object(host.wc, "is_locked", return_value=False), \
             mock.patch.object(host.wc, "screen_bounds", return_value=(0, 0, 2560, 1440)), \
             mock.patch.object(host.wc, "window_at", return_value=konsole), \
             mock.patch.object(host.wc, "scroll") as scrolled:
            blocked = host.maus_scrollen("hoch", 3, x=100, y=100)
        scrolled.assert_not_called()
        self.assertEqual(blocked["status"], "rejected")

    def test_unlocked_desktop_detected(self):
        # Die Tests laufen am entsperrten Rechner: dort ist der normale Desktop aktiv.
        self.assertFalse(host.wc.is_locked())


class FocusTests(unittest.TestCase):
    """focus() darf den Fokus im Fenster nicht verschieben (Log 20.10.: Alt nahm dem Chrome-Feld den Fokus)."""

    def run_focus(self, hwnd, front_before, front_after=None):
        gui = mock.Mock()
        gui.IsIconic.return_value = False
        gui.GetForegroundWindow.return_value = front_before
        user32 = mock.Mock()
        user32.GetForegroundWindow.side_effect = [front_before, front_after if front_after is not None else hwnd]
        user32.GetWindowThreadProcessId.return_value = 7
        user32.AttachThreadInput.return_value = 1
        kernel32 = mock.Mock()
        kernel32.GetCurrentThreadId.return_value = 3
        fake_ctypes = mock.Mock()
        fake_ctypes.windll.user32, fake_ctypes.windll.kernel32 = user32, kernel32
        with mock.patch.object(host.wc, "_win32", return_value=(gui, None, mock.Mock())), \
                mock.patch.object(host.wc, "ctypes", fake_ctypes), mock.patch.object(host.wc.time, "sleep"):
            host.wc.focus(hwnd)
        return gui, user32

    def keys(self, user32):
        return [c.args[0] for c in user32.keybd_event.call_args_list]

    def test_schon_vorn_kein_tastendruck(self):
        gui, user32 = self.run_focus(42, front_before=42)
        self.assertEqual(self.keys(user32), [])
        user32.SetForegroundWindow.assert_not_called()
        gui.SetForegroundWindow.assert_not_called()

    def test_wechsel_ohne_tastendruck(self):
        _, user32 = self.run_focus(42, front_before=99)
        user32.SetForegroundWindow.assert_called_once_with(42)
        self.assertEqual(self.keys(user32), [])
        self.assertEqual(user32.AttachThreadInput.call_args_list[-1].args, (3, 7, False))  # wieder geloest

    def test_notfall_mit_shift_nie_alt(self):
        _, user32 = self.run_focus(42, front_before=99, front_after=99)
        self.assertNotIn(0x12, self.keys(user32))
        self.assertEqual(self.keys(user32), [0x10, 0x10])


class SecretAndSystemAppTests(unittest.TestCase):
    def test_freigabe_schluessel_nicht_in_der_umgebung(self):
        # 21.10.: powershell_lesen "Get-ChildItem Env:" zeigte NIEEEKI_APPROVAL_KEY dem Gespraechsmodell
        import os
        import subprocess
        import sys

        code = "import os, nieeeki_host; print('NIEEEKI_APPROVAL_KEY' in os.environ, bool(nieeeki_host.APPROVAL_KEY))"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={**os.environ, "NIEEEKI_APPROVAL_KEY": "geheim"}, cwd=os.path.dirname(host.__file__))
        self.assertEqual(out.stdout.strip().splitlines()[-1], "False True")

    def test_umgebungsvariablen_und_systemeigenschaften(self):
        with mock.patch.object(host.Path, "is_file", return_value=True):
            for name in ["Umgebungsvariablen bearbeiten", "umgebungsvariablen", "Systemeigenschaften"]:
                with self.subTest(name=name):
                    self.assertTrue(host.resolve_app(name).endswith("SystemPropertiesAdvanced.exe"))
        with self.assertRaises(ValueError):
            host.resolve_app("umgebungsvariablenx")


class FocusCheckTests(unittest.TestCase):
    """21.10.: nieeeki meldete 'VS Code ist vorne', obwohl Windows es nicht nach vorn gelassen hatte."""

    def run_focus(self, vorne_hwnd):
        win = {"hwnd": 7, "titel": "nieeeki-voice - Visual Studio Code", "programm": "Code.exe"}
        with mock.patch.object(host.wc, "is_locked", return_value=False), \
             mock.patch.object(host.wc, "find_window", return_value=win), \
             mock.patch.object(host.wc, "focus"), \
             mock.patch.object(host.wc, "window_rect", return_value=None), \
             mock.patch.object(host.wc, "foreground", return_value={"hwnd": vorne_hwnd, "titel": "x", "programm": "x"}):
            return host.fenster_fokussieren("Code")

    def test_wirklich_vorne(self):
        self.assertEqual(self.run_focus(7)["status"], "success")

    def test_nicht_vorne_wird_ehrlich_gemeldet(self):
        result = self.run_focus(99)
        self.assertEqual(result["status"], "error")
        self.assertIn("nicht nach vorne", result["summary"])


class ClaudeAppTests(unittest.TestCase):
    def test_claude_code_findet_desktop_app(self):
        # 21.10.: programm_oeffnen("Claude Code") fand die App "Claude" nicht
        apps = [{"Name": "Claude", "AppID": "Claude_x!Claude"}, {"Name": "Visual Studio Code", "AppID": "code"}]
        self.assertEqual(host.wc.pick_start_app("Claude Code", apps)["AppID"], "Claude_x!Claude")
        self.assertEqual(host.wc.pick_start_app("claude", apps)["AppID"], "Claude_x!Claude")
        with self.assertRaises(ValueError):  # nur ganze Woerter am Anfang
            host.wc.pick_start_app("code claude", [{"Name": "Claude", "AppID": "c"}])


class FolderTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.home = Path(tempfile.mkdtemp()).resolve()
        (self.home / "Projekte").mkdir()
        patches = [mock.patch.object(host, "HOME", self.home), mock.patch.object(host, "PROJECTS", self.home / "Projekte")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_anlegen_ohne_freigabe_und_wiederholbar(self):
        target = self.home / "Projekte" / "Test"
        first = host.ordner_anlegen(str(target))
        second = host.ordner_anlegen(str(target))
        self.assertEqual((first["status"], second["status"]), ("success", "success"))
        self.assertTrue(target.is_dir())
        self.assertIn("gibt es schon", second["summary"])

    def test_nur_im_benutzerordner(self):
        bad = [
            "Projekte\\relativ", str(self.home), str(self.home.parent / "fremd"), "C:\\Windows\\neu",
            str(self.home / "AppData" / "Roaming" / "x"), str(self.home / ".ssh" / "x"),
            str(self.home / ".config" / "x"), "",
        ]
        for pfad in bad:
            with self.subTest(pfad=pfad):
                self.assertEqual(host.ordner_anlegen(pfad)["status"], "rejected")
        self.assertFalse((self.home / "AppData").exists())


class GithubTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.projects = Path(tempfile.mkdtemp()).resolve()
        for p in [mock.patch.object(host, "PROJECTS", self.projects), mock.patch.object(host, "_gh", return_value="gh.exe")]:
            p.start()
            self.addCleanup(p.stop)
        host._recent_actions.clear()

    @staticmethod
    def proc(code=0, out=""):
        return mock.Mock(returncode=code, stdout=out, stderr="")

    def test_namen_und_adressen(self):
        self.assertEqual(host.validate_github_repo("https://github.com/nieeeko/Test.git"), ("nieeeko", "Test"))
        self.assertEqual(host.validate_github_repo("nieeeko/radar"), ("nieeeko", "radar"))
        for bad in ["Test", "https://evil.com/a/b", "a/b/c", "nieeeko/..", "nieeeko/-x;rm", "", None]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                host.validate_github_repo(bad)
        for bad in ["", ".hidden", "a b", "x" * 101, "name.git", "--help"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                host.validate_repo_name(bad)

    def test_neu_anlegen_privat_und_klonen(self):
        calls = []

        def run(args, timeout=90):
            calls.append(args)
            if args[1:3] == ["api", "user"]:
                return self.proc(out="nieeeko\n")
            if args[1:3] == ["repo", "view"]:
                return self.proc(code=1)
            return self.proc()

        with mock.patch.object(host, "_run", side_effect=run):
            result = host.github_repo_anlegen("Test", beschreibung="Spielwiese")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["url"], "https://github.com/nieeeko/Test")
        self.assertTrue(result["angelegt"])
        create = next(c for c in calls if c[1:3] == ["repo", "create"])
        self.assertEqual(create, ["gh.exe", "repo", "create", "nieeeko/Test", "--private", "--description", "Spielwiese"])
        clone = next(c for c in calls if c[1:3] == ["repo", "clone"])
        self.assertEqual(clone, ["gh.exe", "repo", "clone", "nieeeko/Test", str(self.projects / "Test")])

    def test_vorhandenes_wird_nur_geklont(self):
        calls = []

        def run(args, timeout=90):
            calls.append(args)
            if args[1:3] == ["api", "user"]:
                return self.proc(out="nieeeko")
            if args[1:3] == ["repo", "view"]:
                return self.proc(out="PUBLIC")
            return self.proc()

        with mock.patch.object(host, "_run", side_effect=run):
            result = host.github_repo_anlegen("Test")
        self.assertFalse(result["angelegt"])
        self.assertIn("public", result["hinweis"])
        self.assertFalse(any(c[1:3] == ["repo", "create"] for c in calls))

    def test_klonen_ueberschreibt_nichts(self):
        (self.projects / "radar").mkdir()
        (self.projects / "radar" / "README.md").write_text("da", encoding="utf-8")
        with mock.patch.object(host, "_run") as run:
            result = host.git_klonen("nieeeko/radar")
        run.assert_not_called()
        self.assertEqual(result["status"], "rejected")

    def test_leerer_ordner_ist_ok(self):
        (self.projects / "Test").mkdir()
        with mock.patch.object(host, "_run", return_value=self.proc()) as run:
            result = host.git_klonen("https://github.com/nieeeko/Test")
        self.assertEqual(result["status"], "success")
        run.assert_called_once()


class ReadTimeoutTests(unittest.TestCase):
    def test_lange_suche_wird_klar_gemeldet(self):
        import subprocess

        with mock.patch.object(host.platform, "system", return_value="Windows"), \
             mock.patch.object(host.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell", 20)):
            result = host.powershell_lesen("Get-ChildItem -Path C:\\ -Recurse", "Suche")
        self.assertEqual(result["status"], "timeout")
        self.assertIn("Nicht wiederholen", result["summary"])


if __name__ == "__main__":
    unittest.main()
