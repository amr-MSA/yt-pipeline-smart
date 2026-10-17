"""
سير عمل بوت "لونق":
1. يفحص بوت تلجرام لأي روابط يوتيوب جديدة أُرسلت إليه.
2. لكل رسالة رابط: يحمّل الفيديو كاملًا.
3. ينشره على قناة يوتيوب الخاصة بك كفيديو عام (public).
4. يسجل نجاح كل رسالة حسب chat_id:message_id ثم يحذف رسالة Telegram.
5. يحتفظ بنجاح الرفع إذا تعذر الحذف حتى لا يعاد رفع الطلب.
"""
import os
import re
import sys
import time
import traceback

import yt_dlp
from googleapiclient.http import MediaFileUpload
from cookie_utils import temporary_cookie_file

from telegram_utils import (
    delete_message,
    fetch_new_links,
    acknowledge_pending_link,
    canonicalize_url,
    PUBLISHED_URL_TTL_SECONDS,
    load_published_urls,
    load_uploaded_messages,
    merge_latest_state,
    message_key,
    record_published_url,
    save_uploaded_messages,
    send_message,
)
from youtube_auth import get_youtube_client
from youtube_dedupe import append_request_marker, recent_request_markers, request_marker
from source_validation import probe_source_url
import fixed_meta

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(PROJECT_DIR, "state")
DOWNLOAD_DIR = os.path.join(PROJECT_DIR, "downloads_long")
BOT_NAME = "long"
DEFAULT_TITLE = "مقطع اليوم"
TITLE_FALLBACK_PREFIX = "ميمز العرب #"
YOUTUBE_TITLE_MAX = 100
DEFAULT_DESCRIPTION = """أستغفر الله العظيم واتوب إليه 🤲


قناة تجمع لكم مقاطع منوعة ومختارات أحببت مشاركتها معكم. أتمنى تنال إعجابكم وتستمتعوا بالمشاهدة ✨🎥
شكراً لكل المتابعين على الدعم والتشجيع المستمر، وجودكم يسعدني وتفاعلكم هو الدافع للأفضل دائماً 🙏 طابت أوقاتكم بكل خير 🌸🍃



 تواصل معي (Contact)
📩 Email: 951999aliali@gmail.com
⚖️ تنويه وحقوق (إخلاء مسؤولية)
نحنُ ننشر هذه المواد تقديراً للمحتوى وحرصاً على وصوله لأكبر عدد من المتذوقين، مع كامل اعترافنا بأن الحقوق محفوظة لأصحابها الأصليين.
تنبيه هام: نظراً لأننا ننشر بحسن نية كاملة، فإذا وجد صاحب الحق أي إشكال في عرض محتواه، نرجو التكرم بالتواصل عبر الإيميل أعلاه، وسنقوم بـ حذف المقطع فوراً وبشكل نهائي تقديراً لجهودكم
"""


def download_video(url: str, out_path: str) -> dict:
    print(f"[LONG][DOWNLOAD] START: {url}", flush=True)
    ydl_opts = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best",
        "outtmpl": out_path,
        "merge_output_format": "mp4",
        "overwrites": True,
        "quiet": True,
        "noprogress": True,
        # Deno هو runtime الموصى به لـ EJS، وتثبيت yt-dlp[default] يوفر yt-dlp-ejs.
        "js_runtimes": {"deno": {}},
    }
    with temporary_cookie_file(os.environ.get("YT_COOKIES")) as cookie_path:
        if cookie_path:
            ydl_opts["cookiefile"] = cookie_path
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            print(f"[LONG][DOWNLOAD] OK: title={info.get('title', 'غير معروف')}", flush=True)
            return info


def upload_video(youtube, file_path: str, title: str, description: str) -> str:
    print(f"[LONG][UPLOAD] START: file={file_path}, title={title[:80]}", flush=True)
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
    video_id = response.get("id")
    if not video_id:
        raise RuntimeError("نجح طلب YouTube دون إرجاع videoId؛ لن تُحذف رسالة Telegram")
    print(f"[LONG][UPLOAD] OK: video_id={video_id}", flush=True)
    return video_id


def resolve_title(
    source_title: str | None,
    title_mode: str,
    title_override: str | None = None,
    message_id: int | str | None = None,
) -> str:
    """يستخدم عنوان المصدر، أو fallback ثابتًا يتضمن رقم رسالة Telegram."""
    if title_mode == "custom" and title_override:
        custom = title_override.strip()
        return custom[:YOUTUBE_TITLE_MAX] if custom else _fallback_title(message_id)
    if title_mode == "default":
        return _fallback_title(message_id)
    title = (source_title or "").strip()
    generic_source_title = re.match(r"^video\s+by\b", title, re.IGNORECASE)
    if title and not generic_source_title and len(title) <= YOUTUBE_TITLE_MAX:
        return title
    return _fallback_title(message_id)


def _fallback_title(message_id: int | str | None) -> str:
    suffix = str(message_id).strip() if message_id is not None else "0"
    return f"{TITLE_FALLBACK_PREFIX}{suffix}"[:YOUTUBE_TITLE_MAX]


def reconcile_uploaded_messages(bot_token: str, uploaded: dict) -> dict:
    """يحاول حذف الرسائل التي نجح رفعها في تشغيل سابق ولم يُحذف أصلها بعد."""
    pending = dict(uploaded)
    for key, item in list(pending.items()):
        if item.get("deleted"):
            continue
        if delete_message(bot_token, item["chat_id"], item["message_id"]):
            item["deleted"] = True
            print(f"🗑️ تم حذف رسالة Telegram المؤكدة سابقًا: {key}")
        else:
            print(f"⚠️ تعذر حذف رسالة Telegram بعد رفع مؤكد، ستتم إعادة المحاولة: {key}")
    return pending


def main():
    print("[LONG][BOOT] START: تهيئة مسار اللونق", flush=True)
    bot_token = os.environ["LONG_BOT_TOKEN"]
    youtube_token_json = os.environ["YOUTUBE_TOKEN_JSON"]
    run_mode = os.environ.get("PIPELINE_RUN_MODE", "daily").lower()
    if run_mode not in {"daily", "manual"}:
        run_mode = "daily"
    print(f"[LONG][MODE] {run_mode}", flush=True)
    print("[LONG][BOOT] OK: الأسرار المطلوبة موجودة (القيم مخفية)", flush=True)

    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print("[LONG][BOOT] OK: مجلدات الحالة والتنزيل جاهزة", flush=True)

    print("[LONG][STATE][1/3] START: قراءة سجل الرفع السابق", flush=True)
    uploaded = load_uploaded_messages(STATE_DIR, BOT_NAME)
    print(f"[LONG][STATE][1/3] OK: عدد السجلات={len(uploaded)}", flush=True)
    print("[LONG][STATE][2/3] START: معالجة الرسائل المرفوعة سابقًا", flush=True)
    reconciled = reconcile_uploaded_messages(bot_token, uploaded)
    if reconciled != uploaded:
        save_uploaded_messages(STATE_DIR, BOT_NAME, reconciled)
    uploaded = reconciled
    print("[LONG][STATE][2/3] OK: اكتملت معالجة السجلات السابقة", flush=True)
    published_urls = load_published_urls(STATE_DIR, BOT_NAME)
    print(f"[LONG][STATE][2/3] OK: الروابط المنشورة النشطة خلال 48 ساعة={len(published_urls)}", flush=True)

    print("🔎 جاري فحص رسائل بوت لونق الجديدة...")
    print("[LONG][TELEGRAM][1/2] START: جلب الرسائل الجديدة", flush=True)
    links = fetch_new_links(bot_token, STATE_DIR, BOT_NAME, run_mode=run_mode)
    print(f"[LONG][TELEGRAM][1/2] OK: الروابط المقبولة={len(links)}", flush=True)
    if not links:
        print("📭 لا توجد روابط جديدة. إنهاء.")
        print("[LONG][DONE] OK: لا توجد أعمال جديدة", flush=True)
        return

    print(f"📦 تم العثور على {len(links)} رابط جديد.")
    batch_results: dict[int, list[str]] = {}
    unpublished_results: dict[int, list[str]] = {}

    def add_result(chat_id: int, message: str) -> None:
        batch_results.setdefault(chat_id, []).append(message)

    def add_unpublished(chat_id: int, message: str) -> None:
        unpublished_results.setdefault(chat_id, []).append(message)

    def send_unpublished_reports() -> None:
        for chat_id, results in unpublished_results.items():
            report = (
                "⚠️ تقرير إضافي — مقاطع لم تُنشر أو لم يُؤكَّد نشرها:\n\n"
                + "\n\n".join(results)
            )
            # لا نتجاوز حد رسالة Telegram إذا كانت الدفعة كبيرة.
            for start in range(0, len(report), 3800):
                send_message(bot_token, chat_id, report[start:start + 3800])

    # افحص المصدر أولًا. هذا يفصل أخطاء Instagram/TikTok/yt-dlp عن OAuth
    # ويمنع إظهار خطأ YouTube لرابط لا يمكن تنزيله أصلًا.
    source_checked_links = []
    for item in links:
        print(f"[LONG][SOURCE_CHECK] START: {item['url']}", flush=True)
        try:
            probe_source_url(item["url"], os.environ.get("YT_COOKIES"))
            source_checked_links.append(item)
            print("[LONG][SOURCE_CHECK] OK: المصدر قابل للمعالجة", flush=True)
        except Exception as exc:
            error = f"تعذر فحص/تنزيل المصدر قبل YouTube: {exc}"
            print(f"[LONG][SOURCE_CHECK] FAIL: {error}", flush=True)
            add_unpublished(
                item["chat_id"],
                f"🆔 رسالة الطلب: {item['message_id']}\n"
                f"🎬 المصدر: {item['url']}\n❌ السبب: {error[:500]}",
            )
    links = source_checked_links
    if not links:
        send_unpublished_reports()
        return

    print("[LONG][AUTH] START: تجهيز اتصال YouTube بعد فحص المصادر", flush=True)
    try:
        youtube = get_youtube_client(youtube_token_json)
    except Exception as exc:
        error = f"تعذر تجهيز اتصال YouTube: {exc}"
        for item in links:
            add_unpublished(
                item["chat_id"],
                f"🆔 رسالة الطلب: {item['message_id']}\n"
                f"🎬 المصدر: {item['url']}\n❌ السبب: {error[:500]}",
            )
        send_unpublished_reports()
        raise
    print("[LONG][AUTH] OK: اتصال YouTube جاهز", flush=True)
    print("[LONG][DEDUPE] START: فحص آخر 10 فيديوهات القناة (Long + Shorts)", flush=True)
    recent_markers = recent_request_markers(youtube)
    print(f"[LONG][DEDUPE] OK: تم العثور على {len(recent_markers)} معرف طلب", flush=True)

    for idx, item in enumerate(links, 1):
        url = item["url"]
        chat_id = item["chat_id"]
        message_id = item["message_id"]
        key = message_key(item)
        print(f"\n{'='*50}\n[{idx}/{len(links)}] معالجة: {url} (message={message_id})")
        raw_path = os.path.join(DOWNLOAD_DIR, f"video_{idx}.mp4")

        try:
            # قد تكون الرسالة عادت من دفعة قديمة؛ لا نعيد الرفع إذا كان النجاح مسجلاً.
            canonical_url = item.get("canonical_url") or canonicalize_url(url)
            # منع التكرار لرابط في رسالة جديدة يعتمد على سجل TTL؛ أما سجل
            # الرسالة فيبقى مخصصًا لحماية نفس الطلب إذا عاد Telegram بالتحديث نفسه.
            now = int(time.time())
            duplicate_upload = canonical_url in published_urls or any(
                record.get("status") == "succeeded"
                and record.get("published_at", 0) + PUBLISHED_URL_TTL_SECONDS > now
                and canonicalize_url(record.get("url", "")) == canonical_url
                for record in uploaded.values()
            )
            existing_request = uploaded.get(key)
            marker = request_marker("long", key)
            youtube_existing_id = recent_markers.get(marker)
            if existing_request or duplicate_upload or youtube_existing_id:
                print(f"ℹ️ الرفع مسجل مسبقًا لهذه الرسالة؛ لن يعاد رفعها: {key}")
                if youtube_existing_id and not existing_request:
                    video_url = f"https://youtu.be/{youtube_existing_id}"
                    uploaded[key] = {
                        "status": "succeeded",
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "video_id": youtube_existing_id,
                        "url": url,
                        "published_at": int(time.time()),
                        "dedupe_source": "youtube_recent_10",
                    }
                    save_uploaded_messages(STATE_DIR, BOT_NAME, uploaded)
                    record_published_url(STATE_DIR, BOT_NAME, url, youtube_existing_id)
                    add_result(chat_id, f"✅ تم تأكيد النشر السابق\n📺 YouTube: {video_url}")
                else:
                    add_result(chat_id, "⏭️ تم تجاوز رابط مكرر سبق نشره.")
                acknowledge_pending_link(STATE_DIR, BOT_NAME, item)
                if delete_message(bot_token, chat_id, message_id):
                    if existing_request is not None:
                        existing_request["deleted"] = True
                        save_uploaded_messages(STATE_DIR, BOT_NAME, uploaded)
                continue

            print("⬇️ تحميل الفيديو...")
            info = download_video(url, raw_path)
            source_title = info.get("title") or ""
            desc = info.get("description", "") or DEFAULT_DESCRIPTION
            desc = fixed_meta.build_final_description(
                desc,
                source_handle=fixed_meta.source_handle_from_info(info),
                source_url=url,
            )
            title_mode = item.get("title_mode", "default")
            title = resolve_title(
                source_title,
                title_mode,
                item.get("title_override"),
                message_id,
            )
            desc = append_request_marker(desc, marker)

            actual_file = raw_path
            if not os.path.exists(actual_file):
                candidates = [
                    f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"video_{idx}")
                ]
                if candidates:
                    actual_file = os.path.join(DOWNLOAD_DIR, candidates[0])

            if not os.path.isfile(actual_file):
                raise FileNotFoundError("yt-dlp لم ينتج ملف الفيديو المتوقع.")

            # سجّل بدء طلب الرفع قبل استدعاء YouTube. إذا انقطع الاتصال بعد
            # قبول الطلب فلن نملك videoId، لذلك نمنع إعادة الرفع التلقائي.
            uploaded[key] = {
                "status": "uploading",
                "chat_id": chat_id,
                "message_id": message_id,
                "url": url,
            }
            save_uploaded_messages(STATE_DIR, BOT_NAME, uploaded)
            print("📤 رفع على يوتيوب (public)...")
            video_id = upload_video(youtube, actual_file, title, desc)
            video_url = f"https://youtu.be/{video_id}"
            print(f"✅ تم النشر وتأكيد videoId: {video_url}")

            # يُحفظ النجاح قبل الحذف. إذا فشل الحذف فلن يؤدي التشغيل التالي إلى إعادة الرفع.
            uploaded[key] = {
                "status": "succeeded",
                "chat_id": chat_id,
                "message_id": message_id,
                "video_id": video_id,
                "url": url,
                "published_at": int(time.time()),
            }
            save_uploaded_messages(STATE_DIR, BOT_NAME, uploaded)
            record_published_url(STATE_DIR, BOT_NAME, url, video_id)
            recent_markers[marker] = video_id
            published_urls = load_published_urls(STATE_DIR, BOT_NAME)
            acknowledge_pending_link(STATE_DIR, BOT_NAME, item)

            if delete_message(bot_token, chat_id, message_id):
                uploaded[key]["deleted"] = True
                save_uploaded_messages(STATE_DIR, BOT_NAME, uploaded)
                print(f"🗑️ حُذفت رسالة الرابط بعد نجاح الرفع: {message_id}")
            else:
                print(f"⚠️ نجح الرفع لكن تعذر حذف الرسالة؛ لن يعاد الرفع: {message_id}")

            add_result(
                chat_id,
                f"✅ تم نشر الطلب بنجاح\n"
                f"🆔 رسالة الطلب: {message_id}\n"
                f"🎬 المصدر: {url}\n"
                f"📝 العنوان: {title}\n"
                f"📺 YouTube: {video_url}\n"
                f"ℹ️ لن يُعاد نشر نفس الرابط خلال 48 ساعة.",
            )

        except Exception as e:
            err = f"{e}"
            print(f"❌ فشل: {err}")
            traceback.print_exc()
            if uploaded.get(key, {}).get("status") == "uploading":
                uploaded[key]["status"] = "upload_unknown"
                uploaded[key]["error"] = err[:500]
                save_uploaded_messages(STATE_DIR, BOT_NAME, uploaded)
                print("⚠️ حالة الرفع غير مؤكدة؛ لن تتم إعادة المحاولة تلقائيًا.")
            add_result(chat_id, f"❌ فشلت معالجة الرابط:\n{url}\n\nالخطأ: {err[:300]}")
            add_unpublished(
                chat_id,
                f"🆔 رسالة الطلب: {message_id}\n"
                f"🎬 المصدر: {url}\n"
                f"❌ السبب: {err[:500]}",
            )

        finally:
            for f in os.listdir(DOWNLOAD_DIR):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, f))
                except OSError:
                    pass
            print(f"[LONG][ITEM {idx}/{len(links)}] CLEANUP: اكتمل تنظيف ملفات التنزيل", flush=True)

    for chat_id, results in batch_results.items():
        print(f"[LONG][TELEGRAM][2/2] START: إرسال تقرير الدفعة إلى chat_id={chat_id}", flush=True)
        send_message(
            bot_token,
            chat_id,
            f"📊 تقرير الدفعة — تم قبول {len(results)} رابط:\n\n"
            + "\n\n".join(results),
        )
        print(f"[LONG][TELEGRAM][2/2] OK: تم إرسال التقرير إلى chat_id={chat_id}", flush=True)

    send_unpublished_reports()

    # دمج احتياطي أخير مع أحدث state/ من origin/main، حتى لو كانت هناك
    # تشغيلة موازية دفعت (push) تسجيلات نجاح جديدة أثناء تنفيذ هذا التشغيل.
    # هذا يمنع أن يعيد push الـ workflow اللاحق (rebase) الكتابة فوق
    # تسجيل نجاح حقيقي، وهو ما كان يؤدي لإعادة رفع نفس الفيديو.
    try:
        print("[LONG][STATE][3/3] START: دمج الحالة الأخيرة من origin/main", flush=True)
        merge_latest_state(STATE_DIR, BOT_NAME)
        print("[LONG][STATE][3/3] OK: اكتمل دمج الحالة", flush=True)
    except Exception as e:
        print(f"⚠️ تحذير: فشل الدمج الاحتياطي النهائي للحالة: {e}")

    print("\n🎉 انتهت معالجة سير لونق.")


if __name__ == "__main__":
    sys.exit(main())
