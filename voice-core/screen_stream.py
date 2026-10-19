"""Bildschirm-Stream: zeigt Nikolaj in der Oberflaeche live, was auf dem Laptop passiert.

Fuer den Fernzugriff vom Handy: nieeeki tippt und klickt, Nikolaj sieht mit. Nur auf Wunsch (Knopf
"Bildschirm" in der UI oder Werkzeug bildschirm_teilen) und nur ueber die angemeldete WebRTC-
Verbindung (auth_gate.py), es entsteht kein neuer Port. Aufgenommen wird der Monitor mit dem
Fenster vorne, in einem eigenen Thread und mit wenigen Bildern pro Sekunde: die Sprache hat Vorrang.
Waehrend des Streams bleibt das Display an, sonst saehe Nikolaj nur Schwarz.
Ohne Stream kostet die Videospur nichts: Pipecat kodiert nur, wenn Bilder kommen (video_out_is_live).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass

from loguru import logger
from PIL import Image, ImageDraw
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import CancelFrame, EndFrame, Frame, OutputImageRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.services.llm_service import FunctionCallParams

WIDTH = int(os.getenv("SCREEN_STREAM_WIDTH") or 1280)
HEIGHT = WIDTH * 9 // 16
FPS = float(os.getenv("SCREEN_STREAM_FPS") or 4)
# Fernsteuerung aus dem Live-Bild: Mindestabstand je Art (ein Doppeltippen wird nicht zu zwei Klicks)
MIN_INTERVAL = {"klick": 0.3, "tippen": 0.3, "taste": 0.15, "scrollen": 0.1}
REMOTE_KEYS = {"enter", "esc", "backspace", "tab"}
MAX_REMOTE_TEXT = 500

ES_CONTINUOUS = 0x80000000
ES_DISPLAY_REQUIRED = 0x00000002

SCHEMA = FunctionSchema(
    name="bildschirm_teilen",
    description=(
        "Zeigt Nikolaj den Laptop-Bildschirm live in der Oberflaeche (an=true) oder blendet ihn aus "
        "(an=false). Fuer unterwegs: so sieht er, was du am PC tust."
    ),
    properties={"an": {"type": "boolean", "description": "true = zeigen, false = ausblenden"}},
    required=["an"],
)


def video_params() -> dict:
    """Fuer TransportParams: eine Videospur, ueber die der Stream laeuft."""
    return {
        "video_out_enabled": True,
        "video_out_is_live": True,
        "video_out_width": WIDTH,
        "video_out_height": HEIGHT,
        "video_out_framerate": max(1, round(FPS)),
    }


def _desktop_locked() -> bool:
    """Wie host/windows_control.is_locked: gesperrt, wenn nicht der normale Desktop aktiv ist."""
    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    desk = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not desk:
        return True
    try:
        name = ctypes.create_unicode_buffer(64)
        needed = wintypes.DWORD()
        user32.GetUserObjectInformationW(desk, 2, name, ctypes.sizeof(name), ctypes.byref(needed))
        return name.value.lower() != "default"
    finally:
        user32.CloseDesktop(desk)


def _foreground_monitor(sct) -> dict:
    """Der Monitor mit dem Fenster vorne: dort passiert gerade etwas. Sonst der Hauptmonitor."""
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    hwnd = user32.GetForegroundWindow()
    if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        for m in sct.monitors[1:]:
            if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                return m
    return sct.monitors[1]


@dataclass(frozen=True)
class FrameGeometry:
    """Wo das zuletzt gesendete Bild auf dem Laptop liegt: fuer Klicks aus dem Live-Bild."""

    left: int  # Monitor-Ursprung in Bildschirmpixeln
    top: int
    scale: float  # Bildpixel je Bildschirmpixel
    off_x: int  # Lage des Monitorbildes im 16:9-Rahmen (schwarze Raender)
    off_y: int
    width: int  # Groesse des Monitorbildes im Rahmen
    height: int


def frame_to_screen(geo: FrameGeometry | None, fx: float, fy: float) -> tuple[int, int] | None:
    """Tippstelle im Live-Bild (0..1) -> Bildschirmpixel; None auf den Raendern oder ohne Bild."""
    if geo is None or not (0 <= fx <= 1 and 0 <= fy <= 1):
        return None
    px, py = fx * WIDTH - geo.off_x, fy * HEIGHT - geo.off_y
    if not (0 <= px < geo.width and 0 <= py < geo.height):
        return None
    return geo.left + int(px / geo.scale), geo.top + int(py / geo.scale)


def _keep_display_on(on: bool) -> None:
    """Waehrend des Streams kein Bildschirmschoner und kein abgeschaltetes Display."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_DISPLAY_REQUIRED if on else ES_CONTINUOUS
        )
    except (AttributeError, OSError):
        pass


class _Grabber:
    """Nimmt im eigenen Thread auf: mss-Handles gehoeren zu dem Thread, der sie anlegt."""

    def __init__(self):
        self._sct = None
        self._locked_frame: bytes | None = None
        self.geometry: FrameGeometry | None = None  # zum zuletzt gesendeten Bild

    def grab(self) -> bytes:
        if _desktop_locked():
            self.geometry = None  # auf den Platzhalter wird nicht geklickt
            return self._placeholder()
        if self._sct is None:
            import mss

            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # pixelgenau auf skalierten Monitoren
            except (AttributeError, OSError):
                pass
            self._sct = mss.MSS()
        monitor = _foreground_monitor(self._sct)
        shot = self._sct.grab(monitor)
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        image.thumbnail((WIDTH, HEIGHT), Image.Resampling.BILINEAR)
        off_x, off_y = (WIDTH - image.width) // 2, (HEIGHT - image.height) // 2
        frame = Image.new("RGB", (WIDTH, HEIGHT))
        frame.paste(image, (off_x, off_y))
        self.geometry = FrameGeometry(
            monitor.get("left", 0), monitor.get("top", 0), image.width / shot.size[0],
            off_x, off_y, image.width, image.height,
        )
        return frame.tobytes()

    def _placeholder(self) -> bytes:
        if self._locked_frame is None:
            image = Image.new("RGB", (WIDTH, HEIGHT), (18, 27, 34))
            ImageDraw.Draw(image).text(
                (WIDTH // 2, HEIGHT // 2), "Laptop gesperrt", fill=(228, 236, 239), anchor="mm", font_size=48
            )
            self._locked_frame = image.tobytes()
        return self._locked_frame


class ScreenStreamer(FrameProcessor):
    """Sitzt direkt vor transport.output(): reicht alles durch und schiebt bei Bedarf Bilder nach."""

    def __init__(self):
        super().__init__()
        self._running = False
        self._task: asyncio.Task | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bildschirm")
        self._grabber = _Grabber()
        self._last: dict[str, float] = {}

    @property
    def active(self) -> bool:
        return self._task is not None

    # ---------- Fernsteuerung aus dem Live-Bild ----------
    # Nur bei laufendem Stream und ueber die angemeldete Verbindung. Ausgefuehrt wird alles vom Host,
    # mit denselben Pruefungen wie bei nieekis eigenen Aktionen (gesperrte Fenster, Sperrbildschirm,
    # Audit-Eintrag).

    def _too_soon(self, kind: str) -> bool:
        now = asyncio.get_running_loop().time()
        if self.active and now - self._last.get(kind, -1e9) < MIN_INTERVAL[kind]:
            return True
        self._last[kind] = now
        return False

    def _point(self, fx: object, fy: object) -> tuple[int, int] | None:
        try:
            return frame_to_screen(self._grabber.geometry, float(fx), float(fy))
        except (TypeError, ValueError):
            return None

    async def _remote(self, name: str, pc_client, tool: str | None, args: dict, reason: str = "") -> dict:
        """Gemeinsamer Weg: pruefen, Host aufrufen, Ergebnis in der Zeitleiste melden."""
        if not self.active or pc_client is None:
            result = {"status": "rejected", "summary": "Live-Bild ist aus"}
        elif tool is None:
            result = {"status": "rejected", "summary": reason}
        else:
            raw = await pc_client._call_tool_text(tool, args)
            shown = {**args, "text": f"{len(args['text'])} Zeichen"} if "text" in args else args
            logger.info("Fernsteuerung {} {}: {}", tool, shown, raw[:120])
            result = json.loads(raw)
        status = {"success": "fertig", "locked": "abgelehnt", "rejected": "abgelehnt", "duplicate": "abgelehnt"}.get(
            str(result.get("status")), "fehler"
        )
        await self.push_frame(RTVIServerMessageFrame(data={
            "type": "aktivitaet", "id": f"{name}-{uuid.uuid4().hex[:8]}", "name": name,
            "detail": str(result.get("summary", ""))[:80], "status": status,
        }))
        return result

    async def click(self, fx: object, fy: object, pc_client, taste: object = "links") -> dict:
        """Tippen im Live-Bild = Linksklick, langes Druecken = Rechtsklick am Laptop."""
        if self._too_soon("klick"):
            return {"status": "ignored"}
        point = self._point(fx, fy)
        if point is None:
            return await self._remote("fernklick", pc_client, None, {}, "Stelle liegt nicht auf dem Bildschirm")
        button = "rechts" if taste == "rechts" else "links"
        return await self._remote("fernklick", pc_client, "maus_klicken",
                                  {"x": point[0], "y": point[1], "taste": button})

    async def scroll(self, fx: object, fy: object, richtung: object, schritte: object, pc_client) -> dict:
        """Wischen im Live-Bild = Scrollen an dieser Stelle."""
        if self._too_soon("scrollen"):
            return {"status": "ignored"}
        point = self._point(fx, fy)
        if richtung not in ("hoch", "runter") or point is None:
            return await self._remote("fernscrollen", pc_client, None, {}, "Stelle oder Richtung ungueltig")
        try:
            steps = max(1, min(10, int(schritte)))
        except (TypeError, ValueError):
            steps = 3
        return await self._remote("fernscrollen", pc_client, "maus_scrollen",
                                  {"richtung": richtung, "schritte": steps, "x": point[0], "y": point[1]})

    async def type_text(self, text: object, enter: bool, pc_client) -> dict:
        """Text vom Handy ins Fenster vorne tippen (optional mit Enter)."""
        if self._too_soon("tippen"):
            return {"status": "ignored"}
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_REMOTE_TEXT:
            return await self._remote("ferntippen", pc_client, None, {},
                                      f"Text fehlt oder ist laenger als {MAX_REMOTE_TEXT} Zeichen")
        return await self._remote("ferntippen", pc_client, "tastatur_tippen", {"text": text + ("\n" if enter else "")})

    async def press_key(self, key: object, pc_client) -> dict:
        """Einzelne Taste vom Handy: Enter, Esc, Ruecktaste oder Tab."""
        if self._too_soon("taste"):
            return {"status": "ignored"}
        if key not in REMOTE_KEYS:
            return await self._remote("ferntaste", pc_client, None, {}, "Diese Taste geht vom Handy nicht")
        return await self._remote("ferntaste", pc_client, "tasten_druecken", {"kombination": key})

    async def set_active(self, on: bool, source: str) -> bool:
        """Stream an/aus; die Oberflaeche erfaehrt den Zustand immer per Nachricht."""
        if on and not self._task and self._running:
            _keep_display_on(True)
            self._task = self.create_task(self._stream(), "bildschirm_stream")
            logger.info("Bildschirm-Stream an ({})", source)
        elif not on and self._task:
            await self._stop()
            logger.info("Bildschirm-Stream aus ({})", source)
        await self.push_frame(RTVIServerMessageFrame(data={"type": "bildschirm", "aktiv": self.active}))
        return self.active

    async def _stop(self) -> None:
        task, self._task = self._task, None
        if task:
            await self.cancel_task(task)
            _keep_display_on(False)

    async def _stream(self) -> None:
        loop = asyncio.get_running_loop()
        interval = 1 / FPS
        while True:
            started = loop.time()
            try:
                image = await loop.run_in_executor(self._executor, self._grabber.grab)
                await self.push_frame(OutputImageRawFrame(image=image, size=(WIDTH, HEIGHT), format="RGB"))
            except Exception as e:  # ein verlorenes Bild ist egal, der Stream laeuft weiter
                logger.warning("Bildschirm-Stream: {}", e)
            await asyncio.sleep(max(0.05, interval - (loop.time() - started)))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self._running = True
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._running = False
            await self._stop()
        await self.push_frame(frame, direction)


def register_stream_tool(llm, streamer: ScreenStreamer) -> FunctionSchema:
    async def bildschirm_teilen(params: FunctionCallParams):
        aktiv = await streamer.set_active(bool(params.arguments.get("an", True)), "nieeeki")
        await params.result_callback(
            {
                "status": "success",
                "aktiv": aktiv,
                "summary": "Der Bildschirm ist jetzt in der Oberflaeche zu sehen."
                if aktiv
                else "Der Bildschirm ist ausgeblendet.",
            }
        )

    llm.register_function("bildschirm_teilen", bildschirm_teilen)
    return SCHEMA
