FROM python:3.12-slim

# ffmpeg: yt-dlp audio extraction + demucs mp3 decoding. (No build tools needed —
# all deps ship prebuilt wheels for linux/amd64.)
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch first (no CUDA → much smaller image), then the rest.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
       torch==2.12.1 torchaudio==2.11.0 \
       --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV demucs_models=/app/models \
    demucs_data=/app/data \
    demucs_model=htdemucs

# Bake the model weights into the image so the first request isn't a slow
# download. (Runs download.py with only config.py present — it needs nothing else.)
COPY model/src/config.py model/src/download.py /app/
RUN python /app/download.py

# App source (overwrites the two files copied above with identical content).
COPY model/src /app

# Render provides $PORT; default 8080 for a plain `docker run`.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8080}"]
