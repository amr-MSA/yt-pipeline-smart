"""Idempotency guard backed by the channel's latest YouTube uploads."""
from __future__ import annotations

import re

REQUEST_MARKER_PREFIX = "yt-pipeline-request:"
# الصيغة الجديدة قصيرة وقابلة للقراءة: #<bot>-<chat_id>-<message_id>.
# Long = 1، وبوت Shorts (ويشمل long-smart الذي يعمل من خلاله) = 2.
BOT_CODES = {"long": "1", "shorts": "2", "long-smart": "2"}
CODE_MARKER_RE = re.compile(r"(?<![\w#])#[12]-[^\s]+")
# نقرأ الصيغة القديمة حتى تبقى المقاطع المنشورة قبل هذا التغيير قابلة للاكتشاف.
LEGACY_MARKER_RE = re.compile(r"yt-pipeline-request:[^\s]+")


def request_marker(pipeline: str, request_key: str) -> str:
    """Return a stable # marker shared by retries of the same request.

    request_key is normally ``chat_id:message_id``. The first digit after #
    identifies the bot, while the rest preserves the Telegram request identity.
    """
    try:
        bot_code = BOT_CODES[pipeline]
    except KeyError as exc:
        raise ValueError(f"Unknown pipeline for request marker: {pipeline}") from exc
    safe_key = str(request_key).replace(":", "-")
    return f"#{bot_code}-{safe_key}"


def recent_request_markers(youtube, limit: int = 10) -> dict[str, str]:
    """Read markers from the channel's latest uploads, including Long and Shorts.

    This intentionally fails closed: callers should abort before uploading if the
    check cannot be completed. YouTube has no idempotency key for videos.insert,
    so this is the external source-of-truth safety net when local state is lost.
    """
    if limit != 10:
        raise ValueError("The duplicate guard is deliberately fixed to the latest 10 uploads")

    channels = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = channels.get("items") or []
    if not items:
        raise RuntimeError("لم يتم العثور على قناة YouTube للحساب الموثق")
    uploads_playlist = (
        items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    )
    if not uploads_playlist:
        raise RuntimeError("تعذر تحديد قائمة فيديوهات القناة")

    playlist = youtube.playlistItems().list(
        part="contentDetails", playlistId=uploads_playlist, maxResults=10
    ).execute()
    video_ids = [
        item.get("contentDetails", {}).get("videoId")
        for item in playlist.get("items", [])
    ]
    video_ids = [video_id for video_id in video_ids if video_id]
    if not video_ids:
        return {}

    videos = youtube.videos().list(
        part="snippet", id=",".join(video_ids)
    ).execute()
    markers: dict[str, str] = {}
    for item in videos.get("items", []):
        description = item.get("snippet", {}).get("description") or ""
        video_id = item.get("id")
        if not video_id:
            continue
        for marker in CODE_MARKER_RE.findall(description):
            markers[marker] = video_id
        for marker in LEGACY_MARKER_RE.findall(description):
            markers[marker] = video_id
    return markers


def append_request_marker(description: str, marker: str) -> str:
    """Append a searchable, stable idempotency marker to a YouTube description."""
    description = (description or "").rstrip()
    suffix = f"\n\n{marker}"
    available = max(0, 4900 - len(suffix))
    return description[:available].rstrip() + suffix
