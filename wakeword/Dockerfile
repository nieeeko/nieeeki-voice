# Trainingsumgebung fuer das Weckwort "nieeeki" (livekit-wakeword, Linux + NVIDIA-GPU).
# Bauen und Starten: siehe wakeword/README.md. Die Daten (~20 GB) liegen im Docker-Volume, nicht im Repo.
FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends espeak-ng libsndfile1 ffmpeg sox \
    && rm -rf /var/lib/apt/lists/*

# Versionen wie im uv.lock von livekit-wakeword 0.3.0 (torch 2.10.0); CUDA-12.8-Wheels laufen auf der RTX 4060
RUN pip install --no-cache-dir torch==2.10.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cu128
RUN pip install --no-cache-dir "livekit-wakeword[train,export,eval]==0.3.0"

# Konfiguration kommt per Bind-Mount nach /cfg (Aenderungen ohne Neubau), Daten ins Volume /work
WORKDIR /work
ENTRYPOINT ["livekit-wakeword"]
