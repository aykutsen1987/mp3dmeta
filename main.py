from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging
import os
import requests
import yt_dlp
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

YDL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.5345.16 Mobile Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.youtube.com",
    "Referer": "https://www.youtube.com",
}


def search_youtube(query: str, limit: int = 20) -> list:
    logger.info(f"🔍 iTunes Search: {query}")
    url = f"https://itunes.apple.com/search?term={quote(query)}&limit={limit}&entity=musicVideo"
    resp = requests.get(url, timeout=15)
    if resp.status_code != 200:
        logger.error(f"❌ iTunes hatası {resp.status_code}: {resp.text[:200]}")
        return []

    data = resp.json()
    items = data.get("results", [])
    logger.info(f"✅ iTunes: {len(items)} sonuç")

    results = []
    for item in items:
        track_name = item.get("trackName", item.get("trackCensoredName", "Bilinmeyen"))
        artist_name = item.get("artistName", "")

        if " - " in track_name:
            parts = track_name.split(" - ", 1)
            artist = parts[0].strip()
            song_title = parts[1].strip()
        else:
            artist = artist_name
            song_title = track_name

        artwork = (
            item.get("artworkUrl100", "")
            .replace("100x100bb", "600x600bb")
            .replace("100x100", "600x600")
        )

        track_url = item.get("trackViewUrl", "")

        results.append({
            "id": f"it_{item.get('trackId', 0)}",
            "title": song_title,
            "artist": artist,
            "uploader": artist_name,
            "duration": item.get("trackTimeMillis", 0) // 1000,
            "coverUrl": artwork,
            "audioUrl": track_url if track_url else f"https://music.apple.com/search?term={quote(track_name + ' ' + artist_name)}",
            "isCopyrightFree": True,
        })

    return results


def extract_audio(url: str, audio_format: str) -> dict:
    fmt = audio_format.lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        fmt = "mp3"

    FORMAT_MAP = {
        "mp3": "bestaudio[ext=webm]/bestaudio/best",
        "m4a": "bestaudio[ext=m4a]/bestaudio/best",
        "flac": "bestaudio/best",
    }

    ydl_opts = {
        "format": FORMAT_MAP[fmt],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "http_headers": YDL_HEADERS,
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],
                "skip": ["dash", "hls", "translated_subs"],
            }
        },
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }

    try:
        logger.info(f"🎵 Audio çıkarılıyor: {url} [{fmt}]")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise HTTPException(status_code=404, detail="Video bulunamadı")

        audio_url = None
        duration = info.get("duration", 0)
        title = info.get("title", "Bilinmeyen")
        thumbnail = info.get("thumbnail", "")
        actual_ext = info.get("ext", fmt)
        formats = info.get("formats", [])

        if fmt == "m4a":
            for f in formats:
                if f.get("ext") == "m4a" and f.get("acodec") != "none" and f.get("vcodec") == "none":
                    audio_url = f.get("url")
                    actual_ext = "m4a"
                    break
        elif fmt == "flac":
            for f in sorted(formats, key=lambda x: x.get("abr") or 0, reverse=True):
                if f.get("acodec") != "none" and f.get("vcodec") == "none":
                    audio_url = f.get("url")
                    actual_ext = f.get("ext", "webm")
                    break
        else:
            for f in formats:
                if f.get("acodec") != "none" and f.get("vcodec") == "none":
                    audio_url = f.get("url")
                    actual_ext = f.get("ext", "webm")
                    break

        if not audio_url:
            audio_url = info.get("url")
            actual_ext = info.get("ext", fmt)

        if not audio_url:
            raise HTTPException(status_code=500, detail="Audio URL çıkarılamadı")

        logger.info(f"✅ Başarılı [{fmt}/{actual_ext}]: {title[:50]}")
        return {
            "status": "success",
            "mp3_url": audio_url,
            "title": title,
            "duration": duration,
            "thumbnail": thumbnail,
            "actual_format": actual_ext,
        }

    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Audio hatası: {error_msg[:200]}")
        raise HTTPException(status_code=500, detail=f"İşleme hatası: {error_msg[:200]}")


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
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Desteklenmeyen format. Geçerli: {', '.join(SUPPORTED_FORMATS)}"
        )

    logger.info(f"Stream isteği [{fmt}]: {url}")

    return extract_audio(url, fmt)
