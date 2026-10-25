"""Command-level ingestion acceptance with real cards and local indexes."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from geospoiler import config
from geospoiler.cli import pipeline as cli
from geospoiler.contracts.models import EnrichedCardV2
from geospoiler.ingestion.enricher import pipeline as enrichment
from geospoiler.ingestion.fetcher import state
from geospoiler.ingestion.fetcher.telegram_client import TelegramMessage
from geospoiler.ingestion.normalizer import pipeline as normalization
from geospoiler.ingestion.normalizer.pipeline import _save_normalized
from geospoiler.ingestion.normalizer.youtube_handler import YouTubeArtifact
from geospoiler.providers import llm_backend
from geospoiler.retrieval.cards.card_fts import rebuild_card_index, rebuild_youtube_segment_index, search_card_index
from geospoiler.sources.registry import rebuild_source_registry, resolve_source

RUSSIAN = "Сегодня власти сообщили, что новые переговоры состоятся после встречи министров."


@pytest.fixture
def ingestion_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    for name, relative in {
        "OUTPUT_DIR": "data", "NORMALIZED_DIR": "data/normalized", "ENRICHED_DIR": "data/enriched",
        "YOUTUBE_NORMALIZED_DIR": "data/youtube", "YOUTUBE_SEGMENTS_DIR": "data/segments",
        "YOUTUBE_CHECKPOINT_DIR": "state/youtube", "STATE_DIR": "state",
        "REVIEW_QUEUE_DIR": "data/review", "MEDIA_CACHE_DIR": "data/media",
        "TRANSCRIPTION_DIR": "data/transcripts", "MEDIA_VISUAL_DIR": "data/visuals",
        "CARD_FTS_DB_PATH": "indexes/cards.sqlite", "SOURCE_REGISTRY_DB_PATH": "indexes/sources.sqlite",
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.suffix:
            path.mkdir(exist_ok=True)
        monkeypatch.setattr(config, name, path)
    monkeypatch.setattr(state, "PROGRESS_FILE", config.STATE_DIR / "progress.json")
    monkeypatch.setattr(enrichment, "_PROGRESS_FILE", config.STATE_DIR / "enrichment_progress.json")
    monkeypatch.setattr(config, "WIKI_ENABLED", False)
    monkeypatch.setattr(config, "LLM_PROFILE", "luna")
    monkeypatch.setattr(cli, "rebuild_card_index", partial(rebuild_card_index, config.ENRICHED_DIR, config.CARD_FTS_DB_PATH))
    monkeypatch.setattr(cli, "rebuild_youtube_segment_index", partial(
        rebuild_youtube_segment_index, config.YOUTUBE_SEGMENTS_DIR, config.CARD_FTS_DB_PATH))
    monkeypatch.setattr(cli, "rebuild_source_registry", partial(
        rebuild_source_registry, config.NORMALIZED_DIR, config.ENRICHED_DIR, config.SOURCE_REGISTRY_DB_PATH))
    graph = Mock(side_effect=AssertionError("run must not load a graph"))
    monkeypatch.setattr(cli, "create_rag", graph)
    calls = []

    def text_response(messages, *, role, **kwargs):
        calls.append((role, messages[-1]["content"]))
        return RUSSIAN

    def json_response(messages, *, role, **kwargs):
        calls.append((role, messages[-1]["content"]))
        return {"summary": "Источник сообщил о новых переговорах министров.", "key_points": [],
                "entities": {}, "topics": [], "theses": [], "quotes": [], "events": [],
                "search_phrases": [], "quality_flags": []}

    monkeypatch.setattr(llm_backend, "complete_text_sync", text_response)
    monkeypatch.setattr(llm_backend, "complete_json_sync", json_response)
    return calls


def message(text, message_id=1, **kwargs):
    return TelegramMessage(channel_name="Acceptance", channel_id=123, channel_username="acceptance",
                           message_id=message_id, date=datetime(2026, 10, 25, tzinfo=UTC), text=text, **kwargs)


def run_messages(monkeypatch, messages):
    fetcher = Mock(connect=AsyncMock(), disconnect=AsyncMock(),
                   fetch_all_channels=AsyncMock(return_value={"Acceptance": messages}))
    monkeypatch.setattr(cli, "TelegramFetcher", Mock(return_value=fetcher))
    asyncio.run(cli.cmd_run())


@pytest.mark.parametrize(("text", "translation_calls"), [
    (RUSSIAN, 0),
    ("Это полностью русский текст, который нужно сохранить без изменений.", 0),
    ("Україна обговорює нові переговори та питання безпеки.", 1),
    ("Влада провела переговори про безпеку та нову угоду.", 1),
    ("The ministers discussed a new agreement during negotiations.", 1),
    (RUSSIAN + " The final agreement remains uncertain.", 1),
    (RUSSIAN + " Це нова угода.", 1),
    ("Москва, НАТО, ООН", 1),
])
def test_run_translation_policy_saves_searchable_card(ingestion_runtime, monkeypatch, text, translation_calls):
    run_messages(monkeypatch, [message(text)])
    calls = ingestion_runtime
    assert sum(role == "default" for role, _ in calls) == translation_calls
    assert sum(role == "enrichment" for role, _ in calls) == 1
    normalized = config.NORMALIZED_DIR / "Acceptance/1.txt"
    assert (RUSSIAN if translation_calls else text) in normalized.read_text(encoding="utf-8")
    card_file = config.ENRICHED_DIR / "Acceptance/1.enriched.json"
    card = EnrichedCardV2.model_validate_json(card_file.read_text(encoding="utf-8"))
    assert card.provenance.source_id == "telegram:123:1"
    assert card.provenance.post_url == "https://t.me/acceptance/1"
    matches = search_card_index("переговорах", db_path=config.CARD_FTS_DB_PATH)
    assert [match.source_id for match in matches] == [card.provenance.source_id]
    assert resolve_source(card.provenance.source_id, db_path=config.SOURCE_REGISTRY_DB_PATH).post_url == card.provenance.post_url
    progress = json.loads((config.STATE_DIR / "enrichment_progress.json").read_text(encoding="utf-8"))
    assert progress["enriched"]["Acceptance/1"]["source_fingerprint"]


def test_empty_input_does_not_publish_card(ingestion_runtime, monkeypatch):
    run_messages(monkeypatch, [message("   ")])
    assert ingestion_runtime == []
    assert not list(config.ENRICHED_DIR.rglob("*.enriched.json"))


@pytest.mark.parametrize(("setting", "value"), [
    ("CODEX_LUNA_MODEL", "isolated-future-model"), ("LLM_PROFILE", "current"),
    ("CODEX_LUNA_REASONING_EFFORT", "isolated-future-effort"),
])
def test_ready_cards_survive_future_model_settings(ingestion_runtime, monkeypatch, setting, value):
    run_messages(monkeypatch, [message(RUSSIAN)])
    card_file = config.ENRICHED_DIR / "Acceptance/1.enriched.json"
    before = card_file.read_bytes()
    ingestion_runtime.clear()
    monkeypatch.setattr(config, setting, value)
    monkeypatch.setattr(config, "REGENERATE_ON_PROFILE_CHANGE", True)
    result = cli.cmd_enrich()
    assert ingestion_runtime == []
    assert result.enrichment.skipped_up_to_date == 1
    assert card_file.read_bytes() == before


@pytest.mark.parametrize("change", ["text", "metadata", "prompt", "missing", "corrupt", "unstable", "failed", "force"])
def test_actual_invalidations_reprocess_card(ingestion_runtime, monkeypatch, change):
    run_messages(monkeypatch, [message(RUSSIAN)])
    card_file = config.ENRICHED_DIR / "Acceptance/1.enriched.json"
    if change == "text":
        path = config.NORMALIZED_DIR / "Acceptance/1.txt"
        metadata = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        _save_normalized(message(RUSSIAN), path.read_text(encoding="utf-8") + " Новые сведения.", metadata)
    elif change == "metadata":
        path = config.NORMALIZED_DIR / "Acceptance/1.meta.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["post_url"] = "https://t.me/revised_source/1"
        path.write_text(json.dumps(data), encoding="utf-8")
    elif change == "prompt":
        monkeypatch.setattr(config, "ENRICHMENT_PROMPT_VERSION", "isolated-new-rules")
    elif change == "missing":
        card_file.unlink()
    elif change == "corrupt":
        card_file.write_text("broken JSON", encoding="utf-8")
    elif change == "unstable":
        data = json.loads(card_file.read_text(encoding="utf-8"))
        data["quality_flags"] = ["extraction_unstable"]
        card_file.write_text(json.dumps(data), encoding="utf-8")
    elif change == "failed":
        progress_path = config.STATE_DIR / "enrichment_progress.json"
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        progress["failed"] = {"Acceptance/1": {"error": "controlled failure"}}
        progress_path.write_text(json.dumps(progress), encoding="utf-8")
    ingestion_runtime.clear()
    result = cli.cmd_enrich(force=change == "force")
    assert result.enrichment.enriched == 1
    assert any(role == "enrichment" for role, _ in ingestion_runtime)
    card = EnrichedCardV2.model_validate_json(card_file.read_text(encoding="utf-8"))
    if change == "metadata":
        assert card.provenance.post_url == "https://t.me/revised_source/1"


@pytest.mark.parametrize("setting,value", [
    ("CODEX_LUNA_MODEL", "isolated-future-model"), ("LLM_PROFILE", "current"),
    ("CODEX_LUNA_REASONING_EFFORT", "isolated-future-effort"),
])
def test_published_youtube_generation_survives_model_settings(ingestion_runtime, monkeypatch, setting, value):
    source_text = config.YOUTUBE_NORMALIZED_DIR / "source.youtube.txt"
    source_text.write_text((RUSSIAN + " ") * 200, encoding="utf-8")
    source_text.with_suffix(".meta.json").write_text(json.dumps({
        "url": "https://www.youtube.com/watch?v=abc123", "video_id": "abc123", "title": "Переговоры",
        "channel": "Source", "duration_seconds": 1800, "transcript_source": "subtitles",
        "transcript_path": str(source_text.relative_to(config.PROJECT_ROOT)),
        "telegram_source": {"channel_name": "Acceptance", "channel_id": 123, "message_id": 1},
    }), encoding="utf-8")
    result = cli.cmd_enrich()
    assert result.enrichment.youtube_episodes == 1
    assert result.enrichment.youtube_segments > 0
    files = [*config.ENRICHED_DIR.rglob("*.json"), *config.YOUTUBE_SEGMENTS_DIR.rglob("*.json")]
    original = {path: path.read_bytes() for path in files}
    assert any(path.name.endswith("manifest.json") for path in files)
    ingestion_runtime.clear()
    monkeypatch.setattr(config, setting, value)
    monkeypatch.setattr(config, "REGENERATE_ON_PROFILE_CHANGE", True)
    result = cli.cmd_enrich()
    assert result.enrichment.youtube_skipped == 1
    assert ingestion_runtime == []
    assert all(path.read_bytes() == data for path, data in original.items())


@pytest.mark.parametrize("kind", ["telegram", "youtube"])
def test_first_card_persists_while_next_normalization_is_blocked(ingestion_runtime, monkeypatch, kind):
    blocked = threading.Event()
    release = threading.Event()
    published = threading.Event()
    first_card = config.ENRICHED_DIR / "Acceptance/1.enriched.json"
    first_message = message(RUSSIAN)
    if kind == "youtube":
        url = "https://www.youtube.com/watch?v=abc123"
        first_message = message(url, urls=[url])
        artifact = YouTubeArtifact(url=url, video_id="abc123", title="Переговоры", channel="Source",
                                   duration_seconds=90, language="ru", transcript_source="subtitles",
                                   transcript_text=RUSSIAN, cues=[], description="", chapters=[],
                                   description_links=[], extracted_at="2026-10-25T00:00:00Z")
        monkeypatch.setattr(normalization, "extract_youtube_artifact", lambda _: artifact)
        first_card = config.ENRICHED_DIR / "123/1.youtube.abc123.enriched.json"
    real_replace = Path.replace

    def observe_replace(path, target):
        result = real_replace(path, target)
        if Path(target) == first_card:
            published.set()
        return result

    def translate(messages, **kwargs):
        if "Foreign continuation" not in messages[-1]["content"]:
            return messages[-1]["content"]
        blocked.set()
        assert release.wait(5)
        return RUSSIAN

    monkeypatch.setattr(Path, "replace", observe_replace)
    monkeypatch.setattr(llm_backend, "complete_text_sync", translate)
    with ThreadPoolExecutor(max_workers=1) as executor:
        run = executor.submit(run_messages, monkeypatch, [first_message, message("Foreign continuation", 2)])
        try:
            assert blocked.wait(3)
            assert published.wait(3)
            EnrichedCardV2.model_validate_json(first_card.read_text(encoding="utf-8"))
            assert not (config.NORMALIZED_DIR / "Acceptance/2.txt").exists()
        finally:
            release.set()
        run.result(5)
    assert len(list(config.ENRICHED_DIR.rglob("*.enriched.json"))) == 2


def test_stream_has_bounded_unfinished_inputs(ingestion_runtime, monkeypatch):
    release = threading.Event()
    capacity_reached = threading.Event()
    normalized = []
    real_replace = Path.replace
    real_response = llm_backend.complete_json_sync
    bound = 2 * config.ENRICHMENT_CONCURRENCY + 1

    def observe_replace(path, target):
        result = real_replace(path, target)
        if Path(target).suffix == ".txt":
            normalized.append(Path(target))
            if len(normalized) == bound:
                capacity_reached.set()
        return result

    def extract(*args, **kwargs):
        assert release.wait(5)
        return real_response(*args, **kwargs)

    monkeypatch.setattr(Path, "replace", observe_replace)
    monkeypatch.setattr(llm_backend, "complete_json_sync", extract)
    messages = [message(RUSSIAN, index) for index in range(1, 17)]
    with ThreadPoolExecutor(max_workers=1) as executor:
        run = executor.submit(run_messages, monkeypatch, messages)
        try:
            assert capacity_reached.wait(3)
            assert len(normalized) <= bound
            assert not run.done()
        finally:
            release.set()
        run.result(10)
    assert len(list(config.ENRICHED_DIR.rglob("*.enriched.json"))) == len(messages)


@pytest.mark.parametrize("stage", ["normalization", "enrichment"])
def test_late_success_does_not_hide_early_failure(ingestion_runtime, monkeypatch, stage):
    real_response = llm_backend.complete_json_sync
    original_first = "Foreign input" if stage == "normalization" else RUSSIAN.rstrip(".") + ", номер задания 1."

    def translate(*args, **kwargs):
        raise RuntimeError("controlled normalization failure")

    def extract(messages, **kwargs):
        if "номер задания 1" in messages[-1]["content"]:
            raise RuntimeError("controlled enrichment failure")
        return real_response(messages, **kwargs)

    with monkeypatch.context() as fault:
        if stage == "normalization":
            fault.setattr(llm_backend, "complete_text_sync", translate)
        else:
            fault.setattr(llm_backend, "complete_json_sync", extract)
        run_messages(monkeypatch, [message(original_first), message(RUSSIAN, 2)])
    first = config.ENRICHED_DIR / "Acceptance/1.enriched.json"
    second = config.ENRICHED_DIR / "Acceptance/2.enriched.json"
    assert not first.exists()
    retained = second.read_bytes()
    if stage == "normalization":
        assert state.get_last_message_id(123) == 0
    else:
        progress = json.loads(enrichment._PROGRESS_FILE.read_text(encoding="utf-8"))
        assert "Acceptance/1" in progress["failed"]
    ingestion_runtime.clear()
    run_messages(monkeypatch, [message(original_first), message(RUSSIAN, 2)] if stage == "normalization" else [])
    assert first.exists()
    assert second.read_bytes() == retained
    assert sum(role == "enrichment" for role, _ in ingestion_runtime) == 1
    assert len(search_card_index("переговорах", db_path=config.CARD_FTS_DB_PATH)) == 2


@pytest.mark.parametrize("incomplete", ["missing_metadata", "mismatched_text"])
def test_incomplete_saved_pair_cannot_publish_card(ingestion_runtime, monkeypatch, incomplete):
    run_messages(monkeypatch, [message(RUSSIAN)])
    card = config.ENRICHED_DIR / "Acceptance/1.enriched.json"
    card.unlink()
    normalized = config.NORMALIZED_DIR / "Acceptance/1.txt"
    if incomplete == "missing_metadata":
        normalized.with_suffix(".meta.json").unlink()
    else:
        normalized.write_text("Partially overwritten text", encoding="utf-8")
    ingestion_runtime.clear()
    cli.cmd_enrich()
    assert not card.exists()
    assert ingestion_runtime == []


def test_cancelled_run_drains_saved_work_and_resumes(ingestion_runtime, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    real_response = llm_backend.complete_json_sync
    messages = [message(RUSSIAN, index) for index in range(1, 13)]

    def extract(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_response(*args, **kwargs)

    monkeypatch.setattr(llm_backend, "complete_json_sync", extract)
    fetcher = Mock(connect=AsyncMock(), disconnect=AsyncMock(),
                   fetch_all_channels=AsyncMock(return_value={"Acceptance": messages}))
    monkeypatch.setattr(cli, "TelegramFetcher", Mock(return_value=fetcher))

    async def cancel_run():
        task = asyncio.create_task(cli.cmd_run())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_run())
    retained = {path: path.read_bytes() for path in config.ENRICHED_DIR.rglob("*.enriched.json")}
    assert retained
    assert search_card_index("переговорах", db_path=config.CARD_FTS_DB_PATH)
    run_messages(monkeypatch, messages)
    assert len(list(config.ENRICHED_DIR.rglob("*.enriched.json"))) == len(messages)
    assert all(path.read_bytes() == data for path, data in retained.items())
