from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging
import os
import re
import requests
from typing import Optional
from urllib.parse import quote

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

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.syncpundit.io",
    "https://pipedapi.nosebs.ru",
    "https://pipedapi.r4fo.com",
]


def search_youtube(query: str, limit: int = 20) -> list:
    for instance in PIPED_INSTANCES:
        try:
            logger.info(f"🔍 Piped deneniyor: {instance}")
            url = f"{instance}/search?q={quote(query)}&filter=music"
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"⚠️ {instance} yanıt {resp.status_code}")
                continue
            try:
                data = resp.json()
            except Exception:
                logger.warning(f"⚠️ {instance} JSON hatası")
                continue
            items = data.get("items", [])
            if not items:
                logger.warning(f"⚠️ {instance} boş sonuç")
                continue
            logger.info(f"✅ Piped başarılı: {instance} ({len(items)} sonuç)")
            break
        except Exception as e:
            logger.warning(f"⚠️ {instance} hata: {e}")
            continue
    else:
        logger.error("❌ Tüm Piped instance'ları başarısız")
        return []

    results = []
    for item in items:
        video_id = ""
        url_path = item.get("url", "")
        if url_path:
            m = re.search(r"v=([a-zA-Z0-9_-]{11})|/watch/([a-zA-Z0-9_-]{11})", url_path)
            if m:
                video_id = m.group(1) or m.group(2)
        if not video_id:
            continue

        title = item.get("title", "Bilinmeyen")
        channel = item.get("uploaderName", item.get("uploader", ""))
        duration_seconds = item.get("duration", 0)
        thumbnail = item.get("thumbnailUrl", "")

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
            "duration": duration_seconds if isinstance(duration_seconds, (int, float)) else 0,
            "coverUrl": thumbnail,
            "audioUrl": f"https://www.youtube.com/watch?v={video_id}",
            "isCopyrightFree": False,
        })

        if len(results) >= limit:
            break

    return results


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

    try:
        results = search_youtube(query, request.limit)
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
