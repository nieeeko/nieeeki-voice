"""Gegenbeispiele neu verhallen und dabei wie die Weckwort-Beispiele ans Fensterende legen.

livekit-wakeword 0.3.0 legt Weckwort-Clips ans Ende des 2-s-Fensters (0-200 ms Versatz), Gegenbeispiele
aber in die Mitte. Das Modell lernt so auch "Sprache am Fensterende = Weckwort". Im Betrieb gleitet das
Fenster ueber jedes Wort; aehnliche Woerter ("sneaky", "Nicky") loesten bei Schwelle 0,5 in rund 30 %
der Faelle aus, obwohl die Pipeline-Bewertung (Gegenbeispiele ebenfalls mittig) kaum Fehlalarme zeigte.

Hier: 70 % der Gegenbeispiele ans Ende (gleicher Versatz wie die Weckwort-Clips), 30 % an zufaelliger
Stelle. Sonst genau der Ablauf des Pakets (Rauschen und Hall vor dem Ausrichten, Runden aufeinander).
Danach Merkmale nur fuer die Gegenbeispiele neu; Training mit train_rest.py --nur-training.
Aufruf im Container siehe README.md.
"""

import logging
import random
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from tqdm import tqdm

from livekit.wakeword.cli import load_config
from livekit.wakeword.data.augment import AudioAugmentor, align_clip_to_end
from livekit.wakeword.data.features import extract_features_from_directory
from livekit.wakeword.models.feature_extractor import MelSpectrogramFrontend, SpeechEmbedding
from livekit.wakeword.resources import get_embedding_model_path, get_mel_model_path
from livekit.wakeword.session import session_options

SPLITS = {"negative_train": "negative_features_train.npy", "negative_test": "negative_features_test.npy"}
END_SHARE = 0.7
SAMPLE_RATE = 16000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("augment_negatives")


def place(audio: np.ndarray, length: int) -> np.ndarray:
    """Meist ans Fensterende wie die Weckwort-Clips, sonst an eine zufaellige Stelle."""
    if random.random() < END_SHARE:
        return align_clip_to_end(audio, length)
    if len(audio) >= length:
        start = random.randint(0, len(audio) - length)
        return audio[start:start + length].astype(np.float32)
    out = np.zeros(length, dtype=np.float32)
    start = random.randint(0, length - len(audio))
    out[start:start + len(audio)] = audio
    return out


def augment_split(clip_dir: Path, augmentor: AudioAugmentor, rounds: int, length: int) -> None:
    for old in [p for p in clip_dir.glob("*.wav") if re.fullmatch(r"clip_\d{6}_r\d+", p.stem)]:
        old.unlink()
    for r in range(rounds):
        pattern = r"clip_\d{6}" if r == 0 else rf"clip_\d{{6}}_r{r - 1}"
        sources = sorted(p for p in clip_dir.glob("*.wav") if re.fullmatch(pattern, p.stem))
        for path in tqdm(sources, desc=f"{clip_dir.name} r{r}", unit="clip"):
            audio, _ = sf.read(str(path))
            audio = (audio[:, 0] if audio.ndim > 1 else audio).astype(np.float32)
            audio = augmentor.mix_with_background(augmentor.apply_rir(augmentor.augment_clip(audio)))
            if r == 0:
                audio = place(audio, length)
            stem = re.sub(r"_r\d+$", "", path.stem)
            sf.write(str(path.with_name(f"{stem}_r{r}.wav")), audio, SAMPLE_RATE)


def main() -> None:
    cfg = load_config(sys.argv[1])
    random.seed(20261019)
    augmentor = AudioAugmentor(
        background_paths=[Path(p) for p in cfg.augmentation.background_paths],
        rir_paths=[Path(p) for p in cfg.augmentation.rir_paths],
    )
    length = int(cfg.augmentation.clip_duration * SAMPLE_RATE)
    mel = MelSpectrogramFrontend(onnx_path=get_mel_model_path(), sess_options=session_options())
    emb = SpeechEmbedding(onnx_path=get_embedding_model_path(), sess_options=session_options())
    for split, feature_file in SPLITS.items():
        clip_dir = cfg.model_output_dir / split
        log.info("Gegenbeispiele neu ausrichten: %s", clip_dir)
        augment_split(clip_dir, augmentor, cfg.augmentation.rounds, length)
        features = extract_features_from_directory(clip_dir=clip_dir, mel_frontend=mel, speech_embedding=emb)
        np.save(str(cfg.model_output_dir / feature_file), features)
        log.info("Saved %s features to %s", features.shape, feature_file)
    log.info("Gegenbeispiele fertig. Weiter mit train_rest.py --nur-training")


if __name__ == "__main__":
    main()
