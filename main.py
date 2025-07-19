from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import yt_dlp
from yt_dlp import YoutubeDL, utils as ytdlp_utils
import os
import asyncio
import subprocess
import uuid
import logging
import pathlib
import tempfile
from datetime import datetime

# --- Paths ---
BASE_DIR      = pathlib.Path(__file__).parent.resolve()
COOKIES_FILE  = BASE_DIR / "yt.txt"
# HLS segments go under system temp directory
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
    """
    Read cookies file in Netscape format and build a Cookie header string.
    """
    cookies = []
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

@app.get("/stream/", summary="HLS stream with pre-download extract")
async def stream_video(request: Request, url: str = Query(...), resolution: int = Query(1080)):
    # Limit concurrent extractions
    async with download_semaphore:
        # Prepare basic ydl options with cookies and bypass
        ydl_opts_base = {
            'quiet': True,
            'no_warnings': True,
            'cookiefile': str(COOKIES_FILE),
            'nocheckcertificate': True,
            'geo_bypass': True,
        }
        info = None
        # Attempt extraction with cookies
        try:
            with YoutubeDL(ydl_opts_base) as ydl:
                info = ydl.extract_info(url, download=False)
        except ytdlp_utils.DownloadError as err:
            logger.warning(f"extract with cookies failed, retrying without cookies: {err}")
            # Retry without cookies
            opts = ydl_opts_base.copy()
            opts.pop('cookiefile', None)
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except ytdlp_utils.DownloadError as err2:
                logger.error(f"extract without cookies also failed: {err2}")
                raise HTTPException(status_code=500, detail=str(err2))
        # Ensure info is available
        if not info:
            raise HTTPException(status_code=500, detail="Failed to extract video info")
        # Select target formats
        try:
            vid_fmt = next(
                fmt for fmt in info.get('formats', [])
                if fmt.get('vcodec') != 'none' and fmt.get('height') == resolution and fmt.get('ext') == 'mp4'
            )
        except StopIteration:
            raise HTTPException(status_code=404, detail=f"No {resolution}p video stream available")
        # Best audio-only
        aud_fmt = max(
            (fmt for fmt in info.get('formats', []) if fmt.get('vcodec') == 'none' and fmt.get('acodec') != 'none'),
            key=lambda x: x.get('abr', 0)
        )
        # Create HLS output directory
        session_id = uuid.uuid4().hex
        sess_dir = HLS_ROOT / session_id
        os.makedirs(sess_dir, exist_ok=True)
        # Build ffmpeg command
        cookie_header = load_cookies_header()
        headers = ['-headers', f"User-Agent: Mozilla/5.0\r\nCookie: {cookie_header}\r\n"]
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            *headers, '-i', vid_fmt['url'],
            *headers, '-i', aud_fmt['url'],
            '-c:v', 'copy', '-c:a', 'copy',
            '-f', 'hls',
            '-hls_time', '4',
            '-hls_list_size', '0',
            '-hls_flags', 'delete_segments+append_list',
            '-hls_segment_filename', str(sess_dir / 'seg_%03d.ts'),
            str(sess_dir / 'index.m3u8')
        ]
        proc = subprocess.Popen(cmd, cwd=str(sess_dir))
        # Wait for playlist
        playlist_path = sess_dir / 'index.m3u8'
        for _ in range(20):
            if playlist_path.exists():
                break
            await asyncio.sleep(0.5)
        else:
            proc.kill()
            raise HTTPException(status_code=500, detail="HLS playlist generation failed")
        # Redirect client to HLS playlist
        playlist_url = request.url_for('hls', path=f"{session_id}/index.m3u8")
        return RedirectResponse(playlist_url)

# End of main.py
