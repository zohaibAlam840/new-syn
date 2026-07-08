"""Generate the offline karaoke catalog for the mobile app.

For each song: download the full track (yt-dlp, run from a residential IP so
YouTube doesn't bot-block), strip vocals with Demucs, and fetch synced lyrics +
artwork. Writes the instrumental mp3s into the app's assets and a catalog.json.

Run:  ../.venv/Scripts/python.exe gen_catalog.py
"""
import glob
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from tempfile import mkdtemp

os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")

import yt_dlp  # noqa: E402
from model import Demucs  # noqa: E402

APP_ASSETS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "mobile-app", "assets", "karaoke")
)
os.makedirs(APP_ASSETS, exist_ok=True)

# id (slug) → (track, artist). 8 karaoke staples with reliable synced lyrics.
SONGS = [
    ("blinding-lights", "Blinding Lights", "The Weeknd"),
    ("shape-of-you", "Shape of You", "Ed Sheeran"),
    ("someone-like-you", "Someone Like You", "Adele"),
    ("viva-la-vida", "Viva La Vida", "Coldplay"),
    ("someone-you-loved", "Someone You Loved", "Lewis Capaldi"),
    ("let-her-go", "Let Her Go", "Passenger"),
    ("all-of-me", "All of Me", "John Legend"),
    ("just-the-way-you-are", "Just the Way You Are", "Bruno Mars"),
]

FFMPEG_DIR = os.path.dirname(sys.executable)
model = Demucs(output_dir=mkdtemp(), load=False)


def download(query):
    tmp = mkdtemp()
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp, "src.%(ext)s"),
        "ffmpeg_location": FFMPEG_DIR,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"ytsearch1:{query}"])
    mp3s = glob.glob(os.path.join(tmp, "src.mp3"))
    return mp3s[0] if mp3s else None


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lyricflow-catalog/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def parse_lrc(lrc):
    lines = []
    for ln in lrc.splitlines():
        m = re.match(r"\[(\d+):(\d+)[.:](\d+)\](.*)", ln)
        if not m:
            continue
        mm, ss, cs, txt = m.groups()
        t = int(mm) * 60 + int(ss) + int(cs) / (100 if len(cs) == 2 else 1000)
        txt = txt.strip()
        if txt:
            lines.append({"time": round(t, 2), "text": txt})
    return lines


def lyrics(track, artist):
    url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(
        {"track_name": track, "artist_name": artist}
    )
    try:
        data = _get_json(url)
    except Exception as e:  # noqa: BLE001
        print("  lyrics fetch failed:", e)
        return [], None, None
    for item in data:
        if item.get("syncedLyrics"):
            return parse_lrc(item["syncedLyrics"]), item.get("plainLyrics"), item.get("duration")
    if data:
        return [], data[0].get("plainLyrics"), data[0].get("duration")
    return [], None, None


def artwork(track, artist):
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": f"{track} {artist}", "media": "music", "entity": "song", "limit": 1}
    )
    try:
        r = _get_json(url)["results"][0]
        return r.get("artworkUrl100", "").replace("100x100", "500x500") or None
    except Exception:  # noqa: BLE001
        return None


def main():
    catalog = []
    for slug, track, artist in SONGS:
        print("===", track, "—", artist)
        try:
            src = download(f"{track} {artist}")
            if not src:
                print("  DOWNLOAD FAILED, skipping")
                continue
            out = os.path.join(APP_ASSETS, slug + ".mp3")
            model.separate_instrumental(src, out)
            lines, plain, dur = lyrics(track, artist)
            art = artwork(track, artist)
            catalog.append({
                "id": slug, "title": track, "artist": artist,
                "durationSec": dur, "artworkUrl": art,
                "lines": lines, "plainLyrics": plain,
            })
            print(f"  OK  {os.path.getsize(out)//1024} KB, {len(lines)} synced lines")
            # write incrementally so a crash keeps what's done
            with open(os.path.join(APP_ASSETS, "catalog.json"), "w", encoding="utf-8") as f:
                json.dump(catalog, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            print("  ERROR:", e)
    print("DONE —", len(catalog), "songs ->", APP_ASSETS)


if __name__ == "__main__":
    main()
