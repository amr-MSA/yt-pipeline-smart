import os
import json
import sys
import unittest
import tempfile
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import shorts_pipeline
from one_time_dedupe_override import consume_duplicate_override


class DirectShortsPipelineTests(unittest.TestCase):
    FACEBOOK_URL = "https://www.facebook.com/share/v/1Hw3b2rpNc"

    def write_armed_exception(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                self.FACEBOOK_URL: {
                    "canonical_url": self.FACEBOOK_URL,
                    "status": "armed",
                    "expires_at": 10000,
                    "chat_id": 6828354904,
                    "after_message_id": 156,
                }
            }, handle)

    def test_download_does_not_request_a_60_second_range(self):
        downloader = MagicMock()
        downloader.__enter__.return_value = downloader
        downloader.extract_info.return_value = {"duration": 368}
        with (
            patch.object(shorts_pipeline.yt_dlp, "YoutubeDL", return_value=downloader) as youtube_dl,
            patch.object(shorts_pipeline, "temporary_cookie_file", return_value=nullcontext(None)),
        ):
            result = shorts_pipeline.download_clip("https://example.com/video", "full.mp4")
        self.assertEqual(result["duration"], 368)
        options = youtube_dl.call_args.args[0]
        self.assertNotIn("download_ranges", options)
        self.assertNotIn("force_keyframes_at_cuts", options)

    def test_video_under_three_minutes_is_framed(self):
        with (
            patch.object(shorts_pipeline, "downloaded_duration", return_value=179.9),
            patch.object(shorts_pipeline, "apply_template") as apply_template,
        ):
            upload_file, duration = shorts_pipeline.prepare_upload_file("raw.mp4", "frame.mp4")
        self.assertEqual(upload_file, "frame.mp4")
        self.assertEqual(duration, 179.9)
        apply_template.assert_called_once_with("raw.mp4", "frame.mp4")

    def test_video_at_or_over_three_minutes_is_uploaded_unframed(self):
        for duration in (180, 368):
            with self.subTest(duration=duration):
                with (
                    patch.object(shorts_pipeline, "downloaded_duration", return_value=duration),
                    patch.object(shorts_pipeline, "apply_template") as apply_template,
                ):
                    upload_file, actual_duration = shorts_pipeline.prepare_upload_file(
                        "raw.mp4", "frame.mp4"
                    )
                self.assertEqual(upload_file, "raw.mp4")
                self.assertEqual(actual_duration, duration)
                apply_template.assert_not_called()

    def test_duration_report_uses_hours_when_needed(self):
        self.assertEqual(shorts_pipeline.format_duration(368), "6:08")
        self.assertEqual(shorts_pipeline.format_duration(3600), "1:00:00")

    def test_exact_duplicate_can_bypass_once_for_one_fresh_request_only(self):
        with tempfile.TemporaryDirectory() as state_dir:
            path = os.path.join(state_dir, "shorts_dedupe_exceptions.json")
            self.write_armed_exception(path)
            published = {self.FACEBOOK_URL: {"video_id": "oa4shvar6ds"}}
            ledger = {
                "6828354904:156": {
                    "status": "succeeded",
                    "source_url": self.FACEBOOK_URL + "/",
                    "video_id": "oa4shvar6ds",
                }
            }

            previous, allowed = shorts_pipeline.direct_source_duplicate_decision(
                canonical_url=self.FACEBOOK_URL,
                request_key="6828354904:157",
                chat_id=6828354904,
                message_id=157,
                published_urls=published,
                ledger=ledger,
                exceptions_path=path,
                now=1000,
            )
            self.assertIsNotNone(previous)
            self.assertTrue(allowed)
            self.assertTrue(consume_duplicate_override(
                path,
                canonical_url=self.FACEBOOK_URL,
                request_key="6828354904:157",
                now=1001,
            ))

            previous, allowed = shorts_pipeline.direct_source_duplicate_decision(
                canonical_url=self.FACEBOOK_URL,
                request_key="6828354904:158",
                chat_id=6828354904,
                message_id=158,
                published_urls=published,
                ledger=ledger,
                exceptions_path=path,
                now=1002,
            )
            self.assertIsNotNone(previous)
            self.assertFalse(allowed)

    def test_old_message_or_other_chat_cannot_claim_the_exception(self):
        with tempfile.TemporaryDirectory() as state_dir:
            path = os.path.join(state_dir, "shorts_dedupe_exceptions.json")
            self.write_armed_exception(path)
            published = {self.FACEBOOK_URL: {"video_id": "oa4shvar6ds"}}

            for chat_id, message_id, request_key in (
                (6828354904, 156, "6828354904:156"),
                (999, 157, "999:157"),
            ):
                with self.subTest(chat_id=chat_id, message_id=message_id):
                    previous, allowed = shorts_pipeline.direct_source_duplicate_decision(
                        canonical_url=self.FACEBOOK_URL,
                        request_key=request_key,
                        chat_id=chat_id,
                        message_id=message_id,
                        published_urls=published,
                        ledger={},
                        exceptions_path=path,
                        now=1000,
                    )
                    self.assertIsNotNone(previous)
                    self.assertFalse(allowed)

            previous, allowed = shorts_pipeline.direct_source_duplicate_decision(
                canonical_url=self.FACEBOOK_URL,
                request_key="6828354904:157",
                chat_id=6828354904,
                message_id=157,
                published_urls=published,
                ledger={},
                exceptions_path=path,
                now=1000,
            )
            self.assertIsNotNone(previous)
            self.assertTrue(allowed)

    def test_other_published_links_remain_blocked_and_leave_exception_armed(self):
        other_url = "https://www.facebook.com/share/v/other123"
        with tempfile.TemporaryDirectory() as state_dir:
            path = os.path.join(state_dir, "shorts_dedupe_exceptions.json")
            self.write_armed_exception(path)
            previous, allowed = shorts_pipeline.direct_source_duplicate_decision(
                canonical_url=other_url,
                request_key="6828354904:157",
                chat_id=6828354904,
                message_id=157,
                published_urls={other_url: {"video_id": "other-video"}},
                ledger={},
                exceptions_path=path,
                now=1000,
            )
            self.assertEqual(previous["video_id"], "other-video")
            self.assertFalse(allowed)
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved[self.FACEBOOK_URL]["status"], "armed")

    def test_expired_armed_exception_does_not_bypass_duplicate_guard(self):
        with tempfile.TemporaryDirectory() as state_dir:
            path = os.path.join(state_dir, "shorts_dedupe_exceptions.json")
            self.write_armed_exception(path)
            previous, allowed = shorts_pipeline.direct_source_duplicate_decision(
                canonical_url=self.FACEBOOK_URL,
                request_key="6828354904:157",
                chat_id=6828354904,
                message_id=157,
                published_urls={self.FACEBOOK_URL: {"video_id": "oa4shvar6ds"}},
                ledger={},
                exceptions_path=path,
                now=10000,
            )
            self.assertIsNotNone(previous)
            self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
