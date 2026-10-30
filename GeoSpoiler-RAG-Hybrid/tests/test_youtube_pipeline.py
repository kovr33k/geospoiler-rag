import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import llm_backend  # noqa: E402
from enricher.pipeline import _is_youtube_only_normalized_document  # noqa: E402
from enricher.youtube_pipeline import (  # noqa: E402
    _build_segment_card,
    _checkpoint_fingerprint,
    _checkpoint_segment,
    _episode_card_path,
    _episode_source_id,
    _file_sha256,
    _finish_payload,
    _initialize_checkpoint,
    _iter_legacy_sources,
    _load_reusable_segment,
    _manifest_path,
    _merge_episode_payload,
    _normalize_quality_flags,
    _publish_generation,
    _source_fingerprint,
    _validate_generation,
    enrich_youtube_all,
    load_dedicated_youtube_artifact,
)
from enricher.youtube_segmenter import build_segment_specs, youtube_timestamp_url  # noqa: E402
from models import EnrichedCardV2, LLMPayload, NormalizedMeta, Provenance, YouTubeSegmentCardV2  # noqa: E402
from normalizer.youtube_handler import (  # noqa: E402
    _srt_to_cues,
    is_valid_youtube_url,
    redact_sensitive_url,
    validate_youtube_url,
)
from retrieval.card_fts import (
    list_youtube_segment_ids,
    rebuild_youtube_segment_index,
    search_youtube_segments,
)  # noqa: E402


def _payload(summary: str = "Китай обсуждал поставки компонентов.") -> dict:
    return {
        "summary": summary,
        "key_points": [{
            "text": summary,
            "type": "reported_statement",
            "importance": "high",
            "evidence": summary,
        }],
        "entities": {
            "people": [], "organizations": [], "countries": [{"text": "Китай"}],
            "locations": [], "military_units": [], "equipment": [], "weapons": [],
            "programs_projects": [], "media_sources": [], "other": [],
        },
        "topics": [{"label": "поставки компонентов", "salience": "primary", "type": "technology_topic"}],
        "theses": [], "quotes": [], "events": [],
        "search_phrases": [{"text": "Китай поставки компонентов", "source": "phrase_from_text"}],
        "quality_flags": [],
    }


class YouTubeSegmenterTests(unittest.TestCase):
    def test_persistent_language_failure_is_reported_for_episode_guard(self):
        raw = {"summary": "Україна ухвалила важливе рішення."}

        with patch("enricher.repair._call_llm", return_value=raw):
            payload, issues = _finish_payload(raw, "Україна ухвалила важливе рішення.")

        self.assertEqual(payload.summary, "")
        self.assertIn("extraction_unstable", payload.quality_flags)
        self.assertTrue(any(issue.startswith("Russian-language contract violation") for issue in issues))

    def test_llm_cannot_assign_pipeline_owned_youtube_quality_flags(self):
        payload = LLMPayload(
            summary="Substantive summary",
            quality_flags=[
                "partial_segment_failure",
                "extraction_unstable",
                "transcript_unavailable",
                "timestamps_unavailable",
                "mixed_topics",
            ],
        )

        _normalize_quality_flags(payload)

        self.assertEqual(payload.quality_flags, ["mixed_topics"])

    def test_youtube_url_validation_rejects_non_youtube_hosts(self):
        self.assertTrue(is_valid_youtube_url("https://www.youtube.com/watch?v=abc123"))
        self.assertTrue(is_valid_youtube_url("https://youtu.be/abc123"))
        self.assertTrue(is_valid_youtube_url("https://www.youtube.com/live/abc123"))
        self.assertFalse(is_valid_youtube_url("http://www.youtube.com/watch?v=abc123"))
        self.assertFalse(is_valid_youtube_url("https://127.0.0.1/?next=youtube.com/watch?v=abc123"))
        self.assertFalse(is_valid_youtube_url("https://user:pass@youtube.com/watch?v=abc123"))
        self.assertFalse(is_valid_youtube_url("https://youtube.com:8443/watch?v=abc123"))
        self.assertFalse(is_valid_youtube_url("https://youtube.com/redirect?q=http://127.0.0.1"))
        self.assertFalse(is_valid_youtube_url("https://youtube.com/attribution_link"))
        self.assertEqual(
            validate_youtube_url("https://youtu.be/abc123?t=10"),
            "https://www.youtube.com/watch?v=abc123",
        )
        self.assertEqual(
            redact_sensitive_url("https://example.test/story?accessToken=secret&lang=ru"),
            "https://example.test/story?accessToken=%5BREDACTED%5D&lang=ru",
        )

    def test_dedicated_artifact_rejects_non_youtube_metadata_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            metadata = root / "abc.youtube.meta.json"
            transcript = root / "abc.youtube.txt"
            cues = root / "abc.youtube.cues.json"
            metadata.write_text(
                json.dumps(
                    {
                        "video_id": "abc",
                        "url": "https://evil.test/watch?v=abc",
                        "transcript_source": "subtitles",
                    }
                ),
                encoding="utf-8",
            )
            transcript.write_text("Usable transcript.", encoding="utf-8")
            cues.write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "valid YouTube URL"):
                load_dedicated_youtube_artifact(metadata, transcript, cues)

    def test_dedicated_artifact_rejects_invalid_nested_chapters_and_cues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            metadata = root / "abc.youtube.meta.json"
            transcript = root / "abc.youtube.txt"
            cues = root / "abc.youtube.cues.json"
            metadata.write_text(
                json.dumps(
                    {
                        "video_id": "abc",
                        "url": "https://www.youtube.com/watch?v=abc",
                        "transcript_source": "subtitles",
                        "chapters": ["not-an-object"],
                    }
                ),
                encoding="utf-8",
            )
            transcript.write_text("Usable transcript.", encoding="utf-8")
            cues.write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "chapter 0"):
                load_dedicated_youtube_artifact(metadata, transcript, cues)

            metadata.write_text(
                json.dumps(
                    {
                        "video_id": "abc",
                        "url": "https://www.youtube.com/watch?v=abc",
                        "transcript_source": "subtitles",
                    }
                ),
                encoding="utf-8",
            )
            cues.write_text(json.dumps(["not-an-object"]), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cue 0"):
                load_dedicated_youtube_artifact(metadata, transcript, cues)

    def test_srt_cues_keep_exact_boundaries_and_remove_repeats(self):
        cues = _srt_to_cues(
            "1\n00:00:01,000 --> 00:00:03,000\nКитай поставил детали.\n\n"
            "2\n00:00:03,000 --> 00:00:05,000\nКитай поставил детали.\n\n"
            "3\n00:00:06,000 --> 00:00:08,000\nНачалась дискуссия.\n"
        )

        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start_seconds, 1.0)
        self.assertEqual(cues[0].end_seconds, 5.0)
        self.assertEqual(cues[1].end_seconds, 8.0)

    def test_auto_srt_cues_stitch_rolling_overlaps(self):
        cues = _srt_to_cues(
            "1\n00:00:01,000 --> 00:00:02,000\nAlpha starts\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\nAlpha starts with overlap\n\n"
            "3\n00:00:03,000 --> 00:00:03,100\nwith overlap\n\n"
            "4\n00:00:03,100 --> 00:00:04,000\nwith overlap then continues\n\n"
            "5\n00:00:04,000 --> 00:00:04,100\nthen continues\n"
        )

        self.assertEqual(
            [cue.text for cue in cues],
            ["Alpha starts", "with overlap", "then continues"],
        )
        self.assertEqual(cues[0].end_seconds, 2.0)
        self.assertEqual(cues[1].end_seconds, 3.1)
        self.assertEqual(cues[2].end_seconds, 4.1)

    def test_manual_srt_cues_keep_partial_overlap(self):
        cues = _srt_to_cues(
            "1\n00:00:01,000 --> 00:00:02,000\nAlpha starts\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\nAlpha starts with overlap\n",
            remove_overlaps=False,
        )

        self.assertEqual(
            [cue.text for cue in cues],
            ["Alpha starts", "Alpha starts with overlap"],
        )

    def test_auto_srt_cues_keep_repetition_inside_one_caption(self):
        cues = _srt_to_cues(
            "1\n00:00:01,000 --> 00:00:03,000\nNo, no, this is deliberate.\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nthis is deliberate. and new words\n"
        )

        self.assertEqual(cues[0].text, "No, no, this is deliberate.")
        self.assertEqual(cues[1].text, "and new words")

    def test_plain_segments_have_no_fake_timestamps(self):
        specs = build_segment_specs("Китай поставил детали. Россия обсудила поставки.")
        self.assertEqual(len(specs), 1)
        self.assertIsNone(specs[0].start_seconds)
        self.assertEqual(
            youtube_timestamp_url("https://www.youtube.com/watch?v=abc", None),
            "",
        )

    def test_unpunctuated_text_obeys_hard_segment_limit(self):
        specs = build_segment_specs("word " * 4000)
        self.assertGreaterEqual(len(specs), 2)
        self.assertTrue(all(len(spec.text) <= 9000 for spec in specs))

    def test_url_only_legacy_document_is_not_a_generic_card(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            text_path = Path(tmpdir) / "1.txt"
            text_path.write_text(
                "[YouTube]\nURL: https://www.youtube.com/watch?v=abc123\n\nTranscript",
                encoding="utf-8",
            )
            only_meta = NormalizedMeta(
                youtube_urls=["https://www.youtube.com/watch?v=abc123"],
                has_text=True,
                has_body_text=None,
            )
            mixed_meta = NormalizedMeta(
                youtube_urls=["https://www.youtube.com/watch?v=abc123"],
                has_text=True,
                has_body_text=True,
            )
            self.assertTrue(_is_youtube_only_normalized_document(only_meta, text_path))
            self.assertFalse(_is_youtube_only_normalized_document(mixed_meta, text_path))


class YouTubePipelineTests(unittest.TestCase):
    def test_youtube_segment_v2_requires_enrichment_model(self):
        base = {
            "schema_version": "youtube_segment_v2",
            "segment_id": "episode:segment:0",
            "parent_source_id": "episode",
            "video_id": "abc",
            "segment_index": 0,
        }
        with self.assertRaises(ValueError):
            YouTubeSegmentCardV2.model_validate(base)
        card = YouTubeSegmentCardV2.model_validate({**base, "enrichment_model": "codex-cli:test"})
        self.assertEqual(card.schema_version, "youtube_segment_v2")

    def test_profile_switch_uses_a_different_checkpoint_and_does_not_reuse_segments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = {
                "video_id": "abc",
                "url": "https://www.youtube.com/watch?v=abc",
                "channel": "Test channel",
                "telegram_channel": "Channel",
                "telegram_channel_id": 1,
                "message_id": 10,
                "text_path": str(root / "normalized.txt"),
            }
            spec = build_segment_specs("Expected transcript for this segment.")[0]
            original_profile = config.LLM_PROFILE
            original_model = config.CODEX_LUNA_MODEL
            original_enrichment_model = config.ENRICHMENT_MODEL
            try:
                config.LLM_PROFILE = "current"
                config.ENRICHMENT_MODEL = "api-enrichment-test"
                source_fingerprint = _source_fingerprint(source, spec.text)
                current_checkpoint = _checkpoint_fingerprint(source_fingerprint)
                segment = _build_segment_card(
                    source,
                    spec,
                    LLMPayload.model_validate(_payload()),
                    [],
                )
                with patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"), \
                     patch.object(config, "YOUTUBE_SEGMENTS_DIR", root / "segments"):
                    _initialize_checkpoint(source, current_checkpoint, [spec])
                    _checkpoint_segment(source, current_checkpoint, segment)
                    self.assertIsNotNone(
                        _load_reusable_segment(
                            source,
                            spec,
                            source_fingerprint,
                            checkpoint_fingerprint=current_checkpoint,
                        )
                    )

                    config.LLM_PROFILE = "luna"
                    config.CODEX_LUNA_MODEL = "codex-test-model"
                    luna_checkpoint = _checkpoint_fingerprint(source_fingerprint)
                    self.assertNotEqual(current_checkpoint, luna_checkpoint)
                    self.assertIsNone(
                        _load_reusable_segment(
                            source,
                            spec,
                            source_fingerprint,
                            checkpoint_fingerprint=luna_checkpoint,
                        )
                    )
            finally:
                config.LLM_PROFILE = original_profile
                config.CODEX_LUNA_MODEL = original_model
                config.ENRICHMENT_MODEL = original_enrichment_model

    def test_generation_validator_rejects_segment_content_with_matching_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = {
                "video_id": "abc",
                "url": "https://www.youtube.com/watch?v=abc",
                "channel": "Test channel",
                "telegram_channel": "Channel",
                "telegram_channel_id": 1,
                "message_id": 10,
                "text_path": str(root / "normalized.txt"),
            }
            transcript = "Expected transcript for this segment."
            specs = build_segment_specs(transcript)
            segment = _build_segment_card(
                source,
                specs[0],
                LLMPayload.model_validate(_payload()),
                [],
            )
            card_path = root / "enriched" / "1" / "10.youtube.abc.enriched.json"
            card = EnrichedCardV2(
                schema_version="enriched_v2",
                prompt_version=config.YOUTUBE_ENRICHMENT_PROMPT_VERSION,
                enrichment_model=llm_backend.active_model_for("enrichment"),
                enriched_at="2026-01-01T00:00:00+00:00",
                provenance=Provenance(
                    source_id=_episode_source_id(source),
                    source_type="youtube",
                ),
            )
            fingerprint = _source_fingerprint(source, transcript)

            with patch.object(config, "PROJECT_ROOT", root), \
                 patch.object(config, "ENRICHED_DIR", root / "enriched"), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", root / "segments"):
                _publish_generation(source, card_path, card, [segment], fingerprint, True)
                self.assertTrue(
                    _validate_generation(card_path, source, fingerprint, specs).valid
                )

                segment_path = root / "segments" / "1" / "10" / "abc" / "0000.youtube-segment.json"
                segment_data = json.loads(segment_path.read_text(encoding="utf-8"))
                segment_data["transcript_text"] = "Wrong transcript"
                segment_path.write_text(
                    json.dumps(segment_data, ensure_ascii=False),
                    encoding="utf-8",
                )
                manifest_path = card_path.with_name(f"{card_path.name}.manifest.json")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["segment_sha256"][segment_path.name] = _file_sha256(segment_path)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                validation = _validate_generation(card_path, source, fingerprint, specs)

            self.assertFalse(validation.valid)
            self.assertIn("segment_transcript_mismatch:0000.youtube-segment.json", validation.violations)

    def test_partial_episode_merge_timeout_uses_validated_segment_fallback(self):
        source = {
            "video_id": "abc",
            "url": "https://www.youtube.com/watch?v=abc",
            "title": "Test video",
        }
        segment_payloads = [_payload(f"Сегмент {index} содержит итог.") for index in range(5)]

        with patch(
            "enricher.youtube_pipeline._finish_payload",
            return_value=(LLMPayload(quality_flags=["extraction_unstable"]), ["merge timeout"]),
        ):
            payload, issues = _merge_episode_payload(source, "Содержательный transcript", segment_payloads)

        self.assertIn("Сегмент 0", payload.summary)
        self.assertTrue(payload.key_points)
        self.assertEqual(issues, [])
        self.assertNotIn("extraction_unstable", payload.quality_flags)

    def test_compact_merge_keeps_odd_singleton_batch_valid(self):
        source = {
            "video_id": "abc",
            "url": "https://www.youtube.com/watch?v=abc",
            "title": "Test video",
        }
        # Large summaries force pairs per merge batch, leaving the 7th segment alone.
        segment_payloads = [_payload(f"Сегмент {index}. " + "Китай обсуждал поставки. " * 400) for index in range(7)]
        merged = (LLMPayload.model_validate(_payload("Итог эпизода.")), [])

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(config, "ENRICHED_DIR", Path(tmpdir)), \
             patch("enricher.youtube_pipeline.merge_chunk_results_raw", return_value={}), \
             patch("enricher.youtube_pipeline._finish_payload", return_value=merged):
            payload, issues = _merge_episode_payload(source, "Содержательный transcript", segment_payloads)

        self.assertEqual(issues, [])
        self.assertEqual(payload.summary, "Итог эпизода.")
        self.assertEqual([item.text for item in payload.entities.countries], ["Китай"])
        self.assertTrue(payload.search_phrases)

    def test_partial_generation_does_not_reuse_segment_with_wrong_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = {
                "video_id": "abc",
                "url": "https://www.youtube.com/watch?v=abc",
                "channel": "Test channel",
                "telegram_channel": "Channel",
                "telegram_channel_id": 1,
                "message_id": 10,
                "text_path": str(root / "normalized.txt"),
            }
            specs = build_segment_specs("Expected transcript for reuse.")
            segment = _build_segment_card(
                source,
                specs[0],
                LLMPayload.model_validate(_payload()),
                [],
            )
            segment_data = segment.model_dump(mode="json")
            segment_data["segment_id"] = "wrong-id"
            fingerprint = _source_fingerprint(source, specs[0].text)
            with patch.object(config, "ENRICHED_DIR", root / "enriched"), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", root / "segments"):
                segment_path = root / "segments" / "1" / "10" / "abc" / "0000.youtube-segment.json"
                segment_path.parent.mkdir(parents=True)
                segment_path.write_text(json.dumps(segment_data), encoding="utf-8")
                manifest_path = _manifest_path(_episode_card_path(source))
                manifest_path.parent.mkdir(parents=True)
                manifest_path.write_text(
                    json.dumps({
                        "fingerprint": fingerprint,
                        "status": "partial",
                        "segment_sha256": {segment_path.name: _file_sha256(segment_path)},
                    }),
                    encoding="utf-8",
                )

                reused = _load_reusable_segment(source, specs[0], fingerprint)

        self.assertIsNone(reused)

    def test_malformed_published_manifest_is_treated_as_cache_miss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = {
                "video_id": "abc",
                "url": "https://www.youtube.com/watch?v=abc",
                "channel": "Test channel",
                "telegram_channel": "Channel",
                "telegram_channel_id": 1,
                "message_id": 10,
                "text_path": str(root / "normalized.txt"),
            }
            spec = build_segment_specs("Expected transcript.")[0]
            fingerprint = _source_fingerprint(source, spec.text)
            with patch.object(config, "ENRICHED_DIR", root / "enriched"), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", root / "segments"), \
                 patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"):
                manifest_path = _manifest_path(_episode_card_path(source))
                manifest_path.parent.mkdir(parents=True)
                manifest_path.write_text("[]", encoding="utf-8")

                reused = _load_reusable_segment(source, spec, fingerprint)

        self.assertIsNone(reused)

    def test_malformed_published_manifest_falls_back_to_valid_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = {
                "video_id": "abc",
                "url": "https://www.youtube.com/watch?v=abc",
                "channel": "Test channel",
                "telegram_channel": "Channel",
                "telegram_channel_id": 1,
                "message_id": 10,
                "text_path": str(root / "normalized.txt"),
            }
            spec = build_segment_specs("Expected transcript.")[0]
            fingerprint = _source_fingerprint(source, spec.text)
            segment = _build_segment_card(
                source,
                spec,
                LLMPayload.model_validate(_payload()),
                [],
            )
            with patch.object(config, "ENRICHED_DIR", root / "enriched"), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", root / "segments"), \
                 patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"):
                _initialize_checkpoint(source, fingerprint, [spec])
                _checkpoint_segment(source, fingerprint, segment)
                manifest_path = _manifest_path(_episode_card_path(source))
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text("[]", encoding="utf-8")

                reused = _load_reusable_segment(source, spec, fingerprint)

        self.assertIsNotNone(reused)
        self.assertEqual(reused.segment_id, segment.segment_id)

    def test_malformed_published_segment_hashes_are_treated_as_cache_miss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = {
                "video_id": "abc",
                "url": "https://www.youtube.com/watch?v=abc",
                "channel": "Test channel",
                "telegram_channel": "Channel",
                "telegram_channel_id": 1,
                "message_id": 10,
                "text_path": str(root / "normalized.txt"),
            }
            spec = build_segment_specs("Expected transcript.")[0]
            fingerprint = _source_fingerprint(source, spec.text)
            with patch.object(config, "ENRICHED_DIR", root / "enriched"), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", root / "segments"), \
                 patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"):
                manifest_path = _manifest_path(_episode_card_path(source))
                manifest_path.parent.mkdir(parents=True)
                manifest_path.write_text(
                    json.dumps(
                        {
                            "fingerprint": fingerprint,
                            "status": "partial",
                            "segment_sha256": [],
                        }
                    ),
                    encoding="utf-8",
                )

                reused = _load_reusable_segment(source, spec, fingerprint)

        self.assertIsNone(reused)

    def test_invalid_dedicated_metadata_is_reported_without_stopping_other_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized = root / "normalized_youtube" / "Channel" / "10"
            normalized.mkdir(parents=True)
            (normalized / "bad.youtube.meta.json").write_text("[]", encoding="utf-8")
            text_path = normalized / "good.youtube.txt"
            cues_path = normalized / "good.youtube.cues.json"
            text_path.write_text("A short YouTube transcript.", encoding="utf-8")
            cues_path.write_text("[]", encoding="utf-8")
            (normalized / "good.youtube.meta.json").write_text(
                json.dumps({
                    "url": "https://www.youtube.com/watch?v=good",
                    "video_id": "good",
                    "title": "Good video",
                    "channel": "Test channel",
                    "duration_seconds": 30,
                    "transcript_source": "subtitles",
                    "transcript_path": str(text_path.relative_to(root)),
                    "cues_path": str(cues_path.relative_to(root)),
                    "telegram_source": {
                        "channel_name": "Channel",
                        "channel_id": 1,
                        "message_id": 10,
                    },
                }),
                encoding="utf-8",
            )

            with patch.object(config, "PROJECT_ROOT", root), \
                 patch.object(config, "YOUTUBE_NORMALIZED_DIR", root / "normalized_youtube"), \
                 patch.object(config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(config, "ENRICHED_DIR", root / "enriched"), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", root / "segments"), \
                 patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"), \
                 patch.object(config, "REVIEW_QUEUE_DIR", root / "review_queue"), \
                 patch("enricher.youtube_pipeline.extract_full_post_raw", return_value=_payload()):
                stats = enrich_youtube_all(force=True)

            self.assertEqual(stats.sources_scanned, 1)
            self.assertEqual(stats.episodes_written, 1)
            self.assertEqual(stats.failed_sources, 1)

    def test_legacy_fallback_does_not_turn_mixed_telegram_text_into_transcript(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            channel = root / "normalized" / "Channel"
            channel.mkdir(parents=True)
            (channel / "10.txt").write_text(
                "[Telegram]\n\nOriginal Telegram text.\n\n"
                "[YouTube transcript stored separately: artifact.txt]",
                encoding="utf-8",
            )
            (channel / "10.meta.json").write_text(
                json.dumps({
                    "channel_name": "Channel",
                    "message_id": 10,
                    "youtube_urls": ["https://www.youtube.com/watch?v=abc"],
                    "has_text": True,
                }),
                encoding="utf-8",
            )

            with patch.object(config, "NORMALIZED_DIR", root / "normalized"):
                self.assertEqual(list(_iter_legacy_sources(None)), [])

    def test_long_source_writes_episode_and_child_segments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized = root / "normalized_youtube" / "Channel" / "10"
            enriched = root / "enriched"
            segments = root / "enriched_segments"
            normalized.mkdir(parents=True)
            text_path = normalized / "abc.youtube.txt"
            text_path.write_text("Китай обсуждал поставки. " * 700, encoding="utf-8")
            cues_path = normalized / "abc.youtube.cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            meta_path = normalized / "abc.youtube.meta.json"
            meta_path.write_text(
                json.dumps({
                    "url": "https://www.youtube.com/watch?v=abc",
                    "video_id": "abc",
                    "title": "Поставки компонентов",
                    "channel": "Test channel",
                    "duration_seconds": 1800,
                    "language": "ru",
                    "transcript_source": "subtitles",
                    "transcript_path": str(text_path.relative_to(root)),
                    "cues_path": str(cues_path.relative_to(root)),
                    "telegram_source": {
                        "channel_name": "Channel", "channel_id": 1,
                        "message_id": 10, "date": "2026-01-01T00:00:00",
                        "post_url": "https://t.me/c/1/10",
                    },
                }),
                encoding="utf-8",
            )

            with patch.object(config, "PROJECT_ROOT", root), \
                 patch.object(config, "YOUTUBE_NORMALIZED_DIR", root / "normalized_youtube"), \
                 patch.object(config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(config, "ENRICHED_DIR", enriched), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", segments), \
                 patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"), \
                 patch.object(config, "REVIEW_QUEUE_DIR", root / "review_queue"), \
                 patch("enricher.youtube_pipeline.extract_chunk_raw", return_value=_payload()), \
                 patch("enricher.youtube_pipeline.merge_chunk_results_raw", return_value=_payload()):
                stats = enrich_youtube_all(force=True)

            self.assertEqual(stats.sources_scanned, 1)
            self.assertEqual(stats.episodes_written, 1)
            self.assertGreaterEqual(stats.segments_written, 2)
            episode = next(enriched.rglob("*.enriched.json"))
            data = json.loads(episode.read_text(encoding="utf-8"))
            self.assertEqual(data["content_type"], "youtube_transcript")
            self.assertTrue(list(segments.rglob("*.youtube-segment.json")))

    def test_legacy_cleanup_failure_does_not_rollback_new_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized = root / "normalized_youtube" / "Channel" / "10"
            enriched = root / "enriched"
            segments = root / "enriched_segments"
            normalized.mkdir(parents=True)
            text_path = normalized / "abc.youtube.txt"
            text_path.write_text("Short transcript about policy.", encoding="utf-8")
            cues_path = normalized / "abc.youtube.cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            (normalized / "abc.youtube.meta.json").write_text(
                json.dumps({
                    "url": "https://www.youtube.com/watch?v=abc",
                    "video_id": "abc",
                    "title": "Test video",
                    "duration_seconds": 30,
                    "transcript_source": "subtitles",
                    "transcript_path": str(text_path.relative_to(root)),
                    "cues_path": str(cues_path.relative_to(root)),
                    "telegram_source": {
                        "channel_name": "Channel",
                        "channel_id": 1,
                        "message_id": 10,
                    },
                }),
                encoding="utf-8",
            )
            legacy = enriched / "Channel" / "10.youtube.abc.enriched.json"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text("legacy", encoding="utf-8")

            with patch.object(config, "PROJECT_ROOT", root), \
                 patch.object(config, "YOUTUBE_NORMALIZED_DIR", root / "normalized_youtube"), \
                 patch.object(config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(config, "ENRICHED_DIR", enriched), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", segments), \
                 patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"), \
                 patch.object(config, "REVIEW_QUEUE_DIR", root / "review_queue"), \
                 patch("enricher.youtube_pipeline.extract_full_post_raw", return_value=_payload()), \
                 patch(
                     "enricher.youtube_pipeline._remove_legacy_generation",
                     side_effect=OSError("cleanup unavailable"),
                 ):
                stats = enrich_youtube_all(force=True)

            new_card = enriched / "1" / "10.youtube.abc.enriched.json"
            self.assertEqual(stats.episodes_written, 1)
            self.assertTrue(new_card.exists())

    def test_publish_keyboard_interrupt_restores_previous_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            card_path = root / "enriched" / "1" / "10.youtube.abc.enriched.json"
            manifest_path = card_path.with_name(f"{card_path.name}.manifest.json")
            segment_dir = root / "segments" / "1" / "10" / "abc"
            segment_dir.mkdir(parents=True)
            card_path.parent.mkdir(parents=True)
            card_path.write_text("old-card", encoding="utf-8")
            manifest_path.write_text("old-manifest", encoding="utf-8")
            (segment_dir / "0000.youtube-segment.json").write_text("old-segment", encoding="utf-8")
            source = {
                "video_id": "abc",
                "url": "https://www.youtube.com/watch?v=abc",
                "channel": "Test channel",
                "telegram_channel": "Channel",
                "telegram_channel_id": 1,
                "message_id": 10,
                "text_path": str(root / "normalized.txt"),
            }
            new_card = EnrichedCardV2(
                schema_version="enriched_v2",
                prompt_version=config.YOUTUBE_ENRICHMENT_PROMPT_VERSION,
                enrichment_model=llm_backend.active_model_for("enrichment"),
                enriched_at="2026-01-01T00:00:00+00:00",
                provenance=Provenance(
                    source_id=_episode_source_id(source),
                    source_type="youtube",
                ),
            )
            original_replace = Path.replace
            new_spec = build_segment_specs("New transcript for the replacement segment.")[0]
            new_segment = _build_segment_card(
                source,
                new_spec,
                LLMPayload.model_validate(_payload()),
                [],
            )

            def interrupt_manifest_swap(path: Path, target: Path):
                if path.name.startswith(f".{manifest_path.name}.") and target == manifest_path:
                    raise KeyboardInterrupt
                return original_replace(path, target)

            with patch.object(config, "YOUTUBE_SEGMENTS_DIR", root / "segments"), \
                 patch.object(Path, "replace", new=interrupt_manifest_swap):
                with self.assertRaises(KeyboardInterrupt):
                    _publish_generation(
                        source,
                        card_path,
                        new_card,
                        [new_segment],
                        "fingerprint",
                        True,
                    )

            self.assertEqual(card_path.read_text(encoding="utf-8"), "old-card")
            self.assertEqual(manifest_path.read_text(encoding="utf-8"), "old-manifest")
            self.assertEqual(
                (segment_dir / "0000.youtube-segment.json").read_text(encoding="utf-8"),
                "old-segment",
            )
            self.assertFalse(list(card_path.parent.glob("*.old")))
            self.assertFalse(list(segment_dir.parent.glob("*.old")))

    def test_youtube_fingerprint_changes_when_source_path_changes(self):
        source = {
            "video_id": "abc",
            "url": "https://www.youtube.com/watch?v=abc",
            "text_path": "normalized/old.txt",
        }
        moved = {**source, "text_path": "normalized/new.txt"}
        self.assertNotEqual(
            _source_fingerprint(source, "same transcript"),
            _source_fingerprint(moved, "same transcript"),
        )

    def test_force_regeneration_removes_stale_segments_when_source_becomes_short(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized = root / "normalized_youtube" / "Channel" / "10"
            enriched = root / "enriched"
            segments = root / "enriched_segments"
            normalized.mkdir(parents=True)
            text_path = normalized / "abc.youtube.txt"
            cues_path = normalized / "abc.youtube.cues.json"
            meta_path = normalized / "abc.youtube.meta.json"
            text_path.write_text("Long transcript. " * 700, encoding="utf-8")
            cues_path.write_text("[]", encoding="utf-8")
            meta = {
                "url": "https://www.youtube.com/watch?v=abc123",
                "video_id": "abc123",
                "title": "Test video",
                "channel": "Test channel",
                "duration_seconds": 1800,
                "language": "ru",
                "transcript_source": "subtitles",
                "transcript_path": str(text_path.relative_to(root)),
                "cues_path": str(cues_path.relative_to(root)),
                "telegram_source": {"channel_name": "Channel", "channel_id": 1, "message_id": 10},
            }
            meta_path.write_text(json.dumps(meta), encoding="utf-8")

            with patch.object(config, "PROJECT_ROOT", root), \
                 patch.object(config, "YOUTUBE_NORMALIZED_DIR", root / "normalized_youtube"), \
                 patch.object(config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(config, "ENRICHED_DIR", enriched), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", segments), \
                 patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"), \
                 patch.object(config, "REVIEW_QUEUE_DIR", root / "review_queue"), \
                 patch("enricher.youtube_pipeline.extract_chunk_raw", return_value=_payload()), \
                 patch("enricher.youtube_pipeline.merge_chunk_results_raw", return_value=_payload()), \
                 patch("enricher.youtube_pipeline.extract_full_post_raw", return_value=_payload()):
                first = enrich_youtube_all(force=True)
                self.assertGreaterEqual(first.segments_written, 2)
                self.assertTrue(list(segments.rglob("*.youtube-segment.json")))

                text_path.write_text("Short transcript.", encoding="utf-8")
                meta["duration_seconds"] = 30
                meta_path.write_text(json.dumps(meta), encoding="utf-8")
                second = enrich_youtube_all(force=True)

            self.assertEqual(second.segments_written, 0)
            self.assertFalse(list(segments.rglob("*.youtube-segment.json")))

    def test_partial_run_retries_only_failed_segments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized = root / "normalized_youtube" / "Channel" / "10"
            enriched = root / "enriched"
            segments = root / "enriched_segments"
            normalized.mkdir(parents=True)
            text_path = normalized / "abc.youtube.txt"
            text_path.write_text("Китай обсуждал поставки компонентов. " * 700, encoding="utf-8")
            cues_path = normalized / "abc.youtube.cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            meta_path = normalized / "abc.youtube.meta.json"
            meta_path.write_text(
                json.dumps({
                    "url": "https://www.youtube.com/watch?v=abc123",
                    "video_id": "abc123",
                    "title": "Test video",
                    "channel": "Test channel",
                    "duration_seconds": 1800,
                    "language": "en",
                    "transcript_source": "subtitles",
                    "transcript_path": str(text_path.relative_to(root)),
                    "cues_path": str(cues_path.relative_to(root)),
                    "telegram_source": {"channel_name": "Channel", "channel_id": 1, "message_id": 10},
                }),
                encoding="utf-8",
            )
            calls = 0

            def extract_segment(*_args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("transient segment failure")
                return _payload()

            with patch.object(config, "PROJECT_ROOT", root), \
                 patch.object(config, "YOUTUBE_NORMALIZED_DIR", root / "normalized_youtube"), \
                 patch.object(config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(config, "ENRICHED_DIR", enriched), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", segments), \
                 patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"), \
                 patch.object(config, "REVIEW_QUEUE_DIR", root / "review_queue"), \
                 patch("enricher.youtube_pipeline.extract_chunk_raw", side_effect=extract_segment), \
                 patch("enricher.youtube_pipeline.merge_chunk_results_raw", return_value=_payload()):
                first = enrich_youtube_all(force=True)
                calls_after_first = calls
                second = enrich_youtube_all(force=True)

            self.assertEqual(first.partial_sources, 1)
            self.assertEqual(second.partial_sources, 0)
            self.assertEqual(calls, calls_after_first + 1)

    def test_segment_language_failure_publishes_partial_episode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized = root / "normalized_youtube" / "Channel" / "10"
            enriched = root / "enriched"
            segments = root / "enriched_segments"
            normalized.mkdir(parents=True)
            text_path = normalized / "abc.youtube.txt"
            text_path.write_text("Китай обсуждал поставки компонентов. " * 700, encoding="utf-8")
            cues_path = normalized / "abc.youtube.cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            (normalized / "abc.youtube.meta.json").write_text(
                json.dumps({
                    "url": "https://www.youtube.com/watch?v=abc123",
                    "video_id": "abc123",
                    "title": "Test video",
                    "channel": "Test channel",
                    "duration_seconds": 1800,
                    "language": "uk",
                    "transcript_source": "whisper",
                    "transcript_path": str(text_path.relative_to(root)),
                    "cues_path": str(cues_path.relative_to(root)),
                    "telegram_source": {"channel_name": "Channel", "channel_id": 1, "message_id": 10},
                }),
                encoding="utf-8",
            )
            ukrainian = _payload("Україна ухвалила важливе рішення.")
            calls = 0

            def extract_segment(*_args):
                nonlocal calls
                calls += 1
                return ukrainian if calls == 1 else _payload()

            with patch.object(config, "PROJECT_ROOT", root), \
                 patch.object(config, "YOUTUBE_NORMALIZED_DIR", root / "normalized_youtube"), \
                 patch.object(config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(config, "ENRICHED_DIR", enriched), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", segments), \
                 patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"), \
                 patch.object(config, "REVIEW_QUEUE_DIR", root / "review_queue"), \
                 patch("enricher.repair._call_llm", return_value=ukrainian), \
                 patch("enricher.youtube_pipeline.extract_chunk_raw", side_effect=extract_segment), \
                 patch("enricher.youtube_pipeline.merge_chunk_results_raw", return_value=_payload()):
                stats = enrich_youtube_all(force=True)

            self.assertEqual(stats.failed_sources, 0)
            self.assertEqual(stats.episodes_written, 1)
            self.assertEqual(stats.partial_sources, 1)
            episode = json.loads(next(enriched.rglob("*.enriched.json")).read_text(encoding="utf-8"))
            self.assertIn("partial_segment_failure", episode["quality_flags"])
            self.assertNotRegex(episode["summary"], "[іїєґ]")

    def test_interrupted_run_resumes_from_checkpointed_segments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized = root / "normalized_youtube" / "Channel" / "10"
            enriched = root / "enriched"
            segments = root / "enriched_segments"
            normalized.mkdir(parents=True)
            text_path = normalized / "abc.youtube.txt"
            transcript = "China discussed component deliveries. " * 700
            text_path.write_text(transcript, encoding="utf-8")
            cues_path = normalized / "abc.youtube.cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            meta_path = normalized / "abc.youtube.meta.json"
            meta_path.write_text(
                json.dumps({
                    "url": "https://www.youtube.com/watch?v=abc123",
                    "video_id": "abc123",
                    "title": "Test video",
                    "channel": "Test channel",
                    "duration_seconds": 1800,
                    "transcript_source": "subtitles",
                    "transcript_path": str(text_path.relative_to(root)),
                    "cues_path": str(cues_path.relative_to(root)),
                    "telegram_source": {
                        "channel_name": "Channel",
                        "channel_id": 1,
                        "message_id": 10,
                    },
                }),
                encoding="utf-8",
            )
            calls = 0

            def interrupt_on_second_segment(*_args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt
                return _payload()

            expected_segments = len(build_segment_specs(transcript))
            with patch.object(config, "PROJECT_ROOT", root), \
                 patch.object(config, "YOUTUBE_NORMALIZED_DIR", root / "normalized_youtube"), \
                 patch.object(config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(config, "ENRICHED_DIR", enriched), \
                 patch.object(config, "YOUTUBE_SEGMENTS_DIR", segments), \
                 patch.object(config, "YOUTUBE_CHECKPOINT_DIR", root / "checkpoints"), \
                 patch.object(config, "REVIEW_QUEUE_DIR", root / "review_queue"), \
                 patch("enricher.youtube_pipeline.extract_chunk_raw", side_effect=interrupt_on_second_segment), \
                 patch("enricher.youtube_pipeline.merge_chunk_results_raw", return_value=_payload()):
                with self.assertRaises(KeyboardInterrupt):
                    enrich_youtube_all(force=True)

                checkpoint_segments = list((root / "checkpoints").rglob("*.youtube-segment.json"))
                self.assertEqual(len(checkpoint_segments), 1)
                second = enrich_youtube_all(force=True)

            self.assertEqual(second.episodes_written, 1)
            self.assertEqual(second.segments_written, expected_segments)
            self.assertEqual(calls, expected_segments + 1)
            self.assertFalse(list((root / "checkpoints").rglob("manifest.json")))

    def test_segment_fts_returns_all_matching_children(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            segment_dir = root / "segments" / "Channel" / "10" / "abc"
            segment_dir.mkdir(parents=True)
            for index in range(2):
                (segment_dir / f"{index:04d}.youtube-segment.json").write_text(
                    json.dumps({
                        "schema_version": "youtube_segment_v2",
                        "segment_id": f"episode:segment:{index}",
                        "parent_source_id": "episode",
                        "video_id": "abc",
                        "segment_index": index,
                        "start_seconds": index * 60,
                        "end_seconds": index * 60 + 30,
                        "start_url": f"https://youtu.be/abc?t={index * 60}s",
                        "title": "Test video",
                        "enrichment_model": "codex-cli:test",
                        "search_text": f"Китай поставки компонентов часть {index}",
                    }),
                    encoding="utf-8",
                )
            (segment_dir / "legacy.youtube-segment.json").write_text(
                json.dumps({
                    "schema_version": "youtube_segment_v1",
                    "segment_id": "legacy:segment:0",
                    "parent_source_id": "legacy",
                    "video_id": "legacy",
                    "segment_index": 0,
                    "search_text": "Китай поставки старый формат",
                }),
                encoding="utf-8",
            )
            db_path = root / "card_fts.sqlite"
            stats = rebuild_youtube_segment_index(segment_dir.parent.parent.parent, db_path)
            matches = search_youtube_segments("Китай поставки", top_k=None, db_path=db_path)

            self.assertEqual(stats.segments_indexed, 2)
            self.assertEqual(len(matches), 2)
            self.assertEqual({match.parent_source_id for match in matches}, {"episode"})
            self.assertEqual(
                list_youtube_segment_ids("episode", db_path),
                {"episode:segment:0", "episode:segment:1"},
            )

    def test_segment_fts_skips_invalid_v2_cards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            segment_dir = root / "segments" / "Channel" / "10" / "abc"
            segment_dir.mkdir(parents=True)
            (segment_dir / "valid.youtube-segment.json").write_text(
                json.dumps({
                    "schema_version": "youtube_segment_v2",
                    "enrichment_model": "codex-cli:test",
                    "segment_id": "episode:segment:0",
                    "parent_source_id": "episode",
                    "video_id": "abc",
                    "segment_index": 0,
                    "search_text": "valid segment text",
                }),
                encoding="utf-8",
            )
            (segment_dir / "invalid.youtube-segment.json").write_text(
                json.dumps({
                    "schema_version": "youtube_segment_v2",
                    "segment_id": "episode:segment:1",
                    "parent_source_id": "episode",
                    "video_id": "abc",
                    "segment_index": 1,
                    "search_text": "invalid segment text",
                }),
                encoding="utf-8",
            )
            db_path = root / "card_fts.sqlite"
            stats = rebuild_youtube_segment_index(segment_dir.parent.parent.parent, db_path)

            self.assertEqual(stats.segments_seen, 2)
            self.assertEqual(stats.segments_indexed, 1)
            self.assertEqual(list_youtube_segment_ids("episode", db_path), {"episode:segment:0"})


if __name__ == "__main__":
    unittest.main()
