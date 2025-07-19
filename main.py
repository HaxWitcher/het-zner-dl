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
from datetime import datetime

# --- Paths ---
BASE_DIR      = pathlib.Path(__file__).parent.resolve()
COOKIES_FILE  = BASE_DIR / "yt.txt"
HLS_ROOT_BASE = pathlib.Path(tempfile.gettempdir())
HLS_ROOT      = HLS_ROOT_BASE / "hls_segments"

# --- Ensure dirs exist ---
os.makedirs(HLS_ROOT, exist_ok=True)

# --- Concurrency & Logging ---
download_semaphore = asyncio.Semaphore(30)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- FastAPI setup ---
app = FastAPI(title="YouTube Downloader with HLS (download → HLS)", version="2.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/hls", StaticFiles(directory=str(HLS_ROOT)), name="hls")

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

@app.get("/stream/", summary="Download + HLS stream")
async def stream_video(request: Request, url: str = Query(...), resolution: int = Query(1080)):
    await download_semaphore.acquire()
    try:
        # 1) Kreiraj zaseban folder za ovu sesiju
        session_id = uuid.uuid4().hex
        sess_dir = HLS_ROOT / session_id
        os.makedirs(sess_dir, exist_ok=True)

        # 2) Preuzmi video+audio u jedan MP4 fajl
        ydl_opts = {
            'format': f'bestvideo[height<={resolution}]+bestaudio/best',
            'outtmpl': str(sess_dir / 'input.%(ext)s'),
            'merge_output_format': 'mp4',
            'cookiefile': str(COOKIES_FILE),
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Accept-Language': 'en-US,en;q=0.9'
            },
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            input_path = pathlib.Path(ydl.prepare_filename(info))

        if not input_path.exists():
            raise HTTPException(status_code=500, detail="Download nije uspeo")

        # 3) Generiši HLS segmente iz preuzetog fajla
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-i', str(input_path),
            '-c', 'copy',
            '-f', 'hls',
            '-hls_time', '4',
            '-hls_list_size', '0',
            '-hls_flags', 'delete_segments+append_list',
            '-hls_segment_filename', str(sess_dir / 'seg_%03d.ts'),
            str(sess_dir / 'index.m3u8')
        ]
        proc = subprocess.Popen(cmd, cwd=str(sess_dir))

        # 4) Sačekaj da se playlista pojavi (do 10s)
        playlist = sess_dir / 'index.m3u8'
        for _ in range(20):
            if playlist.exists():
                break
            await asyncio.sleep(0.5)
        else:
            proc.kill()
            raise HTTPException(status_code=500, detail="HLS playlist generation failed")

        # 5) Preusmeri klijenta na HLS playlist
        playlist_url = request.url_for('hls', path=f"{session_id}/index.m3u8")
        return RedirectResponse(playlist_url)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("stream_video error", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        download_semaphore.release()
