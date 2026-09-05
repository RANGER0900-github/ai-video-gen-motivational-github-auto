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

    def _read_usage(self) -> dict[str, int]:
        if not self.config.images_usage_json.exists():
            return {item.name: 0 for item in self.list_images()}
        return json.loads(self.config.images_usage_json.read_text(encoding="utf-8"))

    def _write_usage(self, usage: dict[str, int]) -> None:
        temp_path = self.config.images_usage_json.with_suffix(".tmp")
        temp_path.write_text(json.dumps(usage, indent=2), encoding="utf-8")
        temp_path.replace(self.config.images_usage_json)

    def choose_image(self, requested_name: str | None = None) -> Path:
        images = {item.name: self.config.root_dir / item.path for item in self.list_images()}
        if not images:
            raise FileNotFoundError("No images available")
        if requested_name:
            if requested_name not in images:
                raise FileNotFoundError(f"Image {requested_name} not found")
            return images[requested_name]

        with self._usage_lock:
            usage = self._read_usage()
            for name in images:
                usage.setdefault(name, 0)
            lowest = min(usage[name] for name in images)
            candidates = [name for name in images if usage[name] == lowest]
            chosen = random.choice(candidates)
            usage[chosen] += 1
            self._write_usage(usage)
            return images[chosen]

    def choose_music(self, requested_name: str | None = None) -> Path:
        music = {item.name: self.config.root_dir / item.path for item in self.list_music()}
        if not music:
            raise FileNotFoundError("No music files available")
        if requested_name:
            if requested_name not in music:
                raise FileNotFoundError(f"Music {requested_name} not found")
            return music[requested_name]
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
