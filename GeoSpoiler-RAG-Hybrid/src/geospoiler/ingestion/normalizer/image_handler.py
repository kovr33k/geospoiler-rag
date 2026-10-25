"""
Image Handler - generates text descriptions of images using Vision API.

Uses OpenAI-compatible Vision API (works with OpenAI GPT-4o, Nvidia NIM, etc.)
"""

import base64
import logging
from pathlib import Path

import requests

from geospoiler import config
from geospoiler.ingestion.normalizer.media_vision import analyze_image
from geospoiler.providers import llm_backend

logger = logging.getLogger("geospoiler.normalizer.image")

VISION_SYSTEM_PROMPT = """You are an OSINT image analyst for a geopolitical research project.
Describe the image concisely in the SAME LANGUAGE as any text visible in the image.
Focus on:
- People (who they appear to be, roles, positions)
- Organizations (logos, symbols, flags)
- Locations (signs, landmarks, maps)
- Events (protests, meetings, military activity, etc.)
- Text visible in the image (OCR - transcribe fully)
- Infographics, charts, data (extract key numbers)
- Screenshots of social media posts (extract the text content)

If the image is a meme, political cartoon, or propaganda material - describe both the visual content and the intended message.
If the image contains no meaningful information (decorative, stock photo), say so briefly."""


def describe_image(image_path: str, caption: str = "") -> str:
    """Describe an image through the configured visual backend."""
    if config.MEDIA_VISION_BACKEND == "luna":
        try:
            return _describe_image_luna(image_path, caption)
        except llm_backend.LLMBackendError as exc:
            logger.warning("Luna image analysis failed for %s: %s", image_path, exc)
            if not config.MEDIA_VISION_FALLBACK_ENABLED:
                return _fallback_description(image_path, caption, reason="Luna visual analysis failed")
            logger.warning("Explicit visual API fallback is enabled for %s", image_path)
    return _describe_image_current(image_path, caption)


def _describe_image_luna(image_path: str, caption: str = "") -> str:
    result = analyze_image(image_path, caption=caption)
    if result.status != "success" or not result.text.strip():
        raise llm_backend.LLMBackendError(result.error or "Luna returned empty image analysis")
    return f"[Изображение]\n{result.text.strip()}"


def _describe_image_current(image_path: str, caption: str = "") -> str:
    """
    Generate a text description of an image using Vision API.

    Args:
        image_path: Path to image file
        caption: Optional Telegram caption for context

    Returns:
        Text description of the image
    """
    auth_keys = _candidate_api_keys()
    if not auth_keys:
        return _fallback_description(image_path, caption)

    try:
        img_path = Path(image_path)
        if not img_path.exists():
            logger.warning(f"Image not found: {image_path}")
            return f"[Изображение: файл не найден - {image_path}]"

        with open(img_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")

        suffix = img_path.suffix.lower()
        mime_map = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif", ".webp": "webp"}
        mime = mime_map.get(suffix, "jpeg")

        user_prompt = "Describe this image for an OSINT knowledge base."
        if caption:
            user_prompt += f"\n\nContext (Telegram caption): {caption}"

        payload = {
            "model": config.VISION_MODEL,
            "messages": [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/{mime};base64,{img_data}",
                            },
                        },
                    ],
                },
            ],
            "max_tokens": 500,
        }

        for i, api_key in enumerate(auth_keys):
            try:
                response = requests.post(
                    f"{config.VISION_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()
                description = data["choices"][0]["message"]["content"].strip()
                return f"[Изображение]\n{description}"
            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)
                if status_code == 403 and i + 1 < len(auth_keys):
                    logger.warning(
                        "Vision API key was forbidden; retrying image request with fallback API key."
                    )
                    continue
                raise

    except requests.Timeout:
        logger.warning(f"Vision API timeout for {image_path}")
        return _fallback_description(image_path, caption)
    except Exception as e:
        logger.error(f"Vision API error for {image_path}: {e}")
        return _fallback_description(image_path, caption)


def _candidate_api_keys() -> list[str]:
    """Return Vision API keys to try, preferring VISION_API_KEY, then LLM_API_KEY."""
    keys = []
    for key in (config.VISION_API_KEY, config.LLM_API_KEY):
        if key and key != "your-api-key-here" and key not in keys:
            keys.append(key)
    return keys


def _fallback_description(
    image_path: str,
    caption: str = "",
    *,
    reason: str = "Vision API не настроен",
) -> str:
    """Fallback when Vision API is not available."""
    result = f"[Изображение: описание недоступно ({reason})]"
    if caption:
        result += f"\n[Подпись: {caption}]"
    return result
