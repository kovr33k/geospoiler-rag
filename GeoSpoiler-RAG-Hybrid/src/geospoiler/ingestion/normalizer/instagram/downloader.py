"""Instagram URL, yt-dlp, download, and subtitle helpers."""

import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from geospoiler import config

logger = logging.getLogger("geospoiler.normalizer.instagram")
_YTDLP_PREFLIGHT: dict[str, str] | None = None
_YTDLP_PREFLIGHT_AT = 0.0
_INSTAGRAM_HOSTS = frozenset(
    {
        "instagram.com",
        "www.instagram.com",
        "kkinstagram.com",
        "www.kkinstagram.com",
    }
)
_INSTAGRAM_PATH_RE = re.compile(r"^/(?:reel|p)/[A-Za-z0-9_-]+/?$", re.IGNORECASE)


def is_valid_instagram_url(url: str) -> bool:
    """Return whether *url* targets an allowed Instagram post/reel host."""
    value = str(url or "").strip()
    if not value:
        return False
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").rstrip(".").casefold()
    return host in _INSTAGRAM_HOSTS and bool(_INSTAGRAM_PATH_RE.fullmatch(parsed.path))


def _yt_dlp_command(*args: str) -> list[str]:
    """Run yt-dlp from the interpreter that owns the active project environment."""
    return [sys.executable, "-m", "yt_dlp", *args]


def check_ytdlp_version(*, force: bool = False) -> dict[str, str]:
    """Verify the active interpreter's yt-dlp against minimum and stable release."""
    global _YTDLP_PREFLIGHT, _YTDLP_PREFLIGHT_AT
    if (
        _YTDLP_PREFLIGHT is not None
        and not force
        and (
            config.YTDLP_PREFLIGHT_TTL_SECONDS <= 0
            or time.monotonic() - _YTDLP_PREFLIGHT_AT < config.YTDLP_PREFLIGHT_TTL_SECONDS
        )
    ):
        return dict(_YTDLP_PREFLIGHT)

    def store(value: dict[str, str]) -> dict[str, str]:
        global _YTDLP_PREFLIGHT, _YTDLP_PREFLIGHT_AT
        _YTDLP_PREFLIGHT = value
        _YTDLP_PREFLIGHT_AT = time.monotonic()
        return dict(value)

    result: dict[str, str] = {"status": "failed", "installed": "", "latest": ""}
    try:
        local = subprocess.run(
            _yt_dlp_command("--version"),
            capture_output=True,
            text=True,
            timeout=config.YTDLP_VERSION_CHECK_TIMEOUT_SECONDS,
            encoding="utf-8",
            check=False,
        )
        installed = (local.stdout or "").strip()
        result["installed"] = installed
        if local.returncode != 0 or not installed:
            result["error"] = "cannot_read_installed_version"
            return store(result)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = f"version_command_failed:{exc}"
        return store(result)

    if _version_key(installed) < _version_key(config.YTDLP_MIN_VERSION):
        result["error"] = f"below_minimum:{config.YTDLP_MIN_VERSION}"
        return store(result)

    try:
        response = requests.get(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "GeoSpoiler"},
            timeout=config.YTDLP_VERSION_CHECK_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        tag = str(response.json().get("tag_name") or "").strip().lstrip("v")
        result["latest"] = tag
        if not tag:
            result["error"] = "latest_version_missing"
        elif _version_key(installed) < _version_key(tag):
            result["error"] = f"outdated:{tag}"
            result["status"] = "outdated"
        else:
            result["status"] = "ok"
    except (requests.RequestException, ValueError, TypeError) as exc:
        result["error"] = f"latest_version_unavailable:{exc}"
        result["status"] = "degraded"

    stored = store(result)
    logger.info(
        "yt-dlp preflight: installed=%s latest=%s status=%s%s",
        result.get("installed", ""),
        result.get("latest", ""),
        result.get("status"),
        f" error={result['error']}" if result.get("error") else "",
    )
    return stored


def installed_ytdlp_version() -> str:
    """Read the local yt-dlp version without a network request."""
    try:
        result = subprocess.run(
            _yt_dlp_command("--version"),
            capture_output=True,
            text=True,
            timeout=config.YTDLP_VERSION_CHECK_TIMEOUT_SECONDS,
            encoding="utf-8",
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def _version_key(value: str) -> tuple[int, ...]:
    parts = []
    for chunk in value.strip().lstrip("v").replace("-", ".").split("."):
        digits = "".join(char for char in chunk if char.isdigit())
        parts.append(int(digits or 0))
    return tuple((parts + [0, 0, 0])[:3])


def canonicalize_instagram_url(url: str) -> str:
    """Rewrite archived kkinstagram links back to canonical Instagram URLs."""
    parts = urlsplit(url)
    host = parts.netloc.lower()

    if host == "kkinstagram.com":
        return urlunsplit(
            (parts.scheme or "https", "www.instagram.com", parts.path, parts.query, parts.fragment)
        )

    if host.startswith("kkinstagram.com:"):
        return urlunsplit(
            (
                parts.scheme or "https",
                host.replace("kkinstagram.com", "www.instagram.com", 1),
                parts.path,
                parts.query,
                parts.fragment,
            )
        )

    return url


# ─────────────────────── Deep Extract: Reel ───────────────────────

def _get_info_ytdlp(url: str) -> dict | None:
    """Get post metadata via yt-dlp --dump-json."""
    if not is_valid_instagram_url(url):
        logger.warning("Rejected non-Instagram URL before yt-dlp: %s", url)
        return None
    version = check_ytdlp_version()
    status = version.get("status")
    if status == "failed" or (config.YTDLP_REQUIRE_CURRENT and status != "ok"):
        logger.error(
            "yt-dlp preflight blocked Instagram extraction: %s",
            version.get("error") or "unknown_error",
        )
        return None
    if status != "ok":
        logger.warning(
            "yt-dlp preflight is %s; continuing with local version %s (%s)",
            status,
            version.get("installed") or "unknown",
            version.get("error") or "latest check unavailable",
        )
    try:
        result = subprocess.run(
            _yt_dlp_command("--dump-json", "--no-download", "--no-warnings", url),
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
        )
        if result.returncode == 0 and result.stdout:
            return json.loads(result.stdout)
        details = (result.stderr or "").strip().replace("\r", " ").replace("\n", " ")
        if details:
            logger.warning(
                "yt-dlp metadata extraction failed for %s (exit=%s): %s",
                url,
                result.returncode,
                details[-1000:],
            )
        else:
            logger.warning(
                "yt-dlp metadata extraction failed for %s (exit=%s; empty stderr)",
                url,
                result.returncode,
            )
    except subprocess.TimeoutExpired:
        logger.warning(f"yt-dlp timeout for {url}")
    except json.JSONDecodeError:
        logger.warning(f"yt-dlp returned invalid JSON for {url}")
    except FileNotFoundError:
        logger.error("yt-dlp not found — install it: pip install yt-dlp")
    return None


def _download_video(url: str, work_dir: Path, post_id: str) -> Path | None:
    """Download video via yt-dlp."""
    if not is_valid_instagram_url(url):
        logger.warning("Rejected non-Instagram URL before video download: %s", url)
        return None
    out_path = work_dir / f"{post_id}.%(ext)s"
    try:
        subprocess.run(
            _yt_dlp_command(
                "-f", "best[ext=mp4]/best",
                "--no-warnings",
                "--max-filesize", f"{config.INSTAGRAM_MAX_VIDEO_SIZE_MB}M",
                "-o", str(out_path),
                url,
            ),
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"yt-dlp video download timeout for {url}")
        return None
    except FileNotFoundError:
        logger.error("yt-dlp not found")
        return None

    # Find downloaded file
    for ext in ("mp4", "webm", "mkv"):
        candidate = work_dir / f"{post_id}.{ext}"
        if candidate.exists():
            return candidate

    # Glob fallback
    matches = list(work_dir.glob(f"{post_id}.*"))
    video_exts = {".mp4", ".webm", ".mkv", ".avi", ".mov"}
    for m in matches:
        if m.suffix.lower() in video_exts:
            return m
    return None


def _download_carousel_images(
    url: str, carousel_dir: Path, post_id: str, info: dict
) -> list[Path]:
    """Download carousel images via yt-dlp."""
    if not is_valid_instagram_url(url):
        logger.warning("Rejected non-Instagram URL before carousel download: %s", url)
        return []
    out_template = str(carousel_dir / f"{post_id}_%(autonumber)s.%(ext)s")
    try:
        subprocess.run(
            _yt_dlp_command(
                "--no-warnings",
                "-o", out_template,
                url,
            ),
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"yt-dlp carousel download timeout for {url}")
        return []
    except FileNotFoundError:
        logger.error("yt-dlp not found")
        return []

    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    images = sorted(
        p for p in carousel_dir.iterdir()
        if p.suffix.lower() in image_exts
    )
    return images


# ─────────────────────── Audio extraction & transcription ───────────────────────

def _get_subtitles_from_info(url: str, info: dict) -> str | None:
    """Download and return subtitles if yt-dlp exposes them."""
    if not is_valid_instagram_url(url):
        logger.warning("Rejected non-Instagram URL before subtitle download: %s", url)
        return None
    subtitles = info.get("subtitles", {})
    auto_captions = info.get("automatic_captions", {})
    lang = None
    use_auto = False

    for preferred in ("ru", "uk", "es", "en"):
        if preferred in subtitles:
            lang = preferred
            break
    if not lang:
        for preferred in ("ru", "uk", "es", "en"):
            if preferred in auto_captions:
                lang = preferred
                use_auto = True
                break

    if not lang:
        return None

    sub_dir = config.MEDIA_CACHE_DIR / "subs"
    sub_dir.mkdir(parents=True, exist_ok=True)

    try:
        cmd = _yt_dlp_command(
            "--skip-download",
            "--no-warnings",
            "--write-subs" if not use_auto else "--write-auto-subs",
            "--sub-lang", lang,
            "--sub-format", "vtt",
            "--convert-subs", "srt",
            "-o", str(sub_dir / "%(id)s"),
            url,
        )
        subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8")

        video_id = info.get("id", "")
        srt_files = list(sub_dir.glob(f"{video_id}*.srt"))
        if not srt_files:
            return None

        srt_text = srt_files[0].read_text(encoding="utf-8", errors="replace")
        clean = _srt_to_text(srt_text)

        # Cleanup
        for f in srt_files:
            f.unlink(missing_ok=True)

        return clean if clean and clean.strip() else None

    except subprocess.TimeoutExpired:
        logger.warning(f"yt-dlp subtitle timeout for Instagram {url}")
        return None

def _srt_to_text(srt: str) -> str:
    """Convert SRT subtitle format to plain text."""
    lines = []
    prev_line = ""

    for line in srt.splitlines():
        line = line.strip()
        if not line or line.isdigit():
            continue
        if re.match(r"\d{2}:\d{2}:\d{2}", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\{[^}]+\}", "", line)
        if line != prev_line:
            lines.append(line)
            prev_line = line

    text = " ".join(lines).strip()
    return text or None


# ─────────────────────── Cache ───────────────────────

def _extract_post_id(url: str) -> str | None:
    """Extract the post/reel ID from an Instagram URL."""
    if not is_valid_instagram_url(url):
        return None
    match = re.search(r"instagram\.com/(?:reel|p)/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None
