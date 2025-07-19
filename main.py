from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import yt_dlp
import os
import asyncio
import subprocess
import uuid
import logging
import pathlib
import tempfile
from yt_dlp.utils import DownloadError
from datetime import datetime

# --- Paths ---
BASE_DIR      = pathlib.Path(__file__).parent.resolve()
COOKIES_FILE  = BASE_DIR / "yt.txt"
# /tmp je sigurna lokacija na većini hostova (i HF Spaces i VPS)
HLS_ROOT_BASE = pathlib.Path(tempfile.gettempdir())
HLS_ROOT      = HLS_ROOT_BASE / "hls_segments"

# --- Ensure dirs exist ---
os.makedirs(HLS_ROOT, exist_ok=True)

# --- Concurrency & Logging ---
download_semaphore = asyncio.Semaphore(30)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- FastAPI setup ---
app = FastAPI(title="YouTube Downloader with HLS", version="2.0.4")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/hls", StaticFiles(directory=str(HLS_ROOT)), name="hls")

def load_cookies_header() -> str:
    cookies = []
    if COOKIES_FILE.exists():
        with open(COOKIES_FILE, 'r') as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                parts = line.strip().split('\t')
                if len(parts) >= 7:
                    cookies.append(f"{parts[5]}={parts[6]}")
    return '; '.join(cookies)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = datetime.now()
    response = await call_next(request)
    ms = (datetime.now() - start).microseconds / 1000
    logger.info(f"{request.client.host} {request.method} {request.url} -> {response.status_code} [{ms:.1f}ms]")
    return response

@app.get("/", summary="Root")
async def root():
    return JSONResponse({"status": "ok"})

@app.get("/stream/", summary="HLS stream")
async def stream_video(request: Request, url: str = Query(...), resolution: int = Query(1080)):
    # 1) Pokušaj ekstrakciju sa kolačićima
    def extract_info(with_cookies: bool):
        opts = {
            'quiet': True,
            'no_warnings': True,
        }
        if with_cookies and COOKIES_FILE.exists():
            opts['cookiefile'] = str(COOKIES_FILE)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    try:
        info = extract_info(True)
    except DownloadError as e:
        logger.warning("extract with cookies failed, retrying without cookies: %s", e)
        try:
            info = extract_info(False)
        except DownloadError as e2:
            logger.error("extract without cookies also failed: %s", e2)
            raise HTTPException(status_code=500, detail="Could not extract video info")

    # 2) Nađi baš 1080p mp4 video-only (ili baci 404)
    try:
        vid_fmt = next(
            f for f in info['formats']
            if f.get('vcodec') != 'none' and f.get('height') == resolution and f.get('ext') == 'mp4'
        )
    except StopIteration:
        raise HTTPException(status_code=404, detail=f"No {resolution}p video stream available")

    # 3) Nađi najbolji audio-only tok
    aud_fmt = max(
        (f for f in info['formats']
         if f.get('vcodec') == 'none' and f.get('acodec') != 'none'),
        key=lambda x: x.get('abr', 0)
    )

    # 4) Priprema HLS sesije
    session_id = uuid.uuid4().hex
    sess_dir = HLS_ROOT / session_id
    os.makedirs(sess_dir, exist_ok=True)

    # 5) Buildujemo ffmpeg komandu
    cookie_header = load_cookies_header()
    hdr = []
    if cookie_header:
        hdr = ['-headers', f"User-Agent: Mozilla/5.0\r\nCookie: {cookie_header}\r\n"]
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        *hdr, '-i', vid_fmt['url'],
        *hdr, '-i', aud_fmt['url'],
        '-c:v', 'copy', '-c:a', 'copy',
        '-f', 'hls',
        '-hls_time', '4',
        '-hls_list_size', '0',
        '-hls_flags', 'delete_segments+append_list',
        '-hls_segment_filename', str(sess_dir / 'seg_%03d.ts'),
        str(sess_dir / 'index.m3u8')
    ]
    proc = subprocess.Popen(cmd, cwd=str(sess_dir))

    # 6) Sačekaj playlist (do 10s)
    playlist = sess_dir / 'index.m3u8'
    for _ in range(20):
        if playlist.exists():
            break
        await asyncio.sleep(0.5)
    else:
        proc.kill()
        raise HTTPException(status_code=500, detail="HLS playlist generation failed")

    # 7) Preusmeri klijenta na playlistu
    playlist_url = request.url_for('hls', path=f"{session_id}/index.m3u8")
    return RedirectResponse(playlist_url)
