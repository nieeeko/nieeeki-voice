# Weckwort „nieeeki“ trainieren

Das Modell `voice-core/models/nieeeki.onnx` erkennt „nieeeki“ lokal am Mikrofon (`voice-core/wake.py`).
Trainiert wird es mit [livekit-wakeword](https://github.com/livekit/livekit-wakeword) 0.3.0 (Apache-2.0):
synthetische Stimmen (Piper, 904 englische Sprecher, gemischt), dazu ähnlich klingende Gegenbeispiele,
Raumhall und Hintergrundgeräusche. Aufnahmen von Nikolaj sind nicht nötig.

Voraussetzungen: Docker Desktop mit NVIDIA-GPU (WSL2), rund 30 GB frei (Image ~10 GB, Daten ~20 GB im
Docker-Volume `nieeeki-wakeword`).

```powershell
cd wakeword
docker build -t nieeeki-wakeword:0.3.0 .

# Einmalig: Trainingsdaten laden (ACAV100M-Merkmale ~16 GB, MUSAN ~1 GB, Raumhall, Piper-Stimme)
docker run --rm --gpus all -v nieeeki-wakeword:/work -v ${PWD}:/cfg:ro nieeeki-wakeword:0.3.0 setup --config /cfg/nieeeki.yaml

# Erzeugen und verhallen (Schritte 1-2)
docker run --rm --gpus all -v nieeeki-wakeword:/work -v ${PWD}:/cfg:ro nieeeki-wakeword:0.3.0 generate /cfg/nieeeki.yaml
docker run --rm --gpus all -v nieeeki-wakeword:/work -v ${PWD}:/cfg:ro nieeeki-wakeword:0.3.0 augment /cfg/nieeeki.yaml
# Merkmale, Training, Export, Bewertung (Schritte 3-6). Nicht "run": das bricht in 0.3.0 bei
# Schritt 3 ab (run_extraction ohne sess_options), darum train_rest.py
docker run --rm --gpus all -v nieeeki-wakeword:/work -v ${PWD}:/cfg:ro --entrypoint python nieeeki-wakeword:0.3.0 /cfg/train_rest.py /cfg/nieeeki.yaml

# Modell ins Repo holen
docker create --name nieeeki-wakeword-out -v nieeeki-wakeword:/work nieeeki-wakeword:0.3.0
docker cp nieeeki-wakeword-out:/work/output/nieeeki/nieeeki.onnx ../voice-core/models/nieeeki.onnx
docker rm nieeeki-wakeword-out
```

Danach die Desktop-App neu starten: Im Tray erscheint „Weckwort "nieeeki"“.

- **Schwelle:** `metrics.json` im Ausgabeordner nennt die beste Schwelle (`optimal_threshold`). In
  `voice-core/.env` als `NIEEEKI_WAKE_THRESHOLD` setzen. Fehlalarme und knappe Treffer stehen mit
  ihrem Wert in `desktop/logs/wake.log` (nur Zahlen, kein Ton).
- **GPU gedrosselt?** Der AORUS hält die Grafikkarte im Sparmodus bei 10 W (`nvidia-smi -q -d POWER`). Dann
  ohne `--gpus all` und mit `--cpus=12` auf der CPU trainieren (erste Runde: ~4 Clips/s beim Erzeugen) oder im
  GIGABYTE Control Center auf „Gaming“/„Turbo“ stellen.
- **Aufräumen:** `docker volume rm nieeeki-wakeword` und `docker rmi nieeeki-wakeword:0.3.0`.
