from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from .config import AppConfig
from .models import AssetItem, VideoItem


class AssetStore:
    def __init__(self, config: AppConfig):
        self.config = config
        self._usage_lock = Lock()

    def _iter_assets(self, folder: Path, suffixes: set[str], base_url: str) -> list[AssetItem]:
        items = []
        for path in sorted(folder.iterdir() if folder.exists() else []):
            if path.is_file() and path.suffix.lower() in suffixes:
                rel = path.relative_to(self.config.root_dir).as_posix()
                items.append(AssetItem(name=path.name, path=rel, url=f"{base_url}/{path.name}"))
        return items

    def list_images(self) -> list[AssetItem]:
        return self._iter_assets(self.config.images_dir, {".jpg", ".jpeg", ".png", ".webp"}, "/assets/images")

    def list_background_videos(self) -> list[AssetItem]:
        return self._iter_assets(self.config.videos_dir, {".mp4", ".mov", ".mkv", ".webm"}, "/assets/videos")

    def list_music(self) -> list[AssetItem]:
        return self._iter_assets(self.config.music_dir, {".mp3", ".wav", ".m4a", ".aac", ".ogg"}, "/assets/music")

    def list_fonts(self) -> list[AssetItem]:
        return self._iter_assets(self.config.fonts_dir, {".ttf", ".otf"}, "/assets/fonts")

    def list_videos(self) -> list[VideoItem]:
        items: list[VideoItem] = []
        for path in sorted(self.config.outputs_dir.glob("*.mp4"), key=lambda candidate: candidate.stat().st_mtime, reverse=True):
            rel = path.relative_to(self.config.root_dir).as_posix()
            created_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
            items.append(VideoItem(name=path.name, path=rel, url=f"/assets/outputs/{path.name}", created_at=created_at))
        return items

    def _read_media_usage(self) -> dict:
        usage = {
            "schedule_counter": 0,
            "videos": {},
            "images": {}
        }
        if self.config.media_usage_json.exists():
            try:
                content = self.config.media_usage_json.read_text(encoding="utf-8").strip()
                if content:
                    loaded = json.loads(content)
                    if isinstance(loaded, dict):
                        usage.update(loaded)
            except Exception:
                pass
        elif self.config.images_usage_json.exists():
            try:
                content = self.config.images_usage_json.read_text(encoding="utf-8").strip()
                if content:
                    legacy_img = json.loads(content)
                    if isinstance(legacy_img, dict):
                        usage["images"] = legacy_img
            except Exception:
                pass
        return usage

    def _write_media_usage(self, usage: dict) -> None:
        temp_path = self.config.media_usage_json.with_suffix(".tmp")
        temp_path.write_text(json.dumps(usage, indent=2), encoding="utf-8")
        temp_path.replace(self.config.media_usage_json)
        try:
            legacy_path = self.config.images_usage_json.with_suffix(".tmp")
            legacy_path.write_text(json.dumps(usage.get("images", {}), indent=2), encoding="utf-8")
            legacy_path.replace(self.config.images_usage_json)
        except Exception:
            pass

    def _pick_least_used(self, items_dict: dict[str, Path], usage_map: dict[str, int]) -> Path:
        for name in items_dict:
            usage_map.setdefault(name, 0)
        lowest = min(usage_map[name] for name in items_dict)
        candidates = [name for name in items_dict if usage_map[name] == lowest]
        chosen = random.choice(candidates)
        usage_map[chosen] += 1
        return items_dict[chosen]

    def choose_background_media(
        self,
        media_type: str | None = None,
        requested_name: str | None = None
    ) -> tuple[Path, str]:
        """
        Chooses background media according to the 5:1 daily ratio schedule.
        Returns: (media_path, "video" | "image")
        Schedule: Out of 6 daily runs, 5 are videos (cycle 0..4) and 1 is an image (cycle 5).
        """
        videos = {item.name: self.config.root_dir / item.path for item in self.list_background_videos()}
        images = {item.name: self.config.root_dir / item.path for item in self.list_images()}

        with self._usage_lock:
            usage = self._read_media_usage()
            schedule_counter = usage.get("schedule_counter", 0)

            target_type = media_type
            if not target_type or target_type == "auto":
                if (schedule_counter % 6) == 5:
                    target_type = "image"
                else:
                    target_type = "video"
                usage["schedule_counter"] = schedule_counter + 1

            if target_type == "video":
                if not videos:
                    target_type = "image"
                elif requested_name:
                    if requested_name in videos:
                        self._write_media_usage(usage)
                        return videos[requested_name], "video"
                    raise FileNotFoundError(f"Video {requested_name} not found")
                else:
                    chosen = self._pick_least_used(videos, usage.setdefault("videos", {}))
                    self._write_media_usage(usage)
                    return chosen, "video"

            if not images:
                raise FileNotFoundError("No images available")
            if requested_name:
                if requested_name in images:
                    self._write_media_usage(usage)
                    return images[requested_name], "image"
                raise FileNotFoundError(f"Image {requested_name} not found")

            chosen = self._pick_least_used(images, usage.setdefault("images", {}))
            self._write_media_usage(usage)
            return chosen, "image"

    def choose_image(self, requested_name: str | None = None) -> Path:
        path, _ = self.choose_background_media(media_type="image", requested_name=requested_name)
        return path

    def choose_video(self, requested_name: str | None = None) -> Path:
        path, _ = self.choose_background_media(media_type="video", requested_name=requested_name)
        return path

    def choose_music(self, requested_name: str | None = None) -> Path:
        music = {item.name: self.config.root_dir / item.path for item in self.list_music()}
        if not music:
            raise FileNotFoundError("No music files available")
        if requested_name:
            if requested_name not in music:
                raise FileNotFoundError(f"Music {requested_name} not found")
            return music[requested_name]
        for preferred in ["me and devil.mp3", "me and devil.wav"]:
            if preferred in music:
                return music[preferred]
        return random.choice(list(music.values()))

    def _preferred_system_font(self, candidates: list[str]) -> str | None:
        for candidate in candidates:
            path = Path(candidate)
            if path.exists():
                return str(path)
        return None

    def _preferred_project_font(self, candidates: list[str]) -> str | None:
        for candidate in candidates:
            path = self.config.fonts_dir / candidate
            if path.exists():
                return str(path)
        return None

    def default_quote_font(self) -> str | None:
        bundled = self._preferred_project_font([
            "NotoSans-Bold.ttf",
            "NotoSans-Regular.ttf",
            "PlayfairDisplay-VariableFont_wght.ttf",
        ])
        if bundled:
            return bundled
        preferred = self._preferred_system_font([
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ])
        if preferred:
            return preferred
        fonts = self.list_fonts()
        if not fonts:
            return None
        return str(self.config.root_dir / fonts[0].path)

    def default_author_font(self) -> str | None:
        bundled = self._preferred_project_font([
            "NotoSans-Regular.ttf",
            "NotoSans-Bold.ttf",
            "PlayfairDisplay-VariableFont_wght.ttf",
        ])
        if bundled:
            return bundled
        preferred = self._preferred_system_font([
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ])
        if preferred:
            return preferred
        return self.default_quote_font()
