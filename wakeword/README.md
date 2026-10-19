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

# Erzeugen, verhallen, Merkmale, Training, Export, Bewertung (RTX 4060: einige Stunden)
docker run --rm --gpus all -v nieeeki-wakeword:/work -v ${PWD}:/cfg:ro nieeeki-wakeword:0.3.0 run /cfg/nieeeki.yaml

# Modell ins Repo holen
docker create --name nieeeki-wakeword-out -v nieeeki-wakeword:/work nieeeki-wakeword:0.3.0
docker cp nieeeki-wakeword-out:/work/output/nieeeki/nieeeki.onnx ../voice-core/models/nieeeki.onnx
docker rm nieeeki-wakeword-out
```

Danach die Desktop-App neu starten: Im Tray erscheint „Weckwort "nieeeki"“.

- **Schwelle:** `metrics.json` im Ausgabeordner nennt die beste Schwelle (`optimal_threshold`). In
  `voice-core/.env` als `NIEEEKI_WAKE_THRESHOLD` setzen. Fehlalarme und knappe Treffer stehen mit
  ihrem Wert in `desktop/logs/wake.log` (nur Zahlen, kein Ton).
- **Aufräumen:** `docker volume rm nieeeki-wakeword` und `docker rmi nieeeki-wakeword:0.3.0`.
