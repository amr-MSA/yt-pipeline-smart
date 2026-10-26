"""أدوات التعامل مع بوت تلجرام وإدارة حالة الطلبات."""
import json
import os
import re
import subprocess
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# yt-dlp يدعم آلاف المواقع والمنصات؛ لا نحصر الروابط في YouTube هنا.
GENERIC_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,!?;:'\"،؛؟)]}>"

# نمط للتعرف على Long أو /long، بحالة أحرف مختلفة ومع أو بدون مسافة،
# متبوعًا برابط من أي منصة يدعمها yt-dlp.
LONG_COMMAND_RE = re.compile(
    r"^\s*/?long(?:@\w+)?\s*"
    r"(https?://[^\s<>]+)",
    re.IGNORECASE,
)
TAKE_TITLE_COMMAND_RE = re.compile(r"^/take(?:@\w+)?$", re.IGNORECASE)
DEFAULT_TITLE_COMMAND_RE = re.compile(r"^/d(?:@\w+)?$", re.IGNORECASE)
CUSTOM_TITLE_COMMAND_RE = re.compile(
    r"^/t(?:@\w+)?\s+[\"“](.+?)[\"”]$", re.IGNORECASE | re.DOTALL
)
INLINE_TITLE_PREFIX_RE = re.compile(
    r'^\s*(?:"(?P<ascii>[^"\r\n]+)"|“(?P<arabic>[^”\r\n]+)”)[ \t]*(?=https?://)',
    re.IGNORECASE,
)
PIPELINE_REPORT_PREFIXES = (
    "📊 تقرير الدفعة",
    "✅ تم نشر الفيديو بنجاح:",
    "📊 تم قبول ",
)


def _clean_url(url: str) -> str:
    """يزيل علامات الترقيم التي تلحق بالرابط داخل نص الرسالة."""
    return url.rstrip(TRAILING_URL_PUNCTUATION)


def _is_safe_url(url: str) -> bool:
    """يرفض userinfo حتى لا تُحفظ أو تُرسل بيانات دخول ضمن رابط المصدر."""
    try:
        parsed = urlsplit(url)
        return parsed.scheme.lower() in {"http", "https"} and not parsed.username and not parsed.password
    except ValueError:
        return False


def extract_urls(text: str) -> list[str]:
    """يستخرج روابط HTTP(S) من أي منصة، مع إزالة punctuation المحيط."""
    urls = (_clean_url(match.group(0)) for match in GENERIC_URL_RE.finditer(text))
    return [url for url in urls if _is_safe_url(url)]


def extract_inline_title(text: str) -> str | None:
    """يستخرج عنوانًا بين التنصيص إذا جاء في بداية الرسالة قبل الرابط."""
    match = INLINE_TITLE_PREFIX_RE.match(text or "")
    if not match:
        return None
    title = (match.group("ascii") or match.group("arabic") or "").strip()
    return title or None


def is_boundary_marker(text: str) -> bool:
    """يتعرف على رسالة تحكم مكونة من أرقام/رموز فقط، مثل ``1`` أو ``.``."""
    stripped = text.strip()
    if not stripped or extract_urls(stripped):
        return False
    return all(not char.isalpha() for char in stripped) and any(
        char.isdigit() or not char.isspace() for char in stripped
    )


def is_pipeline_report(text: str) -> bool:
    """يتعرف على تقارير البوت حتى لا تُعاد روابط YouTube الموجودة داخلها."""
    return text.strip().startswith(PIPELINE_REPORT_PREFIXES)


def _apply_title_command(text: str, item: dict | None) -> bool:
    """يطبق أمر عنوان على آخر رابط، ويرجع True عند التعرف على الأمر."""
    command = text.strip()
    if item is None:
        return False
    if TAKE_TITLE_COMMAND_RE.fullmatch(command):
        item["title_mode"] = "source"
        item.pop("title_override", None)
        return True
    if DEFAULT_TITLE_COMMAND_RE.fullmatch(command):
        item["title_mode"] = "default"
        item.pop("title_override", None)
        return True
    match = CUSTOM_TITLE_COMMAND_RE.fullmatch(command)
    if match:
        item["title_mode"] = "custom"
        item["title_override"] = match.group(1).strip()
        return True
    return False


def _offset_file(state_dir: str, bot_name: str) -> str:
    return os.path.join(state_dir, f"{bot_name}_offset.json")


def load_offset(state_dir: str, bot_name: str) -> int:
    """يقرأ آخر update_id تمت معالجته لهذا البوت. 0 إذا لا يوجد."""
    path = _offset_file(state_dir, bot_name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return int(data.get("last_update_id", 0))
    return 0


def save_offset(state_dir: str, bot_name: str, update_id: int) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = _offset_file(state_dir, bot_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_update_id": update_id}, f, ensure_ascii=False, indent=2)


def inbox_path(state_dir: str, bot_name: str) -> str:
    return os.path.join(state_dir, f"{bot_name}_inbox.json")


def smart_long_inbox_path(state_dir: str) -> str:
    """مسار Inbox مستقل لأوامر /long التي يعمل عليها Smart Long."""
    return os.path.join(state_dir, "long_smart_inbox.json")


def load_smart_long_inbox(state_dir: str) -> dict[str, dict]:
    path = smart_long_inbox_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_smart_long_inbox(state_dir: str, inbox: dict[str, dict]) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = smart_long_inbox_path(state_dir)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(inbox, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp_path, path)


def acknowledge_smart_long_command(state_dir: str, item: dict) -> None:
    inbox = load_smart_long_inbox(state_dir)
    inbox.pop(message_key(item), None)
    save_smart_long_inbox(state_dir, inbox)


def canonicalize_url(url: str) -> str:
    """يوحّد الرابط لإسقاط معاملات التتبع وصيغ YouTube المتكافئة."""
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower()
        if host == "youtu.be":
            video_id = parsed.path.strip("/")
            if video_id:
                return f"https://youtube.com/watch?v={video_id}"
        if host in {"youtube.com", "m.youtube.com", "www.youtube.com"} and parsed.path == "/watch":
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if query.get("v"):
                return f"https://youtube.com/watch?v={query['v']}"
        tracking = {"fbclid", "gclid", "si", "feature", "igshid", "ref", "ref_src"}
        query = [
            (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in tracking
        ]
        return urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/") or "/", urlencode(sorted(query)), ""))
    except ValueError:
        return url.strip()


def _inbox_key(item: dict) -> str:
    return item.get("canonical_url") or canonicalize_url(item["url"])


def load_inbox(state_dir: str, bot_name: str) -> dict[str, dict]:
    path = inbox_path(state_dir, bot_name)
    if not os.path.exists(path):
        # ترحيل آمن من النسخة القديمة ذات القائمة.
        legacy = os.path.join(state_dir, f"{bot_name}_pending_links.json")
        path = legacy if os.path.exists(legacy) else path
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {_inbox_key(item): {**item, "canonical_url": _inbox_key(item)} for item in data}
    except (OSError, ValueError):
        pass
    return {}


def save_inbox(state_dir: str, bot_name: str, inbox: dict[str, dict]) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = inbox_path(state_dir, bot_name)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(inbox, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp_path, path)


PUBLISHED_URL_TTL_SECONDS = 48 * 60 * 60


def published_urls_path(state_dir: str, bot_name: str) -> str:
    return os.path.join(state_dir, f"{bot_name}_published_urls.json")


def _save_json_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp_path, path)


def load_published_urls(
    state_dir: str,
    bot_name: str,
    now: int | None = None,
    ttl_seconds: int = PUBLISHED_URL_TTL_SECONDS,
) -> dict[str, dict]:
    """يقرأ سجل الروابط المنشورة وينظفه لكل رابط على حدة بعد انتهاء TTL."""
    path = published_urls_path(state_dir, bot_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    current = int(time.time() if now is None else now)
    active = {}
    for key, record in data.items():
        if not isinstance(record, dict):
            continue
        published_at = int(record.get("published_at", 0) or 0)
        expires_at = int(record.get("expires_at", published_at + ttl_seconds) or 0)
        if published_at and expires_at > current:
            active[key] = {**record, "expires_at": expires_at}
    if active != data:
        _save_json_atomic(path, active)
    return active


def record_published_url(
    state_dir: str,
    bot_name: str,
    url: str,
    video_id: str,
    now: int | None = None,
    ttl_seconds: int = PUBLISHED_URL_TTL_SECONDS,
) -> dict:
    """يسجل رابطًا منشورًا مع انتهاء مستقل بعد 48 ساعة."""
    current = int(time.time() if now is None else now)
    published = load_published_urls(state_dir, bot_name, now=current, ttl_seconds=ttl_seconds)
    canonical = canonicalize_url(url)
    published[canonical] = {
        "url": url,
        "canonical_url": canonical,
        "video_id": video_id,
        "published_at": current,
        "expires_at": current + ttl_seconds,
    }
    _save_json_atomic(published_urls_path(state_dir, bot_name), published)
    return published[canonical]


# أسماء توافقية للكود والاختبارات القديمة.
def load_pending_links(state_dir: str, bot_name: str) -> list[dict]:
    return list(load_inbox(state_dir, bot_name).values())


def save_pending_links(state_dir: str, bot_name: str, links: list[dict]) -> None:
    save_inbox(state_dir, bot_name, {_inbox_key(item): {**item, "canonical_url": _inbox_key(item)} for item in links})


def acknowledge_pending_link(state_dir: str, bot_name: str, item: dict) -> None:
    """يحذف الرابط من الـ Inbox بعد نجاح النشر أو تخطي طلب مسجل."""
    inbox = load_inbox(state_dir, bot_name)
    inbox.pop(_inbox_key(item), None)
    save_inbox(state_dir, bot_name, inbox)


def _acknowledge_updates(state_dir: str, bot_name: str, updates: list[dict]) -> None:
    if updates:
        save_offset(state_dir, bot_name, max(upd["update_id"] for upd in updates))


def fetch_new_links(
    bot_token: str,
    state_dir: str,
    bot_name: str,
    run_mode: str = "daily",
    window_seconds: int = 60 * 60,
) -> list[dict]:
    """يملأ Inbox بقاموس فريد ثم يعيد كل الروابط المعلقة بترتيب الأقدم."""
    updates = _fetch_updates(bot_token, state_dir, bot_name)
    inbox = load_inbox(state_dir, bot_name)
    new_links: list[dict] = []
    last_link_by_chat: dict[int, dict] = {}

    for msg in _iter_messages(updates):
        text = msg.get("text") or msg.get("caption") or ""
        if is_pipeline_report(text) or is_boundary_marker(text):
            new_links = []
            last_link_by_chat = {}
            continue
        chat_id = msg["chat"]["id"]
        if _apply_title_command(text, last_link_by_chat.get(chat_id)):
            continue
        if LONG_COMMAND_RE.search(text):
            continue
        inline_title = extract_inline_title(text)
        for index, u in enumerate(extract_urls(text)):
            canonical = canonicalize_url(u)
            item = {
                "url": u,
                "canonical_url": canonical,
                "chat_id": chat_id,
                "message_id": msg["message_id"],
                "date": int(msg.get("date", time.time())),
                "text": text.strip(),
                "title_mode": "source",
            }
            if index == 0 and inline_title:
                item["title_mode"] = "custom"
                item["title_override"] = inline_title
            # التكرار الذكي: نفس الرابط بصيغة مختلفة أو مع tracking لا ينشئ طلبًا جديدًا.
            if canonical not in inbox and not any(_inbox_key(x) == canonical for x in new_links):
                new_links.append(item)
            last_link_by_chat[chat_id] = item

    for item in new_links:
        inbox.setdefault(_inbox_key(item), item)

    # نقطة الأمان: احفظ الـ Inbox أولًا، ثم أكد Telegram offset.
    save_inbox(state_dir, bot_name, inbox)
    _acknowledge_updates(state_dir, bot_name, updates)

    if not inbox:
        return []
    if run_mode == "manual":
        return [max(inbox.values(), key=lambda x: (x.get("date", 0), x["message_id"]))]
    return sorted(
        inbox.values(),
        key=lambda x: (x.get("date", 0), x.get("message_id", 0)),
    )

def fetch_all_new_messages(bot_token: str, state_dir: str, bot_name: str) -> tuple[list[dict], list[dict]]:
    """يجلب دفعة واحدة ويفرزها إلى روابط عادية وأوامر /long.

    أوامر Smart Long تُحفظ في Inbox ذري قبل تأكيد Telegram offset، ثم تُعاد
    في كل تشغيل حتى ينجح التحليل وإنشاء الطابور.
    """
    updates = _fetch_updates(bot_token, state_dir, bot_name)
    links = []
    long_commands = []
    smart_inbox = load_smart_long_inbox(state_dir)

    for update in updates:
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        text = msg.get("text") or msg.get("caption") or ""
        if is_pipeline_report(text):
            links = []
            long_commands = []
            continue
        if is_boundary_marker(text):
            links = []
            long_commands = []
            continue
        m = LONG_COMMAND_RE.search(text)
        if m:
            url = _clean_url(m.group(1))
            if not _is_safe_url(url):
                continue
            long_commands.append(
                {
                    "url": url,
                    "chat_id": msg["chat"]["id"],
                    "message_id": msg["message_id"],
                    "date": int(msg.get("date", time.time())),
                    "update_id": int(update.get("update_id", 0)),
                    "text": text.strip(),
                }
            )
            continue
        inline_title = extract_inline_title(text)
        for index, u in enumerate(extract_urls(text)):
            links.append(
                {
                    "url": u,
                    "chat_id": msg["chat"]["id"],
                    "message_id": msg["message_id"],
                    "text": text.strip(),
                    "title_mode": "custom" if index == 0 and inline_title else "source",
                    **({"title_override": inline_title} if index == 0 and inline_title else {}),
                }
            )

    for command in long_commands:
        smart_inbox.setdefault(message_key(command), command)

    # احفظ Inbox أولًا، ثم أكد Telegram offset؛ إذا توقف التحليل بعد ذلك
    # فسيعود الأمر في التشغيل التالي بدل أن يضيع بعد تحديث offset.
    save_smart_long_inbox(state_dir, smart_inbox)
    _acknowledge_updates(state_dir, bot_name, updates)
    return links, list(smart_inbox.values())


def _iter_messages(updates: list[dict]):
    for upd in updates:
        msg = upd.get("message") or upd.get("channel_post")
        if msg:
            yield msg


def _fetch_updates(bot_token: str, state_dir: str, bot_name: str) -> list[dict]:
    """يجلب التحديثات الخام ويحدّث offset بعد الجلب."""
    last_offset = load_offset(state_dir, bot_name)
    url = TELEGRAM_API.format(token=bot_token, method="getUpdates")
    params = {"offset": last_offset + 1, "timeout": 0, "limit": 100}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("ok"):
        raise RuntimeError(f"فشل استدعاء Telegram API: {data}")

    updates = data.get("result", [])
    return updates


def send_message(bot_token: str, chat_id: int, text: str) -> None:
    """يرسل رسالة نصية ولا يوقف السير إذا فشل الإشعار."""
    url = TELEGRAM_API.format(token=bot_token, method="sendMessage")
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception:
        pass


def send_batch_summary(bot_token: str, items: list[dict], label: str = "الرابط") -> None:
    """يرسل ملخصًا واحدًا لكل قناة/محادثة عن العناصر المقبولة في الدفعة."""
    counts: dict[int, int] = {}
    for item in items:
        counts[item["chat_id"]] = counts.get(item["chat_id"], 0) + 1
    for chat_id, count in counts.items():
        unit = label if count == 1 else ("روابط" if label == "رابط" else label)
        send_message(
            bot_token,
            chat_id,
            f"📊 تم قبول {count} {unit} للمعالجة في هذه الدفعة.",
        )


def delete_message(bot_token: str, chat_id: int, message_id: int) -> bool:
    """يحذف رسالة Telegram ويعيد True فقط إذا أكد API نجاح الحذف."""
    url = TELEGRAM_API.format(token=bot_token, method="deleteMessage")
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return bool(data.get("ok"))
    except (requests.RequestException, ValueError):
        return False


def upload_state_path(state_dir: str, bot_name: str) -> str:
    return os.path.join(state_dir, f"{bot_name}_uploaded_messages.json")


def load_uploaded_messages(state_dir: str, bot_name: str) -> dict:
    path = upload_state_path(state_dir, bot_name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_uploaded_messages(state_dir: str, bot_name: str, state: dict) -> None:
    """يحفظ سجل النجاح قبل محاولة الحذف لمنع إعادة الرفع بعد فشل الحذف.

    قبل الكتابة، يُعاد دمج الحالة مع أحدث نسخة موجودة فعليًا على القرص
    (والتي قد تكون تحدّثت بعد load_uploaded_messages بسبب checkout جديد
    في خطوة merge_latest_state). هذا يمنع فقدان مفاتيح نجاح سجّلها push
    من تشغيلة أخرى وصل بعد أن قرأنا نسختنا المحلية.
    """
    os.makedirs(state_dir, exist_ok=True)
    path = upload_state_path(state_dir, bot_name)
    on_disk = load_uploaded_messages(state_dir, bot_name)
    merged = dict(on_disk)
    merged.update(state)
    # أي مفتاح كان بالقرص لكن أُزيل عمدًا من `state` (بعد حذف رسالة تلجرام
    # الناجح) يجب أن يبقى محذوفًا، لا أن يعود بسبب الدمج.
    removed_keys = set(on_disk) - set(state)
    for k in removed_keys:
        merged.pop(k, None)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def message_key(item: dict) -> str:
    """هوية الطلب: نفس الرابط في رسالة أخرى يظل طلبًا مختلفًا."""
    return f"{item['chat_id']}:{item['message_id']}"


def message_ledger_path(state_dir: str, bot_name: str) -> str:
    return os.path.join(state_dir, f"{bot_name}_message_ledger.json")


def load_message_ledger(state_dir: str, bot_name: str) -> dict:
    """يقرأ سجل الرسائل؛ السجل طبقة حماية إضافية بجانب Telegram offset."""
    path = message_ledger_path(state_dir, bot_name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_message_ledger(state_dir: str, bot_name: str, ledger: dict) -> None:
    """يحفظ السجل باستبدال ذري حتى لا ينتج ملف JSON جزئي.

    يُدمج مع أحدث نسخة موجودة على القرص أولًا (نفس منطق save_uploaded_messages)
    حتى لا تُفقد تسجيلات نجاح كتبتها تشغيلة أخرى ودُمجت عبر merge_latest_state
    بعد أن قرأنا نسختنا المحلية في بداية هذا التشغيل.
    """
    os.makedirs(state_dir, exist_ok=True)
    path = message_ledger_path(state_dir, bot_name)
    on_disk = load_message_ledger(state_dir, bot_name)
    merged = dict(on_disk)
    merged.update(ledger)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def merge_latest_state(state_dir: str, bot_name: str) -> None:
    """يسحب أحدث state/ من origin/main ويدمجه مع الملفات المحلية قبل الحفظ الأخير.

    يُستدعى في نهاية كل سير عمل (finally)، قبل أن يحفظ السكربت آخر نسخة من
    offset/uploaded_messages/ledger. هذا يحمي من فقدان تسجيلات نجاح كتبتها
    تشغيلة موازية دفعت (push) للريبو أثناء تنفيذ هذه التشغيلة الحالية —
    وهو ما كان يسبب إعادة نشر نفس الفيديو مرتين عند فشل/تعارض git rebase
    في خطوة الـ workflow.

    الدمج يتم على مستوى الحقول (key-by-key) لا على مستوى النص، لذلك لا
    يحتاج git ولا يمكن أن يفشل بتعارض نصي كما يحصل مع `git rebase` على JSON.
    """
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main", "--quiet"],
            check=True, timeout=30, capture_output=True,
        )
        remote_files = subprocess.run(
            ["git", "show", f"origin/main:state/{bot_name}_uploaded_messages.json"],
            capture_output=True, text=True, timeout=15,
        )
        if remote_files.returncode == 0 and remote_files.stdout.strip():
            remote_uploaded = json.loads(remote_files.stdout)
            if isinstance(remote_uploaded, dict):
                local = load_uploaded_messages(state_dir, bot_name)
                combined = dict(remote_uploaded)
                combined.update(local)
                save_uploaded_messages(state_dir, bot_name, combined)

        remote_ledger = subprocess.run(
            ["git", "show", f"origin/main:state/{bot_name}_message_ledger.json"],
            capture_output=True, text=True, timeout=15,
        )
        if remote_ledger.returncode == 0 and remote_ledger.stdout.strip():
            remote_data = json.loads(remote_ledger.stdout)
            if isinstance(remote_data, dict):
                local = load_message_ledger(state_dir, bot_name)
                combined = dict(remote_data)
                combined.update(local)
                save_message_ledger(state_dir, bot_name, combined)

    except Exception as e:
        # لا نوقف السير بسبب فشل الدمج الاحتياطي؛ نكتفي بتسجيل تحذير.
        # آلية retry/merge بخطوة الـ workflow تبقى خط الدفاع الثاني.
        print(f"⚠️ تعذر دمج أحدث state من origin/main (سيُعتمد على retry بالـ workflow): {e}")
