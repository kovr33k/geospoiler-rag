import json
from pathlib import Path
from unittest.mock import patch

import pytest

from geospoiler import config
from geospoiler.contracts.models import EnrichedCardV2, LLMPayload, NormalizedMeta
from geospoiler.ingestion import ingest_report
from geospoiler.ingestion.enricher import pipeline as enricher_pipeline
from geospoiler.ingestion.enricher import youtube_pipeline
from geospoiler.ingestion.enricher.duplicates import post_content_key
from geospoiler.ingestion.enricher.validator import ValidationResult
from geospoiler.ingestion.normalizer import youtube_handler
from geospoiler.retrieval.cards import card_fts

_TEXT = "Длинный пересланный пост о переговорах по Чёрному морю и поставках зерна."


def _meta(channel: str, message_id: int, attachments=("111",), **extra) -> dict:
    return {
        "channel_name": channel,
        "channel_id": f"{100 + message_id}",
        "message_id": message_id,
        "date": f"2026-01-0{message_id}T00:00:00+00:00",
        "post_url": f"https://t.me/c/{100 + message_id}/{message_id}",
        "is_forward": True,
        "forward_from_name": "Original",
        "has_text": True,
        "has_body_text": True,
        "media": [{"media_type": "image", "attachment_id": item} for item in attachments],
        **extra,
    }


def _text(channel: str, message_id: int, body: str = _TEXT) -> str:
    return f"[Канал: {channel} | Пост: https://t.me/c/{100 + message_id}/{message_id}]\n\n{body}"


def _payload() -> LLMPayload:
    return LLMPayload(
        summary="Пост сообщает о переговорах по Чёрному морю.",
        key_points=[{
            "text": "Стороны обсуждали поставки зерна.",
            "type": "reported_statement",
            "importance": "high",
            "evidence": _TEXT,
        }],
    )


def _write_post(root: Path, channel: str, message_id: int, **meta) -> None:
    directory = root / "normalized" / channel
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{message_id}.txt").write_text(_text(channel, message_id), encoding="utf-8")
    (directory / f"{message_id}.meta.json").write_text(
        json.dumps(_meta(channel, message_id, **meta), ensure_ascii=False), encoding="utf-8",
    )


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "NORMALIZED_DIR", tmp_path / "normalized")
    monkeypatch.setattr(config, "ENRICHED_DIR", tmp_path / "enriched")
    monkeypatch.setattr(config, "YOUTUBE_NORMALIZED_DIR", tmp_path / "normalized_youtube")
    monkeypatch.setattr(config, "YOUTUBE_SEGMENTS_DIR", tmp_path / "enriched_segments")
    monkeypatch.setattr(config, "YOUTUBE_CHECKPOINT_DIR", tmp_path / "checkpoints")
    monkeypatch.setattr(config, "REVIEW_QUEUE_DIR", tmp_path / "review_queue")
    monkeypatch.setattr(config, "ENRICHMENT_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(enricher_pipeline, "_PROGRESS_FILE", tmp_path / "enrichment_progress.json")
    return tmp_path


def _enrich(extract=None):
    extract = extract or patch.object(enricher_pipeline, "_extract_payload", side_effect=lambda *_a, **_k: _payload())
    with extract as extract_mock, \
         patch.object(enricher_pipeline, "validate_payload", return_value=ValidationResult()), \
         patch.object(enricher_pipeline, "enrich_youtube_all", return_value=youtube_pipeline.YouTubeEnrichmentStats()):
        stats = enricher_pipeline.enrich_all()
    return stats, extract_mock


def _card(root: Path, channel: str, message_id: int) -> dict:
    return json.loads((root / "enriched" / channel / f"{message_id}.enriched.json").read_text(encoding="utf-8"))


def test_content_key_ignores_channel_header_but_not_attachments_or_links():
    key = post_content_key(NormalizedMeta.model_validate(_meta("A", 1)), _text("A", 1))

    assert key == post_content_key(NormalizedMeta.model_validate(_meta("B", 2)), _text("B", 2))
    assert key != post_content_key(NormalizedMeta.model_validate(_meta("B", 2, attachments=("222",))), _text("B", 2))
    assert key != post_content_key(
        NormalizedMeta.model_validate(_meta("B", 2, web_urls=["https://example.test/a"])), _text("B", 2),
    )
    assert key != post_content_key(NormalizedMeta.model_validate(_meta("B", 2)), _text("B", 2, _TEXT + " Ещё"))
    assert post_content_key(NormalizedMeta.model_validate(_meta("B", 2, attachments=())), _text("B", 2, "👍")) is None
    unnamed_media = {**_meta("B", 2), "media": [{"media_type": "image"}]}
    assert post_content_key(NormalizedMeta.model_validate(unnamed_media), _text("B", 2)) is None


def test_copy_of_a_post_reuses_the_root_card_without_llm(corpus):
    _write_post(corpus, "A", 1)
    _write_post(corpus, "B", 2)
    _write_post(corpus, "B", 3, attachments=("333",))

    stats, extract = _enrich()

    assert extract.call_count == 2
    assert stats.enriched == 3
    assert stats.duplicates == 1
    root, copy = _card(corpus, "A", 1), _card(corpus, "B", 2)
    assert root["duplicate_of"] is None
    assert copy["duplicate_of"] == root["provenance"]["source_id"]
    assert copy["provenance"]["source_id"] == "telegram:102:2"
    assert copy["provenance"]["channel"] == "B"
    assert copy["summary"] == root["summary"]
    assert _card(corpus, "B", 3)["duplicate_of"] is None

    indexed = {record.source_id for record in card_fts.iter_card_records(corpus / "enriched")}
    assert indexed == {"telegram:101:1", "telegram:103:3"}

    second, extract = _enrich()
    assert extract.call_count == 0
    assert second.skipped_up_to_date == 3

    duplicates = ingest_report.collect_exact_duplicates()
    assert duplicates == [{
        "kind": "post",
        "source_id": "telegram:102:2",
        "channel_title": "B",
        "post_url": "https://t.me/c/102/2",
        "duplicate_of": "telegram:101:1",
        "root_channel_title": "A",
        "root_post_url": "https://t.me/c/101/1",
    }]
    rendered = ingest_report.render_ingest_report(
        {"summary": ingest_report._summary([]), "items": [], "duplicates": duplicates},
    )
    assert "  B https://t.me/c/102/2 — копия: A https://t.me/c/101/1" in rendered
    assert "явных дублей: 1 (постов 1, видео 0)" in rendered


def test_existing_copies_are_marked_and_stale_marks_cleared(corpus):
    _write_post(corpus, "A", 1)
    _write_post(corpus, "B", 2)
    # Both posts are enriched independently, as before exact deduplication.
    with patch.object(enricher_pipeline, "_load_duplicate_roots", return_value=None):
        _, extract = _enrich()
    assert extract.call_count == 2
    assert _card(corpus, "B", 2)["duplicate_of"] is None

    roots = enricher_pipeline._load_duplicate_roots()
    assert _card(corpus, "B", 2)["duplicate_of"] == "telegram:101:1"
    assert _card(corpus, "A", 1)["duplicate_of"] is None
    key = post_content_key(NormalizedMeta.model_validate(_meta("A", 1)), _text("A", 1))
    assert roots.get(key)["provenance"]["source_id"] == "telegram:101:1"

    # The root's text changes: its former copy becomes a root again.
    (corpus / "normalized" / "A" / "1.txt").write_text(_text("A", 1, "Другой текст поста."), encoding="utf-8")
    enricher_pipeline._load_duplicate_roots()
    assert _card(corpus, "B", 2)["duplicate_of"] is None


def _write_video(root: Path, channel_id: int, message_id: int, transcript: str) -> None:
    directory = root / "normalized_youtube" / str(channel_id) / str(message_id)
    directory.mkdir(parents=True)
    url = "https://www.youtube.com/watch?v=vid12345"
    text_path = directory / "vid12345.abc.youtube.txt"
    text_path.write_text(
        f"[YouTube]\nАвтор: Channel\nНазвание: Video\nURL: {url}\n\n{transcript}", encoding="utf-8",
    )
    cues_path = directory / "vid12345.abc.youtube.cues.json"
    cues_path.write_text("[]", encoding="utf-8")
    (directory / "vid12345.youtube.meta.json").write_text(json.dumps({
        "url": url,
        "video_id": "vid12345",
        "title": "Video",
        "channel": "Channel",
        "duration_seconds": 1800,
        "language": "ru",
        "transcript_source": "whisper",
        "has_transcript": True,
        "transcript_path": str(text_path.relative_to(root)),
        "cues_path": str(cues_path.relative_to(root)),
        "telegram_source": {"channel_name": f"C{channel_id}", "channel_id": channel_id, "message_id": message_id},
    }), encoding="utf-8")


_VIDEO_PAYLOAD = {
    "summary": "Китай обсуждал поставки компонентов.",
    "key_points": [{"text": "Китай обсуждал поставки.", "type": "reported_statement", "importance": "high",
                    "evidence": "Китай обсуждал поставки компонентов."}],
}


def _enrich_videos(**kwargs):
    with patch.object(youtube_pipeline, "extract_chunk_raw", return_value=_VIDEO_PAYLOAD) as extract, \
         patch.object(youtube_pipeline, "merge_chunk_results_raw", return_value=_VIDEO_PAYLOAD):
        stats = youtube_pipeline.enrich_youtube_all(**kwargs)
    return stats, extract.call_count


def _episode(root: Path, channel_id: int, message_id: int) -> EnrichedCardV2:
    path = root / "enriched" / str(channel_id) / f"{message_id}.youtube.vid12345.enriched.json"
    return EnrichedCardV2.model_validate_json(path.read_text(encoding="utf-8"))


def _assert_second_post_copies_first(root: Path) -> None:
    first, copy = _episode(root, 1, 10), _episode(root, 2, 20)
    assert first.duplicate_of is None
    assert copy.duplicate_of == first.provenance.source_id
    assert copy.provenance.source_id == "telegram:2:20:youtube:vid12345"
    assert copy.summary == first.summary
    segments = list(card_fts.iter_youtube_segment_records(root / "enriched_segments"))
    assert segments and {record[1] for record in segments} == {first.provenance.source_id}


def test_copy_of_a_video_reuses_episode_and_segments_without_llm(corpus):
    transcript = "Китай обсуждал поставки компонентов. " * 700
    _write_video(corpus, 1, 10, transcript)
    # A second download of the same video differs slightly; the video id decides.
    _write_video(corpus, 2, 20, transcript + "Конец записи.")

    stats, calls = _enrich_videos()
    again, calls_again = _enrich_videos()

    assert stats.episodes_written == 2
    assert stats.duplicate_episodes == 1
    segment_count = len(list((corpus / "enriched_segments" / "1").rglob("*.youtube-segment.json")))
    assert segment_count > 1
    assert calls == segment_count
    assert again.episodes_skipped == 2 and calls_again == 0
    assert len(list((corpus / "enriched_segments" / "2").rglob("*.youtube-segment.json"))) == segment_count
    _assert_second_post_copies_first(corpus)

    [duplicate] = ingest_report.collect_exact_duplicates()
    assert duplicate["kind"] == "video"
    assert duplicate["channel_title"] == "C2" and duplicate["root_channel_title"] == "C1"


def test_independently_enriched_copy_of_a_video_becomes_a_copy(corpus):
    _write_video(corpus, 1, 10, "Китай обсуждал поставки компонентов. " * 700)
    _write_video(corpus, 2, 20, "Китай обсуждал поставки компонентов, запись два. " * 600)
    _enrich_videos(force=True)
    assert _episode(corpus, 2, 20).duplicate_of is None

    stats, calls = _enrich_videos()

    assert calls == 0
    assert stats.duplicate_episodes == 1 and stats.episodes_skipped == 1
    _assert_second_post_copies_first(corpus)


def test_normalizer_reuses_the_saved_transcript_of_the_same_video(corpus):
    _write_video(corpus, 1, 10, "Расшифровка видео.")

    artifact = youtube_handler.reuse_youtube_artifact("https://youtu.be/vid12345?si=share")

    assert artifact is not None
    assert artifact.transcript_text == "Расшифровка видео."
    assert artifact.transcript_source == "whisper"
    assert artifact.url == "https://www.youtube.com/watch?v=vid12345"
    assert youtube_handler.reuse_youtube_artifact("https://www.youtube.com/watch?v=other123") is None
