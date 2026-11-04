import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fixed_meta
import gemini_utils
import long_smart_pipeline as pipeline
from youtube_dedupe import append_request_marker


class FakeGeminiResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "candidates": [{
                "content": {"parts": [{
                    "text": json.dumps({
                        "clips": [{
                            "type": "shorts",
                            "start": 0,
                            "end": 30,
                            "title": "عنوان تجريبي",
                            "description": "وصف تجريبي",
                            "hashtags": [
                                "#سياسة", "#خبر", "#History", "#خبر", "هاشتاق تجريبي"
                            ],
                            "keywords": [
                                "كلمة مفتاحية", "YouTube SEO", "youtube seo"
                            ],
                        }]
                    }, ensure_ascii=False),
                }]},
            }],
        }


class GeminiMetadataTests(unittest.TestCase):
    def test_gemini_clip_response_includes_normalized_hashtags_and_keywords(self):
        segments = [{"start": 0, "end": 30, "text": "نص تجريبي"}]
        with patch.object(gemini_utils.requests, "post", return_value=FakeGeminiResponse()) as post, \
                patch.object(gemini_utils, "_wait_for_min_interval"):
            clips = gemini_utils.suggest_clips("test-key", "عنوان المصدر", segments)

        self.assertEqual(len(clips), 1)
        self.assertEqual(
            clips[0]["hashtags"],
            ["#سياسة", "#خبر", "#History", "#هاشتاق_تجريبي"],
        )
        self.assertEqual(clips[0]["keywords"], ["كلمة مفتاحية", "YouTube SEO"])
        request_body = post.call_args.kwargs["json"]
        self.assertIn('"hashtags"', request_body["system_instruction"]["parts"][0]["text"])
        self.assertIn('"keywords"', request_body["system_instruction"]["parts"][0]["text"])

    def test_title_keeps_three_hashtags_and_stays_within_youtube_limit(self):
        with patch.object(fixed_meta, "_read_fixed", return_value=""):
            title = fixed_meta.build_final_title(
                "عنوان طويل " * 30,
                ["#أول", "#ثان", "#ثالث", "#رابع"],
                include_shorts=True,
            )

        self.assertLessEqual(len(title), fixed_meta.YOUTUBE_TITLE_MAX)
        self.assertTrue(title.endswith("#أول #ثان #ثالث #Shorts"))
        self.assertNotIn("#رابع", title)

    def test_remaining_hashtags_stay_before_request_marker_at_description_limit(self):
        description = append_request_marker(
            "وصف" * 2000,
            "#2-clip-1",
            trailing_metadata="#رابع #خامس",
        )

        self.assertLessEqual(len(description), 4900)
        self.assertTrue(description.endswith("#رابع #خامس\n\n#2-clip-1"))

    def test_upload_maps_keywords_to_youtube_snippet_tags(self):
        class FakeVideos:
            body = None

            def insert(self, *, part, body, media_body):
                self.body = body
                return SimpleNamespace(execute=lambda: {"id": "fixture-id"})

        class FakeYoutube:
            def __init__(self):
                self.video_api = FakeVideos()

            def videos(self):
                return self.video_api

        youtube = FakeYoutube()
        with patch.object(pipeline, "MediaFileUpload", return_value="fake-media"):
            video_id = pipeline._upload(
                youtube,
                "fixture.mp4",
                "عنوان",
                "وصف",
                keywords=["كلمة أولى", "keyword two", "keyword two"],
            )

        self.assertEqual(video_id, "fixture-id")
        self.assertEqual(
            youtube.video_api.body["snippet"]["tags"],
            ["كلمة أولى", "keyword two"],
        )
        self.assertLessEqual(
            len(", ".join(pipeline._youtube_tags(["x" * 100] * 6))),
            500,
        )


if __name__ == "__main__":
    unittest.main()
