"""Audio extraction and transcription helpers for Instagram Reels."""

import logging
import subprocess
from pathlib import Path

from geospoiler import config
from geospoiler.ingestion.normalizer.transcription_handler import _call_transcription_api

logger = logging.getLogger("geospoiler.normalizer.instagram")

def _extract_audio(video_path: Path, work_dir: Path, post_id: str) -> Path | None:
    """Extract audio from video as mono 48kbps mp3."""
    audio_path = work_dir / f"{post_id}_audio.mp3"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-ac", "1",
                "-b:a", "48k",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"ffmpeg audio extraction timeout for {video_path}")
        return None
    except FileNotFoundError:
        logger.error("ffmpeg not found")
        return None

    return audio_path if audio_path.exists() else None

def _transcribe_audio(audio_path: Path) -> str | None:
    """Transcribe Instagram audio with the configured Whisper STT model."""
    if not config.TRANSCRIPTION_ENABLED:
        return None
    if not config.TRANSCRIPTION_API_KEY or config.TRANSCRIPTION_API_KEY == "your-api-key-here":
        logger.warning("Instagram transcription unavailable: TRANSCRIPTION_API_KEY is not configured")
        return None
    try:
        result = _call_transcription_api(audio_path)
        return result.text.strip() if result.status == "transcribed" else None
    except Exception as exc:
        logger.warning("Instagram transcription failed: %s", exc)
        return None


# ─────────────────────── Frame extraction & processing ───────────────────────
