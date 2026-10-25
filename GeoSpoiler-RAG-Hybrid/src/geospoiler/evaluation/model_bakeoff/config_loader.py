"""Restricted model registry loader for model bakeoff runs.

The project intentionally avoids adding a YAML dependency for this small
registry. The parser supports the limited `models: - key: value` shape used by
`eval/model_bakeoff/models.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelConfig:
    """One model entry from the bakeoff registry."""

    id: str
    provider: str
    family: str
    roles: tuple[str, ...]
    priority: int
    input_price_per_m: float
    output_price_per_m: float
    api_id: str = ""
    base_url_env: str = ""
    api_key_env: str = ""


def load_model_registry(path: str | Path) -> list[ModelConfig]:
    """Load a restricted YAML model registry."""
    entries = _parse_restricted_yaml(Path(path))
    models = entries.get("models")
    if not isinstance(models, list):
        raise ValueError("Model registry must contain a top-level models list.")
    return [_model_from_mapping(item) for item in models]


def _model_from_mapping(item: dict[str, Any]) -> ModelConfig:
    required = [
        "id",
        "provider",
        "family",
        "roles",
        "priority",
        "input_price_per_m",
        "output_price_per_m",
    ]
    missing = [key for key in required if key not in item]
    if missing:
        raise ValueError(f"Model entry is missing required fields: {', '.join(missing)}")
    roles = item["roles"]
    if not isinstance(roles, list):
        raise ValueError(f"Model {item.get('id', '<unknown>')} roles must be a list.")
    return ModelConfig(
        id=str(item["id"]),
        provider=str(item["provider"]),
        family=str(item["family"]),
        roles=tuple(str(role) for role in roles),
        priority=int(item["priority"]),
        input_price_per_m=float(item["input_price_per_m"]),
        output_price_per_m=float(item["output_price_per_m"]),
        api_id=str(item.get("api_id", "")),
        base_url_env=str(item.get("base_url_env", "")),
        api_key_env=str(item.get("api_key_env", "")),
    )


def _parse_restricted_yaml(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key = ""
    current_item: dict[str, Any] | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and line.endswith(":"):
            current_key = line[:-1].strip()
            result[current_key] = []
            current_item = None
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if not current_key:
                raise ValueError("List item found before a top-level key.")
            current_item = {}
            result[current_key].append(current_item)
            remainder = stripped[2:].strip()
            if remainder:
                key, value = _split_key_value(remainder)
                current_item[key] = _parse_value(value)
            continue
        if current_item is None:
            raise ValueError(f"Nested mapping found outside a list item: {line}")
        key, value = _split_key_value(stripped)
        current_item[key] = _parse_value(value)

    return result


def _split_key_value(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ValueError(f"Expected key: value line, got: {text}")
    key, value = text.split(":", 1)
    return key.strip(), value.strip()


def _parse_value(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(part.strip()) for part in inner.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value
