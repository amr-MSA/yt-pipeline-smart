import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from short_title import combine_original_text, select_short_title


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

    def test_uses_fulltitle_when_title_is_blank_or_generated(self):
        self.assertEqual(
            select_short_title(
                {"title": "Video by el_mulhem", "fulltitle": "Original full title"},
                "https://www.instagram.com/reel/abc123/",
            ),
            "Original full title",
        )

    def test_rejects_generated_generic_url_and_id_titles(self):
        cases = [
            "Video by el_mulhem", "Reel by account", "Photo by user", "Facebook video",
            "video", "https://example.com/watch/123", "abc123456",
        ]
        for title in cases:
            with self.subTest(title=title):
                self.assertEqual(select_short_title({"id": "abc123456", "title": title}, "https://example.com/watch/123"), "مقطع اليوم")

    def test_universal_fallback_and_no_title_invented_from_description(self):
        self.assertEqual(select_short_title({"description": "A descriptive sentence"}, "https://youtu.be/abc123"), "مقطع اليوم")
        self.assertEqual(select_short_title({}, "https://example.com/watch/123"), "مقطع اليوم")

    def test_custom_override_does_not_supersede_original_or_default(self):
        item = {"title_mode": "custom", "title_override": " عنوان مخصص "}
        self.assertEqual(
            select_short_title(
                {"title": "Original source title"},
                "https://example.com/watch/123", item,
            ),
            "Original source title",
        )
        self.assertEqual(select_short_title({}, "https://example.com/watch/123", item), "مقطع اليوم")

    def test_caps_selected_title_but_preserves_full_original_metadata(self):
        long_title = "أ" * 130
        info = {"title": long_title, "description": "first line\nsecond line"}
        self.assertEqual(len(select_short_title(info, "https://example.com/v")), 100)
        combined = combine_original_text(info)
        self.assertIn(long_title, combined)
        self.assertIn("first line\nsecond line", combined)

    def test_description_combiner_keeps_poor_title_and_deduplicates_contained_text(self):
        info = {
            "title": "Video by el_mulhem",
            "fulltitle": "Video by el_mulhem — fuller title",
            "description": "Video by el_mulhem — fuller title\noriginal second line",
        }
        combined = combine_original_text(info)
        self.assertEqual(combined, "Video by el_mulhem — fuller title\noriginal second line")
        self.assertEqual(combined.count("Video by el_mulhem"), 1)


if __name__ == "__main__":
    unittest.main()
