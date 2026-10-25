"""
الطبقة الثانية من خط long-smart: إرسال الترانسكريبت إلى Gemini API
واستخراج مقاطع مقترحة بصيغة JSON.

يُستخدم REST API مباشرة (بدون SDK إضافي) لتفادي زيادة اعتماديات المشروع.
"""
import json
import re
import time

import requests

# أحدث نموذج Flash مستقر ومتاح ضمن الطبقة المجانية وفق توثيق Google.
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={api_key}"
)

REQUIRED_CLIPS = 8
# حدود النوعين (بالثواني). المقطع الذي مدته أقل من 180 ثانية = shorts، وما يساويها أو يزيد = long.
SHORTS_MIN_SECONDS = 20
SHORTS_MAX_SECONDS = 180
LONG_MIN_SECONDS = 180
LONG_MAX_SECONDS = 900  # ربع ساعة كحد أقصى لأي مقطع
# هامش صغير قبل بداية الجملة وبعد نهايتها حتى لا يُقطع الكلام.
BOUNDARY_MARGIN_SECONDS = 0.3
SNAP_TOLERANCE_SECONDS = 1.5

SYSTEM_INSTRUCTIONS = """\
أنت محرر متخصص في اقتراح مقاطع من ترانسكريبت فيديو يوتيوب لإعادة نشرها.
يصلك الترانسكريبت كأسطر بالشكل: [بداية-نهاية] النص، والأرقام بالثواني.

نوعان من المقاطع، ولكل نوع قواعده:
1) shorts: مدة المقطع بين 20 و180 ثانية، لا أقل ولا أكثر. فكرة أو قصة أو معلومة واحدة
   لها بداية ونهاية واضحتان.
2) long: مدة المقطع بين 180 و900 ثانية (ثلاث دقائق إلى ربع ساعة كحد أقصى). موضوع
   متصل له مقدمة وسياق وخلاصة. أي مقطع تتجاوز مدته 900 ثانية مرفوض.

قواعد الموضوع المكتمل (صارمة):
- لا تقترح مقطعًا إلا إذا كان موضوعًا مكتملًا ببداية ونهاية واضحتين.
- المقطع يبدأ بجملة تقدم الفكرة، ولا يبدأ من منتصف كلام، وينتهي بعد اكتمال الفكرة وخلاصتها مباشرة.
- إن كان الموضوع يعتمد على سياق سابق لا يظهر في المقطع، أو ينقطع قبل خلاصته، فارفضه.
- لا تقترح مقطعًا يكون مضمونه تحية أو كلامًا عامًا أو إعلانًا أو تنبيهًا أو فاصلًا.

قواعد التوقيت (صارمة):
- أعطِ start وend بالثواني بدقة من أرقام الترانسكريبت المعطى فقط. لا تخترع أرقامًا ولا
  تقدّر توقيتات غير موجودة في النص.
- يجب أن يبدأ المقطع عند بداية جملة كاملة وينتهي عند نهاية جملة كاملة.
- لا تتداخل المقاطع مع بعضها بأي شكل، ولا تكرر نفس النطاق الزمني.
- end أكبر من start دائمًا، وتلتزم مدة كل مقطع بحدود نوعه.

الكمية والترتيب:
- أعد 8 مقاطع على الأقل إن وُجدت موضوعات مكتملة كافية تحقق كل القواعد أعلاه.
- إن لم توجد، أعد المتاح فقط، ولا تخترع مقاطع لتكملة العدد.
- رتّب المقاطع حسب قوة الموضوع وقابليته للمشاهدة.

صيغة الإخراج: JSON فقط، دون أي نص تمهيدي أو Markdown أو أسوار ```، بهذا الشكل تمامًا:
{
  "clips": [
    {
      "type": "shorts" أو "long",
      "start": رقم بالثواني,
      "end": رقم بالثواني,
      "title": "عنوان جذاب مناسب ليوتيوب",
      "description": "وصف مختصر للمقطع"
    }
  ]
}
"""


def _build_transcript_text(segments: list[dict]) -> str:
    lines = [f"[{s['start']}-{s['end']}] {s['text']}" for s in segments]
    return "\n".join(lines)


def _extract_json(raw_text: str) -> dict:
    """يستخرج ويحلل JSON من رد النموذج، حتى لو أضاف أسوار Markdown أو نص إضافي."""
    text = raw_text.strip()
    text = re.sub(r"^```(json)?", "", text.strip(), flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text.strip()).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise RuntimeError(f"رد جيمناي ليس JSON صالحًا:\n{raw_text[:500]}")
        return json.loads(match.group(0))


def _wait_for_min_interval() -> None:
    """ينتظر حتى يمر الفاصل الأدنى منذ آخر طلب إلى Gemini."""
    global _last_request_at
    if _last_request_at:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < GEMINI_MIN_INTERVAL_SECONDS:
            time.sleep(GEMINI_MIN_INTERVAL_SECONDS - elapsed)
    _last_request_at = time.monotonic()


def _snap_to_segments(start: float, end: float, segments: list[dict]) -> tuple[float, float]:
    """يحاذي البداية والنهاية إلى حدود أقرب مقطع نصي من الترانسكريبت، مع هامش صغير.

    البداية تُحاذى إلى أقرب بداية نص ضمن حد التسامح، والنهاية إلى أقرب نهاية نص ضمن
    حد التسامح، فلا يُقطع الكلام في المنتصف. وإن لم يوجد حد قريب يُبقى التوقيت كما هو.
    """
    if not segments:
        return start, end
    starts = [float(s["start"]) for s in segments]
    ends = [float(s["end"]) for s in segments]
    near_start = [t for t in starts if abs(t - start) <= SNAP_TOLERANCE_SECONDS]
    snapped_start = min(near_start, key=lambda t: abs(t - start)) if near_start else start
    near_end = [t for t in ends if abs(t - end) <= SNAP_TOLERANCE_SECONDS]
    snapped_end = min(near_end, key=lambda t: abs(t - end)) if near_end else end
    return (
        max(0.0, snapped_start - BOUNDARY_MARGIN_SECONDS),
        snapped_end + BOUNDARY_MARGIN_SECONDS,
    )


def suggest_clips(api_key: str, video_title: str, segments: list[dict]) -> list[dict]:
    """
    يرسل الترانسكريبت لجيمناي ويرجع قائمة من 8 قواميس أو أقل إن لم تتوفر موضوعات مكتملة:
    [{"type": "shorts"|"long", "start": float, "end": float,
      "title": str, "description": str}, ...]
    """
    transcript_text = _build_transcript_text(segments)

    prompt = (
        f"عنوان الفيديو الأصلي: {video_title}\n\n"
        f"الترانسكريبت:\n{transcript_text}\n\n"
        "أعطني ما لا يقل عن 8 مقاطع مقترحة إن أمكن، وإلا فأعد المتاح فقط، "
        "بصيغة JSON فقط كما هو محدد في التعليمات."
    )

    # ملاحظة: لا نرسل temperature أو top_p أو top_k أو candidate_count، ولا thinking_level،
    # فإعداداتنا تقتصر على نوع الرد JSON وفق توثيق Gemini 3.x.
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTIONS}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }

    url = GEMINI_URL.format(model=GEMINI_MODEL, api_key=api_key)
    # 429 حد المعدل، و500 و503 ضغط الخدمة: تُعاد المحاولة بتأخير متزايد حتى 5 مرات.
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        _wait_for_min_interval()
        resp = requests.post(url, json=body, timeout=180)
        if resp.status_code not in (429, 500, 503):
            break
        if attempt < max_attempts:
            delay = 60 * attempt
            print(f"⚠️ جيمناي أرجع {resp.status_code} (محاولة {attempt}/{max_attempts})، إعادة المحاولة بعد {delay} ثانية.")
            time.sleep(delay)
    resp.raise_for_status()
    data = resp.json()

    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"رد جيمناي غير متوقع: {data}") from e

    parsed = _extract_json(raw_text)
    clips = parsed.get("clips", [])

    if not clips:
        raise RuntimeError("جيمناي لم يُرجع أي مقطع — لا يمكن المتابعة.")
    if len(clips) < REQUIRED_CLIPS:
        print(f"ℹ️ جيمناي أرجع {len(clips)} مقطعًا فقط (أقل من {REQUIRED_CLIPS})، سيُحفظ المتاح.")

    validated = []
    for c in clips:
        start = float(c.get("start", 0))
        end = float(c.get("end", start + SHORTS_MIN_SECONDS))
        if end <= start:
            print(f"⚠️ مقطع مرفوض: النهاية ليست بعد البداية ({start}-{end}).")
            continue
        start, end = _snap_to_segments(start, end, segments)
        duration = end - start
        if duration < SHORTS_MIN_SECONDS or duration > LONG_MAX_SECONDS:
            print(f"⚠️ مقطع مرفوض: مدته {duration:.1f} ثانية خارج الحدود المسموحة.")
            continue
        # التصنيف يعتمد على المدة بعد المحاذاة لا على اقتراح Gemini للنوع.
        clip_type = "shorts" if duration < LONG_MIN_SECONDS else "long"
        validated.append(
            {
                "type": clip_type,
                "start": round(start, 2),
                "end": round(end, 2),
                "title": (c.get("title") or "").strip() or "بدون عنوان",
                "description": (c.get("description") or "").strip(),
            }
        )

    return validated
# فاصل أدنى بين طلبين متتاليين لتفادي حد المعدل في الطبقة المجانية.
GEMINI_MIN_INTERVAL_SECONDS = 20
_last_request_at = 0.0
