"""Command-level ingestion acceptance with real cards and local indexes."""

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from geospoiler import config
from geospoiler.cli import app
from geospoiler.cli import pipeline as cli
from geospoiler.contracts.models import EnrichedCardV2
from geospoiler.ingestion.enricher import pipeline as enrichment
from geospoiler.ingestion.fetcher import state
from geospoiler.ingestion.fetcher import telegram_client as telegram
from geospoiler.ingestion.fetcher.telegram_client import TelegramMessage
from geospoiler.ingestion.normalizer import media_vision, transcription_handler
from geospoiler.ingestion.normalizer import pipeline as normalization
from geospoiler.ingestion.normalizer.pipeline import _save_normalized, read_normalized_meta
from geospoiler.ingestion.normalizer.youtube_handler import YouTubeArtifact, save_youtube_artifact
from geospoiler.providers import llm_backend
from geospoiler.retrieval.cards.card_fts import rebuild_card_index, rebuild_youtube_segment_index, search_card_index
from geospoiler.retrieval.lightrag.storage import load_source_metadata_index
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
        "RAG_STORAGE_DIR": "indexes/lightrag",
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
    ("Сегодня власти сообщили, президент розпочав позачергову нараду стосовно нового закону.", 1),
    ("Сегодня власти заявили, че няма достатъчно средства.", 1),
    (RUSSIAN + " STOP THE WAR.", 1),
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
    real_replace = os.replace

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

    monkeypatch.setattr(os, "replace", observe_replace)
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
    real_replace = os.replace
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

    monkeypatch.setattr(os, "replace", observe_replace)
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
    original_first = "Foreign input" if stage == "normalization" else RUSSIAN.rstrip(".") + " номер задания 1."

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


def test_interrupted_legacy_pair_overwrite_never_accepts_mixed_provenance(ingestion_runtime, monkeypatch):
    path = _save_normalized(message(RUSSIAN), "Original text", {"channel_id": 123, "message_id": 1, "date": "2026-01-01"})
    meta_path = path.with_suffix(".meta.json")
    old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    old_meta.pop("normalized_text_sha256")
    meta_path.write_text(json.dumps(old_meta), encoding="utf-8")
    real_replace = os.replace

    def interrupt(path, target):
        if Path(target) == meta_path:
            raise KeyboardInterrupt("controlled interruption before sidecar publication")
        return real_replace(path, target)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            _save_normalized(message(RUSSIAN), "Replacement text", {**old_meta, "date": "2026-10-25"})
    accepted = read_normalized_meta(path)
    assert path.read_text(encoding="utf-8") == "Original text"
    assert accepted.date == "2026-01-01"


def test_stream_checkpoint_io_grows_linearly(ingestion_runtime, monkeypatch):
    reads = []
    original_read = Path.read_text

    def observe_read(path, *args, **kwargs):
        if path == state.PROGRESS_FILE:
            reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observe_read)
    messages = [message(RUSSIAN, index) for index in range(1, 13)]
    run_messages(monkeypatch, messages)
    assert len(reads) <= 2 * len(messages) + 1
    assert state.get_last_message_id(123) == 12


def test_stream_prefers_dedicated_youtube_over_same_legacy_occurrence(ingestion_runtime, monkeypatch):
    url = "https://www.youtube.com/watch?v=abc123"
    artifact = YouTubeArtifact(url=url, video_id="abc123", title="Переговоры", channel="Source",
                               duration_seconds=1800, language="ru", transcript_source="subtitles",
                               transcript_text=(RUSSIAN + " ") * 200, cues=[], description="", chapters=[],
                               description_links=[], extracted_at="2026-10-25T00:00:00Z")
    save_youtube_artifact(artifact, channel_name="Acceptance", channel_id=123, message_id=1,
                          source_meta={"channel_name": "Acceptance", "channel_id": 123, "message_id": 1})
    legacy_marker = "Устаревшая краткая версия без таймкодов."
    _save_normalized(message(url), f"[YouTube]\nНазвание: Переговоры\nАвтор: Source\nURL: {url}\n\n{legacy_marker}",
                     {"channel_name": "Acceptance", "channel_id": 123, "message_id": 1,
                      "youtube_urls": [url], "has_body_text": False})
    run_messages(monkeypatch, [message(RUSSIAN, 2)])
    assert not any(legacy_marker in text for role, text in ingestion_runtime if role == "enrichment")
    episode = config.ENRICHED_DIR / "123/1.youtube.abc123.enriched.json"
    manifest = json.loads(episode.with_name(episode.name + ".manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["segment_count"] > 0
    assert len(list(config.YOUTUBE_SEGMENTS_DIR.rglob("*.youtube-segment.json"))) == manifest["segment_count"]


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


def run_raw_media(monkeypatch, kind, caption, *, download_error=False, payload=None):
    """Control Telegram transport while retaining real fetch/group/media routing."""
    mime = {"photo": "image/jpeg", "image_document": "image/png", "video": "video/mp4",
            "audio": "audio/mpeg", "voice": "audio/ogg", "document": "text/plain", "pdf": "application/pdf"}[kind]
    attachment = SimpleNamespace(id=456, mime_type=mime, size=24,
                                 attributes=[SimpleNamespace(voice=kind == "voice")])
    media = telegram.MessageMediaPhoto(photo=attachment) if kind == "photo" else telegram.MessageMediaDocument(document=attachment)

    async def download(file):
        if download_error:
            raise OSError("controlled Telegram download failure")
        path = Path(file).with_suffix({"photo": ".jpg", "image_document": ".png", "document": ".txt", "pdf": ".pdf"}.get(kind, ".bin"))
        path.write_bytes(payload if payload is not None else (b"controlled media" if kind not in {"document", "pdf"} else "Маршрут поставок проходит через порт.".encode()))
        return str(path)

    transport = AsyncMock(side_effect=download)
    raw = SimpleNamespace(id=1, date=datetime(2026, 10, 25, tzinfo=UTC), message=caption,
                          action=None, forward=None, media=media, grouped_id=None, download_media=transport)

    async def iterate(*args, **kwargs):
        yield raw

    fetcher = telegram.TelegramFetcher.__new__(telegram.TelegramFetcher)
    fetcher.client = SimpleNamespace(start=AsyncMock(), disconnect=AsyncMock(), iter_messages=iterate)
    fetcher.discover_channels = AsyncMock(return_value=[{"id": 123, "title": "Acceptance", "name": "acceptance"}])
    monkeypatch.setattr(cli, "TelegramFetcher", Mock(return_value=fetcher))
    asyncio.run(cli.cmd_run())
    return transport


@pytest.fixture
def media_boundaries(ingestion_runtime, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_PROCESSING_MODE", "selective")
    monkeypatch.setattr(config, "MEDIA_CAPTURE_ENABLED", True)
    monkeypatch.setattr(config, "TRANSCRIPTION_ENABLED", True)
    monkeypatch.setattr(config, "TRANSCRIPTION_API_KEY", "offline-test-key")
    marker = "Маршрут поставок проходит через порт."
    vision = Mock(return_value=marker)
    audio = Mock(return_value=SimpleNamespace(ok=True, status_code=200,
                 json=lambda: {"choices": [{"message": {"content": marker}}]}))
    monkeypatch.setattr(llm_backend, "complete_media_analysis_sync", vision)
    monkeypatch.setattr(transcription_handler.requests, "post", audio)
    monkeypatch.setattr(llm_backend, "complete_text_sync", lambda messages, **kwargs: messages[-1]["content"].replace("The route crosses the port.", marker))
    original_json = llm_backend.complete_json_sync

    def extract(messages, **kwargs):
        result = original_json(messages, **kwargs)
        if marker in messages[-1]["content"]:
            result["summary"] = marker
        return result

    monkeypatch.setattr(llm_backend, "complete_json_sync", extract)
    return vision, audio, marker


@pytest.mark.parametrize("kind", ["photo", "image_document", "video", "audio", "voice", "document", "pdf"])
def test_self_contained_caption_skips_download_and_analysis(media_boundaries, monkeypatch, kind):
    transport = run_raw_media(monkeypatch, kind, RUSSIAN)
    transport.assert_not_called()
    vision, audio, _ = media_boundaries
    vision.assert_not_called()
    audio.assert_not_called()
    meta = json.loads((config.NORMALIZED_DIR / "Acceptance/1.meta.json").read_text(encoding="utf-8"))
    item = meta["media"][0]
    assert item["attachment_id"] == "456"
    assert item["analysis_status"] == "not_analyzed"
    assert item["error"] == ""
    card = EnrichedCardV2.model_validate_json((config.ENRICHED_DIR / "Acceptance/1.enriched.json").read_text(encoding="utf-8"))
    assert not any("media:" in issue for issue in card.extraction_issues)
    assert search_card_index("переговорах", db_path=config.CARD_FTS_DB_PATH)


def test_photo_event_noun_is_not_attachment_dependency(media_boundaries, monkeypatch):
    transport = run_raw_media(monkeypatch, "photo", "На фотосессии сегодня власти обсуждают новые переговоры.")
    transport.assert_not_called()
    vision, _, _ = media_boundaries
    vision.assert_not_called()


@pytest.mark.parametrize("caption", ["", "Смотрите", "🔥"])
def test_media_only_or_vague_caption_retains_unanalyzed_source(media_boundaries, monkeypatch, caption):
    transport = run_raw_media(monkeypatch, "photo", caption)
    transport.assert_not_called()
    vision, audio, _ = media_boundaries
    vision.assert_not_called()
    audio.assert_not_called()
    meta = json.loads((config.NORMALIZED_DIR / "Acceptance/1.meta.json").read_text(encoding="utf-8"))
    assert meta["post_url"] == "https://t.me/acceptance/1"
    assert meta["media"][0]["analysis_status"] == "not_analyzed"
    assert resolve_source("telegram:123:1", db_path=config.SOURCE_REGISTRY_DB_PATH)


@pytest.mark.parametrize(("kind", "caption", "vision_count", "audio_count"), [
    ("photo", "На скриншоте приведены данные о маршруте поставок.", 1, 0),
    ("image_document", "На карте показан маршрут поставок.", 1, 0),
    ("voice", "В записи министр говорит о маршруте поставок.", 0, 1),
    ("audio", "Послушайте эту запись: министр рассказал о поставках.", 0, 1),
    ("document", "В документе указаны подробности маршрута поставок.", 0, 0),
])
def test_explicit_attachment_dependency_reaches_card_and_search(media_boundaries, monkeypatch, kind, caption, vision_count, audio_count):
    transport = run_raw_media(monkeypatch, kind, caption)
    transport.assert_awaited_once()
    vision, audio, marker = media_boundaries
    assert vision.call_count == vision_count
    assert audio.call_count == audio_count
    meta = json.loads((config.NORMALIZED_DIR / "Acceptance/1.meta.json").read_text(encoding="utf-8"))
    assert meta["media"][0]["analysis_status"] == "success"
    card = EnrichedCardV2.model_validate_json((config.ENRICHED_DIR / "Acceptance/1.enriched.json").read_text(encoding="utf-8"))
    assert marker in card.search_text
    assert not card.extraction_issues
    assert search_card_index("порт", db_path=config.CARD_FTS_DB_PATH)


@pytest.mark.parametrize("failure", ["download", "analysis"])
def test_requested_media_failure_is_distinct_from_policy_skip(media_boundaries, monkeypatch, failure):
    vision, _, _ = media_boundaries
    if failure == "analysis":
        vision.side_effect = llm_backend.LLMBackendError("controlled analysis failure")
    run_raw_media(monkeypatch, "photo", "На скриншоте приведены данные о маршруте поставок.", download_error=failure == "download")
    meta = json.loads((config.NORMALIZED_DIR / "Acceptance/1.meta.json").read_text(encoding="utf-8"))
    assert meta["media"][0]["analysis_status"] == "failed"
    assert meta["media"][0]["error"] or meta["media"][0].get("visual_error")
    card = EnrichedCardV2.model_validate_json((config.ENRICHED_DIR / "Acceptance/1.enriched.json").read_text(encoding="utf-8"))
    assert card.extraction_issues


@pytest.mark.parametrize("operation", ["vision", "transcribe"])
def test_video_dependency_runs_only_required_operation(media_boundaries, monkeypatch, operation):
    from PIL import Image

    frame = config.MEDIA_CACHE_DIR / "frame.png"
    image = Image.new("RGB", (64, 64))
    image.putdata([(255, 255, 255) if (x // 4 + y // 4) % 2 else (0, 0, 0) for y in range(64) for x in range(64)])
    image.save(frame)
    frames = Mock(return_value=[frame])
    monkeypatch.setattr(media_vision, "_extract_frames", frames)

    def convert(command, **kwargs):
        Path(command[-1]).write_bytes(b"controlled converted audio")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transcription_handler.subprocess, "run", convert)
    caption = "На видео видно карту маршрута поставок." if operation == "vision" else "В видео министр говорит о маршруте поставок."
    transport = run_raw_media(monkeypatch, "video", caption)
    transport.assert_awaited_once()
    vision, audio, marker = media_boundaries
    assert vision.call_count == (operation == "vision")
    assert audio.call_count == (operation == "transcribe")
    assert frames.call_count == (operation == "vision")
    card = EnrichedCardV2.model_validate_json((config.ENRICHED_DIR / "Acceptance/1.enriched.json").read_text(encoding="utf-8"))
    assert marker in card.search_text
    assert not card.extraction_issues


def test_selected_pdf_extracts_actual_text_without_vision(media_boundaries, monkeypatch):
    # A tiny real PDF fixture, with a text stream and standard Helvetica font.
    stream = b"BT /F1 12 Tf 20 100 Td (The route crosses the port.) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdf = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    pdf += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    run_raw_media(monkeypatch, "pdf", "В документе указаны подробности маршрута поставок.", payload=pdf)
    vision, audio, marker = media_boundaries
    vision.assert_not_called()
    audio.assert_not_called()
    card = EnrichedCardV2.model_validate_json((config.ENRICHED_DIR / "Acceptance/1.enriched.json").read_text(encoding="utf-8"))
    assert marker in card.search_text
    assert not card.extraction_issues


class ObservedLightRAG:
    """External graph boundary with persisted status/content and operation receipts."""

    def __init__(self):
        self.statuses = {}
        self.documents = {}
        self.insertions = []
        self.deletions = []

        async def status(doc_id):
            return self.statuses.get(doc_id)

        async def content(doc_id):
            return self.documents.get(doc_id)

        self.doc_status = SimpleNamespace(get_by_id=status)
        self.full_docs = SimpleNamespace(get_by_id=content)

    async def ainsert(self, texts, ids, file_paths):
        for text, doc_id, file_path in zip(texts, ids, file_paths, strict=True):
            self.insertions.append((doc_id, text, file_path))
            self.statuses[doc_id] = {"status": "processed", "file_path": file_path}
            self.documents[doc_id] = {"content": text}

    async def adelete_by_doc_id(self, doc_id):
        self.deletions.append(doc_id)
        self.statuses.pop(doc_id, None)
        self.documents.pop(doc_id, None)
        return SimpleNamespace(status="success", message="deleted")

    async def finalize_storages(self):
        pass


def test_explicit_load_adds_updates_reuses_retries_and_preserves_absent_cards(ingestion_runtime, monkeypatch):
    run_messages(monkeypatch, [message(RUSSIAN)])
    rag = ObservedLightRAG()
    monkeypatch.setattr(cli, "create_rag", AsyncMock(return_value=rag))
    asyncio.run(cli.cmd_load())
    assert len(rag.insertions) == 1
    original_id = rag.insertions[0][0]
    asyncio.run(cli.cmd_load())
    assert len(rag.insertions) == 1 and not rag.deletions

    # Save a second real card without implicitly loading it into the graph.
    before = len(rag.insertions)
    run_messages(monkeypatch, [message(RUSSIAN, 2)])
    assert len(rag.insertions) == before
    asyncio.run(cli.cmd_load())
    assert len(rag.insertions) == before + 1
    second_id = rag.insertions[-1][0]
    assert second_id != original_id

    # Metadata-only updates still reach the graph's source metadata index.
    meta_path = config.NORMALIZED_DIR / "Acceptance/1.meta.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["post_url"] = "https://t.me/revised_source/1"
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    asyncio.run(cli.cmd_load())
    assert len(rag.insertions) == 2 and not rag.deletions
    assert any(item.get("post_url") == metadata["post_url"] for item in load_source_metadata_index().values())

    card_path = config.ENRICHED_DIR / "Acceptance/1.enriched.json"
    card = json.loads(card_path.read_text(encoding="utf-8"))
    card["graph_text"] += "\nИсточник сообщил новые сведения о поставках."
    card_path.write_text(json.dumps(card), encoding="utf-8")
    asyncio.run(cli.cmd_load())
    assert rag.deletions == [original_id]
    assert [row[0] for row in rag.insertions] == [original_id, second_id, original_id]

    for status in ("failed", "processing"):
        rag.statuses[original_id]["status"] = status
        before = len(rag.insertions)
        asyncio.run(cli.cmd_load())
        assert len(rag.insertions) == before + 1
        assert rag.insertions[-1][0] == original_id
    rag.documents.pop(original_id)
    before = len(rag.insertions)
    asyncio.run(cli.cmd_load())
    assert len(rag.insertions) == before + 1

    (config.ENRICHED_DIR / "Acceptance/2.enriched.json").unlink()
    before = len(rag.deletions)
    asyncio.run(cli.cmd_load())
    assert len(rag.deletions) == before
    assert second_id in rag.statuses

    before = len(rag.insertions)
    app.main(["load", "--force"])
    assert len(rag.insertions) == before + 1
    assert rag.insertions[-1][0] == original_id
