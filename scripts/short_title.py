"""Choose a user-facing title for a video sent to the Shorts bot."""
from __future__ import annotations

from urllib.parse import urlsplit

SOCIAL_HOSTS = (
    "instagram.com",
    "instagr.am",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
)
SOCIAL_DEFAULT_TITLE = "مقطع اليوم"
GENERIC_TITLES = {
    "video",
    "instagram video",
    "facebook video",
    "tiktok video",
    "facebook reel",
    "instagram reel",
    "reel",
    "untitled",
    "unknown",
    "no title",
}


def is_social_video(info: dict, source_url: str) -> bool:
    """Return whether yt-dlp metadata or the URL identifies a social platform."""
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").casefold()
    if any(platform in extractor for platform in ("instagram", "tiktok", "facebook")):
        return True

    try:
        host = (urlsplit(source_url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in SOCIAL_HOSTS)


def _original_title(info: dict) -> str | None:
    """Return usable source metadata, excluding empty and obvious placeholders."""
    identifier = str(info.get("id") or "").strip()
    for value in (info.get("title"), info.get("fulltitle")):
        if not isinstance(value, str):
            continue
        title = " ".join(value.split())
        if not title:
            continue
        if identifier and title.casefold() == identifier.casefold():
            continue
        if title.casefold().strip(" .:-–_") in GENERIC_TITLES:
            continue
        return title
    return None


def select_short_title(info: dict, source_url: str, item: dict | None = None) -> str:
    """Use an explicit message title, otherwise the source title or a safe default."""
    item = item or {}
    if item.get("title_mode") == "custom":
        override = item.get("title_override")
        if isinstance(override, str) and override.strip():
            return " ".join(override.split())

    original = _original_title(info)
    if original:
        return original
    if is_social_video(info, source_url):
        return SOCIAL_DEFAULT_TITLE
    return "شورت جديد"
