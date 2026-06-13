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

app = FastAPI(title="MP3 Stream API", version="4.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "")

SUPPORTED_FORMATS = ["mp3", "m4a", "flac"]
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
COLAB_BACKEND_URL = os.environ.get("COLAB_BACKEND_URL", "")

YDL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.5345.16 Mobile Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.youtube.com",
    "Referer": "https://www.youtube.com",
}


# ============================================================
# ARAMA (YouTube Data API + iTunes) - Render'da çalışır
# ============================================================

def _search_youtube(query: str, limit: int = 20) -> list:
    if not YOUTUBE_API_KEY:
        return []
    logger.info(f"YouTube Search: {query}")
    try:
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet", "q": query, "type": "video",
            "videoCategoryId": "10", "maxResults": limit, "key": YOUTUBE_API_KEY,
        }
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            logger.error(f"YouTube API hatası {resp.status_code}")
            return []

        items = resp.json().get("items", [])
        video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
        if not video_ids:
            return []

        import re
        duration_map = {}
        thumbnails_map = {}
        stats_resp = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "contentDetails,snippet", "id": ",".join(video_ids), "key": YOUTUBE_API_KEY},
            timeout=15,
        )
        if stats_resp.status_code == 200:
            for video in stats_resp.json().get("items", []):
                vid = video["id"]
                raw = video.get("contentDetails", {}).get("duration", "PT0S")
                seconds = 0
                m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", raw)
                if m:
                    h, mn, s = [int(x) if x else 0 for x in m.groups()]
                    seconds = h * 3600 + mn * 60 + s
                duration_map[vid] = seconds

        results = []
        for item in items:
            vid = item.get("id", {}).get("videoId", "")
            snippet = item.get("snippet", {})
            title = snippet.get("title", "Bilinmeyen")
            channel = snippet.get("channelTitle", "")
            thumb = snippet.get("thumbnails", {})
            cover = (
                thumb.get("high", {}).get("url", "")
                or thumb.get("medium", {}).get("url", "")
                or thumb.get("default", {}).get("url", "")
            )
            results.append({
                "id": vid, "title": title, "artist": "", "uploader": channel,
                "duration": duration_map.get(vid, 0), "coverUrl": cover,
                "audioUrl": f"https://www.youtube.com/watch?v={vid}",
                "source": "youtube", "previewUrl": "",
            })

        return results
    except Exception as e:
        logger.error(f"YouTube arama hatası: {str(e)[:100]}")
        return []


def _search_itunes(query: str, limit: int = 20) -> list:
    logger.info(f"iTunes Search: {query}")
    try:
        url = f"https://itunes.apple.com/search?term={quote(query)}&limit={limit}&entity=musicVideo"
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return []

        results = []
        for item in resp.json().get("results", []):
            track_name = item.get("trackName", item.get("trackCensoredName", "Bilinmeyen"))
            artist_name = item.get("artistName", "")
            title, artist = track_name, artist_name
            if " - " in track_name:
                parts = track_name.split(" - ", 1)
                artist, title = parts[0].strip(), parts[1].strip()
            artwork = item.get("artworkUrl100", "").replace("100x100bb", "600x600bb").replace("100x100", "600x600")
            results.append({
                "id": f"it_{item.get('trackId', 0)}", "title": title, "artist": artist,
                "uploader": artist_name, "duration": item.get("trackTimeMillis", 0) // 1000,
                "coverUrl": artwork, "audioUrl": item.get("trackViewUrl", ""),
                "source": "itunes", "previewUrl": item.get("previewUrl", ""),
            })

        return results
    except Exception as e:
        logger.error(f"iTunes arama hatası: {str(e)[:100]}")
        return []


# ============================================================
# MP3 ÇIKARMA (Colab proxy veya doğrudan yt-dlp)
# ============================================================

def _build_result(info: dict, fmt: str, audio_url: str, actual_ext: str) -> dict:
    return {
        "status": "success", "mp3_url": audio_url,
        "title": info.get("title", "Bilinmeyen"),
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail", ""),
        "actual_format": actual_ext,
    }


def _try_ytdlp(url: str, fmt: str, cookies_file: str = "") -> dict | None:
    FORMAT_MAP = {
        "mp3": "bestaudio[ext=webm]/bestaudio/best",
        "m4a": "bestaudio[ext=m4a]/bestaudio/best",
        "flac": "bestaudio/best",
    }
    ydl_opts = {
        "format": FORMAT_MAP[fmt],
        "quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
        "http_headers": YDL_HEADERS,
        "extractor_args": {"youtube": {"player_client": ["android"], "skip": ["dash", "hls", "translated_subs"]}},
        "youtube_include_dash_manifest": False, "youtube_include_hls_manifest": False,
        "socket_timeout": 30, "retries": 3, "fragment_retries": 3,
    }
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.warning(f"yt-dlp başarısız: {str(e)[:100]}")
        return None

    if not info:
        return None

    audio_url = None
    actual_ext = info.get("ext", fmt)
    formats = info.get("formats", [])

    if fmt == "m4a":
        for f in formats:
            if f.get("ext") == "m4a" and f.get("acodec") != "none" and f.get("vcodec") == "none":
                audio_url = f.get("url"); actual_ext = "m4a"; break
    elif fmt == "flac":
        for f in sorted(formats, key=lambda x: x.get("abr") or 0, reverse=True):
            if f.get("acodec") != "none" and f.get("vcodec") == "none":
                audio_url = f.get("url"); actual_ext = f.get("ext", "webm"); break
    else:
        for f in formats:
            if f.get("acodec") != "none" and f.get("vcodec") == "none":
                audio_url = f.get("url"); actual_ext = f.get("ext", "webm"); break

    if not audio_url:
        audio_url = info.get("url"); actual_ext = info.get("ext", fmt)
    if not audio_url:
        return None
    return _build_result(info, fmt, audio_url, actual_ext)


def _try_pytubefix(url: str) -> dict | None:
    try:
        from pytubefix import YouTube
        yt = YouTube(url)
        if not yt or not yt.streams:
            return None
        audio = yt.streams.get_audio_only() or yt.streams.filter(only_audio=True).first()
        if not audio or not audio.url:
            return None
        info = {"title": yt.title or "Bilinmeyen", "duration": yt.length or 0, "thumbnail": yt.thumbnail_url or ""}
        return _build_result(info, "mp3", audio.url, "mp4")
    except Exception as e:
        logger.warning(f"pytubefix başarısız: {str(e)[:100]}")
        return None


COOKIES_PATH = os.environ.get("COOKIES_PATH", "/etc/secrets/cookies.txt")
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")


def _get_cookies_file() -> str:
    if YOUTUBE_COOKIES:
        import tempfile
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        f.write(YOUTUBE_COOKIES); f.close()
        logger.info(f"YOUTUBE_COOKIES env var temp dosya: {f.name}")
        return f.name
    if os.path.exists(COOKIES_PATH):
        return COOKIES_PATH
    return ""


def extract_audio(url: str, audio_format: str) -> dict:
    # Eğer COLAB_BACKEND_URL varsa, isteği Colab'a yönlendir
    if COLAB_BACKEND_URL:
        logger.info(f"Colab proxy: {COLAB_BACKEND_URL}/api/mp3")
        try:
            resp = requests.post(
                f"{COLAB_BACKEND_URL}/api/mp3",
                json={"url": url, "format": audio_format},
                timeout=120,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success" and data.get("mp3_url"):
                    logger.info(f"Colab başarılı")
                    return data
            logger.warning(f"Colab hata ({resp.status_code}): {resp.text[:200]}")
        except requests.exceptions.ConnectionError:
            logger.error("Colab bağlantı hatası - Colab kapalı olabilir")
            raise HTTPException(
                status_code=502,
                detail="Colab sunucusuna bağlanılamadı. Colab'ı yeniden başlatıp "
                       "COLAB_BACKEND_URL'i güncelle."
            )
        except Exception as e:
            logger.error(f"Colab proxy hatası: {e}")

    # Colab yoksa doğrudan dene (yt-dlp + cookies vb.)
    return _extract_direct(url, audio_format)


def _extract_direct(url: str, audio_format: str) -> dict:
    fmt = audio_format.lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        fmt = "mp3"

    logger.info(f"[1/3] yt-dlp android: {url} [{fmt}]")
    result = _try_ytdlp(url, fmt)
    if result:
        return result

    logger.info(f"[2/3] pytubefix: {url} [{fmt}]")
    result = _try_pytubefix(url)
    if result:
        return result

    cookies_file = _get_cookies_file()
    if cookies_file:
        logger.info(f"[3/3] yt-dlp cookies: {url} [{fmt}]")
        result = _try_ytdlp(url, fmt, cookies_file)
        if result:
            return result

    raise HTTPException(
        status_code=500,
        detail="YouTube bot engelini aşamadık. 3 yöntem denendi. "
               "Çözüm: Colab'da çalıştır, URL'yi COLAB_BACKEND_URL'e yaz."
    )


# ============================================================
# MODELLER VE ENDPOINT'LER
# ============================================================

class Mp3Request(BaseModel):
    url: str
    format: str = "mp3"


class SearchRequest(BaseModel):
    query: str
    limit: int = 20


def check_api_key(x_api_key: Optional[str]):
    if API_SECRET_KEY and x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Yetkisiz erişim")


@app.get("/")
def root():
    mode = "colab-proxy" if COLAB_BACKEND_URL else "direct"
    return {
        "status": "ok",
        "message": f"MP3 Stream API v4.1 ({mode})",
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
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            yt_future = pool.submit(_search_youtube, query, request.limit)
            it_future = pool.submit(_search_itunes, query, request.limit)
            yt_results = yt_future.result()
            it_results = it_future.result()

        seen = set()
        merged = []
        for r in yt_results + it_results:
            key = r["title"].lower() + r.get("artist", "").lower()
            if key not in seen:
                seen.add(key)
                merged.append(r)

        return {"status": "success", "results": merged}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Arama hatası: {str(e)[:200]}")


@app.post("/api/mp3")
def get_audio_url(
    request: Mp3Request,
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)
    url = request.url.strip()
    fmt = request.format.lower().strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL boş olamaz")
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Desteklenmeyen format. Geçerli: {', '.join(SUPPORTED_FORMATS)}")

    return extract_audio(url, fmt)
