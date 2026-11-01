"""Instagram URL, yt-dlp, download, and subtitle helpers."""

import http.cookiejar
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
# Instagram web app id. Photo carousels are read from the media info API with this header.
_IG_APP_ID = "936619743392459"
_IG_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
# Base64url alphabet used by Instagram shortcodes. Index is the digit value.
_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_CAROUSEL_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_CAROUSEL_SLIDE_LIMIT = 20
_INSTAGRAM_HTTP_TIMEOUT_SECONDS = 30
# Real CDN domains only. A host that merely ends with the same letters is not enough.
_CDN_IMAGE_HOST_SUFFIXES = ("cdninstagram.com", "fbcdn.net")


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


# One warning per missing path. The cookies file itself is never read or logged.
_logged_missing_cookie_files: set[str] = set()


def _instagram_cookies_args() -> list[str]:
    """``--cookies <path>`` when the configured file exists.

    A set-but-missing file logs one warning with the path only, then extraction
    continues without cookies.
    """
    configured = config.INSTAGRAM_COOKIES_FILE
    if not configured:
        return []
    path = Path(configured)
    if not path.is_file():
        key = str(path)
        if key not in _logged_missing_cookie_files:
            _logged_missing_cookie_files.add(key)
            logger.warning("Instagram cookies file is missing: %s", path)
        return []
    return ["--cookies", str(path)]


def _instagram_ytdlp_command(*args: str) -> list[str]:
    """yt-dlp command for Instagram content, including cookies when configured."""
    return _yt_dlp_command(*_instagram_cookies_args(), *args)


def _ytdlp_stderr_for_log(stderr: str) -> str:
    """Stderr tail safe to log.

    With cookies configured, yt-dlp prints malformed cookie rows verbatim.
    Drop any line that mentions cookies or is tab-separated, then keep a
    short tail of what remains.
    """
    text = stderr or ""
    if _instagram_cookies_args():
        kept = [
            line
            for line in text.splitlines()
            if "cookie" not in line.casefold() and "\t" not in line
        ]
        text = "\n".join(kept)
    collapsed = " ".join(text.split())
    return collapsed[-1000:]


def _ytdlp_json_objects(stdout: str) -> list[dict]:
    """Parse yt-dlp stdout as one JSON object per line."""
    objects: list[dict] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


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
            _instagram_ytdlp_command(
                "--dump-json",
                "--no-download",
                "--no-warnings",
                "--ignore-no-formats-error",
                url,
            ),
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
        )
        objects = _ytdlp_json_objects(result.stdout)
        if objects:
            # Mixed carousels can print one JSON object per line and still exit
            # non-zero. Photo-only posts do the same without a video format.
            if result.returncode != 0:
                logger.warning(
                    "yt-dlp metadata for %s exited %s; using parsed JSON",
                    url,
                    result.returncode,
                )
            return next((o for o in objects if o.get("description") or o.get("title")), objects[0])
        if result.returncode == 0 and (result.stdout or "").strip():
            logger.warning("yt-dlp returned invalid JSON for %s", url)
            return None
        details = _ytdlp_stderr_for_log(result.stderr)
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
            _instagram_ytdlp_command(
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


def _shortcode_to_media_pk(shortcode: str) -> int:
    """Decode an Instagram shortcode into the numeric media id."""
    pk = 0
    for char in shortcode:
        pk = pk * 64 + _SHORTCODE_ALPHABET.index(char)
    return pk


def _is_instagram_cdn_image_url(image_url: str) -> bool:
    """True for https URLs on cdninstagram.com or fbcdn.net."""
    parsed = urlsplit(str(image_url or "").strip())
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").rstrip(".").casefold()
    return any(
        host == suffix or host.endswith("." + suffix) for suffix in _CDN_IMAGE_HOST_SUFFIXES
    )


def _carousel_image_suffix(image_url: str) -> str:
    suffix = Path(urlsplit(image_url).path).suffix.lower()
    if suffix in _CAROUSEL_IMAGE_EXTS:
        return suffix
    return ".jpg"


def _log_carousel_api_failure(url: str, status: int | str | None) -> None:
    """Log a carousel API failure with the post URL and status only.

    Cookie values, cookie-file contents, and response bodies stay out of the message.
    """
    logger.warning(
        "Instagram carousel API failed for %s (status=%s)",
        url,
        "none" if status is None else status,
    )


def _instagram_media_slides(payload: object) -> list[dict] | None:
    """Return photo/carousel slides from a media info payload, capped at the slide limit."""
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    item = items[0]
    media_type = item.get("media_type")
    if media_type == 8:
        slides = item.get("carousel_media")
        if not isinstance(slides, list):
            return None
        return [slide for slide in slides if isinstance(slide, dict)][:_CAROUSEL_SLIDE_LIMIT]
    if media_type == 1:
        return [item]
    return None


def _slide_image_url(slide: dict) -> str | None:
    """Largest image for a photo, or the cover image of a video slide."""
    if slide.get("media_type") not in (1, 2):
        return None
    versions = slide.get("image_versions2")
    if not isinstance(versions, dict):
        return None
    candidates = versions.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return None
    image_url = candidates[0].get("url")
    if not isinstance(image_url, str) or not image_url.strip():
        return None
    return image_url.strip()


def _download_cdn_image(image_url: str, dest: Path) -> int | None:
    """Save one CDN image. Return the HTTP status when the save did not happen."""
    try:
        response = requests.get(
            image_url,
            headers={"User-Agent": _IG_BROWSER_USER_AGENT},
            timeout=_INSTAGRAM_HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200 or not response.content:
        return response.status_code
    final_url = response.url if isinstance(getattr(response, "url", None), str) else image_url
    if not _is_instagram_cdn_image_url(final_url):
        return response.status_code
    try:
        dest.write_bytes(response.content)
    except OSError:
        dest.unlink(missing_ok=True)
        return response.status_code
    return None


def _download_carousel_images_via_api(
    url: str, carousel_dir: Path, post_id: str, cookies_path: str
) -> list[Path]:
    """Download slide images from Instagram's web media API.

    An empty list means the caller should fall back to yt-dlp. Saved names use a
    zero-padded slide index so a lexical sort stays in slide order.
    """
    jar = http.cookiejar.MozillaCookieJar(cookies_path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except OSError:
        _log_carousel_api_failure(url, None)
        return []

    shortcode = _extract_post_id(url)
    if not shortcode:
        _log_carousel_api_failure(url, None)
        return []
    try:
        media_pk = _shortcode_to_media_pk(shortcode)
    except ValueError:
        _log_carousel_api_failure(url, None)
        return []

    try:
        response = requests.get(
            f"https://www.instagram.com/api/v1/media/{media_pk}/info/",
            headers={
                "User-Agent": _IG_BROWSER_USER_AGENT,
                "X-IG-App-ID": _IG_APP_ID,
            },
            cookies=jar,
            timeout=_INSTAGRAM_HTTP_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException:
        _log_carousel_api_failure(url, None)
        return []

    status = response.status_code
    if status != 200:
        _log_carousel_api_failure(url, status)
        return []
    try:
        payload = response.json()
    except ValueError:
        _log_carousel_api_failure(url, status)
        return []

    slides = _instagram_media_slides(payload)
    if not slides:
        _log_carousel_api_failure(url, status)
        return []

    saved: list[Path] = []
    image_status: int | None = None
    for index, slide in enumerate(slides, start=1):
        image_url = _slide_image_url(slide)
        if not image_url or not _is_instagram_cdn_image_url(image_url):
            continue
        dest = carousel_dir / f"{post_id}_{index:02d}{_carousel_image_suffix(image_url)}"
        failed_status = _download_cdn_image(image_url, dest)
        if failed_status is None and dest.is_file():
            saved.append(dest)
        elif failed_status is not None:
            image_status = failed_status
    if not saved:
        _log_carousel_api_failure(url, image_status if image_status is not None else status)
        return []
    return saved


def _download_carousel_images(
    url: str, carousel_dir: Path, post_id: str, info: dict
) -> list[Path]:
    """Download carousel images, using the web API when cookies are configured."""
    if not is_valid_instagram_url(url):
        logger.warning("Rejected non-Instagram URL before carousel download: %s", url)
        return []
    cookies_args = _instagram_cookies_args()
    if cookies_args:
        images = _download_carousel_images_via_api(url, carousel_dir, post_id, cookies_args[1])
        if images:
            return images
    return _download_carousel_images_ytdlp(url, carousel_dir, post_id)


def _download_carousel_images_ytdlp(url: str, carousel_dir: Path, post_id: str) -> list[Path]:
    """Download carousel images via yt-dlp and keep the image files."""
    out_template = str(carousel_dir / f"{post_id}_%(autonumber)s.%(ext)s")
    try:
        subprocess.run(
            _instagram_ytdlp_command(
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

    images = sorted(
        p for p in carousel_dir.iterdir()
        if p.suffix.lower() in _CAROUSEL_IMAGE_EXTS
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
        cmd = _instagram_ytdlp_command(
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
