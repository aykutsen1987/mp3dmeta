from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import logging
import os
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="MP3 Stream API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Render Environment Variable'dan alınır
# Render Dashboard → Environment → API_SECRET_KEY = istediğin şifre
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "")


class Mp3Request(BaseModel):
    url: str


def check_api_key(x_api_key: Optional[str]):
    """API key kontrolü - eğer .env'de set edildiyse kontrol eder"""
    if API_SECRET_KEY and x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Yetkisiz erişim")


@app.get("/")
def root():
    return {"status": "ok", "message": "MP3 Stream API v2.0 çalışıyor"}


@app.post("/api/mp3")
def get_mp3_url(
    request: Mp3Request,
    x_api_key: Optional[str] = Header(default=None)
):
    """
    YouTube URL'sini alır, direkt stream edilebilir audio URL döndürür.
    Android uygulaması bu URL'yi ExoPlayer ile çalar veya indirir.
    """
    check_api_key(x_api_key)

    url = request.url.strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL boş olamaz")

    if "youtube.com" not in url and "youtu.be" not in url:
        raise HTTPException(status_code=400, detail="Sadece YouTube URL'leri destekleniyor")

    logger.info(f"İstek alındı: {url}")

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if not info:
                raise HTTPException(status_code=404, detail="Video bulunamadı")

            audio_url = None
            duration = info.get("duration", 0)
            title = info.get("title", "Bilinmeyen")
            thumbnail = info.get("thumbnail", "")

            formats = info.get("formats", [])

            for fmt in formats:
                if fmt.get("acodec") != "none" and fmt.get("vcodec") == "none":
                    if fmt.get("url"):
                        audio_url = fmt["url"]
                        break

            if not audio_url:
                audio_url = info.get("url")

            if not audio_url:
                raise HTTPException(status_code=500, detail="Stream URL çıkarılamadı")

            logger.info(f"Başarılı: {title}")

            return {
                "status": "success",
                "title": title,
                "duration": duration,
                "thumbnail": thumbnail,
                "mp3_url": audio_url,
            }

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"yt-dlp hatası: {error_msg}")

        if "Private video" in error_msg:
            raise HTTPException(status_code=403, detail="Bu video özel")
        elif "not available" in error_msg:
            raise HTTPException(status_code=404, detail="Video mevcut değil")
        elif "copyright" in error_msg.lower():
            raise HTTPException(status_code=403, detail="Telif hakkı kısıtlaması")
        else:
            raise HTTPException(status_code=500, detail=f"Video işlenemedi: {error_msg[:200]}")

    except Exception as e:
        logger.error(f"Beklenmeyen hata: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Sunucu hatası: {str(e)[:200]}")
