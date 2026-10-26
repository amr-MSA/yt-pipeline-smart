import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from short_title import select_short_title


class ShortTitleTests(unittest.TestCase):
    def test_uses_original_social_platform_title(self):
        cases = [
            ({"title": "Instagram caption"}, "https://www.instagram.com/reel/abc123/"),
            ({"title": "TikTok caption"}, "https://www.tiktok.com/@user/video/123"),
            ({"title": "Facebook caption"}, "https://www.facebook.com/share/v/abc/"),
        ]
        for info, url in cases:
            with self.subTest(url=url):
                self.assertEqual(select_short_title(info, url), info["title"])

    def test_uses_fulltitle_if_title_is_blank(self):
        self.assertEqual(
            select_short_title(
                {"title": "  ", "fulltitle": "Original full title"},
                "https://www.instagram.com/reel/abc123/",
            ),
            "Original full title",
        )

    def test_uses_fallback_when_social_title_is_missing_or_placeholder(self):
        cases = [
            ({}, "https://www.instagram.com/reel/abc123/"),
            ({"title": "  "}, "https://www.tiktok.com/@user/video/123"),
            ({"id": "abc123", "title": "abc123"}, "https://www.facebook.com/watch/abc123"),
            ({"title": "Facebook video"}, "https://fb.watch/abc123/"),
            ({"extractor_key": "TikTok", "title": "video"}, "https://example.com/watch/123"),
        ]
        for info, url in cases:
            with self.subTest(info=info, url=url):
                self.assertEqual(select_short_title(info, url), "مقطع اليوم")

    def test_preserves_existing_default_for_non_social_sources(self):
        self.assertEqual(select_short_title({}, "https://youtu.be/abc123"), "شورت جديد")

    def test_explicit_inline_title_takes_precedence(self):
        self.assertEqual(
            select_short_title(
                {"title": "Original source title"},
                "https://www.instagram.com/reel/abc123/",
                {"title_mode": "custom", "title_override": " عنوان مخصص "},
            ),
            "عنوان مخصص",
        )


if __name__ == "__main__":
    unittest.main()
