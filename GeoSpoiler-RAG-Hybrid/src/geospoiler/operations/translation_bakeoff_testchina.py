"""Run an isolated translation bakeoff on saved ``testchina`` inputs.

Writes only under artifacts/translation_bakeoff/. It reuses the project's
translation prompt and does not mutate normalized/enriched output or state.
"""

# ruff: noqa: E402,I001

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT

from geospoiler import config
from geospoiler.evaluation.model_bakeoff.config_loader import ModelConfig
from geospoiler.evaluation.model_bakeoff.run_bakeoff import estimate_cost_usd
from geospoiler.providers.llm_auth import auth_headers
from geospoiler.ingestion.normalizer.translator import TRANSLATOR_SYSTEM_PROMPT


DEFAULT_INPUT_RUN = (
    config.PROJECT_ROOT
    / "artifacts"
    / "enrichment_bakeoff"
    / "testchina_20260616_enrichment_bakeoff"
    / "inputs"
    / "raw"
    / "raw_posts.json"
)
ARTIFACT_ROOT = config.PROJECT_ROOT / "artifacts" / "translation_bakeoff"


@dataclass(frozen=True)
class BakeoffModel:
    id: str
    provider: str
    base_url: str
    api_key: str
    input_price_per_m: float
    output_price_per_m: float

    @property
    def safe_dir(self) -> str:
        return self.id.replace("/", "__").replace(":", "_")

    @property
    def cost_config(self) -> ModelConfig:
        return ModelConfig(
            id=self.id,
            api_id=self.id,
            provider=self.provider,
            family="translation_bakeoff",
            roles=["translation"],
            priority=1,
            input_price_per_m=self.input_price_per_m,
            output_price_per_m=self.output_price_per_m,
        )


def _openrouter_key() -> str:
    return (
        os.getenv("OPENROUTER_API_KEY", "").strip()
        or os.getenv("VISION_API_KEY", "").strip()
        or os.getenv("TRANSCRIPTION_API_KEY", "").strip()
    )


def _deepseek_key() -> str:
    return (
        os.getenv("DEEPSEEK_API_KEY", "").strip()
        or os.getenv("TRANSLATION_API_KEY", "").strip()
        or os.getenv("LLM_API_KEY", "").strip()
    )


def _models() -> list[BakeoffModel]:
    openrouter_key = _openrouter_key()
    deepseek_key = _deepseek_key()
    return [
        BakeoffModel(
            id="openai/gpt-5.4-nano",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_key,
            input_price_per_m=0.20,
            output_price_per_m=1.25,
        ),
        BakeoffModel(
            id="google/gemini-3.1-flash-lite",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_key,
            input_price_per_m=0.25,
            output_price_per_m=1.50,
        ),
        BakeoffModel(
            id="deepseek-v4-flash",
            provider="deepseek",
            base_url="https://api.deepseek.com",
            api_key=deepseek_key,
            input_price_per_m=0.14,
            output_price_per_m=0.28,
        ),
    ]


def _require_model_keys(models: list[BakeoffModel]) -> None:
    missing = sorted({model.provider for model in models if not model.api_key})
    if missing:
        raise RuntimeError(f"Missing API key(s) for provider(s): {', '.join(missing)}")


def _deepseek_v4_options(model: str, base_url: str) -> dict[str, Any]:
    text = f"{model} {base_url}".casefold()
    if "deepseek-v4" not in text and "api.deepseek.com" not in text:
        return {}
    return {"thinking": {"type": "disabled"}}


def _call_translation(model: BakeoffModel, text: str) -> tuple[str, dict[str, Any]]:
    payload = {
        "model": model.id,
        "messages": [
            {"role": "system", "content": TRANSLATOR_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
    }
    payload.update(_deepseek_v4_options(model.id, model.base_url))
    started = time.perf_counter()
    response = requests.post(
        f"{model.base_url}/chat/completions",
        headers=auth_headers(model.api_key, model.base_url),
        json=payload,
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    translated = data["choices"][0]["message"]["content"].strip()
    return translated, {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": estimate_cost_usd(model.cost_config, prompt_tokens, completion_tokens),
        "latency_ms": latency_ms,
    }


def _extract_body(normalized_text: str) -> str:
    lines = normalized_text.splitlines()
    body_lines = [line for line in lines if not line.startswith("[Канал:")]
    return "\n".join(body_lines).strip()


def _language_bucket(text: str) -> str:
    lower = text.casefold()
    ukrainian_markers = ["ї", "є", "ґ", "Україна".casefold(), "Китайська".casefold(), "заявив", "йдеться"]
    if any(marker in lower for marker in ukrainian_markers):
        return "uk_or_mixed"
    return "ru_or_mixed"


def _run(args: argparse.Namespace) -> Path:
    raw_path = Path(args.input).resolve()
    posts = json.loads(raw_path.read_text(encoding="utf-8"))
    posts = sorted(posts, key=lambda item: int(item["message_id"]))[: args.limit]
    if len(posts) != args.limit:
        raise RuntimeError(f"Expected {args.limit} posts, found {len(posts)}")

    run_id = args.run_id or datetime.now(UTC).strftime("testchina_translation_%Y%m%d_%H%M%S")
    run_dir = ARTIFACT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    inputs = []
    for post in posts:
        body = _extract_body(post["normalized_text"])
        inputs.append(
            {
                "message_id": int(post["message_id"]),
                "post_url": post["post_url"],
                "source_language_bucket": _language_bucket(body),
                "input_chars": len(body),
                "input_text": body,
            }
        )
    (run_dir / "inputs.json").write_text(json.dumps(inputs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    models = _models()
    _require_model_keys(models)
    technical_rows = []

    for model in models:
        model_dir = run_dir / "outputs" / model.safe_dir
        model_dir.mkdir(parents=True, exist_ok=True)
        records = []
        for item in inputs:
            try:
                translated, usage = _call_translation(model, item["input_text"])
                error = ""
            except Exception as exc:
                translated = ""
                usage = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "estimated_cost_usd": 0,
                    "latency_ms": 0,
                }
                error = str(exc)
            record = {
                **item,
                "model_id": model.id,
                "translated_chars": len(translated),
                "translated_text": translated,
                "error": error,
                **usage,
            }
            records.append(record)
            (model_dir / f"{item['message_id']}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            time.sleep(args.delay_seconds)

        (model_dir / "outputs.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        technical_rows.append(
            {
                "model_id": model.id,
                "outputs": len(records),
                "errors": sum(1 for record in records if record["error"]),
                "prompt_tokens": sum(int(record["prompt_tokens"]) for record in records),
                "completion_tokens": sum(int(record["completion_tokens"]) for record in records),
                "estimated_cost_usd": round(sum(float(record["estimated_cost_usd"]) for record in records), 8),
                "avg_latency_ms": round(sum(int(record["latency_ms"]) for record in records) / max(1, len(records)), 1),
                "empty_outputs": sum(1 for record in records if not record["translated_text"]),
            }
        )

    (run_dir / "technical_summary.json").write_text(
        json.dumps(technical_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"run_dir": str(run_dir), "summary": technical_rows}, ensure_ascii=False, indent=2))
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT_RUN))
    parser.add_argument("--run-id", default="testchina_20260616_translation_bakeoff")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--delay-seconds", type=float, default=0.2)
    args = parser.parse_args()
    _run(args)


if __name__ == "__main__":
    main()
