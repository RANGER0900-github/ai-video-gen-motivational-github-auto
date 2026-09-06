import subprocess
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

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
    """Verify essential assets are present."""
    assert (ROOT / "quotes.csv").exists(), "quotes.csv missing"
    assert (ROOT / "images").exists() and any((ROOT / "images").iterdir()), "No images found"
    assert (ROOT / "music").exists() and any((ROOT / "music").iterdir()), "No music tracks found"
    assert (ROOT / "fonts").exists() and any((ROOT / "fonts").iterdir()), "No fonts found"

def test_quotes_store_and_selection():
    """Verify QuoteStore correctly loads quotes and handles selection."""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.csv_store import QuoteStore
    store = QuoteStore(ROOT / "quotes.csv")
    quotes = store.list_quotes()
    assert len(quotes) > 0, "No quotes loaded from quotes.csv"
    
    quote = store.choose_random_quote()
    assert quote.quote, "Selected quote has empty text"

def test_cli_help():
    """Verify CLI entrypoint help runs successfully."""
    res = subprocess.run([sys.executable, "-m", "app.cli", "--help"], env={"PYTHONPATH": str(ROOT / "backend")}, capture_output=True, text=True)
    assert res.returncode == 0, f"CLI help failed: {res.stderr}"
