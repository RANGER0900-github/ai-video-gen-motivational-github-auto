import subprocess
import sys
from pathlib import Path
from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import load_config
from app.csv_store import QuoteStore
from app.database import Database
from app.models import CreateJobRequest
from app.renderer import build_ass_subtitles
from app.storage import AssetStore


def test_nodejs_scripts_syntax():
    """Verify that all Node.js files compile without syntax errors."""
    for script_name in ["upload.js", "scripts/get_youtube_token.js"]:
        script_path = ROOT / script_name
        assert script_path.exists(), f"Missing script: {script_name}"
        res = subprocess.run(["node", "-c", str(script_path)], capture_output=True, text=True)
        assert res.returncode == 0, f"Syntax error in {script_name}: {res.stderr}"


def test_python_modules_compile():
    """Verify that all Python modules compile without syntax errors."""
    py_files = list((ROOT / "backend" / "app").glob("*.py"))
    assert py_files, "No python files found in backend/app"
    for py_file in py_files:
        res = subprocess.run([sys.executable, "-m", "py_compile", str(py_file)], capture_output=True, text=True)
        assert res.returncode == 0, f"Compilation error in {py_file}: {res.stderr}"


def test_assets_exist():
    """Verify essential assets are present, including watermark and videos."""
    assert (ROOT / "quotes.csv").exists(), "quotes.csv missing"
    assert (ROOT / "images").exists() and any((ROOT / "images").iterdir()), "No images found"
    assert (ROOT / "music").exists() and any((ROOT / "music").iterdir()), "No music tracks found"
    assert (ROOT / "music" / "me and devil.mp3").exists(), "Me and the Devil soundtrack missing"
    assert (ROOT / "fonts").exists() and any((ROOT / "fonts").iterdir()), "No fonts found"
    
    # Check watermark
    watermark = ROOT / "assets" / "watermark.png"
    assert watermark.exists(), "Channel watermark missing"
    with Image.open(watermark) as img:
        assert img.mode == "RGBA", f"Watermark should be RGBA, got {img.mode}"
        assert img.width >= 100 and img.height >= 100, f"Watermark dimensions unexpected: {img.size}"

    # Check background videos
    videos_dir = ROOT / "videos"
    assert videos_dir.exists(), "videos/ directory missing"
    video_files = list(videos_dir.glob("*.mp4"))
    assert len(video_files) >= 10, f"Expected at least 10 sample video clips, found {len(video_files)}"


def test_config_defaults():
    """Verify production audio, voice, and pipeline configurations."""
    config = load_config(ROOT)
    assert config.voice_model == "en-US-BrianMultilingualNeural"
    assert config.voice_pitch == "-6Hz"
    assert config.voice_rate == "-5%"
    assert config.voice_volume == 1.0
    assert config.music_volume == 0.10
    assert config.video_sfx_volume == 0.15
    assert config.watermark_path.name == "watermark.png"


def test_guaranteed_7_to_3_ratio_scheduler(tmp_path):
    """Verify that AssetStore guarantees an exact 7:3 (70% video / 30% image) ratio over 10-run blocks."""
    config = load_config(ROOT)
    temp_usage_file = tmp_path / "test_media_usage.json"
    temp_legacy_file = tmp_path / "test_images_usage.json"
    
    orig_media_usage = config.media_usage_json
    orig_images_usage = config.images_usage_json
    try:
        config.media_usage_json = temp_usage_file
        config.images_usage_json = temp_legacy_file
        store = AssetStore(config)
        
        # Batch 1: Exactly 7 videos and 3 images
        batch1 = [store.choose_background_media()[1] for _ in range(10)]
        assert batch1.count("video") == 7, f"Batch 1 should have 7 videos, got {batch1.count('video')}"
        assert batch1.count("image") == 3, f"Batch 1 should have 3 images, got {batch1.count('image')}"

        # Batch 2: Exactly 7 videos and 3 images
        batch2 = [store.choose_background_media()[1] for _ in range(10)]
        assert batch2.count("video") == 7, f"Batch 2 should have 7 videos, got {batch2.count('video')}"
        assert batch2.count("image") == 3, f"Batch 2 should have 3 images, got {batch2.count('image')}"

        # Batch 3: Exactly 7 videos and 3 images
        batch3 = [store.choose_background_media()[1] for _ in range(10)]
        assert batch3.count("video") == 7
        assert batch3.count("image") == 3
        
        # Test explicit overrides
        _, force_img = store.choose_background_media(media_type="image")
        assert force_img == "image"
        _, force_vid = store.choose_background_media(media_type="video")
        assert force_vid == "video"
    finally:
        config.media_usage_json = orig_media_usage
        config.images_usage_json = orig_images_usage


def test_subtitles_builder(tmp_path):
    """Verify ASS subtitle generation for both Spotlight and Cumulative modes."""
    sample_words = [
        {"word": "DISCIPLINE", "start": 0.5, "end": 1.2},
        {"word": "BUILDS", "start": 1.3, "end": 1.8},
        {"word": "CHARACTER", "start": 1.9, "end": 2.6},
    ]
    for mode in ["spotlight", "cumulative"]:
        ass_path = tmp_path / f"subtitles_{mode}.ass"
        build_ass_subtitles(
            words=sample_words,
            author="MARCUS AURELIUS",
            ass_path=ass_path,
            mode=mode,
            total_duration=10.0,
        )
        assert ass_path.exists()
        content = ass_path.read_text(encoding="utf-8")
        assert "[Script Info]" in content
        assert "[V4+ Styles]" in content
        assert "Style: Quote" in content
        assert "[Events]" in content
        assert "DISCIPLINE" in content
        assert "MARCUS AURELIUS" in content
        assert "Dialogue: 0," in content


def test_preferred_music_selection():
    """Verify that choose_music defaults to 'me and devil.mp3'."""
    config = load_config(ROOT)
    store = AssetStore(config)
    music_file = store.choose_music()
    assert music_file.name == "me and devil.mp3"


def test_quotes_store_and_selection():
    """Verify QuoteStore correctly loads quotes and handles selection."""
    store = QuoteStore(ROOT / "quotes.csv")
    quotes = store.list_quotes()
    assert len(quotes) > 0, "No quotes loaded from quotes.csv"
    
    quote = store.choose_random_quote()
    assert quote.quote, "Selected quote has empty text"


def test_database_media_type_support(tmp_path):
    """Verify Database correctly records and retrieves media_type."""
    db_file = tmp_path / "test.db"
    db = Database(db_file)
    job_id = db.create_job(
        quote="Test quote",
        author="Test Author",
        source_row_id=None,
        image_name="test_clip.mp4",
        music_name="test.mp3",
        darken=0.88,
        media_type="video",
    )
    job = db.get_job_row(job_id)
    assert job["media_type"] == "video"
    from app.database import row_to_job
    detail = row_to_job(job)
    assert detail.media_type == "video"


def test_cli_help():
    """Verify CLI entrypoint help runs successfully."""
    res = subprocess.run([sys.executable, "-m", "app.cli", "--help"], env={"PYTHONPATH": str(ROOT / "backend")}, capture_output=True, text=True)
    assert res.returncode == 0, f"CLI help failed: {res.stderr}"
