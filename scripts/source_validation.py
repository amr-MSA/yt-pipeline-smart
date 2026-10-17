"""Validate source URLs before attempting YouTube authentication."""
from __future__ import annotations

import yt_dlp

from cookie_utils import temporary_cookie_file


def probe_source_url(url: str, cookies_env: str | None = None) -> dict:
    """Resolve a source URL with yt-dlp without downloading media.

    This intentionally runs before YouTube OAuth so source failures (private,
    deleted, unsupported, or blocked URLs) are reported as source failures.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "js_runtimes": {"deno": {}},
    }
    with temporary_cookie_file(cookies_env) as cookie_path:
        if cookie_path:
            ydl_opts["cookiefile"] = cookie_path
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("لم يُرجع المصدر بيانات قابلة للمعالجة")
    return info
