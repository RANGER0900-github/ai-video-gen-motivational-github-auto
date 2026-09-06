from __future__ import annotations

import asyncio
import logging
import random
import subprocess
import tempfile
import textwrap
import threading
from pathlib import Path
from typing import Callable

import edge_tts
from faster_whisper import WhisperModel
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from .config import AppConfig

logger = logging.getLogger(__name__)

if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.Resampling.LANCZOS


class RenderCancelled(Exception):
    pass


_whisper_cache: dict[str, WhisperModel] = {}


def get_whisper_model(model_size: str = "base.en") -> WhisperModel:
    if model_size not in _whisper_cache:
        _whisper_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _whisper_cache[model_size]


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
    sx, sy = shadow_offset
    draw.text((x + sx, y + sy), text, font=font, fill=shadow_fill, stroke_width=stroke_width, stroke_fill=shadow_fill)
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)


def fit_image_to_frame(image_path: Path, width: int, height: int, darken: float = 0.78) -> Image.Image:
    with Image.open(image_path) as source:
        img = ImageOps.exif_transpose(source).convert("RGB")
    fitted = ImageOps.fit(img, (width, height), method=Image.Resampling.LANCZOS)
    darken_factor = max(0.2, min(darken if darken is not None else 0.78, 1.0))
    if darken_factor < 0.999:
        enhancer = ImageEnhance.Brightness(fitted)
        fitted = enhancer.enhance(darken_factor)
    return fitted


def make_text_overlay(
    quote: str,
    width: int,
    height: int,
    *,
    author: str | None = None,
    quote_font_file: str | None = None,
    author_font_file: str | None = None,
) -> Image.Image:
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    lines = balance_wrap(quote.strip(), width=22)
    font_size = 62
    quote_font = load_font(quote_font_file, font_size)
    line_metrics = [text_size(draw, line, quote_font, stroke_width=2) for line in lines]
    line_height = max(h for _, h in line_metrics) if line_metrics else 60
    line_spacing = int(line_height * 0.28)
    total_quote_height = sum(h for _, h in line_metrics) + (line_spacing * (len(lines) - 1))
    author_text = f"— {author.strip().upper()}" if author and author.strip() else ""
    author_font = load_font(author_font_file, int(font_size * 0.58))
    author_w, author_h = text_size(draw, author_text, author_font) if author_text else (0, 0)
    gap = int(line_height * 0.65) if author_text else 0
    total_height = total_quote_height + gap + author_h
    start_y = max(80, int((height - total_height) * 0.44))
    shadow_offset = (3, 4)
    current_y = start_y
    for line in lines:
        w, h = text_size(draw, line, quote_font, stroke_width=2)
        x = max(40, (width - w) // 2)
        draw_text_with_shadow(
            draw,
            (x, current_y),
            line,
            font=quote_font,
            fill=(255, 255, 255, 255),
            shadow_fill=(0, 0, 0, 210),
            shadow_offset=shadow_offset,
            stroke_width=2,
            stroke_fill=(0, 0, 0, 220),
        )
        current_y += h + line_spacing
    if author_text:
        x = max(40, (width - author_w) // 2)
        y = current_y + gap
        draw_text_with_shadow(
            draw,
            (x, y),
            author_text,
            font=author_font,
            fill=(210, 215, 220, 240),
            shadow_fill=(0, 0, 0, 180),
            shadow_offset=(shadow_offset[0], shadow_offset[1] + 1),
        )
    return canvas


def format_ass_time(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        centis = 99
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def clean_word(word: str) -> str:
    return word.strip().upper().replace("{", "").replace("}", "")


def generate_speech_sync(text: str, voice: str, pitch: str, rate: str, out_path: Path) -> None:
    async def _communicate():
        c = edge_tts.Communicate(text, voice, pitch=pitch, rate=rate)
        await c.save(str(out_path))

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, _communicate()).result()
        else:
            loop.run_until_complete(_communicate())
    except RuntimeError:
        asyncio.run(_communicate())


def extract_whisper_words(audio_path: Path, whisper_model: WhisperModel) -> list[dict]:
    segments, _ = whisper_model.transcribe(str(audio_path), word_timestamps=True)
    all_words = []
    for seg in segments:
        for w in seg.words:
            w_text = clean_word(w.word)
            if w_text:
                all_words.append({
                    "word": w_text,
                    "start": max(0.0, w.start),
                    "end": max(w.start + 0.08, w.end)
                })
    return all_words


def build_ass_subtitles(
    words: list[dict],
    author: str,
    ass_path: Path,
    mode: str = "spotlight",
    active_color: str = "&H0000E5FF",
    inactive_color: str = "&H00FFFFFF",
    muted_color: str = "&H00A5A5A5",
    font_size: int = 62,
    margin_v: int = 720,
    total_duration: float = 10.0,
) -> None:
    quote_text = " ".join([w["word"] for w in words])
    wrapped_lines = balance_wrap(quote_text, width=20)
    words_per_line = [line.split() for line in wrapped_lines]
    author_str = author.strip().upper() if author and author.strip() else ""

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Quote,Noto Sans,{font_size},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,5,3,5,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [header]
    total_words = len(words)

    # 1. Pre-speech intro: display full quote un-highlighted
    if words and words[0]["start"] > 0.1:
        pre_parts = []
        for line_words in words_per_line:
            pre_line = [f"{{\\c{muted_color if mode == 'cumulative' else inactive_color}&}}{w}" for w in line_words]
            pre_parts.append(" ".join(pre_line))
        full_pre_text = "\\N".join(pre_parts)
        if author_str:
            full_pre_text += f"\\N\\N{{\\fs40\\c&H00D0D0D0&}}— {author_str}"
        events.append(f"Dialogue: 0,0:00:00.00,{format_ass_time(words[0]['start'])},Quote,,0,0,0,,{full_pre_text}\n")

    # 2. Spoken word highlighting
    for i, active_w in enumerate(words):
        w_start = active_w["start"]
        w_end = words[i + 1]["start"] if i + 1 < total_words else active_w["end"] + 0.3

        current_word_idx = 0
        dialogue_lines = []
        for line_words in words_per_line:
            line_parts = []
            for w in line_words:
                if mode == "spotlight":
                    if current_word_idx == i:
                        line_parts.append(f"{{\\c{active_color}&\\fscx106\\fscy106}}{w}{{\\fscx100\\fscy100}}")
                    else:
                        line_parts.append(f"{{\\c{inactive_color}&}}{w}")
                elif mode == "cumulative":
                    if current_word_idx <= i:
                        line_parts.append(f"{{\\c{active_color}&}}{w}")
                    else:
                        line_parts.append(f"{{\\c{muted_color}&}}{w}")
                current_word_idx += 1
            dialogue_lines.append(" ".join(line_parts))

        full_text = "\\N".join(dialogue_lines)
        if author_str:
            full_text += f"\\N\\N{{\\fs40\\c&H00D0D0D0&}}— {author_str}"
        events.append(f"Dialogue: 0,{format_ass_time(w_start)},{format_ass_time(w_end)},Quote,,0,0,0,,{full_text}\n")

    # 3. Post-speech outro: hold on screen until the very end of the video
    if words:
        post_start = words[-1]["end"] + 0.3
        post_end = max(total_duration, post_start + 1.0)
        dialogue_lines = []
        for line_words in words_per_line:
            line_parts = []
            for w in line_words:
                color = active_color if mode == "cumulative" else inactive_color
                line_parts.append(f"{{\\c{color}&}}{w}")
            dialogue_lines.append(" ".join(line_parts))
        full_post_text = "\\N".join(dialogue_lines)
        if author_str:
            full_post_text += f"\\N\\N{{\\fs40\\c&H00D0D0D0&}}— {author_str}"
        events.append(f"Dialogue: 0,{format_ass_time(post_start)},{format_ass_time(post_end)},Quote,,0,0,0,,{full_post_text}\n")

    with open(ass_path, "w", encoding="utf-8") as f:
        f.writelines(events)


def render_video(
    config: AppConfig,
    media_path: Path | None = None,
    music_path: Path | None = None,
    quote: str = "",
    author: str | None = None,
    outname: str = "output.mp4",
    darken: float | None = None,
    quote_font_file: str | None = None,
    author_font_file: str | None = None,
    progress_callback: Callable[[str, float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    media_type: str | None = None,
    watermark_path: Path | None = None,
    image_path: Path | None = None,
) -> Path:
    cb = progress_callback or (lambda phase, prog, msg: None)
    target_media = media_path or image_path
    if not target_media or not target_media.exists():
        raise FileNotFoundError(f"Media file not found: {target_media}")

    is_video = (media_type == "video") or (target_media.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"})
    chosen_music = music_path or (config.music_dir / "me and devil.mp3")
    if not chosen_music.exists():
        available = list(config.music_dir.glob("*.mp3"))
        if available:
            chosen_music = available[0]

    wm_path = watermark_path or config.watermark_path
    has_watermark = wm_path is not None and wm_path.exists()

    cb("preparing", 0.15, "Generating Brian neural voiceover")
    config.outputs_dir.mkdir(parents=True, exist_ok=True)
    outpath = config.outputs_dir / outname

    try:
        with tempfile.TemporaryDirectory(prefix="render_", dir=str(config.state_dir)) as tmpdir:
            tmpdir_path = Path(tmpdir)
            voice_path = tmpdir_path / "voice.mp3"
            ass_path = tmpdir_path / "subtitles.ass"

            # 1. Edge-TTS voice generation
            generate_speech_sync(
                text=quote,
                voice=config.voice_model,
                pitch=config.voice_pitch,
                rate=config.voice_rate,
                out_path=voice_path,
            )

            if cancel_event is not None and cancel_event.is_set():
                raise RenderCancelled("Render cancelled")

            # 2. Whisper word-level timestamps
            cb("preparing", 0.35, "Extracting word timestamps with Whisper")
            whisper_model = get_whisper_model(config.whisper_model_size)
            words = extract_whisper_words(voice_path, whisper_model)

            speech_end = words[-1]["end"] if words else 5.0

            # 3. Determine duration
            if is_video:
                duration = 10.0
            else:
                duration = min(config.max_duration, max(8.0, speech_end + 3.5))

            # 4. Generate ASS subtitles (random choice between spotlight and cumulative)
            subtitle_mode = random.choice(["spotlight", "cumulative"])
            cb("preparing", 0.50, f"Generating {subtitle_mode} animated subtitles")
            build_ass_subtitles(
                words=words,
                author=author or "",
                ass_path=ass_path,
                mode=subtitle_mode,
                total_duration=duration,
            )

            # 5. Build FFmpeg command
            cb("rendering", 0.55, "Compositing video and audio with FFmpeg")
            fade_start = max(0.5, duration - 1.0)

            command = [
                "ffmpeg", "-y",
                "-hide_banner",
                "-loglevel", "error",
                "-progress", "pipe:1",
            ]

            if is_video:
                command.extend(["-stream_loop", "-1", "-i", str(target_media)])
                command.extend(["-i", str(voice_path)])
                command.extend(["-stream_loop", "-1", "-i", str(chosen_music)])
                if has_watermark:
                    command.extend(["-i", str(wm_path)])
                    filter_graph = (
                        f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                        f"colorchannelmixer=aa=1:rr=0.88:gg=0.88:bb=0.88[bg];"
                        f"[3:v]scale=130:130[wm];"
                        f"[bg][wm]overlay=835:1675[bg_wm];"
                        f"[bg_wm]subtitles={ass_path}:fontsdir={config.fonts_dir}[v];"
                        f"[0:a]volume={config.video_sfx_volume:.2f}[vidsnd];"
                        f"[1:a]volume={config.voice_volume:.2f}[voice];"
                        f"[2:a]volume={config.music_volume:.2f}[bgm];"
                        f"[voice][bgm][vidsnd]amix=inputs=3:duration=longest:dropout_transition=2:normalize=0,"
                        f"afade=t=out:st={fade_start:.2f}:d=1.0[a]"
                    )
                else:
                    filter_graph = (
                        f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                        f"colorchannelmixer=aa=1:rr=0.88:gg=0.88:bb=0.88[bg];"
                        f"[bg]subtitles={ass_path}:fontsdir={config.fonts_dir}[v];"
                        f"[0:a]volume={config.video_sfx_volume:.2f}[vidsnd];"
                        f"[1:a]volume={config.voice_volume:.2f}[voice];"
                        f"[2:a]volume={config.music_volume:.2f}[bgm];"
                        f"[voice][bgm][vidsnd]amix=inputs=3:duration=longest:dropout_transition=2:normalize=0,"
                        f"afade=t=out:st={fade_start:.2f}:d=1.0[a]"
                    )
            else:
                darken_val = darken if darken is not None else config.default_darken
                darken_val = max(0.2, min(darken_val, 1.0))
                command.extend(["-loop", "1", "-i", str(target_media)])
                command.extend(["-i", str(voice_path)])
                command.extend(["-stream_loop", "-1", "-i", str(chosen_music)])
                if has_watermark:
                    command.extend(["-i", str(wm_path)])
                    filter_graph = (
                        f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                        f"colorchannelmixer=aa=1:rr={darken_val:.2f}:gg={darken_val:.2f}:bb={darken_val:.2f}[bg];"
                        f"[3:v]scale=130:130[wm];"
                        f"[bg][wm]overlay=835:1675[bg_wm];"
                        f"[bg_wm]subtitles={ass_path}:fontsdir={config.fonts_dir}[v];"
                        f"[1:a]volume={config.voice_volume:.2f}[voice];"
                        f"[2:a]volume={config.music_volume:.2f}[bgm];"
                        f"[voice][bgm]amix=inputs=2:duration=longest:dropout_transition=2:normalize=0,"
                        f"afade=t=out:st={fade_start:.2f}:d=1.0[a]"
                    )
                else:
                    filter_graph = (
                        f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                        f"colorchannelmixer=aa=1:rr={darken_val:.2f}:gg={darken_val:.2f}:bb={darken_val:.2f}[bg];"
                        f"[bg]subtitles={ass_path}:fontsdir={config.fonts_dir}[v];"
                        f"[1:a]volume={config.voice_volume:.2f}[voice];"
                        f"[2:a]volume={config.music_volume:.2f}[bgm];"
                        f"[voice][bgm]amix=inputs=2:duration=longest:dropout_transition=2:normalize=0,"
                        f"afade=t=out:st={fade_start:.2f}:d=1.0[a]"
                    )

            command.extend([
                "-filter_complex", filter_graph,
                "-map", "[v]",
                "-map", "[a]",
                "-t", str(duration),
                "-c:v", "libx264",
                "-preset", config.encoder_preset,
                "-crf", config.crf,
                "-pix_fmt", "yuv420p",
                "-r", str(config.fps),
                "-threads", str(config.encoder_threads),
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                str(outpath),
            ])

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            current_prog = 0.55
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
                        next_progress = 0.55 + (0.38 * ratio)
                        if next_progress - current_prog >= 0.01:
                            current_prog = next_progress
                            cb("rendering", current_prog, "Encoding video with FFmpeg")
                    elif key == "progress" and value == "end":
                        cb("finalizing", 0.94, "Finalizing output")
                stderr_output = process.stderr.read() if process.stderr is not None else ""
                return_code = process.wait()
            finally:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            if return_code != 0:
                raise RuntimeError(stderr_output.strip() or f"ffmpeg exited with code {return_code}")

        cb("finalizing", 1.0, "Video render completed")
        return outpath
    except Exception:
        raise
