"""Run an isolated enrichment bakeoff on Telegram channel ``testchina``.

This script intentionally writes only under artifacts/enrichment_bakeoff/.
It does not update the main output/normalized, output/enriched, LightRAG
storage, or Telegram progress state.
"""

# ruff: noqa: E402,I001

from __future__ import annotations

import argparse
import asyncio
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
from geospoiler.ingestion.enricher import llm_enricher
from geospoiler.ingestion.enricher.pipeline import _enrich_single_post
from geospoiler.evaluation.model_bakeoff.run_bakeoff import estimate_cost_usd
from geospoiler.evaluation.model_bakeoff.config_loader import ModelConfig
from geospoiler.ingestion.fetcher.telegram_client import TelegramFetcher
from geospoiler.providers.llm_auth import auth_headers
from geospoiler.ingestion.normalizer.pipeline import _build_header
from geospoiler.ingestion.normalizer.pipeline import _build_metadata
from geospoiler.ingestion.normalizer.pipeline import _strip_urls_from_text
from geospoiler.ingestion.normalizer.router import classify
from geospoiler.ingestion.normalizer.text_handler import normalize_text


ARTIFACT_ROOT = config.PROJECT_ROOT / "artifacts" / "enrichment_bakeoff"
TARGET_CHANNEL = "testchina"


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
            family="bakeoff",
            roles=["enrichment"],
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
        or os.getenv("ENRICHMENT_API_KEY", "").strip()
        or os.getenv("LLM_API_KEY", "").strip()
    )


def _models() -> list[BakeoffModel]:
    openrouter_key = _openrouter_key()
    deepseek_key = _deepseek_key()
    return [
        BakeoffModel(
            id="mistralai/mistral-small-2603",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_key,
            input_price_per_m=0.15,
            output_price_per_m=0.60,
        ),
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


async def _prepare(args: argparse.Namespace) -> Path:
    run_id = args.run_id or datetime.now(UTC).strftime("testchina_%Y%m%d_%H%M%S")
    run_dir = ARTIFACT_ROOT / run_id
    input_dir = run_dir / "inputs"
    normalized_dir = input_dir / "normalized" / TARGET_CHANNEL
    raw_dir = input_dir / "raw"
    media_dir = run_dir / "media_cache"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    original_media_cache = config.MEDIA_CACHE_DIR
    config.MEDIA_CACHE_DIR = media_dir

    fetcher = TelegramFetcher()
    await fetcher.connect()
    try:
        channels = await fetcher.discover_channels()
        target = [
            channel for channel in channels
            if channel["title"].strip().casefold() == TARGET_CHANNEL
            or str(channel["name"]).strip().casefold() == TARGET_CHANNEL
        ]
        if len(target) != 1:
            titles = [channel["title"] for channel in channels]
            raise RuntimeError(f"Expected one {TARGET_CHANNEL!r} channel, found {len(target)}. Channels: {titles}")

        raw_messages = []
        async for msg in fetcher.client.iter_messages(target[0]["id"], limit=args.fetch_limit):
            if getattr(msg, "action", None) is None:
                raw_messages.append(msg)

        grouped = fetcher._group_albums(list(reversed(raw_messages)))
        messages = []
        for group in grouped:
            tg_msg = await fetcher._process_message_group(target[0], group)
            if tg_msg and tg_msg.text.strip():
                messages.append(tg_msg)

        messages = sorted(messages, key=lambda item: item.message_id)[-args.post_count:]
        if len(messages) != args.post_count:
            raise RuntimeError(f"Expected {args.post_count} text posts, found {len(messages)}")

        manifest: list[dict[str, Any]] = []
        raw_posts: list[dict[str, Any]] = []
        for msg in messages:
            classified = classify(msg)
            clean_text = normalize_text(msg.text)
            body = _strip_urls_from_text(clean_text, msg.urls)
            if not body.strip():
                body = clean_text

            normalized_text = f"{_build_header(msg)}\n\n{body.strip()}"
            metadata = _build_metadata(msg, classified, [])
            txt_path = normalized_dir / f"{msg.message_id}.txt"
            meta_path = normalized_dir / f"{msg.message_id}.meta.json"
            txt_path.write_text(normalized_text, encoding="utf-8")
            meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

            record = {
                "message_id": msg.message_id,
                "date": msg.date.isoformat() if msg.date else None,
                "post_url": msg.post_url,
                "text_chars": len(msg.text),
                "normalized_chars": len(normalized_text),
                "is_forward": msg.is_forward,
                "forward_from_name": msg.forward_from_name,
                "urls": list(msg.urls),
                "txt_path": str(txt_path.relative_to(config.PROJECT_ROOT)),
                "meta_path": str(meta_path.relative_to(config.PROJECT_ROOT)),
            }
            manifest.append(record)
            raw_posts.append({**record, "raw_text": msg.text, "normalized_text": normalized_text})

        (input_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (raw_dir / "raw_posts.json").write_text(
            json.dumps(raw_posts, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(run_dir)
        return run_dir
    finally:
        config.MEDIA_CACHE_DIR = original_media_cache
        await fetcher.disconnect()


class _UsageRecorder:
    def __init__(self, model: BakeoffModel) -> None:
        self.model = model
        self.calls: list[dict[str, Any]] = []

    def post_with_hard_timeout(self, payload: dict[str, Any]) -> str:
        started = time.perf_counter()
        if config.LLM_DELAY_SECONDS > 0:
            time.sleep(config.LLM_DELAY_SECONDS)

        response = requests.post(
            f"{config.ENRICHMENT_BASE_URL}/chat/completions",
            headers=auth_headers(config.ENRICHMENT_API_KEY, config.ENRICHMENT_BASE_URL),
            json=payload,
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            self.calls.append(
                {
                    "model_id": self.model.id,
                    "status_code": response.status_code,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "estimated_cost_usd": estimate_cost_usd(
                        self.model.cost_config,
                        prompt_tokens,
                        completion_tokens,
                    ),
                    "latency_ms": latency_ms,
                }
            )
            return data["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            self.calls.append(
                {
                    "model_id": self.model.id,
                    "status_code": response.status_code,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "estimated_cost_usd": 0,
                    "latency_ms": latency_ms,
                    "error": str(exc),
                }
            )
            raise


def _run_models(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    normalized_dir = run_dir / "inputs" / "normalized" / TARGET_CHANNEL
    if not normalized_dir.exists():
        raise RuntimeError(f"Normalized input dir not found: {normalized_dir}")

    selected = set(args.models or [])
    models = [model for model in _models() if not selected or model.id in selected or model.safe_dir in selected]
    _require_model_keys(models)

    txt_paths = sorted(normalized_dir.glob("*.txt"), key=lambda path: int(path.stem) if path.stem.isdigit() else path.stem)
    if len(txt_paths) != args.post_count:
        raise RuntimeError(f"Expected {args.post_count} normalized posts, found {len(txt_paths)}")

    original = {
        "ENRICHMENT_MODEL": config.ENRICHMENT_MODEL,
        "ENRICHMENT_BASE_URL": config.ENRICHMENT_BASE_URL,
        "ENRICHMENT_API_KEY": config.ENRICHMENT_API_KEY,
        "LLM_DELAY_SECONDS": config.LLM_DELAY_SECONDS,
    }
    original_post_with_hard_timeout = llm_enricher._post_with_hard_timeout

    try:
        config.LLM_DELAY_SECONDS = args.delay_seconds
        for model in models:
            config.ENRICHMENT_MODEL = model.id
            config.ENRICHMENT_BASE_URL = model.base_url
            config.ENRICHMENT_API_KEY = model.api_key
            recorder = _UsageRecorder(model)
            llm_enricher._post_with_hard_timeout = recorder.post_with_hard_timeout

            model_dir = run_dir / "outputs" / model.safe_dir / TARGET_CHANNEL
            model_dir.mkdir(parents=True, exist_ok=True)
            model_records: list[dict[str, Any]] = []

            for txt_path in txt_paths:
                meta_path = txt_path.with_suffix(".meta.json")
                card = _enrich_single_post(
                    txt_path=txt_path,
                    meta_path=meta_path,
                    channel_name=TARGET_CHANNEL,
                    msg_id=txt_path.stem,
                )
                out_path = model_dir / f"{txt_path.stem}.enriched.json"
                out_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
                model_records.append(
                    {
                        "message_id": txt_path.stem,
                        "output_path": str(out_path.relative_to(config.PROJECT_ROOT)),
                        "summary_chars": len(str(card.get("summary") or "")),
                        "key_facts": len(card.get("key_facts") or []),
                        "topics": len(card.get("topics") or []),
                        "quotes": len(card.get("quotes") or []),
                        "events": len(card.get("events") or []),
                        "triage": card.get("triage"),
                        "content_type": card.get("content_type"),
                    }
                )

            usage_path = run_dir / "outputs" / model.safe_dir / "usage.json"
            usage_path.write_text(json.dumps(recorder.calls, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            manifest_path = run_dir / "outputs" / model.safe_dir / "manifest.json"
            manifest_path.write_text(json.dumps(model_records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"{model.id}: wrote {len(model_records)} cards")
    finally:
        llm_enricher._post_with_hard_timeout = original_post_with_hard_timeout
        config.ENRICHMENT_MODEL = original["ENRICHMENT_MODEL"]
        config.ENRICHMENT_BASE_URL = original["ENRICHMENT_BASE_URL"]
        config.ENRICHMENT_API_KEY = original["ENRICHMENT_API_KEY"]
        config.LLM_DELAY_SECONDS = original["LLM_DELAY_SECONDS"]


def _summarize(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).resolve()
    rows: list[dict[str, Any]] = []
    for model in _models():
        model_root = run_dir / "outputs" / model.safe_dir
        usage_path = model_root / "usage.json"
        manifest_path = model_root / "manifest.json"
        if not manifest_path.exists():
            continue
        cards = json.loads(manifest_path.read_text(encoding="utf-8"))
        usage = json.loads(usage_path.read_text(encoding="utf-8")) if usage_path.exists() else []
        rows.append(
            {
                "model_id": model.id,
                "cards": len(cards),
                "calls": len(usage),
                "errors": sum(1 for call in usage if call.get("error")),
                "prompt_tokens": sum(int(call.get("prompt_tokens") or 0) for call in usage),
                "completion_tokens": sum(int(call.get("completion_tokens") or 0) for call in usage),
                "estimated_cost_usd": round(sum(float(call.get("estimated_cost_usd") or 0) for call in usage), 8),
                "avg_latency_ms": round(
                    sum(int(call.get("latency_ms") or 0) for call in usage) / max(1, len(usage)),
                    1,
                ),
                "empty_summaries": sum(1 for card in cards if not card.get("summary_chars")),
                "total_key_facts": sum(int(card.get("key_facts") or 0) for card in cards),
            }
        )
    summary_path = run_dir / "technical_summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--run-id", default="")
    prepare.add_argument("--fetch-limit", type=int, default=50)
    prepare.add_argument("--post-count", type=int, default=10)

    run_models = sub.add_parser("run-models")
    run_models.add_argument("--run-dir", required=True)
    run_models.add_argument("--post-count", type=int, default=10)
    run_models.add_argument("--delay-seconds", type=float, default=0.5)
    run_models.add_argument("--models", nargs="*", default=[])

    summarize = sub.add_parser("summarize")
    summarize.add_argument("--run-dir", required=True)

    args = parser.parse_args()
    if args.command == "prepare":
        asyncio.run(_prepare(args))
    elif args.command == "run-models":
        _run_models(args)
    elif args.command == "summarize":
        _summarize(args)


if __name__ == "__main__":
    main()
