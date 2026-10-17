# Smart Channel — Shorts + Long-Smart

هذا المستودع مستقل للقناة الثانية، ويحتوي على مسارين فقط:

- `Shorts Pipeline`: استقبال روابط Shorts، قصها ووضعها داخل `assets/template.png` ثم نشرها.
- `Long-Smart Pipeline`: استقبال `/long <url>`، تحليل المصدر عبر Gemini، إنشاء طوابير المقاطع، ثم نشر المقاطع بالقالب الذكي.

## الأسرار

يجب إنشاء أسرار مستقلة لهذه القناة، حتى لو بقيت الأسماء نفسها داخل الـWorkflows:

- `SHORTS_BOT_TOKEN`
- `YOUTUBE_TOKEN_JSON` للقناة الثانية فقط
- `YT_COOKIES` للحساب المستخدم في القناة الثانية
- `GEMINI_API_KEY`

## الحالة

بدأ المستودع بحالة نظيفة عمدًا. لا تُنسخ طوابير القناة الحالية أو ملفات offsets إليها.

## التشغيل

الـWorkflows الفعالة هي:

- `.github/workflows/shorts_pipeline.yml`
- `.github/workflows/long_smart_pipeline.yml`

تم نقل Workflow Long العادي إلى `.github/workflows-disabled/` لأنه يخص المستودع الرئيسي.

## القالب

القالب الحالي لهذه القناة هو `assets/template.png`، ويمكن استبداله دون تغيير أسرار القناة الرئيسية.

## القوالب المتعددة

ضع ثيمات Shorts الإضافية كملفات PNG بمقاس 720×1280 داخل `assets/templates/shorts/`.
سيختار Shorts وLong-Smart قالبًا عشوائيًا في كل معالجة، مع استخدام `assets/template.png` كـfallback.
لمنع اختلاف موضع الفيديو بين الثيمات، يجب أن تستخدم كل القوالب نفس فتحة الفيديو الحالية.
