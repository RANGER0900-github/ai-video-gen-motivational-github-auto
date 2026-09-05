from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable
import textwrap

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from .config import AppConfig

logger = logging.getLogger(__name__)

if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.Resampling.LANCZOS


class RenderCancelled(Exception):
    pass


def load_font(font_file: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(font_file, size) if font_file else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


def balance_wrap(text: str, width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    best_lines: list[str] | None = None
    best_score: float | None = None
    for candidate_width in range(max(12, width - 6), width + 7):
        lines = textwrap.wrap(text, width=candidate_width, break_long_words=False, break_on_hyphens=False)
        if not lines:
            continue
        lengths = [len(line.strip()) for line in lines]
        if len(lengths) == 1:
            score = lengths[0]
        else:
            mean = sum(lengths) / len(lengths)
            variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
            shortest_penalty = max(0, int(mean * 0.55) - min(lengths)) * 10
            last_line_penalty = max(0, int(mean * 0.65) - lengths[-1]) * 12
            score = variance + shortest_penalty + last_line_penalty + (len(lines) * 2)
        if best_score is None or score < best_score:
            best_score = score
            best_lines = lines
    return best_lines or [text]


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, stroke_width: int = 0) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_text_with_shadow(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    *,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
    shadow_fill: tuple[int, int, int, int],
    shadow_offset: tuple[int, int],
    stroke_width: int = 0,
    stroke_fill: tuple[int, int, int, int] | None = None,
) -> None:
    x, y = position
    offset_x, offset_y = shadow_offset
    draw.text(
        (x + offset_x, y + offset_y),
        text,
        font=font,
        fill=shadow_fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )
    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )


def fit_image_to_frame(image_path: Path, width: int, height: int, darken: float) -> Image.Image:
    with Image.open(image_path).convert("RGB") as image:
        fitted = ImageOps.fit(image, (width, height), method=Image.LANCZOS)
    if darken < 0.999:
        fitted = ImageEnhance.Brightness(fitted).enhance(darken)
    return fitted


def make_text_overlay(
    quote: str,
    width: int,
    height: int,
    author: str | None = None,
    quote_font_file: str | None = None,
    author_font_file: str | None = None,
) -> Image.Image:
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    max_font = int(width * 0.069)
    min_font = int(width * 0.03)
    font_size = max_font
    quote_max_width = width * 0.7
    quote_max_height = height * 0.31
    author_text = f"— {author.strip()}" if author and author.strip() else ""

    while font_size >= min_font:
        quote_font = load_font(quote_font_file, int(font_size))
        author_font = load_font(author_font_file or quote_font_file, max(int(font_size * 0.58), 32))
        lines = balance_wrap(quote, width=max(11, int(19 * (width / 1080))))
        max_w = 0
        total_h = 0
        for line in lines:
            line_w, line_h = text_size(draw, line, quote_font, stroke_width=1)
            max_w = max(max_w, line_w)
            total_h += line_h
        total_h += int((len(lines) - 1) * font_size * 0.13)
        if author_text:
            author_w, author_h = text_size(draw, author_text, author_font, stroke_width=0)
            max_w = max(max_w, author_w)
            total_h += int(font_size * 0.52) + author_h
        if max_w <= quote_max_width and total_h <= quote_max_height:
            break
        font_size -= 6

    quote_font = load_font(quote_font_file, int(font_size))
    author_font = load_font(author_font_file or quote_font_file, max(int(font_size * 0.58), 32))
    lines = balance_wrap(quote, width=max(11, int(19 * (width / 1080))))
    line_gap = int(font_size * 0.11)
    author_gap = int(font_size * 0.5)
    shadow_offset = (max(2, int(font_size * 0.018)), max(3, int(font_size * 0.028)))
    quote_metrics = [text_size(draw, line, quote_font, stroke_width=1) for line in lines]
    quote_height = sum(height_value for _, height_value in quote_metrics) + line_gap * max(len(lines) - 1, 0)
    author_size = text_size(draw, author_text, author_font, stroke_width=0) if author_text else (0, 0)
    total_h = quote_height + (author_gap + author_size[1] if author_text else 0)
    y = (height - total_h) // 2
    for line, (line_w, line_h) in zip(lines, quote_metrics, strict=False):
        x = (width - line_w) // 2
        draw_text_with_shadow(
            draw,
            (x, y),
            line,
            font=quote_font,
            fill=(247, 247, 244, 255),
            shadow_fill=(6, 8, 11, 126),
            shadow_offset=shadow_offset,
            stroke_width=1,
            stroke_fill=(14, 18, 22, 112),
        )
        y += line_h + line_gap

    if author_text:
        y += author_gap
        author_w, _ = author_size
        x = (width - author_w) // 2
        draw_text_with_shadow(
            draw,
            (x, y),
            author_text,
            font=author_font,
            fill=(230, 236, 239, 232),
            shadow_fill=(6, 8, 11, 86),
            shadow_offset=(shadow_offset[0], shadow_offset[1] + 1),
        )
    return canvas


def render_video(
    config: AppConfig,
    image_path: Path,
    music_path: Path,
    quote: str,
    author: str | None,
    outname: str,
    darken: float,
    quote_font_file: str | None,
    author_font_file: str | None,
    progress_callback: Callable[[str, float, str], None],
    cancel_event: threading.Event | None = None,
) -> Path:
    def probe_duration(media_path: Path) -> float:
        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        value = result.stdout.strip()
        if not value:
            raise RuntimeError(f"Could not determine duration for {media_path.name}")
        return float(value)

    def build_filtergraph(duration: float, fade_in: float) -> str:
        safe_fade = min(fade_in, max(duration - 0.05, 0.1))
        background_chain = (
            f"[0:v]fps={config.fps},trim=duration={duration:.3f},setpts=PTS-STARTPTS,format=yuv420p,"
            f"fade=t=in:st=0:d={safe_fade:.3f}[bg]"
        )
        overlay_chain = (
            f"[1:v]fps={config.fps},trim=duration={duration:.3f},setpts=PTS-STARTPTS,format=rgba"
        )
        if config.text_fade > 0:
            overlay_chain += f",fade=t=in:st=0:d={min(config.text_fade, duration):.3f}:alpha=1"
        overlay_chain += "[txt]"
        audio_chain = (
            f"[2:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={safe_fade:.3f}[a]"
        )
        return ";".join((background_chain, overlay_chain, audio_chain, "[bg][txt]overlay=0:0:format=auto[v]"))

    bg_pil = fit_image_to_frame(image_path, config.width, config.height, darken)
    overlay_pil = make_text_overlay(
        quote,
        config.width,
        config.height,
        author=author,
        quote_font_file=quote_font_file,
        author_font_file=author_font_file,
    )

    progress_callback("rendering", 0.35, "Preparing audio and timeline")
    audio_dur = probe_duration(music_path)
    duration = min(audio_dur, config.max_duration)
    if duration <= 0:
        raise RuntimeError(f"Audio track {music_path.name} has invalid duration")
    fade_in = max(0.12, min(duration / 6.0, 1.1))
    config.outputs_dir.mkdir(parents=True, exist_ok=True)
    outpath = config.outputs_dir / outname

    try:
        with tempfile.TemporaryDirectory(prefix="render_", dir=str(config.state_dir)) as tmpdir:
            tmpdir_path = Path(tmpdir)
            bg_path = tmpdir_path / "background.png"
            overlay_path = tmpdir_path / "overlay.png"
            bg_pil.save(bg_path, format="PNG", optimize=True)
            overlay_pil.save(overlay_path, format="PNG", optimize=True)

            progress_callback("rendering", 0.52, "Building FFmpeg render graph")
            command = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-loop",
                "1",
                "-framerate",
                str(config.fps),
                "-i",
                str(bg_path),
                "-loop",
                "1",
                "-framerate",
                str(config.fps),
                "-i",
                str(overlay_path),
                "-i",
                str(music_path),
                "-filter_complex",
                build_filtergraph(duration, fade_in),
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                config.encoder_preset,
                "-crf",
                config.crf,
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(config.fps),
                "-threads",
                str(config.encoder_threads),
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                "-shortest",
                str(outpath),
            ]

            progress_callback("rendering", 0.66, "Encoding video with FFmpeg")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            progress = 0.66
            try:
                for raw_line in process.stdout:
                    if cancel_event is not None and cancel_event.is_set():
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise RenderCancelled("Render cancelled")
                    line = raw_line.strip()
                    if not line or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key == "out_time_ms":
                        try:
                            encoded_seconds = max(0.0, int(value) / 1_000_000.0)
                        except ValueError:
                            continue
                        ratio = min(1.0, encoded_seconds / max(duration, 0.001))
                        next_progress = 0.66 + (0.28 * ratio)
                        if next_progress - progress >= 0.01:
                            progress = next_progress
                            progress_callback("rendering", progress, "Encoding video with FFmpeg")
                    elif key == "progress" and value == "end":
                        progress_callback("finalizing", 0.94, "Finalizing output")
                stderr_output = process.stderr.read() if process.stderr is not None else ""
                return_code = process.wait()
            finally:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            if return_code != 0:
                raise RuntimeError(stderr_output.strip() or f"ffmpeg exited with code {return_code}")

        progress_callback("finalizing", 0.98, "Finalizing output")
        return outpath
    finally:
        bg_pil.close()
        overlay_pil.close()
