from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import logging
import os
import requests
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="MP3 Stream API", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "")

SUPPORTED_FORMATS = ["mp3", "m4a", "flac"]

COBALT_API_URL = os.environ.get("COBALT_API_URL", "http://localhost:9000")

COBALT_AUDIO_FORMAT_MAP = {
    "mp3": "mp3",
    "m4a": "best",
    "flac": "best",
}

def call_cobalt(url: str, audio_format: str) -> dict:
    cobalt_format = COBALT_AUDIO_FORMAT_MAP.get(audio_format, "mp3")
    payload = {
        "url": url,
        "audioFormat": cobalt_format,
        "audioBitrate": "320",
        "downloadMode": "audio",
        "youtubeHLS": True,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    logger.info(f"☕ Cobalt'a yönlendiriliyor: {url} [{audio_format}]")
    resp = requests.post(f"{COBALT_API_URL}/", json=payload, headers=headers, timeout=120)
    if resp.status_code != 200:
        logger.error(f"❌ Cobalt hatası {resp.status_code}: {resp.text[:200]}")
        raise HTTPException(status_code=502, detail=f"Cobalt hatası: {resp.text[:200]}")
    data = resp.json()
    if data.get("status") in ("tunnel", "redirect") and data.get("url"):
        return {
            "status": "success",
            "mp3_url": data["url"],
            "title": data.get("filename", ""),
            "duration": 0,
        }
    error_msg = data.get("error", {}).get("message", "Cobalt işleme hatası")
    logger.error(f"❌ Cobalt: {error_msg}")
    raise HTTPException(status_code=500, detail=error_msg)


class Mp3Request(BaseModel):
    url: str
    format: str = "mp3"   # mp3 | m4a | flac


class SearchRequest(BaseModel):
    query: str
    limit: int = 20


def check_api_key(x_api_key: Optional[str]):
    if API_SECRET_KEY and x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Yetkisiz erişim")


@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "MP3 Stream API v4.0 çalışıyor",
        "supported_formats": SUPPORTED_FORMATS,
    }


@app.post("/api/search")
def search_music(
    request: SearchRequest,
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)

    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Arama terimi boş olamaz")

    logger.info(f"Arama: {query}")

    search_url = f"ytsearch{request.limit}:{query} music"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "noplaylist": False,
        "playlistend": request.limit,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)

            if not info:
                logger.warning("⚠️ yt-dlp boş yanıt döndü")
                return {"status": "success", "results": []}

            entries = info.get("entries")
            if entries is None:
                entries = info.get("items")
            if entries is None:
                logger.warning(f"⚠️ yt-dlp yanıtında entry bulunamadı: {list(info.keys())}")
                return {"status": "success", "results": []}

            logger.info(f"✅ yt-dlp {len(entries)} sonuç döndü")

            results = []
            for entry in entries:
                if not entry:
                    continue

                video_id = entry.get("id", "")
                if not video_id and "url" in entry:
                    import re
                    m = re.search(r"[&?]v=([a-zA-Z0-9_-]{11})", entry["url"])
                    if m:
                        video_id = m.group(1)
                if not video_id:
                    continue

                title = entry.get("title", "Bilinmeyen")
                duration = entry.get("duration", 0)
                channel = entry.get("channel", entry.get("uploader", entry.get("creator", "")))
                thumbnail = entry.get("thumbnail", "")
                webpage_url = entry.get("webpage_url", entry.get("url", ""))

                if not thumbnail and video_id:
                    thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

                if " - " in title:
                    parts = title.split(" - ", 1)
                    artist = parts[0].strip()
                    song_title = parts[1].strip()
                else:
                    artist = channel
                    song_title = title

                results.append({
                    "id": video_id,
                    "title": song_title,
                    "artist": artist,
                    "uploader": channel,
                    "duration": duration if isinstance(duration, (int, float)) else 0,
                    "coverUrl": thumbnail,
                    "audioUrl": f"https://www.youtube.com/watch?v={video_id}",
                    "isCopyrightFree": False,
                })

            logger.info(f"Arama sonucu: {len(results)} şarkı")
            return {"status": "success", "results": results}

    except Exception as e:
        logger.error(f"Arama hatası: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Arama hatası: {str(e)[:200]}")


@app.post("/api/mp3")
def get_audio_url(
    request: Mp3Request,
    x_api_key: Optional[str] = Header(default=None)
):
    """
    YouTube URL + format alır, stream edilebilir audio URL döndürür.
    format: mp3 | m4a | flac
    """
    check_api_key(x_api_key)

    url = request.url.strip()
    fmt = request.format.lower().strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL boş olamaz")
    if "youtube.com" not in url and "youtu.be" not in url:
        raise HTTPException(status_code=400, detail="Sadece YouTube URL'leri destekleniyor")
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Desteklenmeyen format. Geçerli: {', '.join(SUPPORTED_FORMATS)}"
        )

    logger.info(f"Stream isteği [{fmt}]: {url}")

    try:
        return call_cobalt(url, fmt)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Stream hatası: {e}")
        raise HTTPException(status_code=500, detail=f"İşleme hatası: {str(e)[:200]}")
