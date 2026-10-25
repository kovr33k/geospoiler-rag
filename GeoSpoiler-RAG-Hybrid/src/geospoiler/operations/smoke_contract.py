"""Frozen official Late-Fusion activation smoke contract."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from geospoiler.operations.release_identity import canonical_json, release_secret_count, sha256_bytes

CASES = (
    ("SMOKE_ANSWER", "Что в базе говорится о сходстве ультралевых и ультраправых?", "answer"),
    ("SMOKE_SOURCE", "Трамп реально поддерживал Орбана? Дай ссылки на конкретные материалы.", "source"),
    ("SMOKE_YOUTUBE", "Что в длинном видео говорится о проекте «Восточный щит», роли Starlink и применимости российского боевого опыта против НАТО? Собери несколько разных тезисов и дай таймкоды.", "source"),
    ("SMOKE_LF10", "Кто финансирует AfD?", "answer"),
    ("SMOKE_GEO", "Что говорится о ситуации вокруг Тайваня и отношениях Китая с США?", "answer"),
    ("SMOKE_KOREA", "Какие тезисы в базе относятся к Северной Корее и её руководству?", "source"),
    ("SMOKE_BALTIC", "Какие утверждения о странах Балтии подтверждаются материалами базы?", "source"),
    ("SMOKE_FORWARD", "Какие пересказы и цитаты из источников нужно учитывать с осторожностью?", "answer"),
    ("SMOKE_MEDIA", "Назови конкретные материалы, где использованы транскрипция видео или визуальный анализ изображений, и укажи проверяемые URL источников.", "source"),
    ("SMOKE_CITATIONS", "Дай краткий ответ по последним доступным материалам и укажи проверяемые URL.", "answer"),
)


def official_cases_payload() -> list[dict[str, str]]:
    return [{"id": case_id, "question": question, "profile": profile} for case_id, question, profile in CASES]


def official_cases_sha256() -> str:
    payload = json.dumps(official_cases_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


OFFICIAL_CASES_SHA256 = official_cases_sha256()

_ATTEMPT_FIELDS = frozenset(
    {
        "attempt_index",
        "attempt_id",
        "started_at",
        "finished_at",
        "passed",
        "case_count",
        "cases_sha256",
        "errors",
        "previous_attempt_sha256",
        "attempt_sha256",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def smoke_attempt_chain_errors(value: Any) -> tuple[str, ...]:
    """Validate the immutable smoke-attempt chain without exposing its values."""
    if not isinstance(value, list):
        return ("attempt_history_not_list",)
    if release_secret_count(canonical_json(value)):
        return ("attempt_history_contains_secret_material",)

    errors: list[str] = []
    previous_hash: str | None = None
    previous_finished_at: datetime | None = None
    seen_ids: set[str] = set()
    for index, attempt in enumerate(value, start=1):
        prefix = f"attempt_{index}"
        if not isinstance(attempt, dict):
            errors.append(f"{prefix}_not_object")
            continue
        if set(attempt) != _ATTEMPT_FIELDS:
            errors.append(f"{prefix}_fields_invalid")
        attempt_index = attempt.get("attempt_index")
        if (
            not isinstance(attempt_index, int)
            or isinstance(attempt_index, bool)
            or attempt_index != index
        ):
            errors.append(f"{prefix}_index_invalid")
        attempt_id = attempt.get("attempt_id")
        if (
            not isinstance(attempt_id, str)
            or not re.fullmatch(r"attempt-\d{3}", attempt_id)
            or attempt_id != f"attempt-{index:03d}"
            or attempt_id in seen_ids
        ):
            errors.append(f"{prefix}_id_invalid")
        elif isinstance(attempt_id, str):
            seen_ids.add(attempt_id)

        started_at = _aware_timestamp(attempt.get("started_at"))
        finished_at = _aware_timestamp(attempt.get("finished_at"))
        if (
            started_at is None
            or finished_at is None
            or finished_at < started_at
            or (previous_finished_at is not None and started_at < previous_finished_at)
        ):
            errors.append(f"{prefix}_timestamps_invalid")
        if finished_at is not None:
            previous_finished_at = finished_at

        if type(attempt.get("passed")) is not bool:
            errors.append(f"{prefix}_passed_invalid")
        case_count = attempt.get("case_count")
        if (
            not isinstance(case_count, int)
            or isinstance(case_count, bool)
            or case_count < 0
        ):
            errors.append(f"{prefix}_case_count_invalid")
        cases_sha256 = attempt.get("cases_sha256")
        if not isinstance(cases_sha256, str) or not _SHA256_RE.fullmatch(cases_sha256):
            errors.append(f"{prefix}_cases_sha256_invalid")
        attempt_errors = attempt.get("errors")
        if not isinstance(attempt_errors, list) or any(
            not isinstance(item, str) for item in attempt_errors
        ):
            errors.append(f"{prefix}_errors_invalid")
        if attempt.get("previous_attempt_sha256") != previous_hash:
            errors.append(f"{prefix}_previous_hash_invalid")
        attempt_hash = attempt.get("attempt_sha256")
        expected_hash = sha256_bytes(
            canonical_json(
                {key: item for key, item in attempt.items() if key != "attempt_sha256"}
            )
        )
        if not isinstance(attempt_hash, str) or not _SHA256_RE.fullmatch(attempt_hash):
            errors.append(f"{prefix}_hash_invalid")
            previous_hash = None
        elif attempt_hash != expected_hash:
            errors.append(f"{prefix}_hash_mismatch")
            previous_hash = attempt_hash
        else:
            previous_hash = attempt_hash
    return tuple(errors)


def smoke_attempt_chain_is_valid(value: Any) -> bool:
    return not smoke_attempt_chain_errors(value)
