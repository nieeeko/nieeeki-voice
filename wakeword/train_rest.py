"""Schritte 3-6 von livekit-wakeword 0.3.0 einzeln (Merkmale, Training, Export, Bewertung).

Der CLI-Befehl `run` bricht in 0.3.0 bei Schritt 3 ab: er uebergibt run_extraction() das
Pflichtargument sess_options nicht (TypeError). Erzeugen und Verhallen (Schritte 1-2) laufen per
`generate` und `augment`, der Rest hiermit. Aufruf im Container siehe README.md.
Mit --nur-training werden gespeicherte Merkmale (*.npy) wiederverwendet, z. B. um nur das Training
auf der GPU zu wiederholen.
"""

import logging
import sys

from livekit.wakeword.cli import load_config
from livekit.wakeword.data.features import run_extraction
from livekit.wakeword.eval.evaluate import run_eval
from livekit.wakeword.export.onnx import run_export
from livekit.wakeword.session import session_options
from livekit.wakeword.training.trainer import run_train

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("train_rest")
cfg = load_config(sys.argv[1])
if "--nur-training" in sys.argv:
    log.info("Step 3/6: uebersprungen, gespeicherte Merkmale aus %s", cfg.model_output_dir)
else:
    log.info("Step 3/6: Extract features")
    run_extraction(cfg, session_options())
log.info("Step 4/6: Train classifier")
run_train(cfg)
log.info("Step 5/6: Export")
run_export(cfg)
log.info("Step 6/6: Evaluate")
res = run_eval(cfg, cfg.model_output_dir / f"{cfg.model_name}.onnx")
log.info("Eval: AUT=%.4f FPPH=%.2f Recall=%.1f%%", res["aut"], res["fpph"], res["recall"] * 100)
log.info("Full pipeline complete!")
