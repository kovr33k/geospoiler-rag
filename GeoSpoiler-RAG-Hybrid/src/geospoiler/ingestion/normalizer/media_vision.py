"""Shared Luna visual extraction for native and linked media."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

from geospoiler import config
from geospoiler.ingestion.normalizer.atomic_pair import atomic_write_bytes
from geospoiler.ingestion.normalizer.media_frames import _dedup_frames, _extract_frames, _filter_empty_frames
from geospoiler.ingestion.normalizer.media_paths import MediaPathError, portable_media_path
from geospoiler.providers import llm_backend
from geospoiler.providers.llm_auth import get_openai_api_key

logger = logging.getLogger("geospoiler.normalizer.media_vision")


def _visual_model() -> str:
    """Return the model identity used for the active visual backend."""
    if config.MEDIA_VISION_BACKEND == "current":
        return str(config.VISION_MODEL)
    return llm_backend.active_model_for("default")


def _current_api_keys() -> list[str]:
    keys: list[str] = []
    configured = [config.VISION_API_KEY]
    if config.MEDIA_VISION_FALLBACK_ENABLED:
        configured.append(config.LLM_API_KEY)
    for key in configured:
        if key and key != "your-api-key-here" and key not in keys:
            keys.append(key)
    return keys


def _current_media_analysis(frames: list[Path], prompt: str) -> str:
    """Analyze bounded native media through the explicitly selected API backend."""
    api_keys = _current_api_keys()
    if not api_keys:
        raise llm_backend.LLMBackendError("current_visual_api_not_configured")
    content: list[dict] = [{"type": "text", "text": prompt}]
    for frame in frames:
        if not frame.is_file():
            continue
        try:
            if frame.stat().st_size > config.MEDIA_VISION_MAX_FILE_MB * 1024 * 1024:
                raise llm_backend.LLMBackendError(
                    f"visual frame exceeds {config.MEDIA_VISION_MAX_FILE_MB} MB: {frame}"
                )
        except OSError as exc:
            raise llm_backend.LLMBackendError(f"visual frame stat failed: {exc}") from exc
        suffix = frame.suffix.lower()
        mime = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif", ".webp": "webp"}.get(suffix, "jpeg")
        try:
            encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
        except OSError as exc:
            raise llm_backend.LLMBackendError(f"visual frame read failed: {exc}") from exc
        content.append({"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{encoded}"}})
    if len(content) == 1:
        raise llm_backend.LLMBackendError("no_readable_visual_frames")
    base_url = config.VISION_BASE_URL.rstrip("/")
    payload = {"model": config.VISION_MODEL, "messages": [{"role": "user", "content": content}], "max_tokens": 2000}
    for index, api_key in enumerate(api_keys):
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {get_openai_api_key(api_key, base_url)}", "Content-Type": "application/json"},
                json=payload,
                timeout=config.MEDIA_VISION_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            text = str(data["choices"][0]["message"].get("content") or "").strip()
            if not text:
                raise llm_backend.LLMBackendError("empty_visual_response")
            return text
        except requests.HTTPError as exc:
            status_code = getattr(exc.response, "status_code", None)
            if status_code == 403 and index + 1 < len(api_keys):
                logger.warning("Current visual API key was forbidden; trying fallback key")
                continue
            raise llm_backend.LLMBackendError(f"current_visual_http_error:{exc}") from exc
        except requests.RequestException as exc:
            raise llm_backend.LLMBackendError(f"current_visual_request_error:{exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise llm_backend.LLMBackendError(f"current_visual_payload_error:{exc}") from exc
    raise llm_backend.LLMBackendError("current_visual_api_failed")

@dataclass
class VisualExtractionResult:
    status: str
    text: str = ""
    model: str = ""
    artifact_path: str = ""
    error: str = ""
    frame_count: int = 0
    input_sha256: str = ""
    prompt_version: str = ""


def analyze_image(path: str | Path, *, caption: str = "") -> VisualExtractionResult:
    image_path = _resolve_media_path(path)
    if not image_path.is_file():
        return _failed(image_path, f"file_not_found:{image_path}")
    try:
        digest = _sha256_file(image_path)
    except OSError as exc:
        return _failed(image_path, f"hash_failed:{exc}", digest="")
    prompt = (
        "Проанализируй изображение для русскоязычной OSINT-базы. Полностью перепиши "
        "видимый текст (OCR) и отдельно опиши визуальный контекст, инфографику, карты, "
        "людей и организации только по видимым признакам. Не используй внешние знания и "
        "не выдумывай нечитаемый текст. Оригинальные надписи сохраняй дословно."
    )
    if caption:
        prompt += f"\nПодпись для контекста, не источник новых фактов:\n{caption[:4000]}"
    try:
        if config.MEDIA_VISION_BACKEND == "current":
            text = _current_media_analysis([image_path], prompt)
        else:
            text = llm_backend.complete_media_analysis_sync(
                [image_path],
                prompt=prompt,
                timeout_seconds=config.MEDIA_VISION_TIMEOUT_SECONDS,
            ).strip()
    except llm_backend.LLMBackendError as exc:
        return _failed(image_path, str(exc), digest=digest)
    if not text:
        return _failed(image_path, "empty_visual_response", digest=digest)
    artifact = _write_artifact(
        source=image_path,
        input_sha256=digest,
        text=text,
        frame_count=1,
        status="success",
    )
    return VisualExtractionResult(
        status="success",
        text=text,
        model=_visual_model(),
        artifact_path=str(artifact),
        frame_count=1,
        input_sha256=digest,
        prompt_version=config.MEDIA_VISION_PROMPT_VERSION,
    )


def analyze_video(path: str | Path, *, source_key: str = "video") -> VisualExtractionResult:
    video_path = _resolve_media_path(path)
    if not video_path.is_file():
        return _failed(video_path, f"file_not_found:{video_path}")
    try:
        digest = _sha256_file(video_path)
    except OSError as exc:
        return _failed(video_path, f"hash_failed:{exc}", digest="")
    cached = _read_cached_artifact(video_path, digest)
    if cached:
        return cached

    work_dir = config.MEDIA_CACHE_DIR / "native_video_work" / digest[:16]
    frames_dir = work_dir / "frames"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        raw_frames = _extract_frames(
            video_path,
            frames_dir,
            config.MEDIA_VISION_FRAME_INTERVAL_SEC,
            max_frames=config.MEDIA_VISION_MAX_FRAMES,
        )
        if not raw_frames:
            return _unavailable(video_path, "no_frames_extracted", digest=digest)
        frames = _filter_empty_frames(_dedup_frames(raw_frames))
        frames = frames[: config.MEDIA_VISION_MAX_FRAMES]
        batches = [
            frames[index : index + config.MEDIA_VISION_FRAME_BATCH_SIZE]
            for index in range(0, len(frames), config.MEDIA_VISION_FRAME_BATCH_SIZE)
        ]
        parts: list[str] = []
        for index, batch in enumerate(batches):
            result = analyze_frame_batch(
                batch,
                prompt=(
                    "Это кадры одного видео. Для каждого кадра укажи номер, полностью "
                    "перепиши видимый текст (OCR), затем опиши визуальные факты, действия, "
                    "карты, инфографику и людей только по видимым признакам. Не используй "
                    "внешние знания и не выдумывай нечитаемый текст. Ответ на русском, "
                    "оригинальные надписи сохраняй дословно."
                ),
                source_key=source_key,
            )
            if result.status != "success":
                logger.warning("Native video visual batch %s failed: %s", index, result.error)
                continue
            if result.text:
                parts.append(result.text)
        text = "\n\n".join(parts).strip()
        if not text:
            return _failed(video_path, "all_visual_batches_failed", digest=digest)
        artifact = _write_artifact(
            source=video_path,
            input_sha256=digest,
            text=text,
            frame_count=len(frames),
            status="success",
            source_key=source_key,
        )
        return VisualExtractionResult(
            status="success",
            text=text,
            model=_visual_model(),
            artifact_path=str(artifact),
            frame_count=len(frames),
            input_sha256=digest,
            prompt_version=config.MEDIA_VISION_PROMPT_VERSION,
        )
    except Exception as exc:  # pragma: no cover - defensive media boundary
        logger.warning("Native video visual extraction failed for %s: %s", video_path, exc)
        return _failed(video_path, str(exc), digest=digest)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        parent_dir = work_dir.parent
        try:
            if parent_dir.exists() and not any(parent_dir.iterdir()):
                parent_dir.rmdir()
        except OSError:
            pass


def analyze_frame_batch(
    frames: list[Path],
    *,
    prompt: str,
    source_key: str = "frames",
) -> VisualExtractionResult:
    """Analyze one bounded frame batch and persist its provenance artifact."""
    if not frames:
        return VisualExtractionResult(status="unavailable", error="empty_frame_batch")
    try:
        digest = _sha256_paths(frames)
    except OSError as exc:
        return VisualExtractionResult(
            status="failed",
            model=_visual_model(),
            error=f"hash_failed:{exc}",
            frame_count=len(frames),
            prompt_version=config.MEDIA_VISION_PROMPT_VERSION,
        )
    try:
        if config.MEDIA_VISION_BACKEND == "current":
            text = _current_media_analysis(frames, prompt)
        else:
            text = llm_backend.complete_media_analysis_sync(
                frames,
                prompt=prompt,
                timeout_seconds=config.MEDIA_VISION_TIMEOUT_SECONDS,
            ).strip()
    except llm_backend.LLMBackendError as exc:
        return VisualExtractionResult(
            status="failed",
            model=_visual_model(),
            error=str(exc),
            frame_count=len(frames),
            input_sha256=digest,
            prompt_version=config.MEDIA_VISION_PROMPT_VERSION,
        )
    if not text:
        return VisualExtractionResult(
            status="failed",
            model=_visual_model(),
            error="empty_visual_response",
            frame_count=len(frames),
            input_sha256=digest,
            prompt_version=config.MEDIA_VISION_PROMPT_VERSION,
        )
    artifact = _write_artifact(
        source=frames[0],
        input_sha256=digest,
        text=text,
        frame_count=len(frames),
        status="success",
        source_key=source_key,
    )
    return VisualExtractionResult(
        status="success",
        text=text,
        model=_visual_model(),
        artifact_path=str(artifact),
        frame_count=len(frames),
        input_sha256=digest,
        prompt_version=config.MEDIA_VISION_PROMPT_VERSION,
    )


def _failed(path: Path, error: str, *, digest: str = "") -> VisualExtractionResult:
    if not digest and path.is_file():
        try:
            digest = _sha256_file(path)
        except OSError:
            digest = ""
    return VisualExtractionResult(
        status="failed",
        model=_visual_model(),
        error=error,
        input_sha256=digest,
        prompt_version=config.MEDIA_VISION_PROMPT_VERSION,
    )


def _unavailable(path: Path, error: str, *, digest: str) -> VisualExtractionResult:
    return VisualExtractionResult(
        status="unavailable",
        model=_visual_model(),
        error=error,
        input_sha256=digest,
        prompt_version=config.MEDIA_VISION_PROMPT_VERSION,
    )


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _resolve_media_path(value: str | Path) -> Path:
    """Resolve direct analyzer input; persisted sidecars are checked by backfill."""
    path = Path(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise MediaPathError("media_path_traversal")
    if path.is_absolute() and path.exists():
        return path
    if path.is_absolute():
        parts = list(path.parts)
        try:
            cache_index = next(
                index for index, part in enumerate(parts) if part.casefold() == "media_cache"
            )
        except StopIteration:
            return path
        candidate = config.MEDIA_CACHE_DIR.joinpath(*parts[cache_index + 1 :])
        return candidate if candidate.exists() else path
    return config.resolve_project_path(path)


def _sha256_paths(paths: list[Path]) -> str:
    hasher = hashlib.sha256()
    for path in paths:
        hasher.update(path.name.encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


def _artifact_path(source: Path, digest: str) -> Path:
    safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in source.stem)
    return config.MEDIA_VISUAL_DIR / f"{safe_name}_{digest[:16]}.json"


def _write_artifact(
    *,
    source: Path,
    input_sha256: str,
    text: str,
    frame_count: int,
    status: str,
    source_key: str = "",
) -> Path:
    path = _artifact_path(source, input_sha256)
    payload = {
        "status": status,
        "source": portable_media_path(source),
        "source_key": source_key,
        "input_sha256": input_sha256,
        "model": _visual_model(),
        "prompt_version": config.MEDIA_VISION_PROMPT_VERSION,
        "frame_count": frame_count,
        "frame_interval_seconds": config.MEDIA_VISION_FRAME_INTERVAL_SEC,
        "max_frames": config.MEDIA_VISION_MAX_FRAMES,
        "batch_size": config.MEDIA_VISION_FRAME_BATCH_SIZE,
        "created_at": datetime.now(UTC).isoformat(),
        "text": text,
    }
    atomic_write_bytes(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        suffix="visual-artifact",
    )
    return path


def _read_cached_artifact(source: Path, digest: str) -> VisualExtractionResult | None:
    path = _artifact_path(source, digest)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("status") or "").strip().lower() != "success":
        return None
    if not str(payload.get("text") or "").strip():
        return None
    if (
        payload.get("input_sha256") != digest
        or payload.get("model") != _visual_model()
        or payload.get("prompt_version") != config.MEDIA_VISION_PROMPT_VERSION
        or payload.get("frame_interval_seconds") != config.MEDIA_VISION_FRAME_INTERVAL_SEC
        or payload.get("max_frames") != config.MEDIA_VISION_MAX_FRAMES
        or payload.get("batch_size") != config.MEDIA_VISION_FRAME_BATCH_SIZE
    ):
        return None
    try:
        frame_count = int(payload.get("frame_count") or 0)
    except (TypeError, ValueError):
        return None
    if frame_count < 1 or frame_count > config.MEDIA_VISION_MAX_FRAMES:
        return None
    return VisualExtractionResult(
        status="success",
        text=str(payload.get("text") or ""),
        model=str(payload.get("model") or ""),
        artifact_path=str(path),
        error=str(payload.get("error") or ""),
        frame_count=frame_count,
        input_sha256=digest,
        prompt_version=str(payload.get("prompt_version") or ""),
    )
