# Voice-Core fuer Runpod (CPU reicht) oder jeden Docker-Host.
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml
COPY . .
EXPOSE 7860
CMD ["python", "bot.py", "--host", "0.0.0.0", "--port", "7860"]
