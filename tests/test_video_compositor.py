import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import video_compositor

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECT_LAYOUT = os.path.join(PROJECT_DIR, "assets", "shorts_layout_direct.json")
LONG_LAYOUT = os.path.join(PROJECT_DIR, "assets", "shorts_layout.json")


class VideoCompositorTests(unittest.TestCase):
    def test_direct_short_and_long_pipelines_select_separate_layouts(self):
        import shorts_pipeline
        import long_smart_pipeline

        with patch.object(shorts_pipeline, "compose_vertical") as compose_short:
            self.assertTrue(shorts_pipeline.apply_template("raw.mp4", "direct.mp4"))
        self.assertEqual(
            compose_short.call_args.kwargs["layout_path"],
            os.path.join(PROJECT_DIR, "assets", "shorts_layout_direct.json"),
        )

        with patch.object(long_smart_pipeline, "compose_vertical") as compose_long:
            self.assertTrue(long_smart_pipeline._apply_shorts_template("raw.mp4", "long.mp4"))
        self.assertNotIn("layout_path", compose_long.call_args.kwargs)

    def test_direct_layout_expands_foreground_without_changing_long_or_output(self):
        with open(LONG_LAYOUT, encoding="utf-8") as handle:
            long_layout = json.load(handle)
        with open(DIRECT_LAYOUT, encoding="utf-8") as handle:
            direct_layout = json.load(handle)

        self.assertEqual(long_layout["output"], direct_layout["output"])
        self.assertEqual(
            long_layout["main_video"],
            {"x": 32, "y": 430, "width": 800, "height": 450},
        )
        self.assertEqual(
            direct_layout["main_video"],
            {"x": 32, "y": 405, "width": 800, "height": 500},
        )
        self.assertEqual(long_layout["background"]["blur_sigma"], 25)
        self.assertEqual(direct_layout["background"]["blur_sigma"], 18)
        self.assertGreater(
            direct_layout["main_video"]["height"], long_layout["main_video"]["height"]
        )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_real_render_preserves_portrait_aspect_and_uses_direct_background_blur(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_video = os.path.join(temp_dir, "raw.mp4")
            final_video = os.path.join(temp_dir, "final.mp4")

            # A bright green portrait source is distinguishable from the
            # dimmed, blurred copy used as its background.
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i",
                    "color=c=lime:s=360x640:r=5:d=0.4", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", raw_video,
                ],
                capture_output=True,
                check=True,
            )

            real_run = subprocess.run
            ffmpeg_commands = []

            def record_ffmpeg(command, *args, **kwargs):
                if command and command[0] == "ffmpeg":
                    ffmpeg_commands.append(command)
                return real_run(command, *args, **kwargs)

            with patch.object(video_compositor.subprocess, "run", side_effect=record_ffmpeg):
                result = video_compositor.compose_vertical(
                    raw_video,
                    final_video,
                    label="TEST-DIRECT",
                    layout_path=DIRECT_LAYOUT,
                )

            video_stream = next(
                stream for stream in result["final"]["streams"]
                if stream.get("codec_type") == "video"
            )
            self.assertEqual((video_stream["width"], video_stream["height"]), (864, 1536))
            filter_graph = ffmpeg_commands[0][ffmpeg_commands[0].index("-filter_complex") + 1]
            self.assertIn("scale=800:500:force_original_aspect_ratio=decrease", filter_graph)
            self.assertIn("pad=800:500:(ow-iw)/2:(oh-ih)/2", filter_graph)
            self.assertIn("gblur=sigma=18", filter_graph)
            self.assertIn("force_original_aspect_ratio=increase", filter_graph)
            self.assertIn("crop=864:1536", filter_graph)

            frame = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-i", final_video, "-frames:v", "1",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                ],
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(len(frame), 864 * 1536 * 3)
            green_points = []
            for y in range(380, 930):
                for x in range(230, 630):
                    offset = (y * 864 + x) * 3
                    red, green, blue = frame[offset:offset + 3]
                    if red < 40 and green > 220 and blue < 40:
                        green_points.append((x, y))

            self.assertTrue(green_points, "The fitted foreground should remain visible")
            bounds = (
                min(x for x, _ in green_points),
                min(y for _, y in green_points),
                max(x for x, _ in green_points),
                max(y for _, y in green_points),
            )
            foreground_width = bounds[2] - bounds[0] + 1
            foreground_height = bounds[3] - bounds[1] + 1
            self.assertAlmostEqual(foreground_width / foreground_height, 360 / 640, delta=0.02)
            self.assertAlmostEqual(foreground_height, 500, delta=4)


if __name__ == "__main__":
    unittest.main()
