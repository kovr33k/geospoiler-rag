"""
YouTube Handler — extracts subtitles/transcript from YouTube videos via yt-dlp.

Strategy:
1. Try to get manual subtitles in: ru, uk, es, en
2. Fall back to auto-generated subtitles
3. Fall back to Whisper transcription (if TRANSCRIPTION_ENABLED)
4. If nothing works, extract video title + description
"""

import hashlib
import json
import logging
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from geospoiler import config
from geospoiler.providers.llm_auth import get_openai_api_key

logger = logging.getLogger("geospoiler.normalizer.youtube")

# Preferred subtitle languages (in priority order)
SUBTITLE_LANGS = ["ru", "uk", "es", "en"]
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,}$")
_SENSITIVE_QUERY_KEYS = {
    "access_token", "accesstoken", "api_key", "apikey", "auth", "code",
    "key", "sig", "signature", "token",
}


@dataclass(frozen=True)
class YouTubeCue:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass
class YouTubeArtifact:
    """Normalized YouTube source plus optional timed transcript cues."""

    url: str
    video_id: str
    title: str
    channel: str
    duration_seconds: float | None
    language: str
    transcript_source: str
    transcript_text: str
    cues: list[YouTubeCue]
    description: str
    chapters: list[dict]
    description_links: list[str]
    extracted_at: str

    @property
    def has_transcript(self) -> bool:
        return bool(self.transcript_text.strip())

    @property
    def normalized_text(self) -> str:
        header = _format_youtube_header(self.url, self.title, self.channel)
        body = self.transcript_text.strip()
        if not body and self.description:
            body = f"[Описание видео]\n{self.description.strip()}"
        return f"{header}\n\n{body or '[Субтитры и описание недоступны]'}"

    def metadata(self, *, source_meta: dict | None = None) -> dict:
        data = {
            "url": self.url,
            "video_id": self.video_id,
            "title": self.title,
            "channel": self.channel,
            "duration_seconds": self.duration_seconds,
            "language": self.language,
            "transcript_source": self.transcript_source,
            "has_transcript": self.has_transcript,
            "description": self.description,
            "chapters": self.chapters,
            "description_links": self.description_links,
            "cue_count": len(self.cues),
            "extracted_at": self.extracted_at,
        }
        if source_meta:
            data["telegram_source"] = {
                key: source_meta.get(key)
                for key in ("channel_name", "channel_id", "message_id", "date", "post_url")
                if source_meta.get(key) is not None
            }
        return data


def is_valid_youtube_url(value: str) -> bool:
    """Accept only direct HTTPS links to supported YouTube hosts."""
    try:
        parts = urlsplit(str(value).strip())
    except ValueError:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or parts.username or parts.password or port is not None:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in _YOUTUBE_HOSTS:
        return False
    if host == "youtu.be":
        path_parts = [part for part in parts.path.split("/") if part]
        video_id = path_parts[0] if len(path_parts) == 1 else ""
    elif parts.path.rstrip("/").lower() == "/watch":
        video_id = next(iter(parse_qs(parts.query).get("v", [])), "")
    else:
        match = re.fullmatch(
            r"/(?:shorts|live)/([A-Za-z0-9_-]+)",
            parts.path.rstrip("/"),
            re.IGNORECASE,
        )
        video_id = match.group(1) if match else ""
    return bool(_VIDEO_ID_RE.fullmatch(video_id))


def validate_youtube_url(value: str) -> str:
    """Validate a URL before passing it to yt-dlp."""
    url = str(value).strip()
    if not is_valid_youtube_url(url):
        raise ValueError(f"Unsupported or unsafe YouTube URL: {url}")
    parts = urlsplit(url)
    if parts.path.rstrip("/").lower() == "/watch":
        video_id = next(iter(parse_qs(parts.query).get("v", [])), "")
    elif parts.hostname and parts.hostname.lower().rstrip(".") == "youtu.be":
        video_id = parts.path.strip("/")
    else:
        video_id = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return f"https://www.youtube.com/watch?v={video_id}"


def redact_sensitive_url(value: str) -> str:
    """Keep source links useful without persisting capability tokens."""
    try:
        parts = urlsplit(str(value).strip())
    except ValueError:
        return str(value)
    filtered = [
        (key, "[REDACTED]" if key.casefold().replace("-", "_") in _SENSITIVE_QUERY_KEYS else item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered), ""))


def extract_youtube_artifact(url: str) -> YouTubeArtifact:
    """Extract a YouTube source while preserving timed cues when available."""
    url = validate_youtube_url(url)
    info = _get_video_info(url) or {}
    title = _clean_meta_field(info.get("title")) or "без названия"
    channel = _clean_meta_field(info.get("channel") or info.get("uploader")) or "неизвестно"
    description_raw = str(info.get("description") or "")
    description = _clean_description(description_raw, keep_chapters=False)
    chapters = _extract_chapters(info, description_raw)
    links = sorted(set(re.findall(r"https?://[^\s<>\]\)]+", description_raw)))
    duration = _as_float(info.get("duration"))

    cues, language, transcript_source = _get_timed_subtitles(url, info)
    transcript_text = " ".join(cue.text for cue in cues).strip()

    if not transcript_text:
        transcript_text = (_transcribe_audio(url, info) or "").strip()
        if transcript_text:
            transcript_source = "whisper"
            language = str(info.get("language") or "")

    if not transcript_text:
        transcript_source = "description" if description else "unavailable"

    return YouTubeArtifact(
        url=url,
        video_id=str(info.get("id") or _video_id_from_url(url)),
        title=title,
        channel=channel,
        duration_seconds=duration,
        language=language or str(info.get("language") or "unknown"),
        transcript_source=transcript_source,
        transcript_text=transcript_text,
        cues=cues,
        description=description,
        chapters=chapters,
        description_links=links,
        extracted_at=datetime.now(UTC).isoformat(),
    )


def save_youtube_artifact(
    artifact: YouTubeArtifact,
    *,
    channel_name: str,
    channel_id: int | str | None = None,
    message_id: int | str,
    source_meta: dict | None = None,
) -> dict[str, str]:
    """Persist text, compact metadata, and timed cues for one Telegram ingress."""
    channel_component = _safe_component(channel_id if channel_id is not None else channel_name)
    target_dir = config.YOUTUBE_NORMALIZED_DIR / channel_component / str(message_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_component(artifact.video_id or "unknown")
    artifact_digest = hashlib.sha256(
        json.dumps(
            {
                "text": artifact.normalized_text,
                "cues": [asdict(cue) for cue in artifact.cues],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    text_path = target_dir / f"{stem}.{artifact_digest}.youtube.txt"
    metadata_path = target_dir / f"{stem}.youtube.meta.json"
    cues_path = target_dir / f"{stem}.{artifact_digest}.youtube.cues.json"

    _atomic_write_text(text_path, artifact.normalized_text)
    _atomic_write_text(
        cues_path,
        json.dumps([asdict(cue) for cue in artifact.cues], ensure_ascii=False, indent=2),
    )
    metadata = artifact.metadata(source_meta=source_meta)
    metadata["transcript_path"] = str(text_path.relative_to(config.PROJECT_ROOT))
    metadata["cues_path"] = str(cues_path.relative_to(config.PROJECT_ROOT))
    _atomic_write_text(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2))
    return {
        "text_path": str(text_path),
        "metadata_path": str(metadata_path),
        "cues_path": str(cues_path),
    }


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def extract_youtube_text(url: str) -> str:
    """
    Extract text content from a YouTube video.
    Returns formatted text with title and subtitles/description.
    """
    try:
        url = validate_youtube_url(url)
        # First get video info (title, description, available subs)
        info = _get_video_info(url)
        if not info:
            return f'[YouTube: не удалось получить информацию — {url}]'

        title = _clean_meta_field(info.get("title")) or "без названия"
        description = str(info.get("description") or "")
        channel = _clean_meta_field(info.get("channel") or info.get("uploader")) or "неизвестно"

        # Try to get subtitles
        subtitles = _get_subtitles(url, info)

        parts = [_format_youtube_header(url, title, channel)]

        if subtitles:
            parts.append(subtitles)
        else:
            # No subs — try Whisper transcription
            transcript = _transcribe_audio(url, info)
            if transcript:
                parts.append(transcript)
            elif description:
                desc_clean = _clean_description(description, keep_chapters=True)
                if desc_clean:
                    parts.append(f"[Описание видео]\n{desc_clean}")
                else:
                    parts.append("[Субтитры и описание недоступны]")
            else:
                parts.append("[Субтитры и описание недоступны]")

        return "\n\n".join(parts)

    except Exception as e:
        logger.error(f"YouTube extraction failed for {url}: {e}")
        return f'[YouTube: ошибка обработки — {url}]'


def _format_youtube_header(url: str, title: str, author: str) -> str:
    """Return the mandatory metadata block for normalized YouTube sources."""
    return "\n".join(
        [
            "[YouTube]",
            f"Автор: {_clean_meta_field(author) or 'неизвестно'}",
            f"Название: {_clean_meta_field(title) or 'без названия'}",
            f"URL: {url}",
        ]
    )


def _clean_meta_field(value: object) -> str:
    return " ".join(str(value or "").split())


def _get_video_info(url: str) -> dict | None:
    """Get video metadata via yt-dlp --dump-json."""
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--dump-json",
                "--no-download",
                "--no-warnings",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
        )
        if result.returncode == 0 and result.stdout:
            return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning(f"yt-dlp timeout for {url}")
    except json.JSONDecodeError:
        logger.warning(f"yt-dlp returned invalid JSON for {url}")
    return None


def _get_timed_subtitles(url: str, info: dict) -> tuple[list[YouTubeCue], str, str]:
    """Download the preferred subtitle track and keep SRT cue boundaries."""
    available_subs = info.get("subtitles", {}) or {}
    available_auto = info.get("automatic_captions", {}) or {}
    language = next((lang for lang in SUBTITLE_LANGS if lang in available_subs), None)
    use_auto = False
    if not language:
        language = next((lang for lang in SUBTITLE_LANGS if lang in available_auto), None)
        use_auto = bool(language)
    if not language:
        return [], "", "unavailable"

    sub_root = config.MEDIA_CACHE_DIR / "subs"
    sub_root.mkdir(parents=True, exist_ok=True)
    video_id = str(info.get("id") or _video_id_from_url(url))
    with tempfile.TemporaryDirectory(prefix=f"{_safe_component(video_id)}-", dir=sub_root) as temp_dir:
        out_template = str(Path(temp_dir) / "%(id)s")
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--no-warnings",
            "--write-auto-subs" if use_auto else "--write-subs",
            "--sub-lang", language,
            "--sub-format", "vtt",
            "--convert-subs", "srt",
            "-o", out_template,
            url,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, encoding="utf-8"
            )
        except subprocess.TimeoutExpired:
            return [], language, "unavailable"
        if result.returncode != 0:
            return [], language, "unavailable"

        srt_files = sorted(Path(temp_dir).glob(f"{video_id}*.srt"))
        if not srt_files:
            return [], language, "unavailable"
        cues = _srt_to_cues(
            srt_files[0].read_text(encoding="utf-8", errors="replace"),
            remove_overlaps=use_auto,
        )
    return cues, language, "auto_subtitles" if use_auto else "subtitles"


def _srt_to_cues(srt: str, *, remove_overlaps: bool = True) -> list[YouTubeCue]:
    """Parse SRT cues and stitch rolling auto-caption overlaps."""
    timestamp_re = re.compile(
        r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+"
        r"(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})"
    )
    lines = srt.splitlines()
    cues: list[YouTubeCue] = []
    previous_raw_text = ""
    index = 0
    while index < len(lines):
        match = timestamp_re.search(lines[index].strip())
        if not match:
            index += 1
            continue
        text_lines: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index].strip())
            index += 1
        text = " ".join(text_lines)
        text = re.sub(r"<[^>]+>|\{[^}]+\}", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        end_seconds = _srt_time_to_seconds(match.group("end"))
        if cues and cues[-1].text == text:
            previous = cues[-1]
            cues[-1] = YouTubeCue(
                start_seconds=previous.start_seconds,
                end_seconds=max(previous.end_seconds, end_seconds),
                text=previous.text,
            )
            previous_raw_text = text
            continue

        if remove_overlaps and previous_raw_text:
            novel_text = _remove_caption_overlap(previous_raw_text, text)
            previous_raw_text = text
            if not novel_text:
                if cues:
                    previous = cues[-1]
                    cues[-1] = YouTubeCue(
                        start_seconds=previous.start_seconds,
                        end_seconds=max(previous.end_seconds, end_seconds),
                        text=previous.text,
                    )
                continue
            text = novel_text

        cues.append(
            YouTubeCue(
                start_seconds=_srt_time_to_seconds(match.group("start")),
                end_seconds=end_seconds,
                text=text,
            )
        )
        previous_raw_text = text if not remove_overlaps else previous_raw_text or text
    return cues


def _remove_caption_overlap(previous_text: str, current_text: str) -> str:
    """Remove a rolling-caption suffix/prefix overlap without editing speech."""
    previous_tokens = _caption_tokens(previous_text)
    current_tokens = _caption_tokens(current_text)
    max_overlap = min(len(previous_tokens), len(current_tokens))
    for overlap in range(max_overlap, 1, -1):
        if previous_tokens[-overlap:] != current_tokens[:overlap]:
            continue
        token_end = _caption_token_end(current_text, overlap)
        return current_text[token_end:].lstrip(" \t,.;:!?-–—")
    return current_text


def _caption_tokens(text: str) -> list[str]:
    return [token.casefold() for token in re.findall(r"\w+", text, flags=re.UNICODE)]


def _caption_token_end(text: str, token_count: int) -> int:
    matches = list(re.finditer(r"\w+", text, flags=re.UNICODE))
    return matches[token_count - 1].end()


def _srt_time_to_seconds(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return round(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 3)


def _extract_chapters(info: dict, description: str) -> list[dict]:
    """Use yt-dlp chapters, then parse simple timestamped description lines."""
    chapters = []
    for chapter in info.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        title = _clean_meta_field(chapter.get("title"))
        start = _as_float(chapter.get("start_time"))
        end = _as_float(chapter.get("end_time"))
        if title and start is not None:
            chapters.append({"title": title, "start_seconds": start, "end_seconds": end})
    if chapters:
        return chapters

    marker = re.compile(r"^\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s*[—–-]?\s*(?P<title>.+?)\s*$")
    for line in description.splitlines():
        match = marker.match(line)
        if not match:
            continue
        title = _clean_meta_field(match.group("title"))
        if title:
            chapters.append({"title": title, "start_seconds": _clock_to_seconds(match.group("time")), "end_seconds": None})
    for index, chapter in enumerate(chapters[:-1]):
        chapter["end_seconds"] = chapters[index + 1]["start_seconds"]
    return chapters


def _clock_to_seconds(value: str) -> float:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return float(parts[0] * 60 + parts[1])
    return float(parts[0] * 3600 + parts[1] * 60 + parts[2])


def _as_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _video_id_from_url(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]+)", url)
    return match.group(1) if match else "unknown"


def _safe_component(value: object) -> str:
    text = str(value or "unknown").strip()
    return re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("._") or "unknown"


def _get_subtitles(url: str, info: dict) -> str | None:
    """Download and return subtitles text."""
    cues, _, _ = _get_timed_subtitles(url, info)
    clean = " ".join(cue.text for cue in cues).strip()
    return clean or None


CHUNK_DURATION_SEC = 600  # 10-minute chunks for Whisper


def _transcribe_audio(url: str, info: dict) -> str | None:
    """Download audio, split into chunks, transcribe via Whisper API."""
    if not config.TRANSCRIPTION_ENABLED:
        return None

    api_key = config.TRANSCRIPTION_API_KEY
    if not api_key or api_key == "your-api-key-here":
        return None

    video_id = info.get("id", "unknown")
    audio_dir = config.MEDIA_CACHE_DIR / "youtube_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"youtube-{_safe_component(video_id)}-", dir=audio_dir) as temp_dir:
        work_dir = Path(temp_dir)
        raw_path = work_dir / f"{video_id}_raw.%(ext)s"

        # Step 1: download audio into an invocation-specific directory.
        try:
            result = subprocess.run(
                ["yt-dlp", "-x", "--no-warnings", "-o", str(raw_path), url],
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"yt-dlp audio download timeout for {url}")
            return None
        if result.returncode != 0:
            logger.warning("yt-dlp audio download failed for %s: %s", url, result.stderr[-500:])
            return None

        raw_files = list(work_dir.glob(f"{video_id}_raw.*"))
        if not raw_files:
            return None

        # Step 2: split into 10-min chunks as low-bitrate mp3.
        chunks = _split_audio(raw_files[0], work_dir, video_id)
        if not chunks:
            return None

        # Step 3: transcribe each chunk.
        texts = []
        for chunk_path in chunks:
            text = _call_whisper(chunk_path)
            if text:
                texts.append(text)
        return " ".join(texts) if texts else None


def _split_audio(input_path: Path, out_dir: Path, prefix: str) -> list[Path]:
    """Split audio into 10-minute mono 48kbps mp3 chunks."""
    pattern = out_dir / f"{prefix}_chunk_%03d.mp3"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-ac", "1",
                "-b:a", "48k",
                "-f", "segment",
                "-segment_time", str(CHUNK_DURATION_SEC),
                "-reset_timestamps", "1",
                str(pattern),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"ffmpeg split timeout for {input_path}")
        return []

    if result.returncode != 0:
        logger.warning("ffmpeg split failed for %s: %s", input_path, result.stderr[-500:])
        return []
    chunks = sorted(out_dir.glob(f"{prefix}_chunk_*.mp3"))
    return chunks


def _call_whisper(audio_path: Path) -> str | None:
    """Transcribe audio via configured API (supports chat-completions and STT endpoints)."""
    import base64

    api_key = get_openai_api_key(config.TRANSCRIPTION_API_KEY, config.TRANSCRIPTION_BASE_URL)
    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("utf-8")
    base_url = config.TRANSCRIPTION_BASE_URL.rstrip("/")

    # Use chat completions for LLM-based transcription (Gemini, etc.)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": config.TRANSCRIPTION_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "mp3"}},
                        {"type": "text", "text": "Транскрибируй это аудио дословно на русском языке. Верни только текст транскрипции, без комментариев."},
                    ],
                }],
                "max_tokens": 16000,
            },
            timeout=config.TRANSCRIPTION_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        text = str(payload["choices"][0]["message"].get("content") or "").strip()
        return text if text else None
    except requests.Timeout:
        logger.warning(f"Transcription API timeout for {audio_path}")
        return None
    except Exception as exc:
        logger.warning(f"Transcription failed for {audio_path}: {exc}")
        return None


def _srt_to_text(srt: str) -> str:
    """Convert SRT subtitle format to plain text, removing timestamps and duplicates."""
    lines = []
    prev_line = ""

    for line in srt.splitlines():
        line = line.strip()
        # Skip empty, numbering, and timestamp lines
        if not line:
            continue
        if line.isdigit():
            continue
        if re.match(r'\d{2}:\d{2}:\d{2}', line):
            continue
        # Remove HTML tags (SRT may contain <b>, <i>, etc.)
        line = re.sub(r'<[^>]+>', '', line)
        # Remove VTT positioning tags
        line = re.sub(r'\{[^}]+\}', '', line)
        # Skip duplicate consecutive lines (common in auto-subs)
        if line != prev_line:
            lines.append(line)
            prev_line = line

    return " ".join(lines)


def _clean_description(desc: str, keep_chapters: bool = False) -> str:
    """Clean YouTube description: remove promo links, social media blocks, etc."""
    lines = desc.splitlines()
    clean_lines = []
    for line in lines:
        # Stop at common "promo block" markers
        line_lower = line.lower().strip()
        line = _strip_leading_subscribe_prompt(line)
        if not line.strip():
            continue
        line_lower = line.lower().strip()
        normalized = re.sub(r"[^0-9a-zа-яёіїєґ]+", " ", line_lower).strip()
        stop_idx = _find_description_stop_index(line, keep_chapters=keep_chapters)
        if stop_idx is not None:
            prefix = line[:stop_idx].strip(" \t\r\n—–-«»\"'👉📣")
            if prefix:
                clean_lines.append(prefix)
            break
        if _looks_like_timeline_marker(line_lower):
            if keep_chapters:
                clean_lines.append(line)
                continue
            break
        promo_markers = [
            "subscribe", "подписывайтесь", "подписаться",
            "follow us", "наши соцсети", "наши социальные сети",
            "социальные сети", "поддержать", "поддержать канал",
            "донат", "donate", "patreon", "boosty", "monobank",
            "рекламных интеграций", "по вопросам рекламы",
        ]
        chapter_markers = ["содержание", "таймкоды", "chapters"]
        if any(marker in normalized for marker in promo_markers):
            break
        if any(marker in normalized for marker in chapter_markers):
            if keep_chapters:
                continue
            break
        if _is_description_link_farm_line(line_lower):
            if not clean_lines:
                continue
            break
        clean_lines.append(line)

    result = "\n".join(clean_lines).strip()
    # Limit length
    if len(result) > 2000:
        result = result[:2000] + "..."
    return result


def _strip_leading_subscribe_prompt(line: str) -> str:
    """Remove a leading subscription CTA while preserving the useful description."""
    if re.match(r"^\s*еще\s+не\s+подписаны", line, flags=re.IGNORECASE):
        useful_start = re.search(r"\b(почему|зачем|как|кто|что)\b", line, flags=re.IGNORECASE)
        if useful_start:
            return line[useful_start.start():]
        return ""
    return re.sub(
        r"^\s*(?:еще\s+не\s+)?подписан[^\?!.]{0,120}(?:[ᐅᐊ>]+|\s{2,})\s*",
        "",
        line,
        flags=re.IGNORECASE,
    )


def _find_description_stop_index(line: str, keep_chapters: bool = False) -> int | None:
    """Find the earliest promo/timeline marker inside a YouTube description line."""
    stop_patterns = [
        r"[🧡💛❤️]?\s*поддержать(?:\s+канал)?",
        r"по\s+вопросам\s+реклам",
        r"социальные\s+сети",
        r"единозбор",
        r"стрим\s+тут",
        r"мой\s+телеграм",
        r"https?://(?:send|base)\.monobank\.ua/\S+",
        r"https?://(?:www\.)?(?:instagram|facebook|twitter|x|tiktok)\.com/\S+",
        r"https?://t\.me/\S+",
    ]
    if not keep_chapters:
        stop_patterns.extend(
            [
                r"содержание\s*:",
                r"таймкоды\s*:",
                r"chapters\s*:",
                r"\b\d{1,2}:\d{2}(?::\d{2})?\s*[—–-]",
                r"\b\d{1,2}:\d{2}(?::\d{2})?\s+",
            ]
        )
    matches = [
        match.start()
        for pattern in stop_patterns
        if (match := re.search(pattern, line, flags=re.IGNORECASE))
    ]
    return min(matches) if matches else None


def _looks_like_timeline_marker(line: str) -> bool:
    """Return True for YouTube chapter/timeline rows like '0:00 - Intro'."""
    return bool(re.match(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*[-—:]", line))


def _is_description_link_farm_line(line: str) -> bool:
    """Return True for promo-only link blocks in YouTube descriptions."""
    promo_hosts = (
        "send.monobank.ua",
        "base.monobank.ua",
        "instagram.com",
        "facebook.com",
        "twitter.com",
        "x.com/",
        "t.me/",
        "tiktok.com",
        "discord.gg",
        "bit.ly",
    )
    normalized = line.strip(" \t\r\nᐅᐊ<>»«👉📣")
    return normalized.startswith(("http://", "https://")) and any(
        host in normalized for host in promo_hosts
    )
