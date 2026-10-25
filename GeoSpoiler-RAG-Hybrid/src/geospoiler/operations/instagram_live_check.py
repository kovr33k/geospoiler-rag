"""
Instagram Deep Extract — comparison test.

Tests a single Instagram Reel URL through the deep extract pipeline
with two different vision models for frame OCR:
1. MiMo-V2.5 (xiaomi/mimo-v2.5) via OpenRouter
2. Gemini 2.5 Flash Lite (google/gemini-2.5-flash-lite) via OpenRouter

Usage:
    python scripts/instagram_live_check.py <instagram_reel_url>
"""

import logging
import sys
import time
from unittest.mock import patch

from geospoiler import config  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.ingestion.normalizer.instagram_handler import (
    _extract_post_id,
    _get_info_ytdlp,
    canonicalize_instagram_url,
    extract_instagram_text,  # noqa: E402
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("instagram_live_check")


def run_test(url: str):
    canonical = canonicalize_instagram_url(url)
    post_id = _extract_post_id(canonical) or "unknown"

    print(f"\n{'='*70}")
    print("Instagram Deep Extract Test")
    print(f"URL: {canonical}")
    print(f"Post ID: {post_id}")
    print(f"{'='*70}\n")

    # First, get info to check if URL is valid
    info = _get_info_ytdlp(canonical)
    if not info:
        print("ERROR: Could not get video info via yt-dlp. Is the URL valid/public?")
        return

    title = info.get("title", "?")
    duration = info.get("duration", 0)
    uploader = info.get("uploader", "?")
    print(f"Title: {title}")
    print(f"Uploader: @{uploader}")
    print(f"Duration: {duration}s")
    print()

    # Clear cache so both runs are fresh
    cache_path = config.INSTAGRAM_CACHE_DIR / f"{post_id}.json"
    if cache_path.exists():
        cache_path.unlink()
        print("Cleared existing cache.\n")

    # ────── Run 1: MiMo-V2.5 ──────
    print(f"{'─'*70}")
    print("RUN 1: MiMo-V2.5 (xiaomi/mimo-v2.5)")
    print(f"{'─'*70}\n")

    start = time.time()
    result_mimo = extract_instagram_text(canonical)
    elapsed_mimo = time.time() - start

    # Clear cache for second run
    if cache_path.exists():
        cache_path.unlink()

    # ────── Run 2: Gemini 2.5 Flash Lite ──────
    print(f"\n{'─'*70}")
    print("RUN 2: Gemini 2.5 Flash Lite (google/gemini-2.5-flash-lite)")
    print(f"{'─'*70}\n")

    start = time.time()
    with patch("normalizer.instagram_handler.config.INSTAGRAM_VISION_MODEL", "google/gemini-2.5-flash-lite"):
        result_gemini = extract_instagram_text(canonical)
    elapsed_gemini = time.time() - start

    # ────── Report ──────
    print(f"\n{'='*70}")
    print("COMPARISON REPORT")
    print(f"{'='*70}\n")

    print(f"{'Metric':<30} {'MiMo-V2.5':<25} {'Gemini Flash Lite':<25}")
    print(f"{'─'*80}")
    print(f"{'Time (seconds)':<30} {elapsed_mimo:<25.1f} {elapsed_gemini:<25.1f}")
    print(f"{'Output length (chars)':<30} {len(result_mimo):<25} {len(result_gemini):<25}")

    mimo_has_ocr = "[Текст с экрана" in result_mimo
    gemini_has_ocr = "[Текст с экрана" in result_gemini
    print(f"{'Has OCR text':<30} {str(mimo_has_ocr):<25} {str(gemini_has_ocr):<25}")

    mimo_has_transcript = "[Транскрипция" in result_mimo
    gemini_has_transcript = "[Транскрипция" in result_gemini
    print(f"{'Has transcript':<30} {str(mimo_has_transcript):<25} {str(gemini_has_transcript):<25}")

    mimo_has_summary = "[Описание ролика" in result_mimo
    gemini_has_summary = "[Описание ролика" in result_gemini
    print(f"{'Has LLM summary':<30} {str(mimo_has_summary):<25} {str(gemini_has_summary):<25}")

    # Save outputs
    out_dir = PROJECT_ROOT / "artifacts"
    out_dir.mkdir(exist_ok=True)

    (out_dir / f"ig_test_{post_id}_mimo.txt").write_text(result_mimo, encoding="utf-8")
    (out_dir / f"ig_test_{post_id}_gemini.txt").write_text(result_gemini, encoding="utf-8")

    print(f"\n{'─'*70}")
    print("Full outputs saved to:")
    print(f"  artifacts/ig_test_{post_id}_mimo.txt")
    print(f"  artifacts/ig_test_{post_id}_gemini.txt")
    print(f"{'─'*70}\n")

    # Print both outputs
    print(f"\n{'='*70}")
    print("OUTPUT: MiMo-V2.5")
    print(f"{'='*70}")
    print(result_mimo[:3000])
    if len(result_mimo) > 3000:
        print(f"\n... [{len(result_mimo) - 3000} more chars]")

    print(f"\n{'='*70}")
    print("OUTPUT: Gemini 2.5 Flash Lite")
    print(f"{'='*70}")
    print(result_gemini[:3000])
    if len(result_gemini) > 3000:
        print(f"\n... [{len(result_gemini) - 3000} more chars]")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.instagram.com/reel/DMyAoeCopIu/?igsh=MTQyemNqc3RrdW5zZA=="
    run_test(url)
