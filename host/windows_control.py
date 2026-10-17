"""Fenster, Tastatur und installierte Programme fuer den nieeeki-Host (nur Windows).

Sicherheitsregeln (hier, direkt vor der Aktion):
  * Tippen und Tastenkuerzel NIE in Konsolen, Registry, Admin-/Anmeldefenster oder Passwort-Manager:
    dort koennten getippte Zeichen Befehle ausfuehren oder Zugangsdaten beruehren.
  * Keine Tastenkuerzel mit der Windows-Taste (Win+R oeffnet "Ausfuehren").
  * Programme nur aus dem Startmenue des Nutzers, keine Shells/Registry/Admin-Werkzeuge.
  * Laengenlimits fuer Text.
"""

from __future__ import annotations

import ctypes
import json
import re
import subprocess
import time
from ctypes import wintypes

MAX_TYPE_CHARS = 2000

BLOCKED_TARGET_EXES = {
    "powershell.exe", "pwsh.exe", "powershell_ise.exe", "cmd.exe", "windowsterminal.exe", "wt.exe",
    "conhost.exe", "openconsole.exe", "regedit.exe", "mmc.exe", "taskmgr.exe", "consent.exe",
    "credentialuibroker.exe", "lsass.exe", "mstsc.exe", "keepass.exe", "keepassxc.exe",
    "1password.exe", "bitwarden.exe", "dashlane.exe", "gpedit.exe",
}
BLOCKED_TARGET_TITLE = re.compile(
    r"(administrator|ausf(ü|ue)hren$|^run$|kennwort|passwort|password|anmeld|sign in|"
    r"benutzerkontensteuerung|user account control|windows-sicherheit|windows security)",
    re.IGNORECASE,
)
BLOCKED_APP_NAMES = re.compile(
    r"(powershell|eingabeaufforderung|command prompt|registrierungs|registry|regedit|"
    r"computerverwaltung|gruppenrichtlinie|dienste|services|task-manager|ausf(ü|ue)hren|"
    r"windows-tools|administrative|systemkonfiguration)",
    re.IGNORECASE,
)

# ---------- Tasten ----------
VK = {
    "ctrl": 0x11, "strg": 0x11, "shift": 0x10, "alt": 0x12,
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "leertaste": 0x20,
    "backspace": 0x08, "delete": 0x2E, "entf": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{chr(c): ord(chr(c).upper()) for c in range(ord("a"), ord("z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
}
MODIFIERS = {"ctrl", "strg", "shift", "alt"}


def parse_combo(combo: object) -> list[int]:
    """'ctrl+t', 'enter', 'ctrl+shift+n' -> virtuelle Tastencodes. Windows-Taste ist nicht erlaubt."""
    if not isinstance(combo, str) or not combo.strip() or len(combo) > 40:
        raise ValueError("ungueltige Tastenkombination")
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    if any(p in ("win", "windows", "super", "meta", "cmd") for p in parts):
        raise ValueError("Tastenkuerzel mit der Windows-Taste sind gesperrt")
    if not parts or any(p not in VK for p in parts):
        raise ValueError(f"unbekannte Taste in {combo!r}")
    if sum(1 for p in parts if p not in MODIFIERS) != 1:
        raise ValueError("genau eine Haupttaste, z. B. ctrl+t")
    return [VK[p] for p in parts]


def check_target(exe: str, title: str) -> None:
    """Wirft ValueError, wenn in dieses Fenster nicht getippt werden darf."""
    if (exe or "").lower() in BLOCKED_TARGET_EXES:
        raise ValueError(f"In {exe} tippt nieeeki nicht (Konsole/Systemwerkzeug)")
    if BLOCKED_TARGET_TITLE.search(title or ""):
        raise ValueError("In Anmelde-, Admin- oder Passwortfenster tippt nieeeki nicht")


def check_app_name(name: str) -> None:
    if BLOCKED_APP_NAMES.search(name):
        raise ValueError(f"{name} startet nieeeki nicht (Systemwerkzeug)")


# ---------- Windows-API ----------
def _win32():
    import win32con  # pywin32, nur unter Windows
    import win32gui
    import win32process
    return win32gui, win32process, win32con


def _exe_of(hwnd: int) -> str:
    _, win32process, _ = _win32()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _title(hwnd: int) -> str:
    """Fenstertitel als Unicode (win32gui.GetWindowText liefert ANSI und verliert Umlaute)."""
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def list_windows() -> list[dict]:
    """Sichtbare Hauptfenster mit Titel und Programm."""
    win32gui, _, win32con = _win32()
    found: list[dict] = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return True
        title = _title(hwnd)
        if not title.strip() or win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
            return True
        if title == "nieeeki Stimmen-Rand":  # eigene Rand-Fenster der Desktop-App
            return True
        found.append({"hwnd": hwnd, "titel": title[:200], "programm": _exe_of(hwnd)})
        return True

    win32gui.EnumWindows(visit, None)
    return found


def find_window(query: str) -> dict:
    q = (query or "").strip().lower()
    if not q:
        raise ValueError("Fenstername fehlt")
    candidates = [w for w in list_windows()
                  if q in w["titel"].lower() or q in w["programm"].lower().removesuffix(".exe")]
    if not candidates:
        raise ValueError(f"Kein offenes Fenster passt zu {query!r}")
    return candidates[0]


def foreground() -> dict:
    win32gui, _, _ = _win32()
    hwnd = win32gui.GetForegroundWindow()
    return {"hwnd": hwnd, "titel": _title(hwnd), "programm": _exe_of(hwnd)}


def focus(hwnd: int) -> None:
    win32gui, _, win32con = _win32()
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    # Windows erlaubt SetForegroundWindow nur nach einer Eingabe; ein Alt-Druck loest die Sperre
    ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
    ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.25)


# SendInput-Strukturen
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(inputs: list[_INPUT]) -> None:
    arr = (_INPUT * len(inputs))(*inputs)
    ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    i = _INPUT(type=1)
    i.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return i


def type_text(text: str) -> None:
    KEYUP, UNICODE = 0x0002, 0x0004
    for ch in text:
        if ch == "\n":
            _send([_key(vk=0x0D), _key(vk=0x0D, flags=KEYUP)])
        else:
            code = ord(ch)
            _send([_key(scan=code, flags=UNICODE), _key(scan=code, flags=UNICODE | KEYUP)])
        time.sleep(0.004)


def press(codes: list[int]) -> None:
    KEYUP = 0x0002
    _send([_key(vk=c) for c in codes] + [_key(vk=c, flags=KEYUP) for c in reversed(codes)])
    time.sleep(0.05)


def start_menu_apps() -> list[dict]:
    p = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
         "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    data = json.loads(p.stdout or "[]")
    return data if isinstance(data, list) else [data]


def pick_start_app(name: str, apps: list[dict]) -> dict:
    q = (name or "").strip().lower()
    if not q:
        raise ValueError("Programmname fehlt")
    exact = [a for a in apps if a["Name"].lower() == q]
    partial = sorted((a for a in apps if q in a["Name"].lower()), key=lambda a: len(a["Name"]))
    match = (exact or partial or [None])[0]
    if not match:
        raise ValueError(f"Kein installiertes Programm passt zu {name!r}")
    check_app_name(match["Name"])
    return match


def launch_start_app(app_id: str) -> None:
    # explorer.exe mit AppsFolder-Pfad startet Store- und klassische Apps; argv ohne Shell
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], close_fds=True)
