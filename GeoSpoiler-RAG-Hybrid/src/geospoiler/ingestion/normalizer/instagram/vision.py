"""Vision OCR, carousel description, and Reel summary helpers."""

import base64
import logging
from pathlib import Path

import requests

from geospoiler import config
from geospoiler.ingestion.normalizer.media_vision import analyze_frame_batch, analyze_image
from geospoiler.providers import llm_backend
from geospoiler.providers.llm_auth import get_openai_api_key

logger = logging.getLogger("geospoiler.normalizer.instagram")

def _ocr_frames_batched(frames: list[Path], batch_size: int = 5) -> list[str]:
    """
    Send frames in batches to MiMo-V2.5 for OCR and visual content analysis.
    Returns list of OCR texts (one per batch).
    """
    if not frames:
        return []

    api_key = config.INSTAGRAM_VISION_API_KEY
    if config.MEDIA_VISION_BACKEND != "luna" and (
        not api_key or api_key == "your-api-key-here"
    ):
        logger.warning("Instagram vision API key not configured")
        return []

    results = []
    effective_batch_size = min(batch_size, config.MEDIA_VISION_MAX_IMAGES)
    for batch_start in range(0, len(frames), effective_batch_size):
        batch = frames[batch_start : batch_start + effective_batch_size]
        batch_text = _ocr_single_batch(batch, batch_start)
        if batch_text:
            results.append(batch_text)

    return results

def _ocr_single_batch(frames: list[Path], start_idx: int) -> str | None:
    """Analyze a frame batch through Luna or the explicitly selected legacy API."""
    if config.MEDIA_VISION_BACKEND == "luna":
        try:
            return _ocr_single_batch_luna(frames, start_idx)
        except llm_backend.LLMBackendError as exc:
            logger.warning("Luna Instagram visual batch failed: %s", exc)
            if not config.MEDIA_VISION_FALLBACK_ENABLED:
                return None
            logger.warning("Explicit Instagram visual API fallback is enabled")
    return _ocr_single_batch_current(frames, start_idx)


def _ocr_single_batch_luna(frames: list[Path], start_idx: int) -> str:
    prompt = (
        "Проанализируй перечисленные кадры Instagram-видео для OSINT-базы.\n"
        "Для каждого кадра укажи его порядковый номер, полностью перепиши видимый текст "
        "(OCR), затем кратко опиши визуальный контент: людей только по видимым признакам, "
        "инфографику, карты, таблицы, логотипы и действия. Не используй внешние знания и "
        "не выдумывай нечитаемый текст. Отвечай на русском языке; оригинальные надписи "
        "сохраняй дословно."
    )
    result = analyze_frame_batch(
        frames,
        prompt=prompt,
        source_key=f"instagram-frame-batch:{start_idx}",
    )
    if result.status != "success":
        raise llm_backend.LLMBackendError(result.error or "Luna visual batch failed")
    return result.text.strip()


def _ocr_single_batch_current(frames: list[Path], start_idx: int) -> str | None:
    """Send a batch of frames to the explicitly selected legacy Vision API."""
    api_key = config.INSTAGRAM_VISION_API_KEY
    base_url = config.INSTAGRAM_VISION_BASE_URL.rstrip("/")

    content_parts = [
        {
            "type": "text",
            "text": (
                "Проанализируй эти кадры из Instagram видео. Для каждого кадра:\n"
                "1. Извлеки ВЕСЬ видимый текст (заголовки, подписи, данные, цитаты)\n"
                "2. Опиши визуальный контент: инфографика, карты, графики, таблицы\n"
                "3. Если на кадре только лицо/фон без текста — кратко отметь\n\n"
                "Формат ответа:\n"
                "Кадр N: [описание и текст]\n\n"
                "Отвечай на русском языке."
            ),
        }
    ]

    for frame_path in frames:
        try:
            img_b64 = base64.b64encode(frame_path.read_bytes()).decode("utf-8")
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                }
            )
        except Exception as e:
            logger.warning(f"Failed to read frame {frame_path}: {e}")

    if len(content_parts) < 2:
        return None

    headers = {
        "Authorization": f"Bearer {get_openai_api_key(api_key, base_url)}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": config.INSTAGRAM_VISION_MODEL,
                "messages": [{"role": "user", "content": content_parts}],
                "max_tokens": 2000,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        text = str(payload["choices"][0]["message"].get("content") or "").strip()
        return text if text else None

    except requests.Timeout:
        logger.warning("Instagram OCR API timeout")
    except Exception as exc:
        logger.warning(f"Instagram OCR failed: {exc}")
    return None


# ─────────────────────── Vision for carousel images ───────────────────────

def _describe_single_image(image_path: Path) -> str:
    """Describe a single carousel image through the configured visual backend."""
    if config.MEDIA_VISION_BACKEND == "luna":
        try:
            result = analyze_image(image_path)
            if result.status == "success" and result.text.strip():
                return result.text.strip()
            raise llm_backend.LLMBackendError(result.error or "Luna returned empty carousel analysis")
        except llm_backend.LLMBackendError as exc:
            logger.warning("Luna carousel image analysis failed for %s: %s", image_path, exc)
            if not config.MEDIA_VISION_FALLBACK_ENABLED:
                return "[Описание недоступно — Luna visual analysis failed]"
            logger.warning("Explicit carousel visual API fallback is enabled")
    return _describe_single_image_current(image_path)


def _describe_single_image_current(image_path: Path) -> str:
    """Describe a single carousel image via the explicitly selected legacy API."""
    api_key = config.INSTAGRAM_VISION_API_KEY
    if not api_key or api_key == "your-api-key-here":
        return "[Описание недоступно — Vision API не настроен]"

    base_url = config.INSTAGRAM_VISION_BASE_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {get_openai_api_key(api_key, base_url)}",
        "Content-Type": "application/json",
    }

    try:
        img_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
        suffix = image_path.suffix.lower()
        mime_map = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif", ".webp": "webp"}
        mime = mime_map.get(suffix, "jpeg")

        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": config.INSTAGRAM_VISION_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Проанализируй это изображение из Instagram поста для OSINT базы знаний.\n"
                                    "Фокусируйся на:\n"
                                    "- Весь видимый текст (OCR — транскрибируй полностью)\n"
                                    "- Инфографика, графики, таблицы (извлеки данные)\n"
                                    "- Карты, локации (опиши что показано)\n"
                                    "- Люди, организации, логотипы\n"
                                    "- Скриншоты постов (извлеки текст)\n\n"
                                    "Отвечай на русском языке."
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/{mime};base64,{img_b64}"},
                            },
                        ],
                    }
                ],
                "max_tokens": 800,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload["choices"][0]["message"].get("content") or "").strip()

    except requests.Timeout:
        return "[Описание недоступно — таймаут Vision API]"
    except Exception as e:
        logger.warning(f"Vision API error for {image_path}: {e}")
        return "[Описание недоступно — ошибка Vision API]"


# ─────────────────────── LLM Summary ───────────────────────

def _summarize_reel(
    caption: str, transcript: str, ocr_text: str, uploader: str
) -> str | None:
    """Generate a concise summary of the Reel using DeepSeek V4."""
    prompt_parts = [
        "Ты аналитик OSINT. Объедини информацию из Instagram Reel в краткое связное описание "
        "для базы знаний. Сохрани ВСЕ факты, имена, даты, цифры, организации.",
    ]
    if uploader:
        prompt_parts.append(f"\nАвтор: @{uploader}")
    if caption:
        prompt_parts.append(f"\nПодпись поста:\n{caption[:2000]}")
    if transcript:
        prompt_parts.append(f"\nТранскрипция аудио:\n{transcript[:3000]}")
    if ocr_text:
        prompt_parts.append(f"\nТекст с экрана (OCR):\n{ocr_text[:2000]}")
    prompt_parts.append(
        "\nНапиши краткое связное описание на русском. "
        "Не повторяй информацию, объедини. Убери мусор и рекламу."
    )
    messages = [{"role": "user", "content": "\n".join(prompt_parts)}]

    if llm_backend.is_luna_role("default"):
        try:
            return (
                llm_backend.complete_text_sync(
                    messages,
                    role="default",
                    timeout_seconds=config.CODEX_LLM_TIMEOUT_SECONDS,
                ).strip()
                or None
            )
        except llm_backend.LLMBackendError as exc:
            if not config.CODEX_FALLBACK_TO_API:
                logger.warning("Codex Instagram summary failed: %s", exc)
                return None
            logger.warning("Codex Instagram summary failed; explicit API fallback is enabled: %s", exc)

    api_key = config.LLM_API_KEY
    if not api_key or api_key == "your-api-key-here":
        return None

    base_url = config.LLM_BASE_URL.rstrip("/")
    headers = {
        "Authorization": f"Bearer {get_openai_api_key(api_key, base_url)}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": config.LLM_MODEL,
                "messages": messages,
                "max_tokens": 1500,
            },
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload["choices"][0]["message"].get("content") or "").strip() or None

    except requests.Timeout:
        logger.warning("LLM summary timeout for Instagram Reel")
    except Exception as exc:
        logger.warning(f"LLM summary failed: {exc}")
    return None


# ─────────────────────── Subtitles (legacy fallback) ───────────────────────
