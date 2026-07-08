import base64
import glob
import hashlib
import logging
import os
import shutil
import sys
import threading
import time
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

# YouTube bot-check bypass for cloud/datacenter IPs (Render blocks otherwise).
# - YT_COOKIES_FILE: path to a Netscape cookies.txt exported from a logged-in
#   YouTube session. On Render, add it as a Secret File named `cookies.txt`
#   (mounted at /etc/secrets/cookies.txt, the default below).
# - YTDLP_PROXY: optional http/https/socks proxy URL (e.g. a residential proxy).
YT_COOKIES_FILE = os.environ.get("YT_COOKIES_FILE", "/etc/secrets/cookies.txt")
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "")

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
# Each job is a dict: {stage: queued|downloading|separating|done|error,
#                      started: <ts when separating began>, est: <est secs>, error: <msg>}
_jobs = {}
_lock = threading.Lock()


def _set_job(key, **kw):
    with _lock:
        j = _jobs.get(key)
        if not isinstance(j, dict):
            j = {}
        j.update(kw)
        _jobs[key] = j


def _claim_job(key):
    """Mark a job as queued and return True if the caller should start it (i.e. it
    isn't already running). A previously-errored job can be retried."""
    with _lock:
        j = _jobs.get(key)
        if j is None or (isinstance(j, dict) and j.get("stage") == "error"):
            _jobs[key] = {"stage": "queued"}
            return True
        return False


def _progress(key):
    """(stage, percent) for /status. Percent during separation is estimated from
    elapsed time vs the expected duration, so the bar moves smoothly."""
    with _lock:
        j = _jobs.get(key)
    if not isinstance(j, dict):
        return "queued", 0
    stage = j.get("stage", "queued")
    if stage == "downloading":
        return "downloading", 8
    if stage == "separating":
        est = j.get("est") or 180
        frac = (time.time() - j.get("started", time.time())) / est
        return "separating", int(15 + max(0.0, min(0.95, frac)) * 80)  # 15 → 95
    if stage == "done":
        return "done", 100
    if stage == "error":
        return "error", 0
    return "queued", 3


def _process(key, audio_url, out):
    try:
        _set_job(key, stage="downloading")
        with NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            req = urllib.request.Request(audio_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                tmp.write(resp.read())
            in_path = tmp.name
        logger.info("separating %s", key)
        _set_job(key, stage="separating", started=time.time(), est=60)
        model.separate_instrumental(in_path, out)
        _set_job(key, stage="done")
        logger.info("done %s", key)
    except Exception as e:  # noqa: BLE001
        logger.exception("job %s failed", key)
        _set_job(key, stage="error", error=str(e))


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

    if _claim_job(key):
        threading.Thread(target=_process, args=(key, audio_url, out), daemon=True).start()

    return {"file": file_url, "ready": False, "stage": "queued", "percent": 0}


def _process_file(key, in_path, out):
    try:
        logger.info("separating (upload) %s", key)
        _set_job(key, stage="separating", started=time.time(), est=180)
        model.separate_instrumental(in_path, out)
        _set_job(key, stage="done")
        logger.info("done %s", key)
    except Exception as e:  # noqa: BLE001
        logger.exception("job %s failed", key)
        _set_job(key, stage="error", error=str(e))


@app.get("/status/{key}", status_code=200)
def status(key: str):
    """Poll separation progress. Returns {ready, stage, percent} (+ file when ready,
    + error on failure). stage: queued|downloading|separating|done|error."""
    out = served_dir / f"{key}.mp3"
    if out.exists():
        return {"ready": True, "file": f"/files/{key}.mp3", "stage": "done", "percent": 100}
    stage, percent = _progress(key)
    if stage == "error":
        with _lock:
            err = (_jobs.get(key) or {}).get("error", "unknown")
        return {"ready": False, "stage": "error", "percent": 0, "error": err}
    return {"ready": False, "stage": stage, "percent": percent}


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

    if _claim_job(key):
        threading.Thread(target=_process_file, args=(key, in_path, out), daemon=True).start()

    return {"key": key, "file": file_url, "ready": False, "stage": "queued", "percent": 0}


# ── Full-song fetch via yt-dlp ───────────────────────────────────────────────
# Given a "<track> <artist>" query, search for and download the FULL-length audio
# as an mp3 (no 30s preview). Sources are tried in order until one delivers:
#   1. YouTube  — best catalog, but blocks Render's datacenter IP unless cookies
#                 (YT_COOKIES_FILE) or a proxy (YTDLP_PROXY) are configured.
#   2. SoundCloud — no cookies needed; a fallback that often works from the cloud.
# ffmpeg does the mp3 extraction.
SEARCH_SOURCES = ["ytsearch1", "scsearch1"]

# Minimum length (seconds) to accept as a "full song" — rejects 30s previews.
MIN_FULL_SEC = 75


def _download_audio(query):
    base = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ],
    }
    if FFMPEG_DIR:
        base["ffmpeg_location"] = FFMPEG_DIR
    if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
        base["cookiefile"] = YT_COOKIES_FILE
        logger.info("yt-dlp using cookies: %s", YT_COOKIES_FILE)
    if YTDLP_PROXY:
        base["proxy"] = YTDLP_PROXY
        logger.info("yt-dlp using proxy")

    last_err = None
    for source in SEARCH_SOURCES:
        tmpdir = mkdtemp()
        opts = dict(base, outtmpl=os.path.join(tmpdir, "src.%(ext)s"))
        try:
            logger.info("trying source %s for: %s", source, query)
            with yt_dlp.YoutubeDL(opts) as ydl:
                # Check duration BEFORE downloading — reject 30s previews/snippets
                # (SoundCloud gates full mainstream tracks) so we never serve a clip.
                info = ydl.extract_info(f"{source}:{query}", download=False)
                entry = info["entries"][0] if info.get("entries") else info
                dur = entry.get("duration")
                if dur is not None and dur < MIN_FULL_SEC:
                    logger.warning("source %s only had a %ss snippet — skipping", source, int(dur))
                    continue
                target = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
                ydl.download([target])
            mp3s = glob.glob(os.path.join(tmpdir, "src.mp3"))
            if mp3s:
                logger.info("got full track from %s (%ss)", source, int(dur) if dur else "?")
                return mp3s[0], dur
            others = glob.glob(os.path.join(tmpdir, "src.*"))
            if others:
                return others[0], dur
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("source %s failed: %s", source, str(e).splitlines()[-1] if str(e) else e)
    if last_err:
        raise last_err
    return None, None


def _process_search(key, query, out):
    try:
        _set_job(key, stage="downloading")
        logger.info("downloading (yt-dlp) %s: %s", key, query)
        in_path, dur = _download_audio(query)
        if not in_path:
            raise RuntimeError("no audio found for query")
        logger.info("separating (search) %s", key)
        _set_job(key, stage="separating", started=time.time(), est=max(30, (dur or 180) * 1.8))
        model.separate_instrumental(in_path, out)
        _set_job(key, stage="done")
        logger.info("done %s", key)
    except Exception as e:  # noqa: BLE001
        logger.exception("job %s failed", key)
        _set_job(key, stage="error", error=str(e))


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

    if _claim_job(key):
        threading.Thread(target=_process_search, args=(key, query, out), daemon=True).start()

    return {"key": key, "file": file_url, "ready": False, "stage": "queued", "percent": 0}


@app.post("/predict", status_code=200)
async def predict(request: Request):
    """Legacy base64 endpoint. Body: {"instances": [{"b64": "..."}]}"""
    body = await request.json()
    b64 = body["instances"][0]["b64"]
    with NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
        tmp.write(base64.b64decode(b64))
        in_path = tmp.name
    return model.separate(in_path)
