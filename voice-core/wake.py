"""Weckwort-Lauscher: hoert lokal am Mikrofon auf "nieeeki" und meldet Treffer der Desktop-App.

Laeuft als eigener Prozess, gestartet von desktop/main.js. Kein Ton verlaesst den PC: Die Erkennung
laeuft lokal (livekit-wakeword, ONNX); erst nach einem Treffer baut die Oberflaeche das Gespraech auf.
Es wird nichts aufgezeichnet oder gespeichert.

Protokoll, eine JSON-Zeile je Ereignis auf stdout:
  {"bereit": true, "modell": "nieeeki", "schwelle": 0.5}
  {"weckwort": "nieeeki", "score": 0.93}
Befehle auf stdin (je Zeile): "pause" (Gespraech laeuft, Mikrofon wird freigegeben), "weiter".
Ende von stdin (Desktop-App weg) beendet den Lauscher.

Rechenweg wie WakeWordModel.predict, aber fortlaufend: je 80-ms-Block nur 8 neue Mel-Zeilen und ein
Embedding statt 2 s neu zu rechnen (~8 ms statt ~100 ms je Block).
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv()  # NIEEEKI_WAKE_THRESHOLD aus voice-core/.env

SAMPLE_RATE = 16000
BLOCK = 1280  # 80 ms
MEL_CONTEXT = 480  # Vorlauf: Mel-Fenster 640 Samples, Schritt 160 -> 1760 Samples ergeben 8 Zeilen
MEL_WINDOW = 76  # Mel-Zeilen je Embedding
N_EMBEDDINGS = 16  # Eingabelaenge des Klassifikators

MODEL_PATH = Path(os.getenv("NIEEEKI_WAKE_MODEL") or Path(__file__).parent / "models" / "nieeeki.onnx")


def model_threshold(model_path: Path, default: float = 0.5) -> float:
    """Schwelle aus der Begleitdatei des Modells (z. B. models/nieeeki.json), sonst default."""
    try:
        value = json.loads(model_path.with_suffix(".json").read_text(encoding="utf-8"))["schwelle"]
    except (OSError, ValueError, KeyError, TypeError):
        return default
    return float(value) if isinstance(value, (int, float)) and 0 < value < 1 else default


# Umgebung (voice-core/.env) vor Begleitdatei vor 0,5
THRESHOLD = float(os.getenv("NIEEEKI_WAKE_THRESHOLD") or model_threshold(MODEL_PATH))
DEBOUNCE_SECS = 2.0
NEAR_MISS = 0.3  # knappe Fehlschlaege ins Log (nur Zahlen), hilft beim Einstellen der Schwelle


def _sess_options():
    import onnxruntime as ort

    # Ein Thread ohne Spin-Warten: sonst haelt onnxruntime alle Kerne wach
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return options


class StreamingFeatures:
    """Mel-Zeilen und Embeddings fortlaufend aus 80-ms-Bloecken (gleiche Modelle wie im Training)."""

    def __init__(self):
        from livekit.wakeword.models.feature_extractor import MelSpectrogramFrontend, SpeechEmbedding
        from livekit.wakeword.resources import get_embedding_model_path, get_mel_model_path

        self.mel = MelSpectrogramFrontend(get_mel_model_path(), _sess_options())
        self.embed = SpeechEmbedding(get_embedding_model_path(), _sess_options())
        self.reset()

    def reset(self) -> None:
        self._tail = np.zeros(MEL_CONTEXT, dtype=np.float32)
        self._mel_rows: deque[np.ndarray] = deque(maxlen=MEL_WINDOW)
        self._embeddings: deque[np.ndarray] = deque(maxlen=N_EMBEDDINGS)

    def feed(self, block: np.ndarray) -> np.ndarray | None:
        """Einen Block (1280 Samples, int16 oder float32) verarbeiten; (16, 96) sobald genug Audio da ist."""
        audio = block.astype(np.float32) / 32768.0 if block.dtype == np.int16 else block.astype(np.float32)
        mel = self.mel(np.concatenate([self._tail, audio]))[0]  # 8 neue Zeilen
        self._tail = audio[-MEL_CONTEXT:]
        self._mel_rows.extend(mel)
        if len(self._mel_rows) < MEL_WINDOW:
            return None
        self._embeddings.append(self.embed(np.stack(self._mel_rows)[np.newaxis])[0])
        if len(self._embeddings) < N_EMBEDDINGS:
            return None
        return np.stack(self._embeddings)


class StreamingDetector:
    """Bewertet fortlaufend 80-ms-Bloecke; feed() liefert die Wahrscheinlichkeit fuer das Weckwort."""

    def __init__(self, model_path: Path):
        import onnxruntime as ort

        self.name = model_path.stem
        self.features = StreamingFeatures()
        self._classifier = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"], sess_options=_sess_options()
        )
        self._input = self._classifier.get_inputs()[0].name

    def reset(self) -> None:
        self.features.reset()

    def feed(self, block: np.ndarray) -> float:
        features = self.features.feed(block)
        if features is None:
            return 0.0  # Puffer fuellt sich noch (~2 s nach Start oder Pause)
        return float(self._classifier.run(None, {self._input: features[np.newaxis].astype(np.float32)})[0][0, 0])


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {text}", file=sys.stderr, flush=True)


def _read_commands(commands: queue.Queue) -> None:
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd in ("pause", "weiter"):
            commands.put(cmd)
    commands.put("ende")  # stdin zu: Desktop-App beendet


def main() -> int:
    if not MODEL_PATH.exists():
        _emit({"fehler": f"Kein Weckwort-Modell unter {MODEL_PATH}"})
        return 2  # dauerhaft: die Desktop-App startet dann nicht neu

    import sounddevice as sd

    detector = StreamingDetector(MODEL_PATH)
    blocks: queue.Queue[np.ndarray] = queue.Queue(maxsize=50)
    commands: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_read_commands, args=(commands,), daemon=True).start()

    def on_audio(indata, _frames, _time, status):
        if status:
            _log(f"Audio: {status}")
        try:
            blocks.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # Rechner kurz ueberlastet: Block verwerfen statt Rueckstau

    def open_stream():
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=on_audio
        )
        stream.start()
        return stream

    stream = open_stream()
    _emit({"bereit": True, "modell": detector.name, "schwelle": THRESHOLD})
    _log(f"Lausche auf '{detector.name}' (Schwelle {THRESHOLD}) am Mikrofon {sd.query_devices(kind='input')['name']}")
    last_hit = last_note = 0.0
    try:
        while True:
            try:
                cmd = commands.get_nowait()
            except queue.Empty:
                cmd = None
            if cmd == "ende":
                return 0
            if cmd == "pause" and stream is not None:
                stream.close()  # Mikrofon frei, solange das Gespraech laeuft
                stream = None
                _log("Pause (Gespraech laeuft)")
            elif cmd == "weiter" and stream is None:
                detector.reset()
                while not blocks.empty():
                    blocks.get_nowait()
                stream = open_stream()
                _log("Lausche wieder")
            if stream is None:
                time.sleep(0.1)
                continue
            try:
                block = blocks.get(timeout=0.5)
            except queue.Empty:
                continue
            score = detector.feed(block)
            now = time.monotonic()
            if score >= THRESHOLD and now - last_hit > DEBOUNCE_SECS:
                last_hit = now
                _emit({"weckwort": detector.name, "score": round(score, 3)})
                _log(f"Weckwort erkannt ({score:.2f})")
            elif score >= NEAR_MISS and now - max(last_hit, last_note) > DEBOUNCE_SECS:
                last_note = now
                _log(f"Knapp daneben ({score:.2f})")
    finally:
        if stream is not None:
            stream.close()


if __name__ == "__main__":
    sys.exit(main())
