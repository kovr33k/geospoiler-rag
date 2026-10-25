"""Authentication helpers for OpenAI-compatible LLM endpoints."""

from __future__ import annotations


def get_openai_api_key(api_key: str, base_url: str) -> str:
    """Return the configured static API key for OpenAI-compatible clients."""
    return api_key


def auth_headers(api_key: str, base_url: str) -> dict[str, str]:
    """Return authorization headers for requests-based callers."""
    return {
        "Authorization": f"Bearer {get_openai_api_key(api_key, base_url)}",
        "Content-Type": "application/json",
    }
