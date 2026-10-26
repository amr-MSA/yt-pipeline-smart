"""
إدارة طوابير مقاطع long-smart.

تُعالج الطوابير بترتيب الإدراج العالمي، مع إبقاء كل مقطع غير محسوم في مقدمة
الطابور. الفشل المؤكد يسمح بمحاولة ثانية في يوم UTC التالي فقط؛ أما نتيجة الرفع
الملتبسة فتبقى حاجزًا إلى أن تؤكد YouTube النشر أو تُحسم يدويًا.
"""
from __future__ import annotations

import datetime
import glob
import hashlib
import json
import os
import subprocess

QUEUE_DIR_NAME = "long_smart_queue"
QUOTA_FILE_NAME = "long_smart_quota.json"
MAX_CLIPS_PER_FILE = 8
MAX_CONFIRMED_FAILURES = 2

_GIT_ADDED_AT_CACHE: dict[str, float | None] = {}


def _queue_dir(state_dir: str) -> str:
    path = os.path.join(state_dir, QUEUE_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def queue_id_for_request(request_key: str) -> str:
    """معرّف ملف ثابت وآمن مبني على chat_id:message_id، لا على الرابط."""
    return hashlib.sha256(request_key.encode("utf-8")).hexdigest()[:24]


def _queue_file(state_dir: str, queue_id: str) -> str:
    return os.path.join(_queue_dir(state_dir), f"{queue_id}.json")


def queue_exists(state_dir: str, queue_id: str) -> bool:
    return os.path.exists(_queue_file(state_dir, queue_id))


def split_into_files(clips: list[dict], size: int = MAX_CLIPS_PER_FILE) -> list[list[dict]]:
    """يقسّم المقاطع إلى ملفات Queue لا يتجاوز كل منها `size` مقاطع."""
    if size <= 0:
        raise ValueError("حجم الملف يجب أن يكون أكبر من صفر")
    return [clips[i:i + size] for i in range(0, len(clips), size)]


def batch_clip_count(state_dir: str, queue_id: str) -> int:
    """عدد المقاطع في ملف Queue محفوظ، أو صفر إن لم يوجد."""
    try:
        return len(_load_batch(_queue_file(state_dir, queue_id)).get("clips", []))
    except (OSError, ValueError):
        return 0


def _normalize_created_at(value: int | float | str | None) -> str:
    if value is None:
        stamp = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(value, (int, float)):
        stamp = datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    else:
        text = str(value).strip()
        try:
            stamp = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=datetime.timezone.utc)
            stamp = stamp.astimezone(datetime.timezone.utc)
        except ValueError:
            stamp = datetime.datetime.now(datetime.timezone.utc)
    return stamp.isoformat(timespec="seconds")


def save_new_batch(
    state_dir: str,
    queue_id: str,
    request_key: str,
    video_id: str,
    source_url: str,
    source_title: str,
    chat_id: int,
    clips: list[dict],
    source_handle: str = "",
    part_index: int = 0,
    part_count: int = 1,
    file_index: int = 0,
    created_at: int | float | str | None = None,
    fifo_order: int | None = None,
) -> None:
    """يحفظ ملف Queue مع وقت إدراجه لترتيبه عالميًا بنظام FIFO."""
    data = {
        "video_id": video_id,
        "queue_id": queue_id,
        "request_key": request_key,
        "created_at": _normalize_created_at(created_at),
        "fifo_order": int(fifo_order) if fifo_order is not None else None,
        "source_url": source_url,
        "source_title": source_title,
        "source_handle": source_handle,
        "chat_id": chat_id,
        "part_index": part_index,
        "part_count": part_count,
        "file_index": file_index,
        "clips": [
            {
                **clip,
                "status": "pending",
                "youtube_url": None,
            }
            for clip in clips
        ],
    }
    _save_batch(_queue_file(state_dir, queue_id), data)


def _load_batch(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_batch(path: str) -> dict:
    """تحميل Queue محفوظ لإعادة قراءة حالته بعد كل نشر."""
    return _load_batch(path)


def _save_batch(path: str, data: dict) -> None:
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _utc_today(today: datetime.date | str | None = None) -> datetime.date:
    if isinstance(today, datetime.datetime):
        return today.astimezone(datetime.timezone.utc).date()
    if isinstance(today, datetime.date):
        return today
    if today:
        return datetime.date.fromisoformat(str(today))
    return datetime.datetime.now(datetime.timezone.utc).date()


def _as_date(value: str | datetime.date | None) -> datetime.date | None:
    if isinstance(value, datetime.date):
        return value
    if value:
        try:
            return datetime.date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


def _git_added_at(path: str) -> float | None:
    """وقت إضافة ملف طابور قديم، لتثبيت FIFO عند ترحيل السجلات السابقة."""
    if path in _GIT_ADDED_AT_CACHE:
        return _GIT_ADDED_AT_CACHE[path]
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    relative = os.path.relpath(path, repo_root)
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--diff-filter=A", "-1", "--format=%ct", "--", relative],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        value = float(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        value = None
    _GIT_ADDED_AT_CACHE[path] = value
    return value


def _timestamp(value: int | float | str | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return stamp.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _natural_request_key(request_key: str) -> tuple:
    """ترتيب رقمي ثابت لطلبات الطوابير القديمة التي لا تحمل وقت إدراج."""
    parts = []
    for part in str(request_key).split(":"):
        parts.append((0, int(part)) if part.isdigit() else (1, part.casefold()))
    return tuple(parts)


def _batch_order_key(item: dict) -> tuple:
    created = _timestamp(item.get("created_at"))
    if created is None:
        created = _git_added_at(item.get("_path", ""))
    try:
        fifo_order = int(item["fifo_order"]) if item.get("fifo_order") is not None else None
    except (TypeError, ValueError):
        fifo_order = None
    secondary = (
        (0, fifo_order) if fifo_order is not None else (1, 0),
        _natural_request_key(item.get("request_key", "")),
        int(item.get("part_index", 0)),
        int(item.get("file_index", 0)),
        item.get("queue_id", ""),
    )
    # السجلات القديمة التي تعذر استرجاع تاريخها تسبق الطلبات المؤرخة، ويُستخدم
    # request_key كترتيب ثابت احتياطي لها.
    return (0, created, *secondary) if created is not None else (0, 0, *secondary)


def _legacy_retry_metadata(data: dict, path: str) -> bool:
    """ترحيل بيانات الفشل/الالتباس القديمة دون إبقاء نصوص الأخطاء الخام."""
    changed = False
    for clip in data.get("clips", []):
        if clip.get("status") == "upload_unknown":
            if clip.get("unknown_reason") != "publish_result_unconfirmed":
                clip["unknown_reason"] = "publish_result_unconfirmed"
                changed = True
            if clip.pop("error", None) is not None:
                changed = True
            continue
        if clip.get("status") != "failed_retryable":
            continue
        if "confirmed_failures" not in clip:
            # حالة النسخة القديمة لا تحفظ العدد؛ نعتبر فشلًا سابقًا واحدًا معلومًا.
            clip["confirmed_failures"] = 1
            changed = True
        if not clip.get("last_failure_date") or not clip.get("retry_after_date"):
            last_modified = None
            try:
                repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                relative = os.path.relpath(path, repo_root)
                result = subprocess.run(
                    ["git", "log", "-1", "--format=%ct", "--", relative],
                    cwd=repo_root,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    last_modified = datetime.datetime.fromtimestamp(
                        float(result.stdout.strip()), datetime.timezone.utc
                    ).date()
            except (OSError, subprocess.SubprocessError, ValueError, OverflowError):
                pass
            if last_modified is None:
                # إذا تعذر تأريخ الحالة القديمة، نؤجلها يومًا كاملًا بدل إعادة
                # محاولة قد تكون في يوم الفشل نفسه.
                last_modified = _utc_today()
            clip["last_failure_date"] = last_modified.isoformat()
            clip["retry_after_date"] = (last_modified + datetime.timedelta(days=1)).isoformat()
            changed = True
        if clip.pop("error", None) is not None:
            changed = True
    if changed:
        _save_batch(path, data)
    return changed


def list_active_batches(state_dir: str) -> list[dict]:
    """يرجع الملفات التي لا تزال تحتوي مقطعًا قابلًا للمعالجة أو غير محسوم."""
    active = []
    for path in glob.glob(os.path.join(_queue_dir(state_dir), "*.json")):
        data = _load_batch(path)
        _legacy_retry_metadata(data, path)
        if any(
            c.get("status") in {"pending", "failed_retryable", "uploading", "upload_unknown"}
            for c in data.get("clips", [])
        ):
            data["_path"] = path
            active.append(data)
    return sorted(active, key=_batch_order_key)


def get_next_pending_index(batch: dict, today: datetime.date | str | None = None) -> int | None:
    """يعيد أول مقطع غير محسوم؛ الفشل المؤكد لا يصبح متاحًا قبل يومه التالي."""
    today_date = _utc_today(today)
    for i, clip in enumerate(batch.get("clips", [])):
        status = clip.get("status")
        if status == "pending":
            return i
        if status == "failed_retryable":
            retry_after = _as_date(clip.get("retry_after_date"))
            if retry_after is not None and today_date >= retry_after:
                return i
            # لا نتجاوز عنصرًا أولويًا ينتظر موعد محاولته.
            return None
        if status in {"uploading", "upload_unknown"}:
            return i
    return None


def get_clip_status(state_dir: str, queue_id: str, clip_index: int) -> str | None:
    path = _queue_file(state_dir, queue_id)
    try:
        data = _load_batch(path)
        return data["clips"][clip_index].get("status")
    except (OSError, ValueError, KeyError, IndexError):
        return None


def mark_clip_uploading(state_dir: str, queue_id: str, clip_index: int) -> None:
    path = _queue_file(state_dir, queue_id)
    data = _load_batch(path)
    data["clips"][clip_index]["status"] = "uploading"
    data["clips"][clip_index].pop("unknown_reason", None)
    data["clips"][clip_index].pop("error", None)
    _save_batch(path, data)


def mark_clip_upload_unknown(state_dir: str, queue_id: str, clip_index: int, error: str = "") -> None:
    """يحفظ نتيجة رفع مجهولة دون حفظ نص استثناء قد يكشف المصدر أو بيانات شخصية."""
    path = _queue_file(state_dir, queue_id)
    data = _load_batch(path)
    clip = data["clips"][clip_index]
    clip["status"] = "upload_unknown"
    clip["unknown_reason"] = "publish_result_unconfirmed"
    clip.pop("error", None)
    _save_batch(path, data)


def mark_clip_published(state_dir: str, queue_id: str, clip_index: int, youtube_url: str) -> None:
    path = _queue_file(state_dir, queue_id)
    data = _load_batch(path)
    clip = data["clips"][clip_index]
    clip["status"] = "published"
    clip["youtube_url"] = youtube_url
    clip.pop("unknown_reason", None)
    clip.pop("error", None)
    _save_batch(path, data)


def mark_clip_failed(
    state_dir: str,
    queue_id: str,
    clip_index: int,
    error: str = "",
    *,
    failure_type: str = "confirmed_failure",
    failed_on: datetime.date | str | None = None,
) -> str:
    """يسجل فشلًا مؤكدًا؛ المحاولة الثانية تحول العنصر إلى skipped نهائيًا."""
    path = _queue_file(state_dir, queue_id)
    data = _load_batch(path)
    clip = data["clips"][clip_index]
    previous = clip.get("confirmed_failures")
    if previous is None:
        previous = 1 if clip.get("status") == "failed_retryable" else 0
    failures = max(0, int(previous)) + 1
    failure_date = _utc_today(failed_on)
    clip["confirmed_failures"] = failures
    clip["last_failure_date"] = failure_date.isoformat()
    # نوع الخطأ فقط؛ لا نخزن نص الاستثناء الذي قد يحتوي رابط المصدر أو بيانات أخرى.
    safe_type = "".join(ch for ch in str(failure_type) if ch.isalnum() or ch in "_.-")[:60]
    clip["failure_type"] = safe_type or "confirmed_failure"
    clip.pop("error", None)
    clip.pop("unknown_reason", None)

    if failures >= MAX_CONFIRMED_FAILURES:
        clip["status"] = "skipped"
        clip["skip_reason"] = "confirmed_failure_after_retry"
        clip.pop("retry_after_date", None)
    else:
        clip["status"] = "failed_retryable"
        clip["retry_after_date"] = (failure_date + datetime.timedelta(days=1)).isoformat()
        clip.pop("skip_reason", None)
    _save_batch(path, data)
    return clip["status"]


def batch_progress(batch: dict) -> tuple[int, int]:
    """يرجع عدد المقاطع المنشورة أو المتخطاة نهائيًا من إجمالي الدفعة."""
    done = sum(1 for c in batch["clips"] if c.get("status") in ("published", "failed", "skipped"))
    return done, len(batch["clips"])


def _merge_clip_state(local_clip: dict, remote_clip: dict) -> dict:
    """دمج رتيب يمنع ضياع الفشل المسجل أو حالة الرفع الملتبسة عند التزامن."""
    rank = {
        "pending": 0,
        "failed_retryable": 1,
        "failed": 2,
        "skipped": 2,
        "uploading": 3,
        "upload_unknown": 4,
        "published": 5,
    }
    local_status = local_clip.get("status", "pending")
    remote_status = remote_clip.get("status", "pending")
    if rank.get(remote_status, 0) > rank.get(local_status, 0):
        chosen = remote_clip
    elif rank.get(remote_status, 0) < rank.get(local_status, 0):
        chosen = local_clip
    elif local_status == "failed_retryable":
        chosen = max(
            (local_clip, remote_clip),
            key=lambda clip: (
                int(clip.get("confirmed_failures", 1)),
                str(clip.get("retry_after_date", "")),
            ),
        )
    else:
        chosen = local_clip

    merged = {**remote_clip, **local_clip}
    state_fields = (
        "status", "youtube_url", "confirmed_failures", "last_failure_date",
        "retry_after_date", "failure_type", "skip_reason", "unknown_reason", "error",
    )
    for field in state_fields:
        if field in chosen:
            merged[field] = chosen[field]
        else:
            merged.pop(field, None)

    counts = [
        int(clip.get("confirmed_failures", 0))
        for clip in (local_clip, remote_clip)
        if str(clip.get("confirmed_failures", "0")).isdigit()
    ]
    if counts:
        merged["confirmed_failures"] = max(counts)
    return merged


def merge_latest_state(state_dir: str) -> None:
    """يدمج أحدث ملفات الطوابير والعداد من origin/main دون إرجاع تقدم الحالة."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main", "--quiet"],
            check=True, timeout=30, capture_output=True,
        )
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "origin/main", f"state/{QUEUE_DIR_NAME}"],
            check=True, text=True, capture_output=True, timeout=15,
        )
        for relative in result.stdout.splitlines():
            if not relative.endswith(".json"):
                continue
            queue_id = os.path.basename(relative)[:-5]
            local_path = _queue_file(state_dir, queue_id)
            remote = subprocess.run(
                ["git", "show", f"origin/main:{relative}"],
                check=True, text=True, capture_output=True, timeout=15,
            )
            remote_data = json.loads(remote.stdout)
            if not os.path.exists(local_path):
                _save_batch(local_path, remote_data)
                continue
            local_data = _load_batch(local_path)
            if not local_data.get("created_at") and remote_data.get("created_at"):
                local_data["created_at"] = remote_data["created_at"]
            if local_data.get("fifo_order") is None and remote_data.get("fifo_order") is not None:
                local_data["fifo_order"] = remote_data["fifo_order"]
            for index, remote_clip in enumerate(remote_data.get("clips", [])):
                if index >= len(local_data.get("clips", [])):
                    local_data.setdefault("clips", []).append(remote_clip)
                    continue
                local_data["clips"][index] = _merge_clip_state(
                    local_data["clips"][index], remote_clip
                )
            _save_batch(local_path, local_data)

        remote_quota = subprocess.run(
            ["git", "show", f"origin/main:state/{QUOTA_FILE_NAME}"],
            text=True, capture_output=True, timeout=15,
        )
        if remote_quota.returncode == 0 and remote_quota.stdout.strip():
            remote_data = json.loads(remote_quota.stdout)
            local_path = os.path.join(state_dir, QUOTA_FILE_NAME)
            if os.path.exists(local_path):
                with open(local_path, "r", encoding="utf-8") as f:
                    local_data = json.load(f)
            else:
                local_data = {}
            if remote_data.get("date") == local_data.get("date"):
                merged = {
                    "date": local_data.get("date"),
                    "published_count": max(
                        local_data.get("published_count", 0),
                        remote_data.get("published_count", 0),
                    ),
                    "limit_hit": bool(local_data.get("limit_hit") or remote_data.get("limit_hit")),
                }
                os.makedirs(state_dir, exist_ok=True)
                temp_path = f"{local_path}.tmp"
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(merged, f, ensure_ascii=False, indent=2)
                os.replace(temp_path, local_path)
    except Exception as exc:
        # لا نطبع نص الخطأ الخام؛ قد يتضمن مسارات أو بيانات من بيئة التشغيل.
        print(f"⚠️ تعذر دمج أحدث حالة Smart Long من origin ({type(exc).__name__}).")
