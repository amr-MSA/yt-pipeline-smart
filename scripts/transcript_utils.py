"""
الطبقة الأولى من خط long-smart: سحب الملف النصي (نص + توقيت).

بالنسبة لروابط YouTube، نستخدم youtube-transcript-api أولًا حتى لا نحتاج
كوكيز yt-dlp في مرحلة التحليل. يبقى yt-dlp كخطة بديلة، وكذلك لتنزيل
المقاطع لاحقًا في long_smart_pipeline.py.
"""
import glob
import os
import re
from urllib.parse import parse_qs, urlsplit

import requests
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig

from cookie_utils import temporary_cookie_file


def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return text.strip()


def _parse_vtt(path: str) -> list[dict]:
    """يحوّل ملف .vtt إلى قائمة [{start, end, text}, ...] بالثواني."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = re.compile(
        r"(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})[^\n]*\n(.*?)(?=\n\n|\Z)",
        re.DOTALL,
    )

    def to_seconds(ts: str) -> float:
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    segments = []
    last_text = None
    for m in pattern.finditer(content):
        start, end, text = m.group(1), m.group(2), m.group(3)
        clean = _clean_text(text)
        if not clean or clean == last_text:
            continue
        segments.append({"start": round(to_seconds(start), 2), "end": round(to_seconds(end), 2), "text": clean})
        last_text = clean
    return segments


def split_segments(segments: list[dict], max_chars: int = 45000) -> list[list[dict]]:
    """يقسم الترانسكريبت إلى أجزاء مستقلة دون قطع segment زمنيًا."""
    if not segments:
        return []
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for segment in segments:
        segment_chars = len(segment.get("text", "")) + 32
        if current and current_chars + segment_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += segment_chars
    if current:
        chunks.append(current)
    return chunks


def _youtube_video_id(url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/").split("/")[0] or None
    if host.endswith("youtube.com"):
        path_parts = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        if path_parts and path_parts[0] in {"live", "shorts", "embed", "v"}:
            return path_parts[1] if len(path_parts) > 1 else None
    return None


def _youtube_oembed(url: str, video_id: str) -> tuple[str, dict]:
    """يجلب عنوانًا وبيانات قناة عامة دون استخدام yt-dlp أو كوكيز."""
    fallback_title = f"YouTube video {video_id}"
    try:
        response = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("title") or fallback_title, {
            "uploader": data.get("author_name", ""),
            "uploader_url": data.get("author_url", ""),
            "webpage_url": url,
        }
    except requests.RequestException as exc:
        print(f"⚠️ تعذر جلب بيانات oEmbed لـ YouTube، سيتم استخدام عنوان احتياطي: {exc}")
        return fallback_title, {"webpage_url": url}


def _fetch_youtube_transcript_api(url: str, video_id: str) -> dict:
    """يستخرج الترجمة العامة من YouTube مع التوقيت دون yt-dlp cookies."""
    proxy_url = os.environ.get("YT_TRANSCRIPT_PROXY", "").strip()
    api = YouTubeTranscriptApi(
        proxy_config=(
            GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
            if proxy_url
            else None
        )
    )
    transcript = api.fetch(video_id, languages=["ar", "en"])
    segments = []
    for snippet in transcript:
        start = float(snippet.start)
        end = start + float(snippet.duration)
        text = _clean_text(snippet.text)
        if text:
            segments.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    if not segments:
        raise RuntimeError("youtube-transcript-api أعاد ترانسكريبت فارغًا.")
    title, source_info = _youtube_oembed(url, video_id)
    duration = max(segment["end"] for segment in segments)
    return {
        "video_id": video_id,
        "title": title,
        "duration": duration,
        "source_info": source_info,
        "segments": segments,
    }


def _fetch_with_ytdlp(url: str, work_dir: str, cookies_env: str | None = None) -> dict:
    """Fallback للمنصات غير المدعومة أو عند فشل API الترانسكريبت."""
    os.makedirs(work_dir, exist_ok=True)
    sub_out = os.path.join(work_dir, "sub")
    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["ar", "en", "ar.*", "en.*"],
        "subtitlesformat": "vtt",
        "outtmpl": sub_out,
        "quiet": True,
        "noprogress": True,
        "js_runtimes": {"deno": {}},
    }
    with temporary_cookie_file(cookies_env) as cookie_path:
        if cookie_path:
            ydl_opts["cookiefile"] = cookie_path
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

    vtt_candidates = sorted(glob.glob(f"{sub_out}*.vtt"))
    if not vtt_candidates:
        raise RuntimeError(
            "لا يوجد ترجمة/ترانسكريبت متاح لهذا الفيديو (لا يدوي ولا تلقائي). "
            "لا يمكن متابعة معالجة /long الذكية بدونه."
        )
    segments = _parse_vtt(vtt_candidates[0])
    if not segments:
        raise RuntimeError("تم العثور على ملف ترجمة لكنه فارغ بعد التحليل.")
    return {
        "video_id": info.get("id", ""),
        "title": info.get("title", ""),
        "duration": info.get("duration", 0),
        "source_info": info,
        "segments": segments,
    }


def fetch_transcript(url: str, work_dir: str, cookies_env: str | None = None) -> dict:
    """يستخدم API الترانسكريبت لـ YouTube، ثم يعود إلى yt-dlp عند الحاجة."""
    video_id = _youtube_video_id(url)
    if video_id:
        try:
            print(f"📝 YouTube Transcript API: استخراج النص للفيديو {video_id}...")
            result = _fetch_youtube_transcript_api(url, video_id)
            print(f"✅ تم استخراج {len(result['segments'])} جزءًا دون yt-dlp cookies.")
            return result
        except Exception as exc:
            print(f"⚠️ فشل YouTube Transcript API، سيتم تجربة yt-dlp كخطة بديلة: {exc}")
    return _fetch_with_ytdlp(url, work_dir, cookies_env)
