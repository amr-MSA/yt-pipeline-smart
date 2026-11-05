import os
import sys
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import shorts_pipeline


class DirectShortsPipelineTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
