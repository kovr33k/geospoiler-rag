"""Frame extraction, deduplication, and content filtering shared by media routes."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("geospoiler.normalizer.media_frames")


def _extract_frames(video_path: Path, frames_dir: Path, interval: float, max_frames: int | None = None) -> list[Path]:
    """Extract frames from a video at a bounded time interval."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(frames_dir / "frame_%04d.jpg")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(video_path),
                "-vf", f"fps=1/{interval}", "-q:v", "3",
                *(["-frames:v", str(max(1, int(max_frames)))] if max_frames is not None else []),
                pattern,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg frame extraction timeout for %s", video_path)
        return []
    except FileNotFoundError:
        logger.error("ffmpeg not found")
        return []
    return sorted(frames_dir.glob("frame_*.jpg"))


def _dedup_frames(frames: list[Path], threshold: int = 5) -> list[Path]:
    """Remove near-duplicate frames using a deterministic perceptual hash."""
    if not frames:
        return []
    unique = [frames[0]]
    previous = _compute_phash(frames[0])
    if previous is None:
        return frames
    for frame_path in frames[1:]:
        current = _compute_phash(frame_path)
        if current is None:
            unique.append(frame_path)
            continue
        if _hamming_distance(previous, current) > threshold:
            unique.append(frame_path)
            previous = current
    return unique


def _compute_phash(image_path: Path, hash_size: int = 8) -> int | None:
    try:
        from PIL import Image

        image = Image.open(image_path).convert("L").resize(
            (hash_size + 1, hash_size), Image.LANCZOS
        )
        pixels = list(_image_pixels(image))
        width = hash_size + 1
        bits = [
            1 if pixels[y * width + x] > pixels[y * width + x + 1] else 0
            for y in range(hash_size)
            for x in range(hash_size)
        ]
        return int("".join(str(bit) for bit in bits), 2)
    except Exception as exc:
        logger.debug("phash computation failed for %s: %s", image_path, exc)
        return None


def _hamming_distance(first: int, second: int) -> int:
    return bin(first ^ second).count("1")


def _filter_empty_frames(frames: list[Path], edge_threshold: float = 0.10) -> list[Path]:
    """Drop blank frames while retaining one frame when all are low-content."""
    if not frames:
        return []
    content = []
    for frame_path in frames:
        density = _compute_edge_density(frame_path)
        if density is None or density >= edge_threshold:
            content.append(frame_path)
    return content if content else frames[:1]


def _compute_edge_density(image_path: Path) -> float | None:
    try:
        from PIL import Image, ImageFilter

        image = Image.open(image_path).convert("L").resize((160, 120), Image.LANCZOS)
        pixels = list(_image_pixels(image.filter(ImageFilter.FIND_EDGES)))
        return sum(1 for pixel in pixels if pixel > 30) / len(pixels) if pixels else 0.0
    except Exception as exc:
        logger.debug("edge density computation failed for %s: %s", image_path, exc)
        return None


def _image_pixels(image):
    """Read flattened grayscale pixels across Pillow 10-14 APIs."""
    getter = getattr(image, "get_flattened_data", None)
    if getter is not None:
        return getter()
    return image.getdata()
