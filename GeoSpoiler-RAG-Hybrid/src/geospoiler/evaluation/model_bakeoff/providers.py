"""Provider routing and chat-completion calls for model bakeoff."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import requests

from geospoiler import config
from geospoiler.evaluation.model_bakeoff.config_loader import ModelConfig
from geospoiler.providers.llm_auth import auth_headers

BaseUrlFunc = Callable[[ModelConfig], str]
ApiKeyFunc = Callable[[ModelConfig], str]
PostFunc = Callable[[str, str, dict[str, Any]], requests.Response]


def call_chat_completion(
    model: ModelConfig,
    messages: list[dict[str, str]],
    force_json: bool | None = None,
    *,
    base_url_func: BaseUrlFunc | None = None,
    api_key_func: ApiKeyFunc | None = None,
    post_func: PostFunc | None = None,
) -> tuple[str, dict[str, Any]]:
    base_url_resolver = base_url_func or base_url_for
    api_key_resolver = api_key_func or api_key_for
    post = post_func or post_chat_completion

    base_url = base_url_resolver(model)
    api_key = api_key_resolver(model)
    if not api_key:
        raise RuntimeError(f"No API key configured for provider {model.provider}.")
    payload = {
        "model": getattr(model, "api_id", "") or model.id,
        "messages": messages,
        "temperature": 0,
    }
    payload.update(provider_options(model, base_url))
    json_requested = force_json if force_json is not None else any("json" in message["content"].casefold() for message in messages)
    if json_requested:
        payload["response_format"] = {"type": "json_object"}

    response = post(base_url, api_key, payload)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        if response.status_code == 400 and "response_format" in payload:
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            response = post(base_url, api_key, fallback_payload)
            response.raise_for_status()
        else:
            raise exc
    data = response.json()
    return str(data["choices"][0]["message"]["content"] or "").strip(), data.get("usage") or {}


def post_chat_completion(base_url: str, api_key: str, payload: dict[str, Any]) -> requests.Response:
    return requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers=auth_headers(api_key, base_url),
        json=payload,
        timeout=120,
    )


def provider_options(model: ModelConfig, base_url: str) -> dict[str, Any]:
    text = f"{model.provider} {model.id} {getattr(model, 'api_id', '') or ''} {base_url}".casefold()
    if config.LLM_REASONING_EFFORT and model.provider.casefold() == "openrouter":
        return {"reasoning_effort": config.LLM_REASONING_EFFORT}
    if config.LLM_REASONING_EFFORT:
        return {}
    if "deepseek-v4" not in text and "api.deepseek.com" not in text:
        return {}
    return {"thinking": {"type": "disabled"}}


def base_url_for(model: ModelConfig) -> str:
    if model.base_url_env and os.getenv(model.base_url_env):
        return str(os.getenv(model.base_url_env))
    provider = model.provider.casefold()
    if provider == "openrouter":
        return os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_BASE_URL") or config.LLM_BASE_URL
    if provider == "google":
        return os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
    if provider == "mistral":
        return os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
    return config.LLM_BASE_URL


def api_key_for(model: ModelConfig) -> str:
    if model.api_key_env and os.getenv(model.api_key_env):
        return str(os.getenv(model.api_key_env))
    provider = model.provider.casefold()
    if provider == "openrouter":
        return os.getenv("OPENROUTER_API_KEY", "")
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_API_KEY") or config.LLM_API_KEY
    if provider == "google":
        return os.getenv("GEMINI_API_KEY", "")
    if provider == "mistral":
        return os.getenv("MISTRAL_API_KEY", "")
    return config.LLM_API_KEY
