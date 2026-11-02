"""Stimmabdruck: erkennt Nikolaj an der Stimme (Sprecher-Embedding mit 192 Werten).

Modell: NVIDIA NeMo TitaNet-Large als ONNX aus den Releases von sherpa-onnx, 192 Werte je Stimme
(passt zu speakers.voiceprint in 0001). Es rechnet lokal auf der CPU, kein Ton verlaesst dafuer den
PC. Die Datei (~100 MB) laedt der Voice-Core beim ersten Bedarf nach .cache/sprecher/ und prueft
die Pruefsumme aus checksum.txt des Releases.

Vergleich: Kosinus-Aehnlichkeit zweier normierter Abdruecke (1 = gleiche Stimme, um 0 = fremd).
Gemessen am 02.11. mit den Beispielaufnahmen von sherpa-onnx: gleiche Sprecher 0,67 bis 0,80, fremde
hoechstens 0,34. Rechenzeit fuer 8 s Sprache ~250 ms mit 2 Threads, gibt dabei den GIL frei.
Rohaudio wird nie gespeichert, nur der Abdruck (voice_gate.py, memory.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

import httpx
import numpy as np
from loguru import logger

MODEL_NAME = "nemo_en_titanet_large.onnx"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/" + MODEL_NAME
)
MODEL_SHA256 = "d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f699f3425daca8e77"
MODEL_PATH = Path(__file__).resolve().parent / ".cache" / "sprecher" / MODEL_NAME
DIM = 192
THREADS = 2
RETRY_SECS = 60.0  # nach einem Fehler (z. B. Download abgebrochen) erst dann wieder versuchen
# Einlernen: die Sprache in Stuecke dieser Laenge teilen, jedes Stueck ergibt einen Abdruck. Der
# Mittelwert ist der Stimmabdruck, die Streuung zeigt, ob die Aufnahme sauber war.
CHUNK_SECS = 3.0
MIN_CHUNKS = 3
# Sprache von Stille trennen (nur beim Einlernen): 20-ms-Bloecke lauter als das
SPEECH_DBFS = -45.0
BLOCK_SECS = 0.02


class Embedder:
    """Rechnet aus 16-Bit-PCM (mono) einen normierten Abdruck. Blockiert: im Thread aufrufen."""

    def __init__(self, path: Path = MODEL_PATH, threads: int = THREADS):
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path), num_threads=threads)
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        if self._extractor.dim != DIM:
            raise ValueError(f"Modell liefert {self._extractor.dim} statt {DIM} Werte")

    def embed(self, pcm: bytes, sample_rate: int) -> np.ndarray:
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=sample_rate, waveform=pcm_to_float(pcm))
        stream.input_finished()
        return normalize(np.array(self._extractor.compute(stream), dtype=np.float32))


class VoiceModel:
    """Laedt das Modell einmal je Prozess (Download beim ersten Mal, Pruefsumme auch fuer eine schon
    vorhandene Datei). None, wenn es nicht geht; dann neuer Versuch nach RETRY_SECS."""

    def __init__(self, path: Path = MODEL_PATH, sha256: str = MODEL_SHA256, clock=time.monotonic):
        self._path = path
        self._sha256 = sha256
        self._clock = clock
        self._embedder: Embedder | None = None
        self._failed_at: float | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Embedder | None:
        if self._embedder or self._waiting():
            return self._embedder
        async with self._lock:
            if self._embedder is None and not self._waiting():
                try:
                    if not self._path.is_file():
                        await download(self._path, sha256=self._sha256)
                    elif await asyncio.to_thread(file_sha256, self._path) != self._sha256:
                        self._path.unlink()  # beschaedigt oder ausgetauscht: beim naechsten Versuch neu laden
                        raise ValueError("Pruefsumme der Modelldatei stimmt nicht")
                    self._embedder = await asyncio.to_thread(Embedder, self._path)
                    self._failed_at = None
                    logger.info("Stimmabdruck: Modell {} geladen", self._path.name)
                except Exception as e:  # Stimmpruefung ist dann nicht bereit, das Gespraech laeuft
                    self._failed_at = self._clock()
                    logger.warning("Stimmabdruck: Modell nicht verfuegbar: {}", e)
        return self._embedder

    def _waiting(self) -> bool:
        return self._failed_at is not None and self._clock() - self._failed_at < RETRY_SECS


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


async def download(path: Path, url: str = MODEL_URL, sha256: str = MODEL_SHA256) -> None:
    """Laedt nach <path>.part, prueft die Pruefsumme und benennt erst dann um."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    digest = hashlib.sha256()
    logger.info("Stimmabdruck: lade Modell ({})", url)
    async with (
        httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client,
        client.stream("GET", url) as r,
    ):
        r.raise_for_status()
        with part.open("wb") as f:
            async for block in r.aiter_bytes(1 << 20):
                digest.update(block)
                f.write(block)
    if digest.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise ValueError("Pruefsumme des Modells stimmt nicht")
    part.replace(path)


def pcm_to_float(pcm: bytes) -> np.ndarray:
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
    return samples.astype(np.float32) / 32768.0


def normalize(v: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else v


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(normalize(a), normalize(b)))


def centroid(embeddings: list[np.ndarray]) -> tuple[np.ndarray, float]:
    """Mittel der Abdruecke (normiert) und wie nah sie im Schnitt daran liegen (0..1)."""
    center = normalize(np.mean(np.stack([normalize(e) for e in embeddings]), axis=0))
    spread = float(np.mean([cosine(e, center) for e in embeddings]))
    return center, spread


def speech_blocks(pcm: bytes, sample_rate: int, threshold_dbfs: float = SPEECH_DBFS) -> bytes:
    """Nur die lauten 20-ms-Bloecke (Sprache), Pausen dazwischen fallen weg."""
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
    size = max(1, int(sample_rate * BLOCK_SECS))
    keep = [
        samples[i : i + size]
        for i in range(0, len(samples) - size + 1, size)
        if dbfs(samples[i : i + size]) > threshold_dbfs
    ]
    return np.concatenate(keep).tobytes() if keep else b""


def dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    return 20 * float(np.log10(rms / 32768)) if rms > 0 else -120.0


def enroll_chunks(pcm: bytes, sample_rate: int, chunk_secs: float = CHUNK_SECS) -> list[bytes]:
    """Teilt reine Sprache in gleich lange Stuecke; ein kurzer Rest faellt weg."""
    size = int(sample_rate * chunk_secs) * 2
    return [pcm[i : i + size] for i in range(0, len(pcm) - size + 1, size)]


def to_vector(v: np.ndarray) -> str:
    """pgvector-Text fuer PostgREST, z. B. "[0.1,-0.2,...]"."""
    return "[" + ",".join(f"{x:.6f}" for x in v.tolist()) + "]"


def from_vector(value: object) -> np.ndarray | None:
    """Liest den Abdruck aus der Datenbank (pgvector kommt als Text oder Liste)."""
    if isinstance(value, str):
        value = [float(x) for x in value.strip("[] ").split(",") if x.strip()]
    if not isinstance(value, list) or len(value) != DIM:
        return None
    return normalize(np.array(value, dtype=np.float32))
