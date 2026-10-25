"""
الطبقة الثانية من خط long-smart: إرسال الترانسكريبت إلى Gemini API
واستخراج 8 مقاطع مقترحة بصيغة JSON.

يُستخدم REST API مباشرة (بدون SDK إضافي) لتفادي زيادة اعتماديات المشروع.
"""
import json
import re

import requests

# أحدث نموذج Flash مستقر ومتاح ضمن الطبقة المجانية وفق توثيق Google.
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={api_key}"
)

REQUIRED_CLIPS = 8
SHORTS_MAX_SECONDS = 180
MAX_CLIP_SECONDS = 900  # ربع ساعة كحد أقصى لأي مقطع

SYSTEM_INSTRUCTIONS = """\
أنت مساعد متخصص في تحليل ترانسكريبت فيديوهات يوتيوب واقتراح أفضل المقاطع
لإعادة نشرها كفيديوهات (long) أو كشورتس (shorts).

سيصلك الترانسكريبت كقائمة أجزاء نصية، كل جزء له وقت بداية ونهاية بالثواني.

مطلوب منك إرجاع 8 مقاطع مقترحة على الأقل إن وُجدت موضوعات مكتملة كافية في
الترانسكريبت، بصيغة JSON فقط ولا شيء غيره
(بدون أي نص تمهيدي، بدون Markdown، بدون ```), على الشكل التالي تمامًا:

{
  "clips": [
    {
      "type": "long" أو "shorts",
      "start": رقم بالثواني,
      "end": رقم بالثواني,
      "title": "عنوان جذاب مناسب ليوتيوب",
      "description": "وصف مناسب للفيديو"
    }
  ]
}

قواعد مهمة:
- عدد العناصر في "clips": 8 على الأقل إن أمكن، ولا تخترع مقاطع لتكملة العدد.
  إن لم يكن في النص ما يكفي من موضوعات مكتملة، أعد المتاح فقط.
- أي مقطع تقل مدته عن 180 ثانية يُعامل تلقائيًا بوصفه "shorts".
- المقاطع التي مدتها 180 ثانية أو أكثر تُعامل بوصفها "long".
- مدة أي مقطع لا تتجاوز 900 ثانية (ربع ساعة). المقطع الطويل مقبول إذا كان موضوعًا
  مكتملًا له بداية وسياق وخلاصة، حتى لو بلغت مدته ربع ساعة.
- اختر لحظات ذات قيمة عالية: نقاط تشويق، معلومة مفاجئة، لحظة عاطفية، أو خلاصة قوية.
- اختر موضوعًا كاملًا ومترابطًا له بداية وسياق وخلاصة قدر الإمكان، ولا تبدأ
  المقطع من منتصف فكرة ولا تنهيه قبل اكتمالها.
- رتّب الاختيارات حسب قوة الموضوع وقابليته للمشاهدة، وليس لتغطية أجزاء عشوائية.
- لا تحاول توزيع الأنواع بالتساوي؛ اختر نوع كل مقطع حسب طبيعة الفكرة والزمن المتاح.
- لا تهدر الخانات على مقدمات عامة أو مقاطع ضعيفة أو متشابهة؛ كل مقطع يجب أن يقدم
  قيمة مستقلة ومثيرة للمشاهد.
- لا تكرر نفس النطاق الزمني في أكثر من مقطع.
- التزم فقط بصيغة JSON المذكورة أعلاه دون أي إضافات.
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


def suggest_clips(api_key: str, video_title: str, segments: list[dict]) -> list[dict]:
    """
    يرسل الترانسكريبت لجيمناي ويرجع قائمة من 8 قواميس:
    [{"type": "long"|"shorts", "start": float, "end": float,
      "title": str, "description": str}, ...]
    """
    transcript_text = _build_transcript_text(segments)

    prompt = (
        f"عنوان الفيديو الأصلي: {video_title}\n\n"
        f"الترانسكريبت:\n{transcript_text}\n\n"
        "أعطني الآن ما لا يقل عن 8 مقاطع مقترحة إن أمكن، وإلا فأعد المتاح فقط، "
        "بصيغة JSON فقط كما هو محدد في التعليمات."
    )

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTIONS}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }

    url = GEMINI_URL.format(model=GEMINI_MODEL, api_key=api_key)
    resp = requests.post(url, json=body, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"رد جيمناي غير متوقع: {data}") from e

    parsed = _extract_json(raw_text)
    clips = parsed.get("clips", [])

    if not clips:
        raise RuntimeError(
            "جيمناي لم يُرجع أي مقطع صالح — لا يمكن المتابعة."
        )
    if len(clips) < REQUIRED_CLIPS:
        print(f"ℹ️ جيمناي أرجع {len(clips)} مقطعًا فقط (أقل من {REQUIRED_CLIPS})، سيُحفظ المتاح.")

    # نأخذ أول 8 بالضبط (لو رجع أكثر بالخطأ)
    # إن أعاد Gemini أكثر من 8 مقاطع نُبقيها كلها، ويقسّمها المستدعي
    # إلى ملفات Queue كل ملف فيه 8 مقاطع كحد أقصى.

    # فحص أساسي لصحة كل عنصر
    validated = []
    for c in clips:
        start = float(c.get("start", 0))
        end = float(c.get("end", start + 30))
        if end <= start:
            end = start + 30
        if end - start > MAX_CLIP_SECONDS:
            end = start + MAX_CLIP_SECONDS
        # التصنيف النهائي يعتمد على المدة لا على اقتراح Gemini لنوع المقطع:
        # كل مقطع أقل من 3 دقائق يدخل مسار Shorts العمودي، حتى لو أعاده
        # Gemini بالخطأ على أنه long. والمقطع الذي يبلغ 180 ثانية أو أكثر
        # يبقى long حتى لو أعاده Gemini على أنه shorts.
        clip_type = "shorts" if (end - start) < SHORTS_MAX_SECONDS else "long"
        validated.append(
            {
                "type": clip_type,
                "start": start,
                "end": end,
                "title": (c.get("title") or "").strip() or "بدون عنوان",
                "description": (c.get("description") or "").strip(),
            }
        )

    return validated
