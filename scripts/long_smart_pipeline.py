"""
خط إنتاج "long-smart" — يُفعَّل عبر أمر /long <رابط> في بوت الشورتس.

مختلف تمامًا عن خط اللونق الفوري (long_pipeline.py) وخط الشورتس القديم
(القص الثابت 60 ثانية). هذا الخط:

المرحلة أ) عند وصول أمر /long <رابط> جديد:
    1) يسحب الترانسكريبت (نص + توقيت) من يوتيوب (transcript_utils).
    2) يرسله لجيمناي ليقترح بالضبط 8 مقاطع (نوع long/shorts، توقيت، عنوان، وصف)
       بصيغة JSON (gemini_utils).
    3) يحفظ الـ 8 مقاطع في ملف طابور خاص بهذا الفيديو (queue_utils)، كلها
       بحالة "pending".
    4) لا يُنشر أي شيء فورًا في هذه المرحلة — النشر يتم في المرحلة ب فقط.

المرحلة ب) عند كل تشغيل يومي مجدول (run_daily_batch):
    - يمر على كل الطوابير النشطة (فيها مقطع pending واحد على الأقل).
    - يأخذ **مقطعًا واحدًا (أول pending) من كل طابور نشط** وينشره،
      طالما لم نصل لسقف النشر اليومي المشترك لخط long-smart بالكامل
      (عداد منفصل تمامًا عن خط اللونق الفوري وخط الشورتس القديم).
    - عند اكتشاف خطأ كوتا من يوتيوب في أي لحظة، يتوقف فورًا عن أي محاولات
      نشر أخرى في هذا التشغيل ويسجّل ذلك.
    - العنوان والوصف النهائيان = عنوان/وصف جيمناي + توقيع ثابت من
      assets/fixed_title.txt و assets/fixed_description.txt.
"""
import os
import subprocess
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

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(PROJECT_DIR, "state")
DOWNLOAD_DIR = os.path.join(PROJECT_DIR, "downloads_long_smart")
WORK_DIR = os.path.join(PROJECT_DIR, "work_long_smart")
DAILY_LIMIT = 8  # حد نشر يومي منفصل خاص بخط long-smart فقط
TRANSCRIPT_CHUNK_CHARS = 45000

TEMPLATE_PATH = os.path.join(PROJECT_DIR, "assets", "template.png")
TEMPLATE_NATIVE_W, TEMPLATE_NATIVE_H = 720, 1280  # أبعاد assets/template.png
TEMPLATE_BOX_X, TEMPLATE_BOX_Y = 48, 202  # بداية فتحة الفيديو داخل الإطار
TEMPLATE_BOX_W, TEMPLATE_BOX_H = 624, 870  # أبعاد الفتحة الداخلية
SHORTS_OUTPUT_W, SHORTS_OUTPUT_H = 1080, 1920


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
            total_clips = 0
            for part_index, chunk in enumerate(transcript_chunks):
                part_queue_id = queue_utils.queue_id_for_request(f"{key}:part:{part_index}")
                if queue_utils.queue_exists(STATE_DIR, part_queue_id):
                    print(f"⏭️ الجزء {part_index + 1} محفوظ مسبقًا، لن يعاد تحليله.")
                    total_clips += 8
                    continue
                clips = suggest_clips(gemini_api_key, transcript["title"], chunk)
                print(
                    f"✅ جيمناي اقترح {len(clips)} مقطعًا للجزء "
                    f"{part_index + 1}/{len(transcript_chunks)}."
                )
                queue_utils.save_new_batch(
                    state_dir=STATE_DIR,
                    queue_id=part_queue_id,
                    request_key=key,
                    video_id=video_id,
                    source_url=url,
                    source_title=transcript["title"],
                    source_handle=source_handle,
                    chat_id=chat_id,
                    clips=clips,
                    part_index=part_index,
                    part_count=len(transcript_chunks),
                )
                total_clips += len(clips)
            expected_queue_ids = [
                queue_utils.queue_id_for_request(f"{key}:part:{part_index}")
                for part_index in range(len(transcript_chunks))
            ]
            if not transcript_chunks or total_clips <= 0 or not all(
                queue_utils.queue_exists(STATE_DIR, queue_id) for queue_id in expected_queue_ids
            ):
                raise RuntimeError(
                    "لم تُحفظ كل دفعات Gemini في ملفات Queue؛ ستبقى رسالة الطلب للمحاولة التالية."
                )
            results[key] = "succeeded"

            send_message(
                bot_token, chat_id,
                f"✅ تم إنهاء الطلب بنجاح عبر جيمناي.\n"
                f"🆔 رسالة الطلب: {cmd['message_id']}\n"
                f"🎬 المصدر: {url}\n"
                f"📝 العنوان: {transcript['title']}\n"
                f"تم حفظ {total_clips} مقطعًا مقترحًا في {len(transcript_chunks)} ملف/ملفات Queue.\n\n"
                f"📌 سيُنشر ملف Queue واحد كاملًا كل يوم بالترتيب، حتى تنتهي كل المقاطع.\n"
                f"ℹ️ إرسال نفس الرابط في رسالة جديدة سيُعامل كطلب جديد مستقل.",
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
    if not os.path.exists(TEMPLATE_PATH):
        print("⚠️ لا يوجد قالب شورتس — سيُرفع المقطع بدون قالب.")
        return False

    sx = SHORTS_OUTPUT_W / TEMPLATE_NATIVE_W
    sy = SHORTS_OUTPUT_H / TEMPLATE_NATIVE_H
    box_x = int(TEMPLATE_BOX_X * sx)
    box_y = int(TEMPLATE_BOX_Y * sy)
    box_w = int(TEMPLATE_BOX_W * sx)
    box_h = int(TEMPLATE_BOX_H * sy)

    cmd = [
        "ffmpeg", "-y",
        "-i", TEMPLATE_PATH,
        "-i", raw_path,
        "-filter_complex",
        f"[0:v]scale={SHORTS_OUTPUT_W}:{SHORTS_OUTPUT_H}[bg];"
        f"[1:v]scale={box_w}:{box_h}:force_original_aspect_ratio=increase,"
        f"crop={box_w}:{box_h}[vid];"
        f"[bg][vid]overlay={box_x}:{box_y}:format=auto",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        final_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"⚠️ فشل دمج القالب: {result.stderr[-500:]}")
        return False
    return True


def _upload(youtube, file_path: str, title: str, description: str) -> str:
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "categoryId": "22",
        },
        "status": {"privacyStatus": "public"},
    }
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(file_path, resumable=True),
    )
    response = request.execute()
    return response["id"]


def run_daily_batch() -> None:
    bot_token = os.environ.get("SHORTS_BOT_TOKEN")
    youtube_token_json = os.environ["YOUTUBE_TOKEN_JSON"]
    cookies_env = os.environ.get("YT_COOKIES")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    queue_utils.merge_latest_state(STATE_DIR)

    active_batches = queue_utils.list_active_batches(STATE_DIR)
    if not active_batches:
        print("📭 لا توجد طوابير long-smart نشطة اليوم. إنهاء.")
        return

    # التزم بملف واحد في اليوم: لا ننتقل إلى Queue التالي حتى تنتهي كل
    # مقاطع الملف الحالي. نكرر نفس المرجع بعدد المقاطع ونقرأ حالته من القرص
    # في بداية كل دورة، لذلك يبقى غير المنشور لليوم التالي عند توقف quota.
    first_batch = active_batches[0]
    active_batches = [first_batch] * len(first_batch["clips"])
    print(f"📦 عدد ملفات Queue النشطة: {len(queue_utils.list_active_batches(STATE_DIR))}")

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

    def send_unpublished_reports() -> None:
        for chat_id, results in unpublished_results.items():
            report = (
                "⚠️ تقرير إضافي — مقاطع Long-Smart لم تُنشر أو لم يُؤكَّد نشرها:\n\n"
                + "\n\n".join(results)
            )
            for start in range(0, len(report), 3800):
                send_message(bot_token, chat_id, report[start:start + 3800])

    for batch_idx, batch in enumerate(active_batches, 1):
        batch = queue_utils.load_batch(batch["_path"])
        if not publish_quota.can_publish(STATE_DIR, DAILY_LIMIT):
            print("🚫 تم الوصول للحد اليومي أثناء التشغيل. إيقاف باقي الدفعات.")
            break

        video_id = batch["video_id"]
        queue_id = batch.get("queue_id", video_id)
        chat_id = batch["chat_id"]
        clip_idx = queue_utils.get_next_pending_index(batch)
        if clip_idx is None:
            continue

        clip = batch["clips"][clip_idx]
        marker = request_marker("long-smart", f"{queue_id}:{clip_idx}")
        done, total = queue_utils.batch_progress(batch)
        print(
            f"\n{'='*50}\n[ملف Queue {batch.get('part_index', 0) + 1}/"
            f"{batch.get('part_count', 1)} — دورة {batch_idx}/{len(active_batches)}] "
            f"فيديو {video_id} — مقطع {done + 1}/{total} (نوع: {clip['type']})"
        )

        raw_path = os.path.join(DOWNLOAD_DIR, f"raw_{video_id}_{clip_idx}.mp4")
        final_path = os.path.join(DOWNLOAD_DIR, f"final_{video_id}_{clip_idx}.mp4")

        try:
            youtube_existing_id = recent_markers.get(marker)
            if youtube_existing_id:
                video_url = f"https://youtu.be/{youtube_existing_id}"
                print(f"⏭️ تم العثور على النشر السابق في YouTube: {video_url}")
                queue_utils.mark_clip_published(STATE_DIR, queue_id, clip_idx, video_url)
                publish_quota.record_publish_success(STATE_DIR)
                recent_markers[marker] = youtube_existing_id
                if bot_token:
                    send_message(
                        bot_token, chat_id,
                        f"✅ تم تأكيد نشر مقطع {done + 1}/{total} سابقًا:\n{video_url}",
                    )
                add_summary(chat_id, f"✅ مؤكد سابقًا: المقطع {done + 1}/{total} — {video_url}")
                continue

            if queue_utils.get_clip_status(STATE_DIR, queue_id, clip_idx) == "upload_unknown":
                message = (
                    f"⚠️ تعذر تأكيد رفع مقطع {done + 1}/{total} بعد انقطاع الاتصال. "
                    "لن تتم إعادة رفعه تلقائيًا حمايةً من التكرار؛ راجع آخر فيديوهات القناة."
                )
                add_unpublished(chat_id, f"🆔 الطابور: {queue_id}\n{message}")
                if bot_token:
                    send_message(bot_token, chat_id, message)
                add_summary(chat_id, f"⚠️ غير مؤكد: المقطع {done + 1}/{total}")
                continue

            print(f"⬇️ تحميل المقطع ({clip['start']}-{clip['end']} ثانية)...")
            _download_clip_range(batch["source_url"], clip["start"], clip["end"], raw_path, cookies_env)

            actual_raw = raw_path
            if not os.path.exists(actual_raw):
                candidates = [
                    f for f in os.listdir(DOWNLOAD_DIR)
                    if f.startswith(f"raw_{video_id}_{clip_idx}")
                ]
                if candidates:
                    actual_raw = os.path.join(DOWNLOAD_DIR, candidates[0])

            upload_file = actual_raw
            if clip["type"] == "shorts":
                print("🖼️ دمج قالب الشورتس...")
                merged = _apply_shorts_template(actual_raw, final_path)
                upload_file = final_path if merged else actual_raw

            final_title = fixed_meta.build_final_title(clip["title"])
            final_description = append_request_marker(
                fixed_meta.build_final_description(
                    clip["description"],
                    source_handle=batch.get("source_handle"),
                    source_url=batch.get("source_url"),
                ), marker
            )

            title_for_upload = f"{final_title} #Shorts" if clip["type"] == "shorts" else final_title

            # سجّل بداية الرفع قبل استدعاء YouTube. عند انقطاع الاتصال بعد
            # قبول YouTube للملف، نستخدم upload_unknown بدل إعادة الرفع.
            queue_utils.mark_clip_uploading(STATE_DIR, queue_id, clip_idx)
            print("📤 رفع على يوتيوب (public)...")
            video_youtube_id = _upload(youtube, upload_file, title_for_upload, final_description)
            recent_markers[marker] = video_youtube_id
            video_url = f"https://youtu.be/{video_youtube_id}"
            print(f"✅ تم النشر: {video_url}")

            queue_utils.mark_clip_published(STATE_DIR, queue_id, clip_idx, video_url)
            publish_quota.record_publish_success(STATE_DIR)

            if bot_token:
                send_message(
                    bot_token, chat_id,
                    f"✅ نُشر مقطع {done + 1}/{total} من الفيديو المصدر "
                    f"\"{batch['source_title']}\" ({clip['type']}):\n{video_url}",
                )
            add_summary(chat_id, f"✅ نُشر: المقطع {done + 1}/{total} — {video_url}")

        except Exception as e:
            err = f"{e}"
            print(f"❌ فشل: {err}")
            traceback.print_exc()

            if publish_quota.is_quota_error(e):
                print("🚫 تم اكتشاف خطأ كوتا يوتيوب — إيقاف كل النشر لهذا اليوم.")
                publish_quota.record_quota_hit(STATE_DIR)
                if bot_token:
                    send_message(
                        bot_token, chat_id,
                        "⚠️ تم الوصول لحد نشر يوتيوب اليومي (رصدناه من رسالة الخطأ). "
                        "سيتم إكمال النشر غدًا تلقائيًا.",
                    )
                # خطأ quota معروف يعني أن هذا الطلب لم يُقبل للنشر عادةً؛
                # أعد المقطع pending عمليًا لليوم التالي، ولا تجعله
                # upload_unknown الذي قد يوقف الملف بلا داعٍ.
                queue_utils.mark_clip_failed(STATE_DIR, queue_id, clip_idx, err)
                add_unpublished(
                    chat_id,
                    f"🆔 الطابور: {queue_id} — المقطع: {clip_idx + 1}\n"
                    f"🎬 المصدر: {batch['source_url']}\n❌ السبب: {err[:500]}",
                )
                break

            if queue_utils.get_clip_status(STATE_DIR, queue_id, clip_idx) == "uploading":
                queue_utils.mark_clip_upload_unknown(STATE_DIR, queue_id, clip_idx, err)
            else:
                queue_utils.mark_clip_failed(STATE_DIR, queue_id, clip_idx, err)
            add_unpublished(
                chat_id,
                f"🆔 الطابور: {queue_id} — المقطع: {clip_idx + 1}\n"
                f"🎬 المصدر: {batch['source_url']}\n❌ السبب: {err[:500]}",
            )
            if bot_token:
                send_message(
                    bot_token, chat_id,
                    f"❌ فشل نشر أحد مقاطع \"{batch['source_title']}\":\n{err[:300]}",
                )
            add_summary(chat_id, f"❌ فشل: المقطع {done + 1}/{total} — {err[:180]}")

        finally:
            for f in os.listdir(DOWNLOAD_DIR):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, f))
                except OSError:
                    pass

    send_unpublished_reports()
    for chat_id, items in daily_summary.items():
        report = "📋 ملخص تشغيل Smart Long اليوم:\n\n" + "\n".join(items)
        for start in range(0, len(report), 3800):
            send_message(bot_token, chat_id, report[start:start + 3800])
    print("\n🎉 انتهى التشغيل اليومي لخط long-smart.")


if __name__ == "__main__":
    sys.exit(run_daily_batch())
