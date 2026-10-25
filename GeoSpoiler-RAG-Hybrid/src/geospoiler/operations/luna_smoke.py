"""Run a deliberately small, opt-in smoke test against the authenticated Luna backend."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from geospoiler import config
from geospoiler.contracts.models import LLMPayload
from geospoiler.providers import llm_backend


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Allow at most two real Codex calls; without this flag the script is a dry refusal.",
    )
    return parser


def _check_live_preconditions(confirm_live: bool) -> bool:
    if not confirm_live:
        print("Live smoke skipped. Add --confirm-live to allow two Codex calls.")
        return False
    if llm_backend.active_profile() != "luna":
        print("Live smoke refused: set LLM_PROFILE=luna before using --confirm-live.")
        return False
    try:
        llm_backend.validate_luna_configuration()
    except llm_backend.LLMBackendError as exc:
        print(f"Live smoke refused: {exc}")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not _check_live_preconditions(args.confirm_live):
        return 2

    original_runtime_dir = config.CODEX_RUNTIME_DIR

    try:
        with tempfile.TemporaryDirectory(prefix="geospoiler-luna-smoke-") as temp_dir:
            config.CODEX_RUNTIME_DIR = Path(temp_dir) / "codex-runtime"
            config.CODEX_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

            text = llm_backend.complete_text_sync(
                [
                    {"role": "system", "content": "Return exactly the word LUNA_READY."},
                    {"role": "user", "content": "Respond with the required marker."},
                ],
                role="query",
                timeout_seconds=min(config.CODEX_LLM_TIMEOUT_SECONDS, 60),
            ).strip()
            if "LUNA_READY" not in text:
                raise RuntimeError(f"Unexpected text smoke response: {text[:200]!r}")

            raw_payload = llm_backend.complete_json_sync(
                [
                    {
                        "role": "system",
                        "content": (
                            "Extract the supplied text into the requested enriched payload. "
                            "Do not add facts outside the text."
                        ),
                    },
                    {"role": "user", "content": "The source says: China held a meeting."},
                ],
                role="enrichment",
                schema=LLMPayload.model_json_schema(),
                timeout_seconds=min(config.CODEX_LLM_TIMEOUT_SECONDS, 90),
            )
            payload = LLMPayload.model_validate(raw_payload)
            print(
                "Luna smoke passed: "
                f"model={llm_backend.active_model_for('enrichment')}, "
                f"summary_chars={len(payload.summary)}, "
                "real_calls=2"
            )
            return 0
    finally:
        config.CODEX_RUNTIME_DIR = original_runtime_dir


if __name__ == "__main__":
    raise SystemExit(main())
