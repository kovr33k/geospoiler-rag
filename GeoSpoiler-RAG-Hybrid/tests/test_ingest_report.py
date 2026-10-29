"""Command-level checks for the normalization outcome journal and ingest-report."""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from geospoiler import config
from geospoiler.cli import app
from geospoiler.cli import pipeline as cli
from geospoiler.contracts.models import EnrichedCardV2
from geospoiler.ingestion import ingest_report
from geospoiler.ingestion.fetcher import state
from geospoiler.ingestion.fetcher.telegram_client import TelegramFetcher, TelegramMessage
from geospoiler.ingestion.ingest_report import combine_problem_items
from geospoiler.ingestion.normalizer import pipeline as normalization
from geospoiler.ingestion.outcomes import record_open_outcome
from geospoiler.providers import llm_backend
from tests.test_ingestion_acceptance import (  # noqa: F401
    ingestion_runtime,
    media_boundaries,
    message,
    run_messages,
    run_raw_media,
)

RUSSIAN = "Сегодня власти сообщили, что новые переговоры состоятся после встречи министров."
SECRET = "supersecret-value"
FAILURE = f"controlled normalization failure https://files.example/a?token={SECRET}"


def _snapshot(root: Path) -> list[tuple[str, int, bytes | None]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rows.append((path.relative_to(root).as_posix(), path.stat().st_mtime_ns, path.read_bytes()))
        else:
            rows.append((path.relative_to(root).as_posix(), path.stat().st_mtime_ns, None))
    return rows


def _watched_snapshot() -> dict[str, list]:
    return {
        "state": _snapshot(config.STATE_DIR),
        "normalized": _snapshot(config.NORMALIZED_DIR),
        "enriched": _snapshot(config.ENRICHED_DIR),
        "review": _snapshot(config.REVIEW_QUEUE_DIR),
    }


def _report(capsys) -> dict:
    app.main(["ingest-report", "--json"])
    return json.loads(capsys.readouterr().out)


def _fail_message(monkeypatch, message_ids):
    real = normalization.normalize_message

    def wrapped(msg, classified=None):
        if msg.message_id in message_ids:
            raise RuntimeError(FAILURE)
        return real(msg, classified)

    monkeypatch.setattr(normalization, "normalize_message", wrapped)
    return real


def test_combine_problem_items_keeps_one_label_by_priority():
    items = [
        {"channel_id": 1, "message_id": 2, "category": "failed", "reason": "failed"},
        {"channel_id": 1, "message_id": 2, "category": "retry", "reason": "retry"},
        {"channel_id": 1, "message_id": 2, "category": "policy", "reason": "policy"},
        {"channel_id": 1, "message_id": None, "category": "policy", "reason": "channel"},
        {"channel_id": 1, "message_id": 2, "category": "review", "reason": "review"},
    ]
    combined = {(item["channel_id"], item["message_id"]): item for item in combine_problem_items(items)}
    assert combined[(1, 2)]["category"] == "review"
    assert combined[(1, 2)]["reason"] == "review"
    assert combined[(1, None)]["category"] == "policy"


def test_normalization_failure_is_reported_until_the_post_succeeds(ingestion_runtime, monkeypatch, capsys, tmp_path):
    real_normalize = _fail_message(monkeypatch, {1})
    run_messages(monkeypatch, [message(RUSSIAN)])
    assert "Проблемных постов: 1. Подробности: python main.py ingest-report" in capsys.readouterr().out
    assert state.get_last_message_id(123) == 0

    journal_path = config.STATE_DIR / "ingestion_outcomes.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    stored = journal["outcomes"]["123:1"]
    assert stored["outcome"] == "failed"
    assert stored["stage"] == "normalize"
    assert stored["attempts"] == 1
    assert SECRET not in journal_path.read_text(encoding="utf-8")
    assert not (config.STATE_DIR / ".ingestion_outcomes.json.tmp").exists()
    first_seen = stored["first_seen"]

    before = _snapshot(tmp_path)
    report = _report(capsys)
    app.main(["ingest-report"])
    console = capsys.readouterr().out
    app.main(["ingest-report", "--json", "--channel", "Acceptance", "--category", "retry"])
    filtered = json.loads(capsys.readouterr().out)
    app.main(["ingest-report", "--json", "--category", "failed"])
    hidden = json.loads(capsys.readouterr().out)
    app.main(["ingest-report", "--json", "--channel", "Other"])
    other = json.loads(capsys.readouterr().out)
    assert _snapshot(tmp_path) == before

    assert report["summary"] == {"total": 1, "by_category": {"review": 0, "retry": 1, "failed": 0, "policy": 0}}
    datetime.fromisoformat(report["generated_at"])
    item = report["items"][0]
    assert item == {
        "channel_id": 123,
        "channel_title": "Acceptance",
        "message_id": 1,
        "post_url": "https://t.me/acceptance/1",
        "stage": "normalize",
        "category": "retry",
        "reason": item["reason"],
        "attempts": 1,
        "first_seen": first_seen,
        "last_seen": item["last_seen"],
        "blocks_cursor": True,
    }
    assert item["reason"].startswith("RuntimeError:")
    assert "controlled normalization failure" in item["reason"]
    assert SECRET not in item["reason"]
    assert "[REDACTED]" in item["reason"] or "%5BREDACTED%5D" in item["reason"]
    assert filtered["items"] == report["items"]
    assert hidden["summary"]["total"] == 0
    assert hidden["items"] == []
    assert other["items"] == []
    assert "ожидает повтора" in console
    assert "https://t.me/acceptance/1" in console
    assert "блокирует курсор канала: да" in console
    assert "ожидает повтора: 1" in console

    state.set_last_message_id(123, "Acceptance", 1)
    assert _report(capsys)["items"][0]["blocks_cursor"] is False
    state.set_last_message_id(123, "Acceptance", 0)

    run_messages(monkeypatch, [message(RUSSIAN)])
    capsys.readouterr()
    again = _report(capsys)
    assert again["items"][0]["attempts"] == 2
    assert again["items"][0]["first_seen"] == first_seen
    assert again["items"][0]["last_seen"] >= first_seen

    monkeypatch.setattr(normalization, "normalize_message", real_normalize)
    run_messages(monkeypatch, [message(RUSSIAN)])
    capsys.readouterr()
    cleared = _report(capsys)
    assert cleared["summary"]["total"] == 0
    assert cleared["items"] == []
    assert "123:1" not in json.loads(journal_path.read_text(encoding="utf-8"))["outcomes"]


def test_repeated_failures_and_filters_are_per_channel(ingestion_runtime, monkeypatch, capsys):
    _fail_message(monkeypatch, {1, 5})
    fetcher = Mock(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        fetch_all_channels=AsyncMock(return_value={
            "Acceptance": [message(RUSSIAN, 1)],
            "Other": [TelegramMessage(
                channel_name="Other", channel_id=456, channel_username="other", message_id=5,
                date=datetime(2026, 10, 25, tzinfo=UTC), text=RUSSIAN,
            )],
        }),
    )
    monkeypatch.setattr(cli, "TelegramFetcher", Mock(return_value=fetcher))
    asyncio.run(cli.cmd_run())
    assert "Проблемных постов: 2. Подробности: python main.py ingest-report" in capsys.readouterr().out

    report = _report(capsys)
    assert [item["channel_title"] for item in report["items"]] == ["Acceptance", "Other"]
    assert all(item["category"] == "retry" and item["blocks_cursor"] is True for item in report["items"])
    assert state.get_last_message_id(123) == 0
    assert state.get_last_message_id(456) == 0

    app.main(["ingest-report", "--json", "--channel", "Other", "--category", "retry"])
    narrowed = json.loads(capsys.readouterr().out)
    assert [item["message_id"] for item in narrowed["items"]] == [5]
    assert narrowed["summary"]["total"] == 1
    assert narrowed["items"][0]["post_url"] == "https://t.me/other/5"

    app.main(["ingest-report", "--channel", "Acceptance", "--category", "policy"])
    console = capsys.readouterr().out
    assert "https://t.me/acceptance/1" not in console
    assert "всего: 0" in console


def test_normalize_command_records_and_closes_outcomes(ingestion_runtime, monkeypatch, capsys, tmp_path):
    real = normalization.normalize_message
    fail = {"on": True}

    def wrapped(msg, classified=None):
        if fail["on"] and msg.message_id == 1:
            raise RuntimeError("normalize command failure")
        return real(msg, classified)

    monkeypatch.setattr(normalization, "normalize_message", wrapped)
    fetcher = Mock(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        fetch_all_channels=AsyncMock(return_value={"Acceptance": [message(RUSSIAN)]}),
    )
    monkeypatch.setattr(cli, "TelegramFetcher", Mock(return_value=fetcher))
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(config, "LOG_DIR", log_dir)

    app.main(["normalize"])
    capsys.readouterr()
    report = _report(capsys)
    assert report["items"][0]["category"] == "retry"
    assert report["items"][0]["attempts"] == 1
    assert "normalize command failure" in report["items"][0]["reason"]
    assert state.get_last_message_id(123) == 0

    fail["on"] = False
    app.main(["normalize"])
    capsys.readouterr()
    cleared = _report(capsys)
    assert cleared["items"] == [{
        "channel_id": 123,
        "channel_title": "Acceptance",
        "message_id": 1,
        "post_url": "https://t.me/acceptance/1",
        "stage": "enrich",
        "category": "retry",
        "reason": "нет актуальной карточки",
        "attempts": None,
        "first_seen": None,
        "last_seen": None,
        "blocks_cursor": False,
    }]
    journal = json.loads((config.STATE_DIR / "ingestion_outcomes.json").read_text(encoding="utf-8"))
    assert "123:1" not in journal["outcomes"]
    assert state.get_last_message_id(123) == 1


def test_successful_run_prints_pointer_without_opening_a_journal(ingestion_runtime, monkeypatch, capsys):
    run_messages(monkeypatch, [message(RUSSIAN)])
    assert "Проблемных постов: 0. Подробности: python main.py ingest-report" in capsys.readouterr().out
    assert not (config.STATE_DIR / "ingestion_outcomes.json").exists()


def test_run_continues_when_the_report_cannot_be_built(ingestion_runtime, monkeypatch, capsys):
    def explode(**kwargs):
        raise OSError("report unavailable")

    monkeypatch.setattr(ingest_report, "build_ingest_report", explode)
    run_messages(monkeypatch, [message(RUSSIAN)])
    output = capsys.readouterr().out
    assert "python main.py ingest-report" not in output
    assert (config.ENRICHED_DIR / "Acceptance/1.enriched.json").is_file()


def test_fetch_and_normalize_journal_outcomes_are_reported(ingestion_runtime, capsys):
    record_open_outcome(
        channel_id=123,
        channel_title="Acceptance",
        channel_username="acceptance",
        message_id=None,
        post_url=None,
        stage="fetch",
        outcome="failed",
        reason="fetch failed",
    )
    record_open_outcome(
        channel_id=123,
        channel_title="Acceptance",
        channel_username="acceptance",
        message_id=1,
        post_url="https://t.me/acceptance/1",
        stage="normalize",
        outcome="failed",
        reason="RuntimeError: normalize failed",
    )
    report = _report(capsys)
    assert [(item["stage"], item["message_id"], item["category"]) for item in report["items"]] == [
        ("normalize", 1, "retry"),
        ("fetch", None, "retry"),
    ]
    assert report["items"][1]["reason"] == "fetch failed"
    assert report["items"][1]["blocks_cursor"] is False
    assert report["summary"]["total"] == 2
    assert report["summary"]["by_category"]["retry"] == 2


BROKEN_ID = 111
HEALTHY_ID = 222
FETCH_FAILURE = f"controlled channel fetch failure https://files.example/a?token={SECRET}"


def _raw_post(text, message_id=1):
    return SimpleNamespace(
        id=message_id,
        date=datetime(2026, 10, 25, tzinfo=UTC),
        message=text,
        action=None,
        forward=None,
        media=None,
        grouped_id=None,
    )


def _install_fetcher(monkeypatch, channels, iterate):
    fetcher = TelegramFetcher.__new__(TelegramFetcher)
    fetcher.client = SimpleNamespace(start=AsyncMock(), disconnect=AsyncMock(), iter_messages=iterate)
    fetcher.discover_channels = AsyncMock(return_value=channels)
    monkeypatch.setattr(cli, "TelegramFetcher", Mock(return_value=fetcher))


def test_fetch_failure_of_one_channel_still_reaches_cards_for_the_other(
    ingestion_runtime, monkeypatch, capsys, caplog,
):
    failing = {"on": True}
    seen = []

    async def iterate(entity, min_id=0, **kwargs):
        seen.append(entity)
        if entity == BROKEN_ID and failing["on"]:
            raise RuntimeError(FETCH_FAILURE)
        raw = _raw_post(RUSSIAN)
        if raw.id <= min_id:
            return
        yield raw

    _install_fetcher(monkeypatch, [
        {"id": BROKEN_ID, "title": "Broken", "name": "broken"},
        {"id": HEALTHY_ID, "title": "Healthy", "name": "healthy"},
    ], iterate)

    asyncio.run(cli.cmd_run())
    assert seen == [BROKEN_ID, HEALTHY_ID]
    run_output = capsys.readouterr().out
    assert "Проблемных постов: 1. Подробности: python main.py ingest-report" in run_output
    fetch_logs = [record.getMessage() for record in caplog.records if record.name == "geospoiler.fetcher"]
    assert any(f"Fetch failed for 'Broken' (id={BROKEN_ID})" in message for message in fetch_logs)
    assert all(SECRET not in message for message in fetch_logs)

    journal_path = config.STATE_DIR / "ingestion_outcomes.json"
    stored = json.loads(journal_path.read_text(encoding="utf-8"))["outcomes"][str(BROKEN_ID)]
    assert stored["outcome"] == "failed"
    assert stored["stage"] == "fetch"
    assert stored["message_id"] is None
    assert stored["attempts"] == 1
    assert SECRET not in journal_path.read_text(encoding="utf-8")
    first_seen = stored["first_seen"]

    report = _report(capsys)
    assert report["summary"] == {"total": 1, "by_category": {"review": 0, "retry": 1, "failed": 0, "policy": 0}}
    item = report["items"][0]
    assert item["channel_id"] == BROKEN_ID
    assert item["channel_title"] == "Broken"
    assert item["message_id"] is None
    assert item["post_url"] is None
    assert item["stage"] == "fetch"
    assert item["category"] == "retry"
    assert item["attempts"] == 1
    assert item["first_seen"] == first_seen
    assert item["blocks_cursor"] is False
    assert item["reason"].startswith("RuntimeError:")
    assert "controlled channel fetch failure" in item["reason"]
    assert SECRET not in item["reason"]
    assert "[REDACTED]" in item["reason"] or "%5BREDACTED%5D" in item["reason"]

    healthy = EnrichedCardV2.model_validate_json(
        (config.ENRICHED_DIR / "Healthy" / "1.enriched.json").read_text(encoding="utf-8")
    )
    assert healthy.provenance.source_id == f"telegram:{HEALTHY_ID}:1"
    assert healthy.provenance.post_url == "https://t.me/healthy/1"
    assert not (config.ENRICHED_DIR / "Broken" / "1.enriched.json").exists()
    assert state.get_last_message_id(BROKEN_ID) == 0
    assert state.get_last_message_id(HEALTHY_ID) == 1

    asyncio.run(cli.cmd_run())
    assert seen == [BROKEN_ID, HEALTHY_ID, BROKEN_ID, HEALTHY_ID]
    capsys.readouterr()
    again = _report(capsys)
    assert again["summary"]["total"] == 1
    assert again["items"][0]["attempts"] == 2
    assert again["items"][0]["first_seen"] == first_seen
    assert again["items"][0]["message_id"] is None
    assert again["items"][0]["category"] == "retry"
    assert again["items"][0]["channel_id"] == BROKEN_ID
    assert state.get_last_message_id(BROKEN_ID) == 0
    assert (config.ENRICHED_DIR / "Healthy" / "1.enriched.json").is_file()

    failing["on"] = False
    asyncio.run(cli.cmd_run())
    assert "Проблемных постов: 0. Подробности: python main.py ingest-report" in capsys.readouterr().out
    assert _report(capsys)["items"] == []
    assert str(BROKEN_ID) not in json.loads(journal_path.read_text(encoding="utf-8"))["outcomes"]
    recovered = EnrichedCardV2.model_validate_json(
        (config.ENRICHED_DIR / "Broken" / "1.enriched.json").read_text(encoding="utf-8")
    )
    assert recovered.provenance.source_id == f"telegram:{BROKEN_ID}:1"
    assert recovered.provenance.post_url == "https://t.me/broken/1"
    assert state.get_last_message_id(BROKEN_ID) == 1


def test_channel_discovery_failure_still_stops_the_run(ingestion_runtime, monkeypatch):
    fetched = []

    async def iterate(entity, **kwargs):
        fetched.append(entity)
        yield _raw_post(RUSSIAN)

    fetcher = TelegramFetcher.__new__(TelegramFetcher)
    fetcher.client = SimpleNamespace(start=AsyncMock(), disconnect=AsyncMock(), iter_messages=iterate)
    fetcher.discover_channels = AsyncMock(side_effect=RuntimeError("folder lookup failed"))
    monkeypatch.setattr(cli, "TelegramFetcher", Mock(return_value=fetcher))

    with pytest.raises(RuntimeError, match="folder lookup failed"):
        asyncio.run(cli.cmd_run())

    assert fetched == []
    assert not (config.STATE_DIR / "ingestion_outcomes.json").exists()
    assert not list(config.ENRICHED_DIR.rglob("*.enriched.json"))


SCREENSHOT = "На скриншоте приведены данные о маршруте поставок."
DOCUMENT = "В документе указаны подробности маршрута поставок."


@pytest.mark.parametrize("failure", ["download", "analysis"])
def test_selected_photo_failure_is_reported_as_failed(media_boundaries, monkeypatch, capsys, tmp_path, failure):
    vision, _, _ = media_boundaries
    if failure == "analysis":
        vision.side_effect = llm_backend.LLMBackendError("controlled analysis failure")
    run_raw_media(monkeypatch, "photo", SCREENSHOT, download_error=failure == "download")
    assert "Проблемных постов: 1. Подробности: python main.py ingest-report" in capsys.readouterr().out

    before = _snapshot(tmp_path)
    report = _report(capsys)
    assert _snapshot(tmp_path) == before
    assert report["summary"]["by_category"]["failed"] == 1
    item = report["items"][0]
    assert item["channel_id"] == 123
    assert item["channel_title"] == "Acceptance"
    assert item["message_id"] == 1
    assert item["post_url"] == "https://t.me/acceptance/1"
    assert item["stage"] == "media"
    assert item["category"] == "failed"
    assert item["attempts"] is None
    expected = "download_failed" if failure == "download" else "controlled analysis failure"
    assert item["reason"] == f"image: {expected}"
    assert item["blocks_cursor"] is False

    app.main(["ingest-report", "--json", "--category", "policy"])
    assert json.loads(capsys.readouterr().out)["items"] == []


@pytest.mark.parametrize("caption", ["", "www.example.com"])
def test_review_item_outranks_media_policy_skip(media_boundaries, monkeypatch, capsys, caption):
    # An empty caption is queued as uninformative and a link-only caption as an
    # external link; that review item wins over the video policy skip.
    run_raw_media(monkeypatch, "video", caption)
    capsys.readouterr()
    report = _report(capsys)
    assert [(item["message_id"], item["category"]) for item in report["items"]] == [(1, "review")]
    assert report["summary"]["by_category"]["policy"] == 0


@pytest.mark.parametrize("caption", ["🔥", "#2026news"])
def test_video_without_meaningful_caption_is_policy(media_boundaries, monkeypatch, capsys, caption):
    run_raw_media(monkeypatch, "video", caption)
    capsys.readouterr()
    report = _report(capsys)
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["stage"] == "media"
    assert item["category"] == "policy"
    assert item["channel_title"] == "Acceptance"
    assert item["message_id"] == 1
    assert item["post_url"] == "https://t.me/acceptance/1"
    assert item["reason"] == "video: вложение не анализировалось по выборочной политике"
    assert report["summary"]["by_category"]["failed"] == 0

    app.main(["ingest-report", "--category", "policy"])
    console = capsys.readouterr().out
    assert "пропуск по политике" in console
    assert "https://t.me/acceptance/1" in console
    assert "ошибка обработки: 0" in console


@pytest.mark.parametrize(("kind", "caption"), [
    ("video", RUSSIAN),
    ("video", "Смотрите"),
    ("photo", RUSSIAN),
    ("audio", RUSSIAN),
    ("voice", RUSSIAN),
    ("document", RUSSIAN),
])
def test_meaningful_caption_does_not_report_unselected_attachment(media_boundaries, monkeypatch, capsys, kind, caption):
    run_raw_media(monkeypatch, kind, caption)
    capsys.readouterr()
    assert _report(capsys)["items"] == []


def test_file_above_size_limit_is_policy_not_failure(media_boundaries, monkeypatch, capsys):
    monkeypatch.setattr(config, "MEDIA_CAPTURE_MAX_BYTES", 1024)
    run_raw_media(monkeypatch, "document", DOCUMENT, size=1025)
    capsys.readouterr()
    report = _report(capsys)
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["stage"] == "media"
    assert item["category"] == "policy"
    assert item["message_id"] == 1
    assert "document" in item["reason"]
    assert "file_size_exceeds_limit:1025>1024" in item["reason"]

    app.main(["ingest-report", "--json", "--category", "failed"])
    hidden = json.loads(capsys.readouterr().out)
    assert hidden["items"] == []
    assert hidden["summary"]["by_category"]["failed"] == 0


def test_disabled_media_capture_is_policy_not_failure(media_boundaries, monkeypatch, capsys):
    monkeypatch.setattr(config, "MEDIA_CAPTURE_ENABLED", False)
    run_raw_media(monkeypatch, "photo", SCREENSHOT)
    capsys.readouterr()
    item = _report(capsys)["items"][0]
    assert item["stage"] == "media"
    assert item["category"] == "policy"
    assert item["reason"] == "image: MEDIA_CAPTURE_ENABLED=false"

    app.main(["ingest-report", "--json", "--category", "failed"])
    assert json.loads(capsys.readouterr().out)["items"] == []


def test_successful_attachment_is_absent_from_the_report(media_boundaries, monkeypatch, capsys):
    run_raw_media(monkeypatch, "photo", SCREENSHOT)
    capsys.readouterr()
    report = _report(capsys)
    assert report["items"] == []
    assert report["summary"] == {"total": 0, "by_category": {"review": 0, "retry": 0, "failed": 0, "policy": 0}}


def test_failed_text_extraction_is_reported(media_boundaries, monkeypatch, capsys):
    run_raw_media(monkeypatch, "document", DOCUMENT, payload=b"\xff")
    capsys.readouterr()
    item = _report(capsys)["items"][0]
    assert item["stage"] == "media"
    assert item["category"] == "failed"
    assert item["reason"].startswith("document: ")
    assert "codec" in item["reason"] or "decode" in item["reason"]


def test_several_policy_attachments_make_one_item(media_boundaries, monkeypatch, capsys):
    run_raw_media(monkeypatch, "video", "🔥", kinds=["video", "document"])
    capsys.readouterr()
    report = _report(capsys)
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["category"] == "policy"
    assert item["stage"] == "media"
    assert item["message_id"] == 2
    assert item["post_url"] == "https://t.me/acceptance/2"
    assert item["reason"] == "video, document (2): вложение не анализировалось по выборочной политике"


def test_several_failed_attachments_make_one_item(media_boundaries, monkeypatch, capsys):
    run_raw_media(monkeypatch, "photo", "🔥", kinds=["photo", "photo"], download_error=True)
    capsys.readouterr()
    report = _report(capsys)
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["category"] == "failed"
    assert item["stage"] == "media"
    assert item["message_id"] == 2
    assert item["reason"] == "image (2): download_failed"


def test_failed_attachment_outranks_policy_skip_on_the_same_post(media_boundaries, monkeypatch, capsys):
    monkeypatch.setattr(config, "MEDIA_CAPTURE_MAX_BYTES", 1024)
    run_raw_media(
        monkeypatch,
        "photo",
        "На скриншоте и в документе указаны подробности маршрута поставок.",
        download_error=True,
        kinds=["photo", "document"],
        size=2048,
    )
    capsys.readouterr()
    report = _report(capsys)
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["category"] == "failed"
    assert item["stage"] == "media"
    assert item["message_id"] == 2
    assert item["reason"] == "image: download_failed"
    assert "file_size_exceeds_limit" not in item["reason"]


WEB = "https://example.com/negotiations"


def test_enrichment_failure_is_retry_while_its_review_item_exists(ingestion_runtime, monkeypatch, capsys):
    def fail_enrichment(messages, *, role, **kwargs):
        raise RuntimeError("controlled enrichment failure")

    monkeypatch.setattr(llm_backend, "complete_json_sync", fail_enrichment)
    run_messages(monkeypatch, [message(RUSSIAN)])
    output = capsys.readouterr().out
    assert "Проблемных постов: 1. Подробности: python main.py ingest-report" in output
    pending = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in config.REVIEW_QUEUE_DIR.glob("*.json")
    ]
    assert any(item.get("review_type") == "enrichment_failure" and item.get("status") == "pending" for item in pending)

    before = _watched_snapshot()
    report = _report(capsys)
    assert _watched_snapshot() == before
    assert report["summary"] == {"total": 1, "by_category": {"review": 0, "retry": 1, "failed": 0, "policy": 0}}
    assert report["items"] == [{
        "channel_id": 123,
        "channel_title": "Acceptance",
        "message_id": 1,
        "post_url": "https://t.me/acceptance/1",
        "stage": "enrich",
        "category": "retry",
        "reason": "нет актуальной карточки",
        "attempts": None,
        "first_seen": None,
        "last_seen": None,
        "blocks_cursor": False,
    }]


def test_external_link_is_review_and_a_ready_card_is_omitted(ingestion_runtime, monkeypatch, capsys):
    run_messages(monkeypatch, [
        message(RUSSIAN, 1),
        message(f"{RUSSIAN} {WEB}", 2, urls=[WEB]),
    ])
    assert "Проблемных постов: 1. Подробности: python main.py ingest-report" in capsys.readouterr().out
    assert (config.ENRICHED_DIR / "Acceptance/1.enriched.json").is_file()
    assert (config.ENRICHED_DIR / "Acceptance/2.enriched.json").is_file()

    before = _watched_snapshot()
    report = _report(capsys)
    app.main(["ingest-report"])
    console = capsys.readouterr().out
    app.main(["ingest-report", "--json", "--category", "review"])
    reviewed = json.loads(capsys.readouterr().out)
    app.main(["ingest-report", "--json", "--category", "retry"])
    hidden = json.loads(capsys.readouterr().out)
    assert _watched_snapshot() == before

    assert report["summary"] == {"total": 1, "by_category": {"review": 1, "retry": 0, "failed": 0, "policy": 0}}
    assert report["items"] == [{
        "channel_id": 123,
        "channel_title": "Acceptance",
        "message_id": 2,
        "post_url": "https://t.me/acceptance/2",
        "stage": "review",
        "category": "review",
        "reason": "external_link: External web link",
        "attempts": None,
        "first_seen": None,
        "last_seen": None,
        "blocks_cursor": False,
    }]
    assert reviewed["items"] == report["items"]
    assert hidden["items"] == []
    assert "ожидает ревью" in console
    assert "https://t.me/acceptance/2" in console
    assert "https://t.me/acceptance/1" not in console


def test_external_link_without_a_card_keeps_one_review_label(ingestion_runtime, monkeypatch, capsys):
    run_messages(monkeypatch, [message(f"{RUSSIAN} {WEB}", urls=[WEB])])
    capsys.readouterr()
    (config.ENRICHED_DIR / "Acceptance/1.enriched.json").unlink()

    before = _watched_snapshot()
    report = _report(capsys)
    assert _watched_snapshot() == before
    assert report["summary"] == {"total": 1, "by_category": {"review": 1, "retry": 0, "failed": 0, "policy": 0}}
    assert report["items"][0]["category"] == "review"
    assert report["items"][0]["stage"] == "review"
    assert report["items"][0]["reason"] == "external_link: External web link"
    assert report["items"][0]["channel_id"] == 123
    assert report["items"][0]["message_id"] == 1


def test_unstable_card_is_waiting_for_enrichment(ingestion_runtime, monkeypatch, capsys):
    run_messages(monkeypatch, [message(RUSSIAN)])
    capsys.readouterr()
    card_file = config.ENRICHED_DIR / "Acceptance/1.enriched.json"
    card = json.loads(card_file.read_text(encoding="utf-8"))
    card["quality_flags"] = ["extraction_unstable"]
    card_file.write_text(json.dumps(card), encoding="utf-8")

    before = _watched_snapshot()
    report = _report(capsys)
    assert _watched_snapshot() == before
    assert [item["message_id"] for item in report["items"]] == [1]
    assert report["items"][0]["category"] == "retry"
    assert report["items"][0]["stage"] == "enrich"
    assert report["items"][0]["reason"] == "нет актуальной карточки"


def test_report_does_not_stamp_a_legacy_media_contract(ingestion_runtime, monkeypatch, capsys):
    run_messages(monkeypatch, [message(RUSSIAN)])
    capsys.readouterr()
    card_file = config.ENRICHED_DIR / "Acceptance/1.enriched.json"
    card = json.loads(card_file.read_text(encoding="utf-8"))
    card["media_contract_version"] = "legacy-contract"
    card_file.write_text(json.dumps(card), encoding="utf-8")

    before = _watched_snapshot()
    report = _report(capsys)
    assert _watched_snapshot() == before
    assert report["items"] == []


def test_review_without_metadata_keeps_empty_channel_id(ingestion_runtime, capsys):
    queue = config.REVIEW_QUEUE_DIR
    (queue / "missing_external.json").write_text(json.dumps({
        "review_type": "external_link",
        "url": "https://example.com/orphan",
        "channel": "Missing",
        "message_id": 9,
        "reason": "",
        "status": "pending",
    }), encoding="utf-8")
    (queue / "missing_failure.json").write_text(json.dumps({
        "review_type": "enrichment_failure",
        "url": "https://t.me/missing/8",
        "channel": "Missing",
        "message_id": 8,
        "reason": "Enrichment failed after bounded retry",
        "status": "pending",
    }), encoding="utf-8")
    (queue / "done.json").write_text(json.dumps({
        "review_type": "external_link",
        "url": "https://example.com/done",
        "channel": "Missing",
        "message_id": 7,
        "reason": "External web link",
        "status": "processed",
    }), encoding="utf-8")

    before = _watched_snapshot()
    report = _report(capsys)
    assert _watched_snapshot() == before
    assert report["items"] == [{
        "channel_id": None,
        "channel_title": "Missing",
        "message_id": 9,
        "post_url": None,
        "stage": "review",
        "category": "review",
        "reason": "external_link: https://example.com/orphan",
        "attempts": None,
        "first_seen": None,
        "last_seen": None,
        "blocks_cursor": False,
    }]


def test_report_leaves_an_interrupted_normalized_pair_unchanged(ingestion_runtime, monkeypatch, capsys):
    path = normalization._save_normalized(
        message(RUSSIAN),
        "Original text",
        {
            "channel_name": "Acceptance",
            "channel_id": 123,
            "message_id": 1,
            "post_url": "https://t.me/acceptance/1",
            "date": "2026-01-01",
        },
    )
    meta_path = path.with_suffix(".meta.json")
    old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    real_replace = os.replace

    def interrupt(source, target):
        if Path(target) == meta_path:
            raise KeyboardInterrupt("controlled interruption before sidecar publication")
        return real_replace(source, target)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            normalization._save_normalized(
                message(RUSSIAN),
                "Replacement text",
                {**old_meta, "date": "2026-10-25"},
            )

    journal = meta_path.with_name(f".{meta_path.name}.normalized.journal")
    assert journal.is_file()
    before = _watched_snapshot()
    assert any(row[0].endswith(journal.name) for row in before["normalized"])
    report = _report(capsys)
    assert _watched_snapshot() == before
    assert report["items"] == [{
        "channel_id": 123,
        "channel_title": "Acceptance",
        "message_id": 1,
        "post_url": "https://t.me/acceptance/1",
        "stage": "enrich",
        "category": "retry",
        "reason": "нет актуальной карточки",
        "attempts": None,
        "first_seen": None,
        "last_seen": None,
        "blocks_cursor": True,
    }]


def test_review_keeps_unsanitized_channel_title(ingestion_runtime, capsys):
    msg = TelegramMessage(
        channel_name="World/News",
        channel_id=789,
        channel_username="worldnews",
        message_id=4,
        date=datetime(2026, 10, 25, tzinfo=UTC),
        text=RUSSIAN,
    )
    normalization._save_normalized(msg, RUSSIAN, {
        "channel_name": "World/News",
        "channel_id": 789,
        "channel_username": "worldnews",
        "message_id": 4,
        "post_url": "https://t.me/worldnews/4",
    })
    (config.REVIEW_QUEUE_DIR / "World_News_4_uninformative.json").write_text(json.dumps({
        "review_type": "uninformative",
        "url": "",
        "channel": "World_News",
        "message_id": 4,
        "reason": "insufficient content",
        "status": "pending",
    }), encoding="utf-8")

    before = _watched_snapshot()
    app.main(["ingest-report", "--json", "--channel", "World/News"])
    report = json.loads(capsys.readouterr().out)
    app.main(["ingest-report", "--json", "--channel", "World_News"])
    hidden = json.loads(capsys.readouterr().out)
    assert _watched_snapshot() == before
    assert report["summary"]["total"] == 1
    item = report["items"][0]
    assert item["channel_title"] == "World/News"
    assert item["category"] == "review"
    assert item["stage"] == "review"
    assert item["channel_id"] == 789
    assert item["message_id"] == 4
    assert item["reason"] == "uninformative: insufficient content"
    assert hidden["items"] == []
