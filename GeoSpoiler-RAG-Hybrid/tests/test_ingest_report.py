"""Command-level checks for the normalization outcome journal and ingest-report."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from geospoiler import config
from geospoiler.cli import app
from geospoiler.cli import pipeline as cli
from geospoiler.ingestion import ingest_report
from geospoiler.ingestion.fetcher import state
from geospoiler.ingestion.fetcher.telegram_client import TelegramMessage
from geospoiler.ingestion.ingest_report import combine_problem_items
from geospoiler.ingestion.normalizer import pipeline as normalization
from geospoiler.ingestion.outcomes import record_open_outcome
from tests.test_ingestion_acceptance import ingestion_runtime, message, run_messages  # noqa: F401

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
    assert _report(capsys)["items"] == []
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


def test_journal_collector_skips_outcomes_from_other_stages(ingestion_runtime, capsys):
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
    assert [item["stage"] for item in report["items"]] == ["normalize"]
    assert report["items"][0]["message_id"] == 1
    assert report["items"][0]["category"] == "retry"
