FROM python:3.11-slim

# Install system dependencies:
#   ffmpeg       — used by websocket_audio.py to decode WebM → PCM via subprocess
#   gcc          — required to compile webrtcvad C extension
#   python3-dev  — C headers needed for webrtcvad compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source (data/ and .env are excluded via .dockerignore)
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY config/ ./config/

# Create the data directory structure; will be overridden by the bind-mount volume
RUN mkdir -p data/recordings data/audio data/raw_audio data/vad_audio data/decoded_audio

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
