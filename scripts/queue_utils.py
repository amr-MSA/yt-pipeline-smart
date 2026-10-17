"""
إدارة طابور مقاطع خط long-smart.

لكل طلب Telegram تمت معالجته بجيمناي، نحفظ ملف JSON واحد فيه 8 مقاطع مقترحة،
كل مقطع بحالة "pending" أو "published" أو "failed".

كل تشغيل يومي: يمر على كل ملفات الطابور النشطة (فيها مقطع pending واحد
على الأقل)، ويأخذ **أول مقطع pending واحد من كل رابط**، وينشره — بشرط
عدم تجاوز سقف النشر اليومي المشترك لخط long-smart بالكامل (انظر publish_quota.py).
"""
import glob
import hashlib
import json
import os
import subprocess

QUEUE_DIR_NAME = "long_smart_queue"
QUOTA_FILE_NAME = "long_smart_quota.json"


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
) -> None:
    """يحفظ دفعة من 8 مقاطع مرتبطة برسالة Telegram وجزء ترانسكريبت محدد."""
    data = {
        "video_id": video_id,
        "queue_id": queue_id,
        "request_key": request_key,
        "source_url": source_url,
        "source_title": source_title,
        "source_handle": source_handle,
        "chat_id": chat_id,
        "part_index": part_index,
        "part_count": part_count,
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


def list_active_batches(state_dir: str) -> list[dict]:
    """يرجع الدفعات التي فيها مقطع pending أو failed_retryable."""
    active = []
    for path in glob.glob(os.path.join(_queue_dir(state_dir), "*.json")):
        data = _load_batch(path)
        if any(c["status"] in {"pending", "failed_retryable", "upload_unknown"} for c in data["clips"]):
            data["_path"] = path
            active.append(data)
    return sorted(
        active,
        key=lambda item: (
            item.get("request_key", ""),
            int(item.get("part_index", 0)),
            item.get("queue_id", ""),
        ),
    )


def get_next_pending_index(batch: dict) -> int | None:
    for i, clip in enumerate(batch["clips"]):
        if clip["status"] in {"pending", "failed_retryable", "upload_unknown"}:
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
    data["clips"][clip_index].pop("error", None)
    _save_batch(path, data)


def mark_clip_upload_unknown(state_dir: str, queue_id: str, clip_index: int, error: str) -> None:
    path = _queue_file(state_dir, queue_id)
    data = _load_batch(path)
    data["clips"][clip_index]["status"] = "upload_unknown"
    data["clips"][clip_index]["error"] = error[:300]
    _save_batch(path, data)


def mark_clip_published(state_dir: str, queue_id: str, clip_index: int, youtube_url: str) -> None:
    path = _queue_file(state_dir, queue_id)
    data = _load_batch(path)
    data["clips"][clip_index]["status"] = "published"
    data["clips"][clip_index]["youtube_url"] = youtube_url
    _save_batch(path, data)


def mark_clip_failed(state_dir: str, queue_id: str, clip_index: int, error: str) -> None:
    path = _queue_file(state_dir, queue_id)
    data = _load_batch(path)
    data["clips"][clip_index]["status"] = "failed_retryable"
    data["clips"][clip_index]["error"] = error[:300]
    _save_batch(path, data)


def batch_progress(batch: dict) -> tuple[int, int]:
    """يرجع (المنشور/المعالَج، الإجمالي)."""
    done = sum(1 for c in batch["clips"] if c["status"] in ("published", "failed"))
    return done, len(batch["clips"])


def merge_latest_state(state_dir: str) -> None:
    """يدمج طوابير Smart Long والعداد من آخر origin قبل المعالجة.

    هذا يحمي Queue أنشأه workflow الشورت أثناء تشغيل Smart Long. عند وجود
    الملف محليًا، تُحفظ حالة كل مقطع المنشورة من أي من النسختين.
    """
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
            for index, remote_clip in enumerate(remote_data.get("clips", [])):
                if index >= len(local_data.get("clips", [])):
                    local_data.setdefault("clips", []).append(remote_clip)
                    continue
                local_clip = local_data["clips"][index]
                if remote_clip.get("status") == "published":
                    local_data["clips"][index] = {**local_clip, **remote_clip}
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
        print(f"⚠️ تعذر دمج أحدث حالة Smart Long من origin: {exc}")
