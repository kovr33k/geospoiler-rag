"""Command-level ingestion acceptance with real cards and local indexes."""

import asyncio
import json
from datetime import UTC, datetime
from functools import partial
from unittest.mock import AsyncMock, Mock

import pytest

from geospoiler import config
from geospoiler.cli import pipeline as cli
from geospoiler.contracts.models import EnrichedCardV2
from geospoiler.ingestion.enricher import pipeline as enrichment
from geospoiler.ingestion.fetcher import state
from geospoiler.ingestion.fetcher.telegram_client import TelegramMessage
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
        path.write_text(path.read_text(encoding="utf-8") + " Новые сведения.", encoding="utf-8")
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
