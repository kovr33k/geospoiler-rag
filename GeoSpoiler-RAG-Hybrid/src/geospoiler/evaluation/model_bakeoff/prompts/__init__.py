"""Prompt construction helpers for model bakeoff cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent

TASK_PROMPTS = {
    "direct_qa": "direct_qa_system.txt",
    "source_preservation": "source_preservation_system.txt",
    "translation_fidelity": "translation_fidelity_system.txt",
    "enrichment_json": "enrichment_json_system.txt",
    "rag_build_extraction": "rag_build_extraction_system.txt",
    "rag_build_tuple_extraction": "rag_build_tuple_extraction_system.txt",
    "fixed_context_synthesis": "fixed_context_synthesis_system.txt",
    "fallback_synth": "fallback_synth_system.txt",
    "script_pack": "fixed_context_synthesis_system.txt",
}


def messages_for_case(
    case: dict[str, Any],
    *,
    prompts_dir: Path = DEFAULT_PROMPTS_DIR,
) -> tuple[list[dict[str, str]], str]:
    task_type = str(case.get("task_type", "direct_qa"))
    system_path = prompts_dir / TASK_PROMPTS.get(task_type, "direct_qa_system.txt")
    system = system_path.read_text(encoding="utf-8")
    user = case_user_prompt(case)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], f"{system}\n\n{user}"


def case_user_prompt(case: dict[str, Any]) -> str:
    if "prompt" in case:
        return str(case["prompt"])
    if case.get("task_type") == "translation_fidelity":
        payload = {
            "language": case.get("language", ""),
            "text": case.get("input", ""),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if "input" in case:
        return json.dumps(case["input"], ensure_ascii=False, indent=2)
    if "context" in case:
        payload = {"question": case.get("question", ""), "context": case.get("context", [])}
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return json.dumps(case, ensure_ascii=False, indent=2)
