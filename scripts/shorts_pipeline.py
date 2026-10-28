"""Telegram -> YouTube Shorts pipeline."""
import os
import sys
import traceback
import time

import yt_dlp
from googleapiclient.http import MediaFileUpload
from cookie_utils import temporary_cookie_file

from telegram_utils import (
    PUBLISHED_URL_TTL_SECONDS,
    canonicalize_url,
    fetch_all_new_messages,
    load_published_urls,
    load_message_ledger,
    merge_latest_state,
    message_key,
    record_published_url,
    save_message_ledger,
    send_batch_summary,
    send_message,
    delete_message,
    acknowledge_smart_long_command,
)
from youtube_auth import get_youtube_client
from youtube_dedupe import append_request_marker, recent_request_markers, request_marker
from source_validation import probe_source_url
import long_smart_pipeline
import fixed_meta
from short_title import combine_original_text, select_short_title
from video_compositor import compose_vertical

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(PROJECT_DIR, "state")
DOWNLOAD_DIR = os.path.join(PROJECT_DIR, "downloads_short")
DIRECT_LAYOUT_PATH = os.path.join(PROJECT_DIR, "assets", "shorts_layout_direct.json")
BOT_NAME = "shorts"
CLIP_START = 0
CLIP_END = 60


def download_clip(url: str, out_path: str) -> dict:
    ydl_opts = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "outtmpl": out_path,
        "download_ranges": yt_dlp.utils.download_range_func(None, [(CLIP_START, CLIP_END)]),
        "force_keyframes_at_cuts": True,
        "overwrites": True,
        "quiet": True,
        "noprogress": True,
        # YouTube now requires a JS runtime for reliable player responses.
        # Keep Shorts aligned with the Long and Long-Smart download paths.
        "js_runtimes": {"deno": {}},
    }
    with temporary_cookie_file(os.environ.get("YT_COOKIES")) as cookie_path:
        if cookie_path:
            ydl_opts["cookiefile"] = cookie_path
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)


def apply_template(raw_path: str, final_path: str) -> bool:
    compose_vertical(
        raw_path,
        final_path,
        label="SHORTS",
        layout_path=DIRECT_LAYOUT_PATH,
    )
    return True


def upload_video(youtube, file_path: str, title: str, description: str) -> str:
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "categoryId": "22",
        },
        "status": {"privacyStatus": "public"},
    }
    response = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(file_path, resumable=True),
    ).execute()
    return response["id"]


def process_long_commands(bot_token, long_commands, ledger):
    # أعد محاولة حذف رسائل أوامر Smart Long التي نجح تحليلها سابقًا لكن
    # تعذر حذف أصلها من Telegram.
    for key, record in ledger.items():
        if record.get("pipeline") != "long-smart" or not record.get("delete_pending"):
            continue
        if delete_message(bot_token, record["chat_id"], record["message_id"]):
            record["delete_pending"] = False
            print(f"🗑️ أُعيدت محاولة حذف رسالة /long بنجاح: {key}")
    save_message_ledger(STATE_DIR, BOT_NAME, ledger)

    if not long_commands:
        return

    # قد يكون الطابور أُنشئ والـ ledger حُفظ قبل توقف التشغيل، لكن قبل حذف
    # نسخة الأمر من Inbox. في هذه الحالة نؤكد الطلب ونزيله دون إعادة Gemini.
    for item in long_commands:
        key = message_key(item)
        if ledger.get(key, {}).get("status") == "succeeded":
            deleted = delete_message(bot_token, item["chat_id"], item["message_id"])
            ledger[key]["pipeline"] = "long-smart"
            ledger[key]["delete_pending"] = not deleted
            acknowledge_smart_long_command(STATE_DIR, item)

    unique = []
    seen = set()
    for item in long_commands:
        key = message_key(item)
        if key in seen:
            continue
        seen.add(key)
        if ledger.get(key, {}).get("status") in {"succeeded", "upload_unknown"}:
            continue
        unique.append(item)

    for item in unique:
        key = message_key(item)
        ledger[key] = {
            "status": "processing",
            "stage": "analyzing_long",
            "chat_id": item["chat_id"],
            "message_id": item["message_id"],
            "source_url": item["url"],
        }
    if unique:
        save_message_ledger(STATE_DIR, BOT_NAME, ledger)

    results = long_smart_pipeline.handle_new_long_commands(
        bot_token=bot_token, long_commands=unique
    )
    for item in unique:
        key = message_key(item)
        result = results.get(key, "failed_retryable")
        ledger[key]["status"] = result
        ledger[key]["stage"] = "queue_created" if result == "succeeded" else "analysis_failed"
        if result == "succeeded":
            deleted = delete_message(bot_token, item["chat_id"], item["message_id"])
            ledger[key]["pipeline"] = "long-smart"
            ledger[key]["delete_pending"] = not deleted
            acknowledge_smart_long_command(STATE_DIR, item)
            if deleted:
                print(f"🗑️ حُذفت رسالة /long: {item['message_id']}")
            else:
                print(f"⚠️ تعذر حذف رسالة /long: {item['message_id']}")
    if unique:
        save_message_ledger(STATE_DIR, BOT_NAME, ledger)


def main():
    bot_token = os.environ["SHORTS_BOT_TOKEN"]
    youtube_token_json = os.environ["YOUTUBE_TOKEN_JSON"]
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    print("🔎 جاري فحص رسائل بوت شورتس الجديدة...")
    links, long_commands = fetch_all_new_messages(bot_token, STATE_DIR, BOT_NAME)
    manual_long_url = os.environ.get("MANUAL_LONG_URL", "").strip()
    if manual_long_url:
        manual_chat_id = int(os.environ.get("MANUAL_LONG_CHAT_ID", "6828354904"))
        manual_message_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
        long_commands.append({
            "url": manual_long_url,
            "chat_id": manual_chat_id,
            "message_id": manual_message_id,
            "date": int(time.time()),
            "text": f"/long {manual_long_url}",
        })
        print(
            f"📌 تشغيل يدوي لـ Smart Long: {manual_long_url} "
            f"(message={manual_message_id})"
        )
    send_batch_summary(bot_token, links, "رابط")
    send_batch_summary(bot_token, long_commands, "أمر /long")
    ledger = load_message_ledger(STATE_DIR, BOT_NAME)

    process_long_commands(bot_token, long_commands, ledger)

    # الطلب = رسالة Telegram. الرابط لا يدخل في deduplication.
    unique_links = []
    seen_keys = set()
    for item in links:
        key = message_key(item)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        status = ledger.get(key, {}).get("status")
        if status in {"succeeded", "upload_unknown", "processing"}:
            print(f"ℹ️ الرسالة مسجلة مسبقًا ({status}): {key}")
            continue
        unique_links.append(item)

    # أعد فقط الطلبات التي فشلت قبل بدء الرفع.
    for key, record in ledger.items():
        if record.get("status") != "failed_retryable" or key in seen_keys:
            continue
        if not record.get("source_url") or "chat_id" not in record or "message_id" not in record:
            continue
        seen_keys.add(key)
        unique_links.append({
            "url": record["source_url"],
            "chat_id": record["chat_id"],
            "message_id": record["message_id"],
            "text": record["source_url"],
        })
    links = unique_links

    if not links:
        print("📭 لا توجد روابط شورتس عادية جديدة.")
        return

    total = len(links)
    unpublished_results: dict[int, list[str]] = {}

    def add_unpublished(chat_id: int, message: str) -> None:
        unpublished_results.setdefault(chat_id, []).append(message)

    def send_unpublished_reports() -> None:
        for chat_id, results in unpublished_results.items():
            report = (
                "⚠️ تقرير إضافي — مقاطع Shorts لم تُنشر أو لم يُؤكَّد نشرها:\n\n"
                + "\n\n".join(results)
            )
            for start in range(0, len(report), 3800):
                send_message(bot_token, chat_id, report[start:start + 3800])

    # افحص كل رابط مصدر أولًا، قبل محاولة تجديد توكن YouTube.
    source_checked_links = []
    for item in links:
        url = item["url"]
        key = message_key(item)
        print(f"[SHORTS][SOURCE_CHECK] START: {url}", flush=True)
        try:
            probe_source_url(url, os.environ.get("YT_COOKIES"))
            source_checked_links.append(item)
            print("[SHORTS][SOURCE_CHECK] OK: المصدر قابل للمعالجة", flush=True)
        except Exception as exc:
            error = f"تعذر فحص/تنزيل المصدر قبل YouTube: {exc}"
            print(f"[SHORTS][SOURCE_CHECK] FAIL: {error}", flush=True)
            ledger[key] = {
                "status": "failed_retryable",
                "stage": "source_check",
                "chat_id": item["chat_id"],
                "message_id": item["message_id"],
                "source_url": url,
                "error": error[:500],
            }
            save_message_ledger(STATE_DIR, BOT_NAME, ledger)
            unpublished_results.setdefault(item["chat_id"], []).append(
                f"🆔 رسالة الطلب: {item['message_id']}\n"
                f"🎬 المصدر: {url}\n❌ السبب: {error[:500]}"
            )
    links = source_checked_links
    if not links:
        send_unpublished_reports()
        return

    print("[SHORTS][AUTH] START: تجهيز اتصال YouTube بعد فحص المصادر", flush=True)
    youtube = get_youtube_client(youtube_token_json)
    print("[SHORTS][DEDUPE] START: فحص آخر 10 فيديوهات القناة (Long + Shorts)", flush=True)
    recent_markers = recent_request_markers(youtube)
    print(f"[SHORTS][DEDUPE] OK: تم العثور على {len(recent_markers)} معرف طلب", flush=True)
    published_urls = load_published_urls(STATE_DIR, BOT_NAME)
    print(
        f"[SHORTS][DEDUPE] OK: الروابط المنشورة النشطة خلال "
        f"{PUBLISHED_URL_TTL_SECONDS // 3600} ساعة={len(published_urls)}",
        flush=True,
    )

    for idx, item in enumerate(links, 1):
        url = item["url"]
        chat_id = item["chat_id"]
        message_id = item["message_id"]
        key = message_key(item)
        marker = request_marker("shorts", key)
        raw_path = os.path.join(DOWNLOAD_DIR, f"raw_{idx}.mp4")
        final_path = os.path.join(DOWNLOAD_DIR, f"final_{idx}.mp4")
        canonical_url = item.get("canonical_url") or canonicalize_url(url)

        ledger[key] = {
            "status": "processing",
            "stage": "downloading",
            "chat_id": chat_id,
            "message_id": message_id,
            "source_url": url,
        }
        save_message_ledger(STATE_DIR, BOT_NAME, ledger)

        try:
            # حماية إضافية للرابط نفسه عند وصوله من رسالة Telegram ثانية أو
            # بصيغة YouTube مختلفة؛ marker وحده يحمي message_id فقط.
            published_record = published_urls.get(canonical_url)
            if not published_record:
                for previous in ledger.values():
                    if (
                        previous.get("status") == "succeeded"
                        and canonicalize_url(previous.get("source_url", "")) == canonical_url
                    ):
                        published_record = previous
                        break
            if published_record:
                previous_video_url = published_record.get("video_url")
                if not previous_video_url and published_record.get("video_id"):
                    previous_video_url = f"https://youtu.be/{published_record['video_id']}"
                ledger[key] = {
                    "status": "succeeded",
                    "stage": "uploaded",
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "source_url": url,
                    "video_id": published_record.get("video_id"),
                    "video_url": previous_video_url,
                    "dedupe_source": "published_url_48h",
                }
                save_message_ledger(STATE_DIR, BOT_NAME, ledger)
                print(
                    f"⏭️ تم تجاوز مصدر منشور خلال 48 ساعة: "
                    f"{previous_video_url or canonical_url}"
                )
                if delete_message(bot_token, chat_id, message_id):
                    print(f"🗑️ حُذفت رسالة الرابط المكرر: {message_id}")
                continue

            youtube_existing_id = recent_markers.get(marker)
            if youtube_existing_id:
                video_url = f"https://youtu.be/{youtube_existing_id}"
                ledger[key] = {
                    "status": "succeeded",
                    "stage": "uploaded",
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "source_url": url,
                    "video_id": youtube_existing_id,
                    "video_url": video_url,
                    "dedupe_source": "youtube_recent_10",
                }
                save_message_ledger(STATE_DIR, BOT_NAME, ledger)
                print(f"⏭️ تم العثور على النشر السابق في YouTube: {video_url}")
                if delete_message(bot_token, chat_id, message_id):
                    print(f"🗑️ حُذفت رسالة الرابط المؤكدة سابقًا: {message_id}")
                continue

            info = download_clip(url, raw_path)
            title = select_short_title(info, url, item)
            desc = fixed_meta.build_final_description(
                combine_original_text(info),
                source_handle=fixed_meta.source_handle_from_info(info),
                source_url=url,
            )
            desc = append_request_marker(desc, marker)

            actual_raw = raw_path
            if not os.path.exists(actual_raw):
                candidates = [
                    f for f in os.listdir(DOWNLOAD_DIR)
                    if f.startswith(f"raw_{idx}")
                ]
                if candidates:
                    actual_raw = os.path.join(DOWNLOAD_DIR, candidates[0])

            apply_template(actual_raw, final_path)
            upload_file = final_path

            ledger[key]["stage"] = "uploading"
            save_message_ledger(STATE_DIR, BOT_NAME, ledger)

            video_id = upload_video(
                youtube, upload_file, title, desc
            )
            recent_markers[marker] = video_id
            video_url = f"https://youtu.be/{video_id}"

            # ثبّت نجاح الرفع قبل أي عملية Telegram.
            ledger[key] = {
                "status": "succeeded",
                "stage": "uploaded",
                "chat_id": chat_id,
                "message_id": message_id,
                "source_url": url,
                "video_id": video_id,
                "video_url": video_url,
            }
            save_message_ledger(STATE_DIR, BOT_NAME, ledger)
            record_published_url(STATE_DIR, BOT_NAME, url, video_id)
            published_urls = load_published_urls(STATE_DIR, BOT_NAME)

            report = (
                f"✅ <b>تم نشر الطلب بنجاح</b> ({idx}/{total})\n\n"
                f"🆔 <b>رسالة الطلب:</b> {message_id}\n"
                f"🎬 <b>المصدر:</b> {url}\n"
                f"📝 <b>العنوان:</b> {title}\n"
                f"⏱️ <b>المقطع:</b> {CLIP_END - CLIP_START} ثانية\n"
                f"📺 <b>الفيديو:</b> {video_url}\n\n"
                "🗑️ تم حذف رسالة الطلب بعد نجاح النشر.\n"
                "ℹ️ لن يُعاد نشر نفس الرابط خلال 48 ساعة."
            )
            send_message(bot_token, chat_id, report)

            if delete_message(bot_token, chat_id, message_id):
                print(f"🗑️ حُذفت رسالة الرابط بعد التقرير: {message_id}")
            else:
                print(f"⚠️ تعذر حذف رسالة الرابط: {message_id}")

        except Exception as e:
            err = str(e)
            print(f"❌ فشل: {err}")
            traceback.print_exc()
            stage = ledger.get(key, {}).get("stage")
            ledger[key]["status"] = (
                "upload_unknown" if stage == "uploading" else "failed_retryable"
            )
            ledger[key]["error"] = err[:500]
            save_message_ledger(STATE_DIR, BOT_NAME, ledger)
            send_message(
                bot_token, chat_id,
                f"❌ <b>فشل الطلب</b>\n\n"
                f"🆔 رسالة الطلب: {message_id}\n"
                f"🎬 المصدر: {url}\n\n"
                f"الخطأ: {err[:500]}",
            )
            add_unpublished(
                chat_id,
                f"🆔 رسالة الطلب: {message_id}\n"
                f"🎬 المصدر: {url}\n❌ السبب: {err[:500]}",
            )

        finally:
            for f in os.listdir(DOWNLOAD_DIR):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, f))
                except OSError:
                    pass

    send_unpublished_reports()

    try:
        merge_latest_state(STATE_DIR, BOT_NAME)
    except Exception as e:
        print(f"⚠️ فشل الدمج الاحتياطي للحالة: {e}")


if __name__ == "__main__":
    sys.exit(main())
