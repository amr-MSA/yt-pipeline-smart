import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import video_compositor

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECT_LAYOUT = os.path.join(PROJECT_DIR, "assets", "shorts_layout_direct.json")
LONG_LAYOUT = os.path.join(PROJECT_DIR, "assets", "shorts_layout.json")


def _write_transparent_png(path):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    image = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as handle:
        handle.write(image)


def _frame_rgb(frame, width, x, y, radius=4):
    samples = []
    for py in range(y - radius, y + radius):
        for px in range(x - radius, x + radius):
            offset = (py * width + px) * 3
            samples.append(frame[offset:offset + 3])
    count = len(samples)
    return tuple(sum(pixel[channel] for pixel in samples) / count for channel in range(3))


def _green_bounds(frame, width, height):
    points = []
    for y in range(height):
        for x in range(width):
            offset = (y * width + x) * 3
            red, green, blue = frame[offset:offset + 3]
            if red < 60 and green > 220 and blue < 60:
                points.append((x, y))
    if not points:
        return None
    return (
        min(x for x, _ in points),
        min(y for _, y in points),
        max(x for x, _ in points),
        max(y for _, y in points),
    )


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
            {"x": 92, "y": 243, "width": 680, "height": 1130},
        )
        self.assertAlmostEqual(
            direct_layout["main_video"]["width"] / direct_layout["output"]["width"],
            0.787,
            places=3,
        )
        self.assertEqual(long_layout["background"]["blur_sigma"], 25)
        self.assertNotIn("zoom", long_layout["background"])
        self.assertEqual(direct_layout["background"]["blur_sigma"], 24)
        self.assertEqual(direct_layout["background"]["zoom"], 1.08)
        self.assertGreater(
            direct_layout["main_video"]["height"], long_layout["main_video"]["height"]
        )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_real_render_preserves_full_foreground_and_keeps_long_layout_distinct(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_video = os.path.join(temp_dir, "raw.mp4")
            transparent_template = os.path.join(temp_dir, "transparent.png")
            direct_video = os.path.join(temp_dir, "direct.mp4")
            long_video = os.path.join(temp_dir, "long.mp4")
            _write_transparent_png(transparent_template)

            # The green source has a differently colored block in each corner.
            # Seeing all four blocks after rendering proves the foreground was
            # scaled to fit, rather than cropped, while the green bounds measure its size.
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i",
                    "color=c=lime:s=360x640:r=5:d=0.6", "-vf",
                    "drawbox=x=0:y=0:w=32:h=32:color=red:t=fill,"
                    "drawbox=x=328:y=0:w=32:h=32:color=blue:t=fill,"
                    "drawbox=x=0:y=608:w=32:h=32:color=yellow:t=fill,"
                    "drawbox=x=328:y=608:w=32:h=32:color=magenta:t=fill",
                    "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p", raw_video,
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

            with patch.object(video_compositor, "choose_template_path", return_value=transparent_template):
                with patch.object(video_compositor.subprocess, "run", side_effect=record_ffmpeg):
                    direct_result = video_compositor.compose_vertical(
                        raw_video,
                        direct_video,
                        label="TEST-DIRECT",
                        layout_path=DIRECT_LAYOUT,
                    )
                    long_result = video_compositor.compose_vertical(
                        raw_video,
                        long_video,
                        label="TEST-LONG",
                        layout_path=LONG_LAYOUT,
                    )

            self.assertEqual(len(ffmpeg_commands), 2)
            direct_graph = ffmpeg_commands[0][ffmpeg_commands[0].index("-filter_complex") + 1]
            long_graph = ffmpeg_commands[1][ffmpeg_commands[1].index("-filter_complex") + 1]
            self.assertIn("scale=680:1130:force_original_aspect_ratio=decrease", direct_graph)
            self.assertIn("pad=680:1130:(ow-iw)/2:(oh-ih)/2", direct_graph)
            self.assertIn("scale=934:1660:force_original_aspect_ratio=increase", direct_graph)
            self.assertIn("gblur=sigma=24", direct_graph)
            self.assertIn("scale=800:450:force_original_aspect_ratio=decrease", long_graph)
            self.assertIn("scale=864:1536:force_original_aspect_ratio=increase", long_graph)
            self.assertIn("gblur=sigma=25", long_graph)

            for result in (direct_result, long_result):
                video_stream = next(
                    stream for stream in result["final"]["streams"]
                    if stream.get("codec_type") == "video"
                )
                self.assertEqual((video_stream["width"], video_stream["height"]), (864, 1536))

            def first_frame(path):
                return subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-i", path, "-frames:v", "1",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                    ],
                    capture_output=True,
                    check=True,
                ).stdout

            direct_frame = first_frame(direct_video)
            self.assertEqual(len(direct_frame), 864 * 1536 * 3)
            direct_bounds = _green_bounds(direct_frame, 864, 1536)
            self.assertIsNotNone(direct_bounds, "The fitted foreground should remain visible")
            direct_width = direct_bounds[2] - direct_bounds[0] + 1
            direct_height = direct_bounds[3] - direct_bounds[1] + 1
            self.assertAlmostEqual(direct_width, 636, delta=8)
            self.assertAlmostEqual(direct_height, 1129, delta=10)
            self.assertAlmostEqual(direct_width / direct_height, 360 / 640, delta=0.03)

            scale = min(680 / 360, 1130 / 640)
            marker_centers = {
                "red": (92 + round(16 * scale), 243 + round(16 * scale)),
                "blue": (92 + round(344 * scale), 243 + round(16 * scale)),
                "yellow": (92 + round(16 * scale), 243 + round(624 * scale)),
                "magenta": (92 + round(344 * scale), 243 + round(624 * scale)),
            }
            thresholds = {
                "red": lambda r, g, b: r > 150 and g < 90 and b < 90,
                "blue": lambda r, g, b: r < 90 and g < 90 and b > 120,
                "yellow": lambda r, g, b: r > 150 and g > 150 and b < 100,
                "magenta": lambda r, g, b: r > 150 and g < 100 and b > 150,
            }
            for marker, (x, y) in marker_centers.items():
                with self.subTest(marker=marker):
                    self.assertTrue(
                        thresholds[marker](*_frame_rgb(direct_frame, 864, x, y)),
                        f"The {marker} source-corner marker should remain visible after fitting",
                    )

            long_frame = first_frame(long_video)
            long_bounds = _green_bounds(long_frame, 864, 1536)
            self.assertIsNotNone(long_bounds, "The /long foreground should remain visible")
            long_width = long_bounds[2] - long_bounds[0] + 1
            long_height = long_bounds[3] - long_bounds[1] + 1
            self.assertAlmostEqual(long_width / long_height, 360 / 640, delta=0.03)
            self.assertAlmostEqual(long_height, 450, delta=8)
            self.assertGreater(direct_width, long_width * 2.45)


if __name__ == "__main__":
    unittest.main()
