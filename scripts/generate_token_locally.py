"""
شغّل هذا الملف مرة واحدة فقط، على جهازك (وليس داخل GitHub Actions).
الهدف: عمل توثيق يوتيوب التفاعلي عبر المتصفح، وإخراج ملف youtube_token.json.
محتوى هذا الملف هو ما تضعه في GitHub Secret باسم YOUTUBE_TOKEN_JSON.

طريقة الاستخدام:
1. نزّل client_secret.json من Google Cloud Console (OAuth Client - Desktop App).
2. ضعه بجانب هذا الملف بنفس الاسم "client_secret.json".
3. شغّل: python generate_token_locally.py
4. افتح الرابط الذي يظهر، سجّل دخول ووافق.
5. سيُنشأ ملف youtube_token.json — انسخ محتواه كاملاً إلى GitHub Secret.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

def main():
    flow = InstalledAppFlow.from_client_secrets_file(
        "client_secret.json", scopes=SCOPES
    )
    creds = flow.run_local_server(port=0)

    with open("youtube_token.json", "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print("\n✅ تم إنشاء youtube_token.json بنجاح.")
    print("➡️ افتح الملف وانسخ محتواه بالكامل إلى GitHub Secret باسم: YOUTUBE_TOKEN_JSON")


if __name__ == "__main__":
    main()
