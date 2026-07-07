import base64
import glob
import hashlib
import logging
import os
import shutil
import sys
import threading
import urllib.request
from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp

# Make the venv's bundled ffmpeg/ffprobe (same folder as this python) findable by
# demucs whether or not the venv is "activated" (Windows local dev).
os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")

# Directory that actually contains the ffmpeg binary, resolved cross-platform:
# the venv's Scripts dir on Windows, /usr/bin (apt) on Render/Linux. None if not
# found on PATH — yt-dlp then searches PATH itself.
_ffmpeg = shutil.which("ffmpeg")
FFMPEG_DIR = os.path.dirname(_ffmpeg) if _ffmpeg else None

import torch as th
import yt_dlp
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from model import Demucs

served_dir = Path(settings.data) / "served"
served_dir.mkdir(parents=True, exist_ok=True)

model = Demucs(output_dir=str(Path(settings.data) / "separated"), load=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("demucs")

th.hub.set_dir(str(settings.models))

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.mount("/files", StaticFiles(directory=str(served_dir)), name="files")

# Background job tracking so requests return instantly (separation is slow on CPU).
_jobs = {}  # key -> "processing" | "done" | "error:<msg>"
_lock = threading.Lock()


def _process(key, audio_url, out):
    try:
        with NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            req = urllib.request.Request(audio_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                tmp.write(resp.read())
            in_path = tmp.name
        logger.info("separating %s", key)
        model.separate_instrumental(in_path, out)
        with _lock:
            _jobs[key] = "done"
        logger.info("done %s", key)
    except Exception as e:  # noqa: BLE001
        logger.exception("job %s failed", key)
        with _lock:
            _jobs[key] = f"error:{e}"


@app.get("/", status_code=200, response_class=HTMLResponse)
def root():
    return 'Demucs karaoke API. POST /karaoke {"audio_url": "..."}. See <a href="/docs">/docs</a>.'


@app.get("/health", status_code=200)
def health():
    return {"status": "healthy"}


@app.post("/karaoke", status_code=200)
async def karaoke(request: Request):
    """Non-blocking. Body: {"audio_url": "<mp3 url>"}
    Returns immediately: {"file": "/files/<id>.mp3", "ready": bool} (poll until ready)."""
    body = await request.json()
    audio_url = body["audio_url"]

    key = hashlib.sha256(audio_url.encode()).hexdigest()[:16]
    out = served_dir / f"{key}.mp3"
    file_url = f"/files/{key}.mp3"

    if out.exists():
        return {"file": file_url, "ready": True, "cached": True}

    with _lock:
        status = _jobs.get(key)
        if status is None or status.startswith("error:"):
            _jobs[key] = "processing"
            threading.Thread(target=_process, args=(key, audio_url, out), daemon=True).start()
            status = "processing"

    if status.startswith("error:"):
        return {"ready": False, "error": status[6:]}
    return {"file": file_url, "ready": False}


def _process_file(key, in_path, out):
    try:
        logger.info("separating (upload) %s", key)
        model.separate_instrumental(in_path, out)
        with _lock:
            _jobs[key] = "done"
        logger.info("done %s", key)
    except Exception as e:  # noqa: BLE001
        logger.exception("job %s failed", key)
        with _lock:
            _jobs[key] = f"error:{e}"


@app.get("/status/{key}", status_code=200)
def status(key: str):
    """Poll separation progress for an uploaded file."""
    out = served_dir / f"{key}.mp3"
    if out.exists():
        return {"ready": True, "file": f"/files/{key}.mp3"}
    with _lock:
        st = _jobs.get(key, "unknown")
    if st.startswith("error:"):
        return {"ready": False, "error": st[6:]}
    return {"ready": False}


@app.post("/separate_upload", status_code=200)
async def separate_upload(file: UploadFile = File(...)):
    """Upload a full-length audio file. Returns {key, file, ready}; poll /status/{key}."""
    data = await file.read()
    key = hashlib.sha256(data).hexdigest()[:16]
    out = served_dir / f"{key}.mp3"
    file_url = f"/files/{key}.mp3"

    if out.exists():
        return {"key": key, "file": file_url, "ready": True, "cached": True}

    suffix = os.path.splitext(file.filename or "")[1] or ".mp3"
    with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        in_path = tmp.name

    with _lock:
        st = _jobs.get(key)
        if st is None or st.startswith("error:"):
            _jobs[key] = "processing"
            threading.Thread(target=_process_file, args=(key, in_path, out), daemon=True).start()

    return {"key": key, "file": file_url, "ready": False}


# ── Full-song fetch via yt-dlp ───────────────────────────────────────────────
# Given a "<track> <artist>" query, search YouTube and download the full-length
# audio as an mp3. This lifts the 30s iTunes-preview cap so the karaoke tune is
# the whole song. ffmpeg (bundled in the venv's Scripts dir) does the extraction.
def _download_audio(query):
    tmpdir = mkdtemp()
    out_tmpl = os.path.join(tmpdir, "src.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "default_search": "ytsearch1",
        "format": "bestaudio/best",
        "outtmpl": out_tmpl,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ],
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([query])
    files = glob.glob(os.path.join(tmpdir, "src.*"))
    if not files:
        return None
    mp3s = [f for f in files if f.lower().endswith(".mp3")]
    return mp3s[0] if mp3s else files[0]


def _process_search(key, query, out):
    try:
        logger.info("downloading (yt-dlp) %s: %s", key, query)
        in_path = _download_audio(query)
        if not in_path:
            raise RuntimeError("no audio found for query")
        logger.info("separating (search) %s", key)
        model.separate_instrumental(in_path, out)
        with _lock:
            _jobs[key] = "done"
        logger.info("done %s", key)
    except Exception as e:  # noqa: BLE001
        logger.exception("job %s failed", key)
        with _lock:
            _jobs[key] = f"error:{e}"


@app.post("/separate_search", status_code=200)
async def separate_search(request: Request):
    """Non-blocking. Body: {"track": "...", "artist": "..."}.
    Searches YouTube, downloads the FULL song, strips vocals. Poll /status/{key}."""
    body = await request.json()
    track = (body.get("track") or "").strip()
    artist = (body.get("artist") or "").strip()
    query = f"{track} {artist}".strip()
    if not query:
        return {"ready": False, "error": "empty query"}

    key = hashlib.sha256(query.lower().encode()).hexdigest()[:16]
    out = served_dir / f"{key}.mp3"
    file_url = f"/files/{key}.mp3"

    if out.exists():
        return {"key": key, "file": file_url, "ready": True, "cached": True}

    with _lock:
        st = _jobs.get(key)
        if st is None or st.startswith("error:"):
            _jobs[key] = "processing"
            threading.Thread(target=_process_search, args=(key, query, out), daemon=True).start()

    return {"key": key, "file": file_url, "ready": False}


@app.post("/predict", status_code=200)
async def predict(request: Request):
    """Legacy base64 endpoint. Body: {"instances": [{"b64": "..."}]}"""
    body = await request.json()
    b64 = body["instances"][0]["b64"]
    with NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
        tmp.write(base64.b64decode(b64))
        in_path = tmp.name
    return model.separate(in_path)
