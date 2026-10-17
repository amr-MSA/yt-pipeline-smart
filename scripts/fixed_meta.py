"""
عنوان ووصف ثابت يُدمج تلقائيًا في كل نشر.

يُضاف إلى الوصف أيضًا سطر مصدر ديناميكي يذكر اسم مستخدم الحساب الأصلي
المستخرج من بيانات yt-dlp، مع استخدام رابط المصدر عند عدم توفر اسم مستخدم.
"""
import os
import re
from urllib.parse import unquote

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXED_TITLE_PATH = os.path.join(PROJECT_DIR, "assets", "fixed_title.txt")
FIXED_DESCRIPTION_PATH = os.path.join(PROJECT_DIR, "assets", "fixed_description.txt")

YOUTUBE_TITLE_MAX = 100
YOUTUBE_DESCRIPTION_MAX = 4900


def _read_fixed(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def build_final_title(gemini_title: str) -> str:
    fixed = _read_fixed(FIXED_TITLE_PATH)
    if not fixed:
        return gemini_title[:YOUTUBE_TITLE_MAX]
    combined = f"{gemini_title} {fixed}".strip()
    if len(combined) <= YOUTUBE_TITLE_MAX:
        return combined
    # لو تجاوز الحد، نقلّم عنوان جيمناي ونحافظ على التوقيع الثابت كاملاً
    room = YOUTUBE_TITLE_MAX - len(fixed) - 1
    if room <= 0:
        return fixed[:YOUTUBE_TITLE_MAX]
    return f"{gemini_title[:room].rstrip()} {fixed}"


def _clean_handle(value: str | None) -> str:
    value = unquote(str(value or "")).strip().strip("/")
    if value.startswith("@"):
        value = value[1:]
    return value.strip().strip("/")


def source_handle_from_info(info: dict | None) -> str:
    """استخرج handle الحساب من بيانات yt-dlp، لا اسم القناة المعروض فقط."""
    info = info or {}
    for key in ("channel_handle", "uploader_id"):
        value = _clean_handle(info.get(key))
        if value and not value.startswith("UC"):
            return value

    for key in ("channel_url", "uploader_url", "webpage_url"):
        url = str(info.get(key) or "")
        match = re.search(r"/(?:@|channel/|user/|c/)([^/?#]+)", url)
        if match:
            value = _clean_handle(match.group(1))
            if value and not value.startswith("UC"):
                return value
    return ""


def source_attribution(source_handle: str | None = None, source_url: str | None = None) -> str:
    """أنشئ سطر الإسناد المطلوب، مع رابط المصدر إذا تعذر استخراج الـ handle."""
    handle = _clean_handle(source_handle)
    if handle:
        return f"📌 المصدر: هذا المقطع مأخوذ من قناة @{handle}."
    if source_url:
        return f"📌 المصدر: هذا المقطع مأخوذ من الحساب الأصلي: {source_url}"
    return "📌 المصدر: هذا المقطع مأخوذ من الحساب الأصلي، والحقوق محفوظة لأصحابه."


def build_final_description(
    gemini_description: str,
    source_handle: str | None = None,
    source_url: str | None = None,
) -> str:
    fixed = _read_fixed(FIXED_DESCRIPTION_PATH)
    attribution = source_attribution(source_handle, source_url)
    parts = [part for part in (gemini_description.strip(), attribution, fixed) if part]
    return "\n\n".join(parts)[:YOUTUBE_DESCRIPTION_MAX]
