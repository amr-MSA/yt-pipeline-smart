"""
خط إنتاج "long-smart" — يُفعَّل عبر أمر /long <رابط> في بوت الشورتس.

مختلف تمامًا عن خط اللونق الفوري (long_pipeline.py) وخط الشورتس القديم
(القص الثابت 60 ثانية). هذا الخط:

المرحلة أ) عند وصول أمر /long <رابط> جديد:
    1) يسحب الترانسكريبت (نص + توقيت) من يوتيوب (transcript_utils).
    2) يرسله لجيمناي ليقترح مقاطع (نوع long/shorts، توقيت، عنوان، وصف، هاشتاقات،
       وكلمات مفتاحية) بصيغة JSON (gemini_utils).
    3) يحفظ الـ 8 مقاطع في ملف طابور خاص بهذا الفيديو (queue_utils)، كلها
       بحالة "pending".
    4) لا يُنشر أي شيء فورًا في هذه المرحلة — النشر يتم في المرحلة ب فقط.

المرحلة ب) عند كل تشغيل يومي مجدول (run_daily_batch):
    - يبدأ بمقاطع ملف الإعادة المؤقتة المستحقة، ثم يواصل المقاطع العادية
      في ملف المصدر الأقدم، بحد أقصى 8 مقاطع في الملف.
    - الإخفاقات المؤكدة والنتائج الملتبسة تُعاد مرة واحدة في يوم UTC التالي؛
      تُفحص علامة الطلب أولًا لحماية الرفع من التكرار.
    - إذا نجح أي مقطع من الإعادة المؤقتة لا يبدأ ملف مصدر جديد؛ وإذا فشلت
      كل الإعادات تُنهى المقاطع الفاشلة ويبدأ الملف التالي في التشغيل نفسه.
    - عند اكتشاف خطأ كوتا من يوتيوب في أي لحظة، يتوقف فورًا عن أي محاولات
      نشر أخرى في هذا التشغيل ويسجّل ذلك.
    - العنوان والوصف النهائيان = عنوان/وصف جيمناي + توقيع ثابت من
      assets/fixed_title.txt و assets/fixed_description.txt.
"""
import datetime
import os
import sys
import traceback

from googleapiclient.http import MediaFileUpload

from cookie_utils import temporary_cookie_file
import fixed_meta
import publish_quota
import queue_utils
from gemini_utils import suggest_clips
from telegram_utils import send_message
from transcript_utils import fetch_transcript, split_segments
from youtube_auth import get_youtube_client
from youtube_dedupe import append_request_marker, recent_request_markers, request_marker
from video_compositor import compose_vertical

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(PROJECT_DIR, "state")
DOWNLOAD_DIR = os.path.join(PROJECT_DIR, "downloads_long_smart")
WORK_DIR = os.path.join(PROJECT_DIR, "work_long_smart")
DAILY_LIMIT = 8  # حد نشر يومي منفصل خاص بخط long-smart فقط
TRANSCRIPT_CHUNK_CHARS = 45000
QUEUE_REPORT_PREFIX = "📋 تقرير ملفات /long"
QUEUE_REPORT_MAX_CHARS = 3500


def _report_value(value: object, max_length: int | None = None) -> str:
    text = " ".join(str(value or "").split()) or "—"
    if max_length is not None and len(text) > max_length:
        return text[:max_length - 1] + "…"
    return text


def _format_clip_timestamp(value: object) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return _report_value(value, 40)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _telegram_text_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _split_text_by_limit(text: str, max_chars: int) -> list[str]:
    fragments = []
    current = []
    current_length = 0
    for character in text:
        character_length = 2 if ord(character) > 0xFFFF else 1
        if current and current_length + character_length > max_chars:
            fragments.append("".join(current))
            current = []
            current_length = 0
        current.append(character)
        current_length += character_length
    if current:
        fragments.append("".join(current))
    return fragments or [""]


def _split_report_blocks(blocks: list[str], max_chars: int) -> list[str]:
    chunks = []
    current = ""
    for block in blocks:
        if _telegram_text_length(block) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_text_by_limit(block, max_chars))
            continue

        separator = "\n\n" if current else ""
        if current and _telegram_text_length(current + separator + block) > max_chars:
            chunks.append(current)
            current = block
        else:
            current += separator + block
    if current:
        chunks.append(current)
    return chunks or [""]


def _send_queue_manifest_report(
    bot_token: str,
    chat_id: int,
    cmd: dict,
    source_url: str,
    source_title: str,
    queue_files: list[tuple[str, dict]],
) -> None:
    total_clips = sum(len(batch.get("clips", [])) for _, batch in queue_files)
    blocks = [
        "\n".join((
            "✅ جاهز — انتهت المعالجة؛ لم يبدأ النشر.",
            f"🎬 {_report_value(source_title)}",
            f"🔗 {_report_value(source_url)}",
            f"📦 {len(queue_files)} ملفات · {total_clips} مقطعًا | الطلب {cmd['message_id']}",
        ))
    ]
    for file_number, (filename, batch) in enumerate(queue_files, 1):
        clips = batch.get("clips", [])
        part_number = int(batch.get("part_index", 0)) + 1
        file_index = int(batch.get("file_index", file_number - 1)) + 1
        statuses = [_report_value(clip.get("status", "pending")) for clip in clips]
        uniform_status = bool(statuses) and len(set(statuses)) == 1
        file_status = (
            f"الحالة: {statuses[0]}" if uniform_status
            else "حالات متعددة" if statuses
            else "لا مقاطع"
        )
        blocks.append(
            f"📁 {file_number}/{len(queue_files)} · {filename} · "
            f"الجزء {part_number}، الملف {file_index} · {len(clips)} مقاطع · {file_status}"
        )
        for clip_index, clip in enumerate(clips, 1):
            title = _report_value(clip.get("title"))
            clip_type = _report_value(clip.get("type"), 40)
            start = _format_clip_timestamp(clip.get("start"))
            end = _format_clip_timestamp(clip.get("end"))
            status = "" if uniform_status else f" · {_report_value(clip.get('status', 'pending'))}"
            blocks.append(
                f"{clip_index}. {clip_type} · {start}–{end}{status}\n{title}"
            )

    body_limit = QUEUE_REPORT_MAX_CHARS - 80
    chunks = _split_report_blocks(blocks, body_limit)
    for index, body in enumerate(chunks, 1):
        heading = f"{QUEUE_REPORT_PREFIX} ({index}/{len(chunks)})"
        send_message(bot_token, chat_id, f"{heading}\n{body}")



# ---------------------------------------------------------------------------
# المرحلة أ: استقبال أوامر /long جديدة → ترانسكريبت → جيمناي → حفظ طابور
# ---------------------------------------------------------------------------

def handle_new_long_commands(bot_token: str, long_commands: list[dict]) -> dict[str, str]:
    results = {}
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        for cmd in long_commands:
            results[f"{cmd['chat_id']}:{cmd['message_id']}"] = "failed_retryable"
            send_message(
                bot_token,
                cmd["chat_id"],
                "❌ لم يتم إعداد GEMINI_API_KEY على الخادم — لا يمكن معالجة أمر /long.",
            )
        print("❌ GEMINI_API_KEY غير موجود في متغيرات البيئة.")
        return results

    cookies_env = os.environ.get("YT_COOKIES")
    os.makedirs(WORK_DIR, exist_ok=True)

    for idx, cmd in enumerate(long_commands, 1):
        url = cmd["url"]
        chat_id = cmd["chat_id"]
        key = f"{chat_id}:{cmd['message_id']}"
        queue_id = queue_utils.queue_id_for_request(key)
        print(f"\n{'='*50}\n[/long {idx}/{len(long_commands)}] معالجة: {url}")

        try:
            # إذا أُنشئ الطابور ثم انقطع التشغيل قبل تحديث ledger، لا نعيد
            # استدعاء Gemini ولا ننشئ دفعة ثانية لنفس رسالة Telegram.
            if queue_utils.queue_exists(STATE_DIR, queue_id):
                print(f"⏭️ الطابور موجود مسبقًا لهذا الطلب: {queue_id}")
                results[key] = "succeeded"
                continue

            print("📝 سحب الترانسكريبت من يوتيوب...")
            transcript = fetch_transcript(url, os.path.join(WORK_DIR, f"t{idx}"), cookies_env)
            video_id = transcript["video_id"]
            source_handle = fixed_meta.source_handle_from_info(transcript.get("source_info"))

            print(f"✅ تم سحب {len(transcript['segments'])} جزء نصي. جاري تقسيمه وإرساله لجيمناي...")
            transcript_chunks = split_segments(
                transcript["segments"], max_chars=TRANSCRIPT_CHUNK_CHARS
            )
            def _file_queue_id(part_index: int, file_index: int) -> str:
                return queue_utils.queue_id_for_request(
                    f"{key}:part:{part_index}:file:{file_index}"
                )

            # كل جزء ترانسكريبت قد يعطي أكثر من 8 مقاطع، فنقسمها إلى ملفات Queue
            # كل ملف فيه 8 مقاطع كحد أقصى. نكتب الملف الأول آخرًا، فوجوده يعني أن
            # الجزء كله اكتمل، فلا يُعاد استدعاء Gemini لجزء محفوظ جزئيًا.
            for part_index, chunk in enumerate(transcript_chunks):
                if queue_utils.queue_exists(STATE_DIR, _file_queue_id(part_index, 0)):
                    print(f"⏭️ الجزء {part_index + 1} محفوظ مسبقًا، لن يعاد تحليله.")
                    file_index = 0
                    while queue_utils.queue_exists(STATE_DIR, _file_queue_id(part_index, file_index)):
                        file_index += 1
                    continue
                clips = suggest_clips(gemini_api_key, transcript["title"], chunk)
                groups = queue_utils.split_into_files(clips)
                print(
                    f"✅ جيمناي اقترح {len(clips)} مقطعًا للجزء "
                    f"{part_index + 1}/{len(transcript_chunks)}، في {len(groups)} ملف(ات) Queue."
                )
                for file_index in reversed(range(len(groups))):
                    queue_utils.save_new_batch(
                        state_dir=STATE_DIR,
                        queue_id=_file_queue_id(part_index, file_index),
                        request_key=key,
                        video_id=video_id,
                        source_url=url,
                        source_title=transcript["title"],
                        source_handle=source_handle,
                        chat_id=chat_id,
                        clips=groups[file_index],
                        part_index=part_index,
                        part_count=len(transcript_chunks),
                        file_index=file_index,
                        created_at=cmd.get("date"),
                        fifo_order=cmd.get("update_id"),
                    )
            expected_queue_ids = [
                _file_queue_id(part_index, 0)
                for part_index in range(len(transcript_chunks))
            ]
            if not transcript_chunks or not all(
                queue_utils.queue_exists(STATE_DIR, queue_id) for queue_id in expected_queue_ids
            ):
                raise RuntimeError(
                    "لم تُحفظ كل دفعات Gemini في ملفات Queue؛ ستبقى رسالة الطلب للمحاولة التالية."
                )
            queue_files = []
            for part_index in range(len(transcript_chunks)):
                file_index = 0
                while queue_utils.queue_exists(STATE_DIR, _file_queue_id(part_index, file_index)):
                    queue_id_for_file = _file_queue_id(part_index, file_index)
                    path = os.path.join(
                        STATE_DIR, queue_utils.QUEUE_DIR_NAME, f"{queue_id_for_file}.json"
                    )
                    queue_files.append((os.path.basename(path), queue_utils.load_batch(path)))
                    file_index += 1
            total_clips = sum(len(batch.get("clips", [])) for _, batch in queue_files)
            if not queue_files or total_clips <= 0:
                raise RuntimeError("لم تُحفظ أي مقاطع في ملفات Queue.")
            results[key] = "succeeded"

            _send_queue_manifest_report(
                bot_token=bot_token,
                chat_id=chat_id,
                cmd=cmd,
                source_url=url,
                source_title=transcript["title"],
                queue_files=queue_files,
            )

        except Exception as e:
            err = f"{e}"
            print(f"❌ فشل تحليل /long: {err}")
            traceback.print_exc()
            results[key] = "failed_retryable"
            send_message(
                bot_token, chat_id,
                f"❌ فشلت معالجة أمر /long للرابط:\n{url}\n\nالخطأ: {err[:300]}",
            )

    return results


# ---------------------------------------------------------------------------
# المرحلة ب: التشغيل اليومي — نشر مقطع واحد من كل طابور نشط
# ---------------------------------------------------------------------------

def _download_clip_range(url: str, start: float, end: float, out_path: str, cookies_env: str | None) -> None:
    import yt_dlp

    ydl_opts = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "outtmpl": out_path,
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "overwrites": True,
        "quiet": True,
        "noprogress": True,
        "js_runtimes": {"deno": {}},
    }
    with temporary_cookie_file(cookies_env) as cookie_path:
        if cookie_path:
            ydl_opts["cookiefile"] = cookie_path
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)


def _apply_shorts_template(raw_path: str, final_path: str) -> bool:
    compose_vertical(raw_path, final_path, label="LONG-SMART")
    return True


def _youtube_tags(keywords: object) -> list[str]:
    """نظّف وسوم YouTube النصية وضمن حد 500 محرف إجماليًا."""
    if isinstance(keywords, str):
        values = [keywords]
    elif isinstance(keywords, (list, tuple)):
        values = keywords
    else:
        return []

    tags = []
    seen = set()
    for value in values:
        tag = " ".join(str(value or "").split()).lstrip("#").strip()[:100].rstrip()
        if not tag or tag.casefold() in seen:
            continue
        if len(", ".join([*tags, tag])) > 500:
            break
        seen.add(tag.casefold())
        tags.append(tag)
    return tags


def _upload(
    youtube,
    file_path: str,
    title: str,
    description: str,
    keywords: object = None,
) -> str:
    snippet = {
        "title": title[:100],
        "description": description[:4900],
        "categoryId": "22",
    }
    tags = _youtube_tags(keywords)
    if tags:
        snippet["tags"] = tags
    body = {
        "snippet": snippet,
        "status": {"privacyStatus": "public"},
    }
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(file_path, resumable=True),
    )
    response = request.execute()
    return response["id"]


def _safe_failure_label(exception: Exception) -> str:
    """وصف آمن للفشل لا يتضمن نصًا قد يحتوي رابط المصدر أو بيانات شخصية."""
    if publish_quota.is_quota_error(exception):
        return "youtube_quota_error"
    status = getattr(getattr(exception, "resp", None), "status", None)
    if status is not None:
        try:
            return f"youtube_http_{int(status)}"
        except (TypeError, ValueError):
            pass
    return type(exception).__name__


def _is_confirmed_upload_failure(exception: Exception) -> bool:
    """استجابة HTTP 4xx تعني رفض الطلب؛ أخطاء النقل/5xx تبقى ملتبسة."""
    status = getattr(getattr(exception, "resp", None), "status", None)
    try:
        return 400 <= int(status) < 500
    except (TypeError, ValueError):
        return False


def run_daily_batch() -> None:
    bot_token = os.environ.get("SHORTS_BOT_TOKEN")
    youtube_token_json = os.environ["YOUTUBE_TOKEN_JSON"]
    cookies_env = os.environ.get("YT_COOKIES")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    queue_utils.merge_latest_state(STATE_DIR)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    active_batches = queue_utils.list_active_batches(STATE_DIR)
    retry_candidates = queue_utils.refresh_temporary_retry_queue(STATE_DIR, today)
    if not active_batches:
        print("📭 لا توجد طوابير long-smart نشطة اليوم. إنهاء.")
        return

    if not publish_quota.can_publish(STATE_DIR, DAILY_LIMIT):
        print(f"🚫 تم الوصول للحد اليومي لخط long-smart ({DAILY_LIMIT}). لا نشر اليوم.")
        return

    youtube = get_youtube_client(youtube_token_json)
    print("[LONG-SMART][DEDUPE] START: فحص آخر 10 فيديوهات القناة (Long + Shorts)")
    recent_markers = recent_request_markers(youtube)
    print(f"[LONG-SMART][DEDUPE] OK: تم العثور على {len(recent_markers)} معرف طلب")
    unpublished_results: dict[int, list[str]] = {}
    daily_summary: dict[int, list[str]] = {}

    def add_unpublished(chat_id: int, message: str) -> None:
        unpublished_results.setdefault(chat_id, []).append(message)

    def add_summary(chat_id: int, message: str) -> None:
        daily_summary.setdefault(chat_id, []).append(message)

    def report_failed(batch: dict, clip_index: int, message: str) -> None:
        queue_id = batch.get("queue_id", batch["video_id"])
        chat_id = batch["chat_id"]
        total = len(batch.get("clips", []))
        details = f"🆔 الطابور: {queue_id} — المقطع: {clip_index + 1}/{total}\n{message}"
        add_unpublished(chat_id, details)
        if bot_token:
            send_message(bot_token, chat_id, message)
        add_summary(chat_id, message)

    def send_unpublished_reports() -> None:
        for chat_id, results in unpublished_results.items():
            report = (
                "⚠️ تقرير إضافي — مقاطع Long-Smart لم تُنشر أو لم يُؤكَّد نشرها:\n\n"
                + "\n\n".join(results)
            )
            for start in range(0, len(report), 3800):
                send_message(bot_token, chat_id, report[start:start + 3800])

    def process_clip(queue_id: str, clip_index: int, *, retry_attempt: bool) -> str:
        batch_path = os.path.join(
            STATE_DIR, queue_utils.QUEUE_DIR_NAME, f"{queue_id}.json"
        )
        try:
            batch = queue_utils.load_batch(batch_path)
            clip = batch["clips"][clip_index]
        except (OSError, ValueError, KeyError, IndexError):
            return "stale"

        prior_status = clip.get("status", "pending")
        if prior_status in {"published", "skipped", "failed"}:
            return "stale"
        if not retry_attempt and prior_status != "pending":
            return "stale"
        if retry_attempt and prior_status not in {
            "failed_retryable", "upload_unknown", "uploading"
        }:
            return "stale"

        video_id = batch["video_id"]
        chat_id = batch["chat_id"]
        marker = request_marker("long-smart", f"{queue_id}:{clip_index}")
        done, total = queue_utils.batch_progress(batch)
        if prior_status == "uploading" and bool(clip.get("retry_attempted")):
            # بقيت محاولة اليوم السابق في حالة uploading بعد انقطاع؛ لا نعيدها.
            existing_id = recent_markers.get(marker)
            if existing_id:
                prior_status = "uploading"
            else:
                queue_utils.mark_clip_skipped(
                    STATE_DIR, queue_id, clip_index, "retry_interrupted_unconfirmed"
                )
                report_failed(
                    batch, clip_index,
                    "⏭️ تعذر تأكيد محاولة الإعادة السابقة بعد انقطاع؛ تم إنهاء المقطع دون إعادة رفعه مرة أخرى.",
                )
                return "failed"

        try:
            youtube_existing_id = recent_markers.get(marker)
            if youtube_existing_id:
                video_url = f"https://youtu.be/{youtube_existing_id}"
                print(f"⏭️ تم العثور على النشر السابق في YouTube: {video_url}")
                queue_utils.mark_clip_published(STATE_DIR, queue_id, clip_index, video_url)
                publish_quota.record_publish_success(STATE_DIR)
                if bot_token:
                    send_message(
                        bot_token, chat_id,
                        f"✅ تم تأكيد نشر مقطع {done + 1}/{total} سابقًا:\n{video_url}",
                    )
                add_summary(chat_id, f"✅ مؤكد سابقًا: المقطع {done + 1}/{total} — {video_url}")
                return "success"

            # الحالة الملتبسة تحصل على فرصة واحدة في اليوم التالي. إذا كانت
            # المحاولة نفسها قد انقطعت، لا نكرر الرفع الأعمى بعد فحص العلامة.
            if retry_attempt and bool(clip.get("retry_attempted")):
                queue_utils.mark_clip_skipped(
                    STATE_DIR, queue_id, clip_index, "retry_interrupted_unconfirmed"
                )
                report_failed(
                    batch, clip_index,
                    "⏭️ لم تظهر علامة نشر للمحاولة المؤجلة التي انقطعت؛ تم إنهاء المقطع لتجنب التكرار.",
                )
                return "failed"

            raw_path = os.path.join(DOWNLOAD_DIR, f"raw_{video_id}_{clip_index}.mp4")
            final_path = os.path.join(DOWNLOAD_DIR, f"final_{video_id}_{clip_index}.mp4")
            print(f"⬇️ تحميل المقطع ({clip['start']}-{clip['end']} ثانية)...")
            _download_clip_range(
                batch["source_url"], clip["start"], clip["end"], raw_path, cookies_env
            )

            actual_raw = raw_path
            if not os.path.exists(actual_raw):
                candidates = [
                    filename for filename in os.listdir(DOWNLOAD_DIR)
                    if filename.startswith(f"raw_{video_id}_{clip_index}")
                ]
                if candidates:
                    actual_raw = os.path.join(DOWNLOAD_DIR, candidates[0])

            upload_file = actual_raw
            if clip["type"] == "shorts":
                print("🖼️ دمج قالب الشورتس...")
                merged = _apply_shorts_template(actual_raw, final_path)
                upload_file = final_path if merged else actual_raw

            all_hashtags = fixed_meta.format_hashtags(clip.get("hashtags"))
            title_for_upload = fixed_meta.build_final_title(
                clip["title"],
                all_hashtags[:fixed_meta.TITLE_HASHTAG_COUNT],
                include_shorts=clip["type"] == "shorts",
            )
            final_description = append_request_marker(
                fixed_meta.build_final_description(
                    clip["description"],
                    source_handle=batch.get("source_handle"),
                    source_url=batch.get("source_url"),
                ),
                marker,
                trailing_metadata=" ".join(
                    all_hashtags[fixed_meta.TITLE_HASHTAG_COUNT:]
                ),
            )

            # تحفظ حالة المحاولة قبل الشبكة؛ بعد الانقطاع نفحص علامة الطلب أولًا.
            queue_utils.mark_clip_uploading(
                STATE_DIR, queue_id, clip_index, retry_attempt=retry_attempt
            )
            print("📤 رفع على يوتيوب (public)...")
            video_youtube_id = _upload(
                youtube,
                upload_file,
                title_for_upload,
                final_description,
                keywords=clip.get("keywords"),
            )
            recent_markers[marker] = video_youtube_id
            video_url = f"https://youtu.be/{video_youtube_id}"
            print(f"✅ تم النشر: {video_url}")
            queue_utils.mark_clip_published(STATE_DIR, queue_id, clip_index, video_url)
            publish_quota.record_publish_success(STATE_DIR)
            if bot_token:
                send_message(
                    bot_token, chat_id,
                    f"✅ نُشر مقطع {done + 1}/{total}: "
                    f"\"{title_for_upload}\" ({clip['type']}):\n{video_url}",
                )
            add_summary(
                chat_id,
                f"✅ نُشر: المقطع {done + 1}/{total} — "
                f"\"{title_for_upload}\" — {video_url}",
            )
            return "success"

        except Exception as exc:
            safe_label = _safe_failure_label(exc)
            print(f"❌ تعذر إكمال المقطع ({safe_label}).")
            current = queue_utils.get_clip_state(STATE_DIR, queue_id, clip_index) or {}
            current_status = current.get("status")
            if current_status == "published":
                add_summary(chat_id, f"✅ النشر مؤكد للمقطع {done + 1}/{total}.")
                return "success"

            if publish_quota.is_quota_error(exc):
                print("🚫 تم اكتشاف خطأ كوتا يوتيوب — إيقاف كل النشر لهذا اليوم.")
                publish_quota.record_quota_hit(STATE_DIR)
                if retry_attempt and prior_status in {"upload_unknown", "uploading"}:
                    queue_utils.mark_clip_skipped(
                        STATE_DIR, queue_id, clip_index, "ambiguous_retry_quota"
                    )
                    message = "⏭️ تعذرت إعادة المقطع الملتبس ضمن حد YouTube؛ أُغلق بعد محاولة الإعادة."
                else:
                    result_status = queue_utils.mark_clip_failed(
                        STATE_DIR, queue_id, clip_index, failure_type=safe_label
                    )
                    message = (
                        "⏭️ تم تخطي المقطع بعد فشلين مؤكدين؛ سيتابع الطابور لاحقًا."
                        if result_status == "skipped"
                        else "⚠️ رُصد حد YouTube اليومي؛ سيتوقف النشر الآن وستعاد محاولة المقطع لاحقًا."
                    )
                report_failed(batch, clip_index, message)
                return "quota"

            upload_started = current_status in {"uploading", "upload_unknown"}
            confirmed_failure = _is_confirmed_upload_failure(exc)
            if retry_attempt:
                if prior_status == "failed_retryable" and confirmed_failure:
                    result_status = queue_utils.mark_clip_failed(
                        STATE_DIR, queue_id, clip_index, failure_type=safe_label
                    )
                    message = (
                        "⏭️ فشلت المحاولة الثانية المؤكدة؛ تم تخطي المقطع نهائيًا."
                        if result_status == "skipped"
                        else "⏸️ فشل المقطع؛ بقيت محاولة مؤجلة واحدة."
                    )
                elif upload_started and not confirmed_failure:
                    queue_utils.mark_clip_skipped(
                        STATE_DIR, queue_id, clip_index, "retry_result_unconfirmed"
                    )
                    message = "⏭️ بقيت نتيجة محاولة الإعادة غير مؤكدة؛ أُغلق المقطع دون تكرار رفعه."
                elif prior_status in {"upload_unknown", "uploading"}:
                    queue_utils.mark_clip_skipped(
                        STATE_DIR, queue_id, clip_index, "ambiguous_retry_failed"
                    )
                    message = "⏭️ لم تنجح إعادة المقطع الملتبس؛ تم إنهاؤه ومتابعة الطابور."
                else:
                    result_status = queue_utils.mark_clip_failed(
                        STATE_DIR, queue_id, clip_index, failure_type=safe_label
                    )
                    message = (
                        "⏭️ فشلت المحاولة الثانية؛ تم تخطي المقطع نهائيًا."
                        if result_status == "skipped"
                        else "⏸️ فشل المقطع بشكل مؤكد؛ ستتاح له محاولة واحدة لاحقًا."
                    )
            elif upload_started and not confirmed_failure:
                queue_utils.mark_clip_upload_unknown(
                    STATE_DIR, queue_id, clip_index, safe_label
                )
                message = (
                    f"⏸️ نتيجة رفع المقطع {done + 1}/{total} غير مؤكدة؛ "
                    "نُقل إلى قائمة الإعادة المؤقتة لليوم التالي، وسيستمر الملف بالمقاطع الباقية."
                )
            else:
                result_status = queue_utils.mark_clip_failed(
                    STATE_DIR, queue_id, clip_index, failure_type=safe_label
                )
                message = (
                    "⏭️ تم تخطي المقطع بعد فشلين مؤكدين؛ سيستمر الملف بالمقطع التالي."
                    if result_status == "skipped"
                    else f"⏸️ فشل المقطع {done + 1}/{total} بشكل مؤكد؛ أُضيف لقائمة الإعادة المؤقتة لليوم التالي."
                )
            report_failed(batch, clip_index, message)
            return "failed"
        finally:
            for filename in os.listdir(DOWNLOAD_DIR):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, filename))
                except OSError:
                    pass

    retry_successes = 0
    retry_stopped = False
    retry_processed = 0
    if retry_candidates:
        print(f"📥 مقاطع الإعادة المؤقتة المستحقة اليوم: {len(retry_candidates)}")
        for candidate in retry_candidates:
            if not publish_quota.can_publish(STATE_DIR, DAILY_LIMIT):
                retry_stopped = True
                break
            result = process_clip(
                candidate["queue_id"], candidate["clip_index"], retry_attempt=True
            )
            retry_processed += 1
            if result == "success":
                retry_successes += 1
            if result == "quota":
                retry_stopped = True
                break

    # إذا نجح أي مقطع مؤقت، ينتهي التشغيل قبل بدء ملف مصدر جديد. وإذا فشلت
    # كل الإعادات، نختار ملف المصدر الأقدم المتبقي ونكمل مقاطعه العادية.
    if retry_successes == 0 and not retry_stopped and retry_processed == len(retry_candidates):
        refreshed_batches = queue_utils.list_active_batches(STATE_DIR)
        source_batch = None
        for candidate_batch in refreshed_batches:
            pending = any(
                clip.get("status") == "pending"
                for clip in candidate_batch.get("clips", [])
            )
            if pending:
                source_batch = candidate_batch
                break
            if any(
                clip.get("status") in {"failed_retryable", "upload_unknown", "uploading"}
                for clip in candidate_batch.get("clips", [])
            ):
                # قبل يوم الإعادة لا نتجاوز ملف FIFO؛ وبعد فشل كل إعادات
                # اليوم نبدأ الملف التالي كما اختار المستخدم.
                if not retry_candidates:
                    break

        if source_batch is not None:
            queue_id = source_batch.get("queue_id", source_batch["video_id"])
            source_path = source_batch["_path"]
            source_snapshot = queue_utils.load_batch(source_path)
            pending_indices = [
                index for index, clip in enumerate(source_snapshot.get("clips", []))
                if clip.get("status") == "pending"
            ]
            print(
                f"📦 معالجة ملف المصدر {queue_id}؛ المقاطع العادية المتبقية: "
                f"{len(pending_indices)}"
            )
            for clip_index in pending_indices:
                if not publish_quota.can_publish(STATE_DIR, DAILY_LIMIT):
                    print("🚫 تم الوصول للحد اليومي أثناء التشغيل. إيقاف بقية المقاطع.")
                    break
                result = process_clip(queue_id, clip_index, retry_attempt=False)
                if result == "quota":
                    break
        elif not refreshed_batches:
            print("✅ لا توجد مقاطع مصدر متبقية بعد إنهاء إعادة المحاولة المؤقتة.")

    queue_utils.refresh_temporary_retry_queue(STATE_DIR, today)
    send_unpublished_reports()
    for chat_id, items in daily_summary.items():
        report = "📋 ملخص تشغيل Smart Long اليوم:\n\n" + "\n".join(items)
        for start in range(0, len(report), 3800):
            send_message(bot_token, chat_id, report[start:start + 3800])
    print("\n🎉 انتهى التشغيل اليومي لخط long-smart.")

if __name__ == "__main__":
    sys.exit(run_daily_batch())
