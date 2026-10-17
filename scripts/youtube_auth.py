"""
توثيق يوتيوب داخل بيئة GitHub Actions (بدون تدخل بشري).
يعتمد على توكن مُنشأ مسبقًا محليًا (عملية لمرة واحدة) ومحفوظ كـ GitHub Secret،
ثم يجدده تلقائيًا باستخدام refresh_token عند الحاجة.
"""
import json
import os
import traceback

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


def get_youtube_client(token_json_str: str):
    """
    ياخذ محتوى youtube_token.json (كنص، من متغير بيئة/Secret)
    ويرجع (عميل YouTube API، كائن creds) جاهز. يجدد التوكن تلقائيًا إذا انتهت صلاحيته.

    ملاحظة: access_token له صلاحية قصيرة (~ساعة) ويُجدَّد تلقائيًا هنا في كل تشغيل
    باستخدام refresh_token طويل الأمد، لذلك لا حاجة لتحديث الـ GitHub Secret بشكل
    دوري. الـ Secret يحتاج تحديث فقط إذا تم إلغاء الصلاحية يدويًا من حساب Google.
    """
    print("[LONG][AUTH][1/5] START: قراءة YOUTUBE_TOKEN_JSON", flush=True)
    try:
        token_data = json.loads(token_json_str)
    except Exception as exc:
        print(f"[LONG][AUTH][1/5] FAIL: JSON غير صالح: {type(exc).__name__}: {exc}", flush=True)
        raise
    print(
        "[LONG][AUTH][1/5] OK: تم تحليل JSON "
        f"(refresh_token={'موجود' if token_data.get('refresh_token') else 'مفقود'}, "
        f"token_uri={token_data.get('token_uri', 'مفقود')})",
        flush=True,
    )

    print("[LONG][AUTH][2/5] START: إنشاء Credentials بالنطاقات المطلوبة", flush=True)
    try:
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    except Exception as exc:
        print(f"[LONG][AUTH][2/5] FAIL: {type(exc).__name__}: {exc}", flush=True)
        raise
    print(
        f"[LONG][AUTH][2/5] OK: valid={creds.valid}, expired={creds.expired}, "
        f"has_refresh_token={bool(creds.refresh_token)}",
        flush=True,
    )

    if creds.expired and creds.refresh_token:
        print("[LONG][AUTH][3/5] START: تجديد access token عبر Google", flush=True)
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            message = (
                "YOUTUBE_TOKEN_JSON يحتوي refresh_token منتهيًا أو ملغىً "
                "(invalid_grant). YT_COOKIES لا تصلح هذا الخطأ؛ "
                "أعد إنشاء youtube_token.json محليًا وحدّث GitHub Secret "
                "YOUTUBE_TOKEN_JSON كاملًا."
            )
            print(f"[LONG][AUTH][3/5] FAIL: {message}", flush=True)
            raise RuntimeError(message) from exc
        except Exception as exc:
            print(
                f"[LONG][AUTH][3/5] FAIL: فشل تجديد التوكن — "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            traceback.print_exc()
            raise
        print("[LONG][AUTH][3/5] OK: تم تجديد access token", flush=True)
    else:
        print("[LONG][AUTH][3/5] SKIP: لا حاجة لتجديد access token", flush=True)

    print("[LONG][AUTH][4/5] START: التحقق من صلاحية Credentials", flush=True)
    if not creds.valid:
        print("[LONG][AUTH][4/5] FAIL: Credentials غير صالحة بعد التجديد", flush=True)
        raise RuntimeError(
            "توكن يوتيوب غير صالح ولا يمكن تجديده. "
            "أعد تشغيل عملية التوثيق المحلية وحدّث الـ Secret."
        )
    print("[LONG][AUTH][4/5] OK: Credentials صالحة", flush=True)

    print("[LONG][AUTH][5/5] START: إنشاء عميل YouTube API", flush=True)
    try:
        youtube = build("youtube", "v3", credentials=creds)
    except Exception as exc:
        print(f"[LONG][AUTH][5/5] FAIL: {type(exc).__name__}: {exc}", flush=True)
        raise
    print("[LONG][AUTH][5/5] OK: عميل YouTube API جاهز", flush=True)
    return youtube
