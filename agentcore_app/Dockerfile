# AgentCore Runtime only accepts ARM64 images. Building this on an x86 Windows
# machine without --platform produces an amd64 image that is accepted at
# create time and then fails to start, with little explanation.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

# portaudio is pulled in by the Strands bidi extra. The container never opens a
# microphone, but the import fails without the system library present.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libasound-dev libportaudio2 libportaudiocpp0 portaudio19-dev \
      gcc python3-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agentcore_app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/
COPY ingest/ ./ingest/
COPY knowledge/ ./knowledge/
COPY config/ ./config/
COPY samples/listings/ ./samples/listings/
COPY agentcore_app/voice_server.py ./agentcore_app/

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

ENV CONTAINER_ENV=true
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping').read()"]

CMD ["uvicorn", "agentcore_app.voice_server:app", "--host", "0.0.0.0", "--port", "8080"]
