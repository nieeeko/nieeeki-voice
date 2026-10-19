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


if __name__ == "__main__":
    unittest.main()
