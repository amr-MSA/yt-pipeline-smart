"""
عدّاد نشر يومي "ذكي" لخط long-smart.

- عداد محلي: يُصفَّر تلقائيًا عند تغيّر اليوم (بتوقيت UTC).
- كشف ذكي لحد يوتيوب: إذا رجع من يوتيوب خطأ كوتا (quotaExceeded / dailyLimitExceeded)
  نعتبر أننا وصلنا للحد فورًا، حتى لو العداد المحلي لسّه ما وصل، ونحفظ ذلك
  في نفس ملف الحالة لتفادي محاولات رفع فاشلة متكررة تهدر الوقت.

ملاحظة: هذا عداد **منفصل** عن أي عدّاد آخر في المشروع (خط اللونق الفوري
وخط الشورتس الحالي لا يستخدمان هذا الملف إطلاقًا) — حسب طلب المستخدم.
"""
import datetime
import json
import os

QUOTA_FILE_NAME = "long_smart_quota.json"
DEFAULT_DAILY_LIMIT = 8

# رسائل/أكواد يوتيوب الشائعة عند تجاوز حصة الـ API اليومية
QUOTA_ERROR_MARKERS = [
    "quotaexceeded",
    "dailylimitexceeded",
    "userratelimitexceeded",
    "quota_exceeded",
    "the request cannot be completed because you have exceeded your",
]


def _today_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _quota_file(state_dir: str) -> str:
    return os.path.join(state_dir, QUOTA_FILE_NAME)


def _load(state_dir: str) -> dict:
    path = _quota_file(state_dir)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    today = _today_str()
    if data.get("date") != today:
        # يوم جديد: نصفّر العداد وعلم "وصلنا للحد"
        data = {"date": today, "published_count": 0, "limit_hit": False}
    return data


def _save(state_dir: str, data: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = _quota_file(state_dir)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def can_publish(state_dir: str, daily_limit: int = DEFAULT_DAILY_LIMIT) -> bool:
    """يرجع True إذا لسّه فيه مجال للنشر اليوم."""
    data = _load(state_dir)
    if data.get("limit_hit"):
        return False
    return data.get("published_count", 0) < daily_limit


def remaining_slots(state_dir: str, daily_limit: int = DEFAULT_DAILY_LIMIT) -> int:
    data = _load(state_dir)
    if data.get("limit_hit"):
        return 0
    return max(0, daily_limit - data.get("published_count", 0))


def record_publish_success(state_dir: str) -> None:
    """يُستدعى بعد كل نشر ناجح لزيادة العداد."""
    data = _load(state_dir)
    data["published_count"] = data.get("published_count", 0) + 1
    _save(state_dir, data)


def is_quota_error(exception: Exception) -> bool:
    """يفحص نص الخطأ القادم من يوتيوب API ليكتشف إذا كان بسبب تجاوز الحصة."""
    text = str(exception).lower()
    return any(marker in text for marker in QUOTA_ERROR_MARKERS)


def record_quota_hit(state_dir: str) -> None:
    """يُستدعى عند اكتشاف خطأ كوتا من يوتيوب، لإيقاف أي محاولات نشر أخرى اليوم فورًا."""
    data = _load(state_dir)
    data["limit_hit"] = True
    _save(state_dir, data)
