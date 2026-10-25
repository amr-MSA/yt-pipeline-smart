"""Shared 9:16 Shorts compositor for both publishing pipelines."""
from __future__ import annotations

import glob
import json
import os
import subprocess
from typing import Any

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYOUT_PATH = os.path.join(PROJECT_DIR, "assets", "shorts_layout.json")
DEFAULT_TEMPLATE = os.path.join(PROJECT_DIR, "assets", "template.png")
TEMPLATE_GLOB = os.path.join(PROJECT_DIR, "assets", "templates", "shorts", "*.png")
COMPOSITE_MARKER = "yt-pipeline-smart-vertical-v1"


def _load_layout() -> dict[str, Any]:
    with open(LAYOUT_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def choose_template_path() -> str:
    candidates = sorted(glob.glob(TEMPLATE_GLOB))
    if os.path.exists(DEFAULT_TEMPLATE):
        candidates.append(DEFAULT_TEMPLATE)
    candidates = sorted(set(candidates))
    if not candidates:
        raise FileNotFoundError("لم يوجد قالب PNG في assets/template.png أو assets/templates/shorts/")
    return candidates[0]


def _probe(path: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", path,
        ], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe فشل: {result.stderr[-500:]}")
    return json.loads(result.stdout)


def _duration(probe: dict[str, Any]) -> float:
    return float(probe.get("format", {}).get("duration", 0) or 0)


def _png_has_alpha(path: str) -> bool:
    """Read the PNG color type from IHDR without adding an image dependency."""
    with open(path, "rb") as handle:
        header = handle.read(26)
    return len(header) >= 26 and header[:8] == b"\x89PNG\r\n\x1a\n" and header[25] in (4, 6)


def validate_output(raw_path: str, final_path: str, tolerance: float = 0.25) -> dict[str, Any]:
    raw = _probe(raw_path)
    final = _probe(final_path)
    video = next((s for s in final.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video or (video.get("width"), video.get("height")) != (864, 1536):
        raise RuntimeError("خرج التركيب ليس 864x1536 بالضبط")
    if video.get("codec_name") != "h264":
        raise RuntimeError("خرج التركيب ليس H.264")
    if video.get("pix_fmt") != "yuv420p":
        raise RuntimeError("خرج التركيب ليس yuv420p")
    raw_duration = _duration(raw)
    final_duration = _duration(final)
    if raw_duration and abs(final_duration - raw_duration) > tolerance:
        raise RuntimeError(
            f"مدة الخرج لا تطابق المصدر: raw={raw_duration:.3f}s final={final_duration:.3f}s"
        )
    return {"raw": raw, "final": final}


def compose_vertical(raw_path: str, final_path: str, label: str = "SHORTS") -> dict[str, Any]:
    """Create one output without cropping the foreground source or duplicating audio."""
    source_probe = _probe(raw_path)
    source_video = next(
        (s for s in source_probe.get("streams", []) if s.get("codec_type") == "video"),
        {},
    )
    if source_video.get("tags", {}).get("handler_name") == COMPOSITE_MARKER:
        raise RuntimeError("المصدر سبق تركيبه؛ أوقف العملية لمنع تراكب القالب مرتين")
    layout = _load_layout()
    out = layout["output"]
    box = layout["main_video"]
    background = layout["background"]
    video = layout["video"]
    template_path = choose_template_path()
    template_has_alpha = _png_has_alpha(template_path)
    print(f"[{label}][COMPOSITOR] template={os.path.basename(template_path)}", flush=True)
    print(
        f"[{label}][COMPOSITOR] output={out['width']}x{out['height']} "
        f"main_box={box['x']},{box['y']} {box['width']}x{box['height']} "
        f"blur_sigma={background['blur_sigma']}", flush=True,
    )

    # Background: the same source, scaled-to-cover and cropped only here.
    # Foreground: scaled-to-fit, padded transparently, never cropped or stretched.
    template_layer = (
        f"[0:v]scale={out['width']}:{out['height']}:force_original_aspect_ratio=decrease,"
        f"format=rgba[template];"
    )
    if template_has_alpha:
        # Designed transparent overlays sit above the source composition.
        composition = (
            f"[bg][fg]overlay={box['x']}:{box['y']}:format=auto[composed];"
        )
        final_layer = "[composed][template]overlay=0:0:format=auto,format=yuv420p[v]"
    else:
        # Preserve compatibility with the repository's existing opaque frame.
        composition = ""
        final_layer = (
            "[template][bg]overlay=0:0:format=auto[base];"
            f"[base][fg]overlay={box['x']}:{box['y']}:format=auto,format=yuv420p[v]"
        )
    filter_graph = (
        f"[1:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={out['width']}:{out['height']}:force_original_aspect_ratio=increase,"
        f"crop={out['width']}:{out['height']},"
        f"gblur=sigma={background['blur_sigma']},"
        f"eq=brightness={background['brightness']}:saturation={background['saturation']}[bg];"
        f"[fgsrc]format=rgba,scale={box['width']}:{box['height']}:"
        f"force_original_aspect_ratio=decrease,"
        f"pad={box['width']}:{box['height']}:(ow-iw)/2:(oh-ih)/2:color=black@0[fg];"
        f"{composition}"
        f"{template_layer}{final_layer}"
    )
    cmd = [
        "ffmpeg", "-y", "-i", template_path, "-i", raw_path,
        "-filter_complex", filter_graph,
        "-map", "[v]", "-map", "1:a?",
        "-c:v", video["codec"], "-preset", video["preset"], "-crf", str(video["crf"]),
        "-c:a", video["audio_codec"], "-b:a", video["audio_bitrate"],
        "-r", source_video.get("avg_frame_rate") or source_video.get("r_frame_rate", "30"),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-metadata:s:v:0", f"handler_name={COMPOSITE_MARKER}", final_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"فشل FFmpeg: {result.stderr[-1200:]}")
    return validate_output(raw_path, final_path)
