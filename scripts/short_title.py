"""Choose a user-facing title for a video sent to the Shorts bot."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

SOCIAL_HOSTS = (
    "instagram.com",
    "instagr.am",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
)
SOCIAL_DEFAULT_TITLE = "مقطع اليوم"
YOUTUBE_TITLE_MAX = 100
GENERIC_TITLES = {
    "video", "instagram video", "facebook video", "tiktok video",
    "facebook reel", "instagram reel", "reel", "untitled", "unknown",
    "no title", "photo", "image", "post", "clip", "short", "shorts",
}
_GENERATED_BY_TITLE = re.compile(r"^(?:video|reel|photo|image)\s+by\s+.+$", re.I)
_URL_TITLE = re.compile(r"^(?:https?://|www\.)\S+$", re.I)


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


def _usable_title(value: object, identifier: str) -> str | None:
    if not isinstance(value, str):
        return None
    title = " ".join(value.split())
    if not title:
        return None
    folded = title.casefold().strip(" .:-–_")
    if identifier and folded == identifier.casefold():
        return None
    if folded in GENERIC_TITLES or _GENERATED_BY_TITLE.fullmatch(title) or _URL_TITLE.fullmatch(title):
        return None
    if re.fullmatch(r"[\w-]{6,}", title) and re.search(r"\d", title):
        return None
    return title


def _original_title(info: dict) -> str | None:
    """Return usable source metadata, excluding empty and generated placeholders."""
    identifier = str(info.get("id") or "").strip()
    for value in (info.get("title"), info.get("fulltitle")):
        title = _usable_title(value, identifier)
        if title:
            return title
    return None


def select_short_title(info: dict, source_url: str, item: dict | None = None) -> str:
    """Use a valid original source title, otherwise the universal safe default.

    ``item`` remains accepted for caller compatibility; custom overrides no longer
    supersede the user's original-title-or-default rule.
    """
    original = _original_title(info)
    return original[:YOUTUBE_TITLE_MAX].rstrip() if original else SOCIAL_DEFAULT_TITLE


def combine_original_text(info: dict) -> str:
    """Combine original title/fulltitle and description without duplicating contained text.

    Original description line breaks and wording are retained; only surrounding
    whitespace is removed when forming the combined description block.
    """
    candidates = [info.get("title"), info.get("fulltitle"), info.get("description")]
    parts: list[str] = []
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if any(text in existing for existing in parts):
            continue
        # If a later, fuller field contains an earlier title, replace the shorter
        # field in-place so each original passage is included only once.
        contained = [i for i, existing in enumerate(parts) if existing in text]
        if contained:
            first = contained[0]
            parts[first] = text
            parts[:] = [part for i, part in enumerate(parts) if i == first or part not in text]
        else:
            parts.append(text)
    return "\n\n".join(parts)
