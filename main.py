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
HLS_ROOT      = HLS_ROOT_BASE / "hls_sessions"

# --- Ensure dirs exist ---
os.makedirs(HLS_ROOT, exist_ok=True)

# --- Concurrency & Logging ---
download_semaphore = asyncio.Semaphore(30)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ytdl-hls")

# --- FastAPI setup ---
app = FastAPI(title="YouTube → HLS downloader", version="2.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/hls", StaticFiles(directory=str(HLS_ROOT)), name="hls")


def load_cookies_header() -> str:
    cookies = []
    for line in open(COOKIES_FILE, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        parts = line.strip().split("\t")
        if len(parts) >= 7:
            cookies.append(f"{parts[5]}={parts[6]}")
    return "; ".join(cookies)


@app.get("/")
async def root():
    return JSONResponse({"status": "ok"})


@app.get("/stream/", summary="Download + HLS stream")
async def stream_video(request: Request, url: str = Query(...), resolution: int = Query(1080)):
    await download_semaphore.acquire()
    try:
        # 1) Kreiraj session folder
        session_id = uuid.uuid4().hex
        sess_dir = HLS_ROOT / session_id
        os.makedirs(sess_dir, exist_ok=True)

        # 2) Download MP4
        mp4_path = sess_dir / "video.mp4"
        ydl_opts = {
            "outtmpl": str(mp4_path),
            "format": f"bestvideo[height<={resolution}]+bestaudio/best",
            "cookiefile": str(COOKIES_FILE),
            "nocheckcertificate": True,
            "geo_bypass": True,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 2,
            "extractor_args": {"youtube": {"player_client": "android"}},
        }
        logger.info("📥 Preuzimam video %s u %s …", url, mp4_path)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not mp4_path.exists():
            raise HTTPException(500, "Preuzimanje nije uspelo")

        # 3) Konvertuj u HLS
        cookie_header = load_cookies_header()
        hdr = ["-headers", f"User-Agent: Mozilla/5.0\r\nCookie: {cookie_header}\r\n"]
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(mp4_path),
            *hdr, "-i", "anullsrc",       # audio source ako nema audio track
            "-c:v", "copy", "-c:a", "aac",
            "-f", "hls",
            "-hls_time", "4",
            "-hls_list_size", "0",
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_filename", str(sess_dir / "seg_%03d.ts"),
            str(sess_dir / "index.m3u8"),
        ]
        logger.info("🚀 Pokrećem FFMPEG za HLS u %s …", sess_dir)
        proc = subprocess.Popen(cmd, cwd=str(sess_dir))

        # 4) Sačekaj da se playlist pojavi (max 10s)
        playlist = sess_dir / "index.m3u8"
        for _ in range(20):
            if playlist.exists():
                break
            await asyncio.sleep(0.5)
        else:
            proc.kill()
            raise HTTPException(500, "HLS playlist generation failed")

        # 5) Redirektuj klijenta na HLS listu
        url_hls = request.url_for("hls", path=f"{session_id}/index.m3u8")
        return RedirectResponse(url_hls)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("❌ stream_video error", exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        download_semaphore.release()
