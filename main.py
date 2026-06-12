from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import logging
import os
import tempfile
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

@app.on_event("shutdown")
def cleanup():
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        try:
            os.unlink(COOKIES_FILE)
            logger.info("🧹 Cookies temp dosyası temizlendi")
        except Exception:
            pass

SUPPORTED_FORMATS = ["mp3", "m4a", "flac"]

FORMAT_MAP = {
    "mp3":  "bestaudio[ext=webm]/bestaudio/best",
    "m4a":  "bestaudio[ext=m4a]/bestaudio/best",
    "flac": "bestaudio/best",
}

COOKIES_FILE = None
cookies_env = os.environ.get("YOUTUBE_COOKIES", "")
if cookies_env:
    try:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.write(cookies_env)
        tmp.close()
        COOKIES_FILE = tmp.name
        logger.info("✅ YouTube cookies yüklendi")
    except Exception as e:
        logger.error(f"❌ Cookies yüklenemedi: {e}")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

def get_ydl_opts(base_opts: dict) -> dict:
    opts = {
        **base_opts,
        "http_headers": BROWSER_HEADERS,
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],
                "skip": ["dash", "hls", "translated_subs"],
            }
        },
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    return opts


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

    ydl_opts = get_ydl_opts({
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": False,
    })

    try:
        search_url = f"ytsearch{request.limit}:{query} music"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)

            if not info or "entries" not in info:
                return {"status": "success", "results": []}

            results = []
            for entry in info["entries"]:
                if not entry:
                    continue

                video_id = entry.get("id", "")
                title = entry.get("title", "Bilinmeyen")
                duration = entry.get("duration", 0)
                channel = entry.get("channel", entry.get("uploader", ""))
                thumbnail = entry.get("thumbnail", "")

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
                    "duration": duration,
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

    ydl_opts = get_ydl_opts({
        "format": FORMAT_MAP[fmt],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if not info:
                raise HTTPException(status_code=404, detail="Video bulunamadı")

            audio_url = None
            duration = info.get("duration", 0)
            title = info.get("title", "Bilinmeyen")
            thumbnail = info.get("thumbnail", "")
            actual_ext = info.get("ext", fmt)  # gerçek uzantı

            # İstenen formata göre en iyi stream'i seç
            formats = info.get("formats", [])

            if fmt == "m4a":
                for f in formats:
                    if f.get("ext") == "m4a" and f.get("acodec") != "none" and f.get("vcodec") == "none":
                        audio_url = f.get("url")
                        actual_ext = "m4a"
                        break
            elif fmt == "flac":
                # FLAC direkt stream genelde yoktur; en yüksek kaliteli audio alınır
                for f in sorted(formats, key=lambda x: x.get("abr") or 0, reverse=True):
                    if f.get("acodec") != "none" and f.get("vcodec") == "none":
                        audio_url = f.get("url")
                        actual_ext = f.get("ext", "webm")
                        break
            else:  # mp3
                for f in formats:
                    if f.get("acodec") != "none" and f.get("vcodec") == "none":
                        audio_url = f.get("url")
                        actual_ext = f.get("ext", "webm")
                        break

            # Fallback
            if not audio_url:
                audio_url = info.get("url")
                actual_ext = info.get("ext", fmt)

            if not audio_url:
                raise HTTPException(status_code=500, detail="Stream URL çıkarılamadı")

            logger.info(f"Başarılı [{fmt}/{actual_ext}]: {title}")

            return {
                "status": "success",
                "title": title,
                "duration": duration,
                "thumbnail": thumbnail,
                "requested_format": fmt,
                "actual_format": actual_ext,
                "mp3_url": audio_url,   # alan adı aynı kaldı (geriye dönük uyumluluk)
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
