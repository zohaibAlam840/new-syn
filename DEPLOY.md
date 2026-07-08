# Deploying the Demucs Karaoke Backend to Render

This service fetches a full song from YouTube (`yt-dlp`), strips the vocals with
Demucs, and serves the instrumental. The LyricFlow app calls it from the Karaoke
tab so the tune is the **whole song**, not a 30-second preview.

## What's in here (deploy files)

| File | Purpose |
|------|---------|
| `Dockerfile` | Builds the image: Python 3.12 + ffmpeg + CPU torch + Demucs + yt-dlp, bakes in the `htdemucs` model weights. |
| `requirements.txt` | Python deps (torch/torchaudio are installed separately in the Dockerfile from the CPU wheel index). |
| `render.yaml` | Render Blueprint — one-click service definition. |
| `.dockerignore` | Keeps the build context small (excludes the local venv, model cache, docs site). |
| `model/src/api.py` | FastAPI app. Endpoints below. |

## ⚠️ Sizing — read this first

Demucs loads a neural model and separates a full song **on CPU**. It needs about
**2 GB RAM**. Render's **Free and Starter (512 MB) plans will crash with an
out-of-memory error.** Use **Standard (2 GB)** or higher — that's what `render.yaml`
requests.

Separation is CPU-heavy: roughly **2–6 minutes per song** on a Standard instance
(the first ever request also loads the model, ~10s extra). The API is **non-blocking**
— every request returns immediately with a `key`, and the app polls `/status/{key}`
— so there are no long HTTP requests for Render's proxy to time out.

## Steps

### 1. Put this folder in its own Git repo
The backend currently lives inside the app repo but is git-ignored there. Render
deploys from a Git repo, so give it its own:

```bash
cd demucs-service-main
git init
git add .
git commit -m "Demucs karaoke backend"
# create an empty repo on GitHub, then:
git remote add origin https://github.com/<you>/demucs-karaoke.git
git push -u origin main
```

The `.gitignore` already excludes `model/.venv`, `model/models`, and `model/data`,
so the local venv / weights / audio are NOT pushed. Good.

### 2. Create the service on Render
**Option A — Blueprint (uses `render.yaml`):**
Render dashboard → **New → Blueprint** → connect the repo → it reads `render.yaml`
and creates the `demucs-karaoke` web service on the Standard plan. Click **Apply**.

**Option B — manual Web Service:**
New → **Web Service** → connect the repo → Runtime **Docker** → Plan **Standard** →
Health check path `/health` → Create.

First build takes ~8–12 min (downloads torch + the model). Watch the build logs.

### 3. Get the URL and point the app at it
Render gives you `https://demucs-karaoke-xxxx.onrender.com`. Verify:

```bash
curl https://demucs-karaoke-xxxx.onrender.com/health   # {"status":"healthy"}
```

Then set it in the app — [mobile-app/lib/karaoke.ts](../mobile-app/lib/karaoke.ts):

```ts
export const DEMUCS_URL = 'https://demucs-karaoke-xxxx.onrender.com';
```

(HTTPS from the phone works anywhere — unlike the old `http://192.168.x.x` LAN URL,
which only worked on the same Wi-Fi and needed cleartext-traffic allowances.)
Rebuild/reload the app and the Karaoke tab now gets full-length instrumentals.

## API (all served by `model/src/api.py`)

| Method / path | Body | Returns |
|---|---|---|
| `GET /health` | — | `{"status":"healthy"}` |
| `POST /separate_search` | `{"track","artist"}` | `{"key","file","ready"}` → poll `/status/{key}`. **Full song** via yt-dlp. |
| `POST /separate_upload` | multipart `file` | `{"key","file","ready"}` → poll `/status/{key}`. Full song from a user file. |
| `POST /karaoke` | `{"audio_url"}` | `{"file","ready"}` — separate an arbitrary audio URL (e.g. the 30s iTunes preview). |
| `GET /status/{key}` | — | `{"ready":true,"file":"/files/<key>.mp3"}` when done. |
| `GET /files/<key>.mp3` | — | the instrumental (StaticFiles). |

## ⚠️ YouTube blocks datacenter IPs — cookies required on Render

`/separate_search` downloads from YouTube. From a home connection this just works,
but **from Render's datacenter IP YouTube returns "Sign in to confirm you're not a
bot"** and the download fails. To fix it, give yt-dlp cookies from a logged-in
YouTube session. (Without cookies, `/separate_search` fails and the app falls back
to the 30s iTunes preview; `/separate_upload` — the "Import a song" button — always
works because it never touches YouTube.)

### Set up cookies (free)
1. In a browser **logged into YouTube**, install a cookies exporter extension —
   e.g. **"Get cookies.txt LOCALLY"** (Chrome/Firefox). Open `youtube.com`, click
   the extension, **Export** → you get a `cookies.txt` (Netscape format).
   - Tip: do this in a Chrome **Incognito** window logged into a throwaway Google
     account, then close it *without logging out*, so the cookie session isn't
     rotated/invalidated. Use a burner account — not your main one.
2. In the Render dashboard for the service → **Environment → Secret Files → Add
   Secret File**:
   - **Filename:** `cookies.txt`
   - **Contents:** paste the exported file
   Render mounts it at `/etc/secrets/cookies.txt` — which is the default the code
   already looks for (`YT_COOKIES_FILE`). No env var needed.
3. **Save** → Render redeploys. Done. On the next `/separate_search` the logs show
   `yt-dlp using cookies: /etc/secrets/cookies.txt` and the full download succeeds.

**Cookies expire** (YouTube rotates them every few weeks) — when `/separate_search`
starts failing again with the bot message, re-export and replace the Secret File.

### Alternative: residential proxy (no expiry)
Set env var **`YTDLP_PROXY`** to a proxy URL (e.g. `http://user:pass@host:port` from
a residential proxy provider). yt-dlp routes through it so YouTube sees a home IP.
More reliable than cookies, but costs money.

## Notes / gotchas

- **yt-dlp + JS runtime:** yt-dlp prints a *"no supported JavaScript runtime"*
  deprecation warning. Downloads work today, but YouTube occasionally tightens
  this. If searches start failing, add a JS runtime — put `deno` on the image
  (add to the `apt-get install` line: `&& curl -fsSL https://deno.land/install.sh | sh`
  or use the deno apt repo) — no code change needed, yt-dlp finds it automatically.
- **yt-dlp updates:** YouTube changes break old yt-dlp versions. If downloads fail
  after a while, bump the `yt-dlp==` pin in `requirements.txt` and redeploy.
- **Cold start:** on plans that spin down when idle, the first request after idle
  waits for the container to wake (~30s) plus the separation time. Standard does
  not spin down.
- **Ephemeral storage:** separated files are cached on the instance's disk and are
  cleared on each deploy/restart. That's fine — they re-generate on demand. (Add a
  Render Disk if you want a persistent cache.)
- **Copyright:** this downloads full copyrighted tracks to your server. Fine for
  testing/demo; a licensing question for public release (see the karaoke note in
  the root `CLAUDE.md`).

## Local test (Docker)

```bash
cd demucs-service-main
docker build -t demucs-karaoke .
docker run -p 8080:8080 demucs-karaoke
# then:
curl -X POST http://localhost:8080/separate_search \
  -H "Content-Type: application/json" \
  -d '{"track":"Blinding Lights","artist":"The Weeknd"}'
# poll /status/<key> until ready, then GET /files/<key>.mp3
```
