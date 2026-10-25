import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enricher import pipeline as enricher_pipeline  # noqa: E402
from enricher.content_classifier import classify_content  # noqa: E402
from enricher.graph_text_builder import build_graph_text, build_search_text  # noqa: E402
from enricher.llm_enricher import (  # noqa: E402
    _normalize_to_payload,
    _serialize_chunk_results,
)
from enricher.preprocessor import PreprocessedText  # noqa: E402
from enricher.repair import RepairContext, StructuralRepairError, repair_if_needed  # noqa: E402
from enricher.triage import auto_triage  # noqa: E402
from enricher.validator import (
    ValidationResult,  # noqa: E402
    has_required_language_violations,  # noqa: E402
    validate_payload,  # noqa: E402
)
from models import EnrichedCardV2, IgnoredBlock, LLMPayload, NormalizedMeta  # noqa: E402


class ContentClassifierTests(unittest.TestCase):
    def test_known_quality_flag_alias_is_canonicalized_before_validation(self):
        payload = _normalize_to_payload(
            {
                "summary": "Содержательный текст.",
                "quality_flags": ["noisy_boilerplate_detected", "mixed_topics"],
            }
        )

        self.assertEqual(payload.quality_flags, ["mostly_boilerplate", "mixed_topics"])

    def test_mixed_telegram_text_with_instagram_link_stays_telegram(self):
        content_type = classify_content(
            {
                "instagram_urls": ["https://instagram.com/reel/example"],
                "has_body_text": True,
                "has_text": True,
            },
            "[Channel: test]\n\nRussia discussed negotiations.",
        )
        self.assertEqual(content_type, "telegram_post")


class AutoTriageTests(unittest.TestCase):
    def test_short_curated_post_is_kept(self):
        text = (
            "[Канал: Ультра левые и ультра правые | Дата: 2026-02-09 19:05]\n\n"
            "Ультра-левые и ультра-правые совпадают."
        )

        triage, reason = auto_triage("text", {}, text)

        self.assertEqual(triage, "keep")
        self.assertIn("minimum quality", reason)

    def test_native_video_with_caption_is_kept(self):
        text = (
            "[Канал: Балтийские страны | Дата: 2026-03-24 21:18]\n\n"
            "На российском телевидении пропагандисты предлагают захватить эстонский город Нарва.\n"
            "[Видео: пост содержал видео - не обработано]"
        )

        triage, reason = auto_triage("video_native", {"has_video": True}, text)

        self.assertEqual(triage, "keep")
        self.assertIn("caption", reason)

    def test_placeholder_only_video_still_requires_review(self):
        text = (
            "[Канал: Example | Дата: 2026-03-24 21:18]\n\n"
            "[Видео: пост содержал видео - не обработано]"
        )

        triage, reason = auto_triage("video_native", {"has_video": True}, text)

        self.assertEqual(triage, "review")
        self.assertIn("needs Whisper", reason)


class PipelineQualityFlagTests(unittest.TestCase):
    def test_ukrainian_semantic_prose_is_rejected(self):
        payload = LLMPayload(summary="Україна обговорила важливі події.")

        result = validate_payload(payload, "Україна обговорила важливі події.")

        self.assertFalse(result.is_valid)
        self.assertTrue(has_required_language_violations(result))

    def test_mixed_russian_ukrainian_semantic_prose_is_rejected(self):
        payload = LLMPayload(summary="Россия сообщила об этой події.")

        result = validate_payload(payload, "Россия сообщила об этой події.")

        self.assertTrue(has_required_language_violations(result))

    def test_all_required_semantic_collections_enforce_russian(self):
        cases = {
            "key_points": {
                "key_points": [{"text": "Україна ухвалила важливе рішення"}]
            },
            "topics": {"topics": [{"label": "Важливі події"}]},
            "theses": {"theses": [{"text": "Це важливе рішення"}]},
            "events": {"events": [{"description": "Відбулася важлива подія"}]},
        }

        for field_name, values in cases.items():
            with self.subTest(field_name=field_name):
                result = validate_payload(LLMPayload(**values), "Україна ухвалила рішення.")
                self.assertTrue(has_required_language_violations(result))

    def test_non_russian_quote_and_search_phrase_are_allowed(self):
        payload = LLMPayload(
            summary="Президент описал решение правительства.",
            quotes=[{"text": "Україна ухвалила важливе рішення"}],
            search_phrases=[
                {"text": "Україна важливе рішення", "source": "phrase_from_text"}
            ],
        )
        source = "Президент заявил: Україна ухвалила важливе рішення."

        result = validate_payload(payload, source)

        self.assertTrue(result.is_valid, result.violations)

    def test_original_entity_surface_form_is_allowed_in_russian_topic(self):
        payload = LLMPayload(
            summary="Компания представила новую стратегию.",
            entities={"organizations": [{"text": "Українська правда"}]},
            topics=[
                {
                    "label": "Стратегия Українська правда",
                    "salience": "primary",
                    "type": "source_topic",
                }
            ],
        )

        result = validate_payload(payload, "Українська правда представила новую стратегию.")

        self.assertTrue(result.is_valid, result.violations)

    def test_english_semantic_prose_is_rejected_but_latin_entity_is_allowed(self):
        english = LLMPayload(summary="The government announced a major policy decision today.")
        russian = LLMPayload(
            summary="Компания OpenAI представила новую модель для анализа данных.",
            entities={"organizations": [{"text": "OpenAI"}]},
        )

        english_result = validate_payload(english, english.summary)
        russian_result = validate_payload(russian, russian.summary)

        self.assertTrue(has_required_language_violations(english_result))
        self.assertTrue(russian_result.is_valid, russian_result.violations)

    def test_graph_text_preserves_quote_terms_while_search_text_preserves_them(self):
        card = {
            "provenance": {"channel": "Канал", "date": "2026-08-10T00:00:00Z"},
            "summary": "Президент прокомментировал решение.",
            "quotes": [{"speaker": "Президент", "text": "Мы приняли это решение"}],
        }
        card["summary"] += " " + card["quotes"][0]["text"]

        graph_text = build_graph_text(card)
        search_text = build_search_text(card)

        self.assertIn("Мы приняли это решение", graph_text)
        self.assertIn("Мы приняли это решение", search_text)

    def test_graph_text_removes_foreign_quote_without_empty_quote_artifact(self):
        card = {
            "provenance": {"channel": "Канал", "date": "2026-08-10T00:00:00Z"},
            "summary": "В ролике звучит лозунг «Kill the Boer».",
            "quotes": [{"speaker": "source", "text": "Kill the Boer"}],
        }

        graph_text = build_graph_text(card)

        self.assertNotIn("Kill the Boer", graph_text)
        self.assertNotIn("«»", graph_text)
        self.assertIn("В ролике звучит лозунг", graph_text)

    def test_substantive_payload_drops_no_substantive_and_llm_unstable_flags(self):
        payload = LLMPayload(
            summary="Содержательный текст.",
            quality_flags=[
                "no_substantive_content",
                "extraction_unstable",
                "contains_legacy_media_placeholders",
                "possible_duplicate",
                "mixed_topics",
            ],
        )

        enricher_pipeline._normalize_pipeline_quality_flags(payload)

        self.assertEqual(payload.quality_flags, ["mixed_topics"])

    def test_empty_payload_keeps_no_substantive_but_drops_llm_unstable(self):
        payload = LLMPayload(
            quality_flags=[
                "no_substantive_content",
                "extraction_unstable",
                "timestamps_unavailable",
            ],
        )

        enricher_pipeline._normalize_pipeline_quality_flags(payload)

        self.assertEqual(payload.quality_flags, ["no_substantive_content"])

    def test_media_quality_flags_are_derived_from_normalized_metadata(self):
        meta = NormalizedMeta(
            media=[
                {
                    "media_type": "video",
                    "message_id": 10,
                    "download_status": "downloaded",
                    "transcription_status": "failed",
                }
            ]
        )

        self.assertEqual(
            enricher_pipeline._media_quality_flags(meta),
            ["transcript_unavailable"],
        )
        self.assertEqual(
            enricher_pipeline._media_quality_issues(meta),
            ["media:video:10:transcription_failed", "media:video:10:visual_missing"],
        )

    def test_video_visual_failure_is_recorded_as_media_issue(self):
        meta = NormalizedMeta(
            media=[
                {
                    "media_type": "video",
                    "message_id": 12,
                    "download_status": "downloaded",
                    "transcription_status": "transcribed",
                    "visual_status": "failed",
                }
            ]
        )
        self.assertEqual(
            enricher_pipeline._media_quality_issues(meta),
            ["media:video:12:visual_failed"],
        )
    def test_successful_media_does_not_add_media_failure_diagnostics(self):
        meta = NormalizedMeta(
            media=[
                {
                    "media_type": "video",
                    "message_id": 10,
                    "download_status": "downloaded",
                    "transcription_status": "transcribed",
                    "visual_status": "success",
                },
                {
                    "media_type": "image",
                    "message_id": 11,
                    "download_status": "downloaded",
                    "visual_status": "success",
                },
            ]
        )

        self.assertEqual(enricher_pipeline._media_quality_flags(meta), [])
        self.assertEqual(enricher_pipeline._media_quality_issues(meta), [])

    def test_validation_text_includes_header_for_chunk_merge(self):
        preprocessed = PreprocessedText(
            header="Video title: calls for strikes against Poland",
            clean_text="The transcript contains the substantive discussion.",
            body_char_count=60,
        )

        self.assertIn("calls for strikes against Poland", enricher_pipeline._validation_text(preprocessed))

    def test_ignored_media_path_does_not_hide_entity_match(self):
        payload = LLMPayload(
            entities={"countries": [{"text": "Куба"}]},
            summary="Куба предъявила обвинения.",
        )

        result = validate_payload(
            payload,
            "Куба предъявила обвинения.",
            ["[Видео: пост содержал видео | status=downloaded | path=C:\\WikiRag\\Куба\\video\\msg_10.mp4]"],
        )

        self.assertTrue(result.is_valid)

    def test_search_phrase_allows_conservative_inflection_match(self):
        payload = LLMPayload(
            summary="Литве сообщили о позиции.",
            search_phrases=[{"text": "Литва", "source": "phrase_from_text"}],
        )

        result = validate_payload(payload, "Литве сообщили о позиции.")

        self.assertTrue(result.is_valid)

    def test_queue_placeholder_filename_is_not_an_entity_source(self):
        payload = LLMPayload(
            summary="Китай ввёл ограничение.",
            entities={"countries": [{"text": "Китай"}]},
        )

        result = validate_payload(
            payload,
            "Китай ввёл ограничение.",
            ["[Отправлено в очередь на ручной просмотр: Китай_103_abcd.json]"],
        )

        self.assertTrue(result.is_valid)


class EnrichmentPipelineConcurrencyTests(unittest.TestCase):
    def test_enrich_all_runs_independent_posts_with_bounded_concurrency(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized_dir = root / "normalized"
            enriched_dir = root / "enriched"
            state_dir = root / "state"
            channel_dir = normalized_dir / "Channel"
            channel_dir.mkdir(parents=True)
            state_dir.mkdir()

            for message_id in (1, 2):
                (channel_dir / f"{message_id}.txt").write_text(
                    (
                        "[Канал: Channel | Дата: 2026-01-01 00:00]\n\n"
                        f"Useful post body {message_id} with enough text for enrichment."
                    ),
                    encoding="utf-8",
                )
                (channel_dir / f"{message_id}.meta.json").write_text(
                    json.dumps(
                        {
                            "channel_name": "Channel",
                            "message_id": message_id,
                            "date": "2026-01-01T00:00:00",
                            "post_url": "",
                        }
                    ),
                    encoding="utf-8",
                )

            active = 0
            max_active = 0
            lock = threading.Lock()
            both_started = threading.Event()

            def fake_run_enrichment_job(job, stats):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    if active >= 2:
                        both_started.set()
                try:
                    both_started.wait(0.2)
                    return {
                        "schema_version": "enriched_v2",
                        "summary": f"summary {job.msg_id}",
                        "content_type": "telegram_post",
                        "key_points": [],
                        "entities": {},
                        "topics": [],
                        "theses": [],
                        "quotes": [],
                        "events": [],
                        "search_phrases": [],
                        "provenance": {"source_id": f"telegram:Channel:{job.msg_id}"},
                        "source_chain": {},
                        "graph_text": "",
                        "search_text": "",
                        "ignored_blocks": [],
                        "quality_flags": [],
                    }
                finally:
                    with lock:
                        active -= 1

            with patch.object(enricher_pipeline.config, "NORMALIZED_DIR", normalized_dir):
                with patch.object(enricher_pipeline.config, "ENRICHED_DIR", enriched_dir):
                    with patch.object(enricher_pipeline.config, "ENRICHMENT_CONCURRENCY", 2, create=True):
                        with patch.object(
                            enricher_pipeline,
                            "_PROGRESS_FILE",
                            state_dir / "enrichment_progress.json",
                        ):
                            with patch.object(
                                enricher_pipeline,
                                "_run_enrichment_job",
                                side_effect=fake_run_enrichment_job,
                            ):
                                stats = enricher_pipeline.enrich_all(channel_filter="Channel", force=True)

            progress = json.loads(
                (state_dir / "enrichment_progress.json").read_text(encoding="utf-8")
            )
            file_1_exists = (enriched_dir / "Channel" / "1.enriched.json").exists()
            file_2_exists = (enriched_dir / "Channel" / "2.enriched.json").exists()

        self.assertEqual(stats.enriched, 2)
        self.assertGreaterEqual(max_active, 2)
        self.assertTrue(file_1_exists)
        self.assertTrue(file_2_exists)
        self.assertEqual(set(progress["enriched"]), {"Channel/1", "Channel/2"})
        for record in progress["enriched"].values():
            self.assertEqual(record["schema_version"], enricher_pipeline.config.ENRICHMENT_SCHEMA_VERSION)
            self.assertEqual(record["prompt_version"], enricher_pipeline.config.ENRICHMENT_PROMPT_VERSION)
            self.assertEqual(
                record["enrichment_model"],
                enricher_pipeline.llm_backend.active_model_for("enrichment"),
            )


class EnrichmentFailurePolicyTests(unittest.TestCase):
    def _make_jobs(self, root: Path, count: int) -> list[enricher_pipeline._EnrichmentJob]:
        normalized_dir = root / "output" / "normalized" / "Channel"
        enriched_dir = root / "output" / "enriched" / "Channel"
        normalized_dir.mkdir(parents=True)
        jobs = []
        for message_id in range(1, count + 1):
            txt_path = normalized_dir / f"{message_id}.txt"
            meta_path = normalized_dir / f"{message_id}.meta.json"
            txt_path.write_text(
                f"Substantive source text {message_id} with enough content for extraction.",
                encoding="utf-8",
            )
            meta_path.write_text(
                json.dumps({"channel_name": "Channel", "message_id": message_id}),
                encoding="utf-8",
            )
            jobs.append(
                enricher_pipeline._EnrichmentJob(
                    txt_path=txt_path,
                    meta_path=meta_path,
                    channel_name="Channel",
                    msg_id=str(message_id),
                    progress_key=f"Channel/{message_id}",
                    source_fingerprint="test-fingerprint",
                    out_path=enriched_dir / f"{message_id}.enriched.json",
                )
            )
        return jobs

    def test_failed_structural_repair_records_failure_and_continues_batch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            jobs = self._make_jobs(root, 2)
            progress = {"enriched": {}}
            stats = enricher_pipeline.EnrichmentStats()
            state_dir = root / "state"
            state_dir.mkdir()

            with patch.object(enricher_pipeline, "_PROGRESS_FILE", state_dir / "enrichment_progress.json"):
                with patch.object(enricher_pipeline.config, "PROJECT_ROOT", root):
                    with patch.object(enricher_pipeline.config, "ENRICHMENT_CONCURRENCY", 1):
                        with patch.object(enricher_pipeline, "classify_content", return_value="telegram_post"):
                            with patch.object(enricher_pipeline, "auto_triage", return_value=("keep", "")):
                                with patch.object(
                                    enricher_pipeline,
                                    "extract_short_post_raw",
                                    side_effect=[{}, {"summary": "Вторая задача выполнена успешно."}],
                                ):
                                    with patch(
                                        "enricher.repair._call_llm",
                                        return_value={},
                                    ) as repair_call:
                                        enricher_pipeline._run_enrichment_jobs(jobs, progress, stats)

            first_exists = jobs[0].out_path.exists()
            second_exists = jobs[1].out_path.exists()

        self.assertEqual(repair_call.call_count, 1)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.enriched, 1)
        self.assertFalse(first_exists)
        self.assertTrue(second_exists)
        self.assertNotIn("Channel/1", progress["enriched"])
        self.assertIn("Channel/2", progress["enriched"])

    def test_provider_failure_retains_previous_published_card(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            jobs = self._make_jobs(root, 1)
            jobs[0].out_path.parent.mkdir(parents=True)
            jobs[0].out_path.write_text('{"previous": true}', encoding="utf-8")
            stats = enricher_pipeline.EnrichmentStats()
            enricher_pipeline._record_enrichment_failure(
                jobs[0], RuntimeError("provider unavailable"), stats
            )
            saved = jobs[0].out_path.read_text(encoding="utf-8")
        self.assertEqual(stats.failed, 1)
        self.assertEqual(saved, '{"previous": true}')

    def test_transient_or_unusable_enrichment_is_retried_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            jobs = self._make_jobs(root, 1)
            progress = {"enriched": {}}
            stats = enricher_pipeline.EnrichmentStats()
            successful_result = {
                "schema_version": "enriched_v2",
                "summary": "Повторная попытка успешно завершилась.",
                "content_type": "telegram_post",
                "key_points": [],
                "entities": {},
                "topics": [],
                "theses": [],
                "quotes": [],
                "events": [],
                "search_phrases": [],
                "provenance": {"source_id": "telegram:Channel:1"},
                "source_chain": {},
                "graph_text": "",
                "search_text": "",
                "ignored_blocks": [],
                "quality_flags": [],
            }

            state_dir = root / "state"
            state_dir.mkdir()
            with patch.object(enricher_pipeline, "_PROGRESS_FILE", state_dir / "enrichment_progress.json"):
                with patch.object(enricher_pipeline.config, "ENRICHMENT_CONCURRENCY", 1), patch.object(
                    enricher_pipeline,
                    "_run_enrichment_job",
                    side_effect=[enricher_pipeline.UnusableExtractionError("language contract"), successful_result],
                ) as run_job:
                    enricher_pipeline._run_enrichment_jobs(jobs, progress, stats)

        self.assertEqual(run_job.call_count, 2)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.enriched, 1)
        self.assertIn("Channel/1", progress["enriched"])

    def test_final_failure_is_recorded_with_fingerprint_and_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            jobs = self._make_jobs(root, 1)
            progress = {"enriched": {}}
            stats = enricher_pipeline.EnrichmentStats()
            state_dir = root / "state"
            state_dir.mkdir()
            with patch.object(enricher_pipeline, "_PROGRESS_FILE", state_dir / "enrichment_progress.json"):
                with patch.object(enricher_pipeline.config, "ENRICHMENT_CONCURRENCY", 1), patch.object(
                    enricher_pipeline,
                    "_run_enrichment_job",
                    side_effect=enricher_pipeline.UnusableExtractionError("language contract"),
                ) as run_job:
                    enricher_pipeline._run_enrichment_jobs(jobs, progress, stats)

        failure = progress["failed"]["Channel/1"]
        self.assertEqual(run_job.call_count, 2)
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["source_fingerprint"], "test-fingerprint")
        self.assertIn("language contract", failure["error"])
        self.assertEqual(failure["attempts"], 2)

    def test_failed_semantic_repair_with_content_saves_unstable_card(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            jobs = self._make_jobs(root, 1)
            progress = {"enriched": {}}
            stats = enricher_pipeline.EnrichmentStats()
            state_dir = root / "state"
            state_dir.mkdir()
            raw = {
                "summary": "Источник приводит содержательное утверждение.",
                "quotes": [{"text": "quote absent from source"}],
            }

            with patch.object(enricher_pipeline, "_PROGRESS_FILE", state_dir / "enrichment_progress.json"):
                with patch.object(enricher_pipeline.config, "PROJECT_ROOT", root):
                    with patch.object(enricher_pipeline.config, "ENRICHMENT_CONCURRENCY", 1):
                        with patch.object(enricher_pipeline, "classify_content", return_value="telegram_post"):
                            with patch.object(enricher_pipeline, "auto_triage", return_value=("keep", "")):
                                with patch.object(enricher_pipeline, "extract_short_post_raw", return_value=raw):
                                    with patch(
                                        "enricher.repair._call_llm",
                                        return_value={},
                                    ) as repair_call:
                                        enricher_pipeline._run_enrichment_jobs(jobs, progress, stats)

            saved = json.loads(jobs[0].out_path.read_text(encoding="utf-8"))

        self.assertEqual(repair_call.call_count, 1)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.enriched, 1)
        self.assertEqual(stats.repaired, 0)
        self.assertNotIn("extraction_unstable", saved["quality_flags"])
        self.assertTrue(saved["extraction_issues"])
        self.assertIn("Channel/1", progress["enriched"])

    def test_persistent_language_failure_does_not_publish_card_or_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            jobs = self._make_jobs(root, 1)
            progress = {"enriched": {}}
            stats = enricher_pipeline.EnrichmentStats()
            state_dir = root / "state"
            state_dir.mkdir()
            raw = {"summary": "Україна ухвалила важливе рішення."}

            with patch.object(enricher_pipeline, "_PROGRESS_FILE", state_dir / "enrichment_progress.json"):
                with patch.object(enricher_pipeline.config, "PROJECT_ROOT", root):
                    with patch.object(enricher_pipeline.config, "ENRICHMENT_CONCURRENCY", 1):
                        with patch.object(enricher_pipeline, "classify_content", return_value="telegram_post"):
                            with patch.object(enricher_pipeline, "auto_triage", return_value=("keep", "")):
                                with patch.object(enricher_pipeline, "extract_short_post_raw", return_value=raw):
                                    with patch("enricher.repair._call_llm", return_value=raw):
                                        enricher_pipeline._run_enrichment_jobs(jobs, progress, stats)

            self.assertFalse(jobs[0].out_path.exists())
            self.assertEqual(progress["enriched"], {})
            self.assertEqual(stats.failed, 1)
            self.assertEqual(stats.enriched, 0)


class EnrichedV2BoundaryTests(unittest.TestCase):
    def _run_job_with_raw(self, raw, repair_raw):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            txt_path = root / "1.txt"
            meta_path = root / "1.meta.json"
            txt_path.write_text("Substantive source text with enough content to validate.", encoding="utf-8")
            meta_path.write_text(
                json.dumps({"channel_name": "Channel", "message_id": 1}),
                encoding="utf-8",
            )
            job = enricher_pipeline._EnrichmentJob(
                txt_path=txt_path,
                meta_path=meta_path,
                channel_name="Channel",
                msg_id="1",
                progress_key="Channel/1",
                source_fingerprint="test-fingerprint",
                out_path=root / "1.enriched.json",
            )
            preprocessed = PreprocessedText(
                header="header",
                clean_text="Substantive source text with enough content to validate.",
                body_char_count=58,
            )

            def fake_assemble(**kwargs):
                payload = kwargs["payload"]
                return SimpleNamespace(
                    model_dump=lambda mode: payload.model_dump(mode=mode)
                )

            stats = enricher_pipeline.EnrichmentStats()
            with patch.object(enricher_pipeline, "classify_content", return_value="telegram_post"):
                with patch.object(enricher_pipeline, "auto_triage", return_value=("keep", "")):
                    with patch.object(enricher_pipeline, "preprocess", return_value=preprocessed):
                        with patch.object(
                            enricher_pipeline,
                            "extract_short_post_raw",
                            return_value=raw,
                        ):
                            with patch.object(
                                enricher_pipeline,
                                "_assemble_card",
                                side_effect=fake_assemble,
                            ):
                                with patch(
                                    "enricher.repair._call_llm",
                                    return_value=repair_raw,
                                ) as repair_call:
                                    result = enricher_pipeline._run_enrichment_job(job, stats)

        return result, stats, repair_call.call_count

    def test_raw_payload_rejects_legacy_and_unknown_fields(self):
        for raw in (
            {"summary": "x", "query_aliases": ["legacy"]},
            {"summary": "x", "entities": {"people": [{"text": "A", "canonical_id": "a"}]}},
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValidationError):
                    _normalize_to_payload(raw)

    def test_entity_name_is_normalized_to_v2_surface_form(self):
        payload = _normalize_to_payload(
            {"summary": "x", "entities": {"countries": [{"name": "Китай"}]}}
        )

        self.assertEqual(payload.entities.countries[0].text, "Китай")

    def test_internal_chunking_preserves_all_semantic_categories(self):
        preprocessed = PreprocessedText(
            header="header",
            clean_text="long text",
            body_char_count=10_000,
        )
        chunk_payload = {
            "summary": "part",
            "key_points": [{"text": "point"}],
            "entities": {"people": [{"text": "Person"}]},
            "topics": [{"label": "specific topic"}],
            "theses": [{"text": "thesis"}],
            "quotes": [{"text": "quote"}],
            "events": [{"description": "event"}],
            "search_phrases": [{"text": "phrase"}],
            "quality_flags": ["mixed_topics"],
        }
        chunks = [
            {"index": 0, "text": "part one", "char_range": [0, 8]},
            {"index": 1, "text": "part two", "char_range": [8, 16]},
        ]
        captured = []

        def fake_merge(header, chunk_results):
            captured.extend(chunk_results)
            return json.loads(json.dumps(chunk_payload))

        with patch.object(enricher_pipeline, "needs_chunking", return_value=True):
            with patch.object(enricher_pipeline, "chunk_text", return_value=chunks):
                with patch.object(
                    enricher_pipeline,
                    "extract_chunk_raw",
                    side_effect=lambda *args: json.loads(json.dumps(chunk_payload)),
                ) as extract:
                    with patch.object(
                        enricher_pipeline,
                        "merge_chunk_results_raw",
                        side_effect=fake_merge,
                    ):
                        result = enricher_pipeline._extract_payload(
                            preprocessed,
                            "youtube_transcript",
                            RepairContext(),
                        )

        self.assertEqual(extract.call_count, 2)
        self.assertEqual(len(captured), 2)
        for field in (
            "summary",
            "key_points",
            "entities",
            "topics",
            "theses",
            "quotes",
            "events",
            "search_phrases",
            "quality_flags",
        ):
            self.assertIn(field, captured[0])
        serialized = json.loads(_serialize_chunk_results(captured))
        self.assertEqual(
            set(serialized[0]["payload"]),
            {
                "summary",
                "key_points",
                "entities",
                "topics",
                "theses",
                "quotes",
                "events",
                "search_phrases",
                "quality_flags",
            },
        )
        self.assertEqual(result.summary, "part")
        self.assertEqual(result.key_points[0].text, "point")
        self.assertEqual(result.entities.people[0].text, "Person")
        self.assertEqual(result.topics[0].label, "specific topic")
        self.assertEqual(result.theses[0].text, "thesis")
        self.assertEqual(result.quotes[0].text, "quote")
        self.assertEqual(result.events[0].description, "event")
        self.assertEqual(result.search_phrases[0].text, "phrase")
        self.assertEqual(result.quality_flags, ["mixed_topics"])

    def test_existing_meta_source_id_wins(self):
        meta = NormalizedMeta.model_validate(
            {"source_id": "custom:source:1", "channel_id": 1, "message_id": 2}
        )
        self.assertEqual(
            enricher_pipeline._build_source_id(meta, "Channel", "2"),
            "custom:source:1",
        )

    def test_progress_invalidates_schema_prompt_and_model_changes(self):
        key = "Channel/1"
        current = {
            "enriched": {
                key: {
                    "source_fingerprint": "source-fingerprint",
                    "schema_version": enricher_pipeline.config.ENRICHMENT_SCHEMA_VERSION,
                    "prompt_version": enricher_pipeline.config.ENRICHMENT_PROMPT_VERSION,
                    "enrichment_model": enricher_pipeline.llm_backend.active_model_for(
                        "enrichment"
                    ),
                    "media_contract_version": enricher_pipeline._MEDIA_CONTRACT_VERSION,
                }
            }
        }
        self.assertFalse(
            enricher_pipeline._needs_enrichment(current, key, "source-fingerprint")
        )

        for field in ("schema_version", "prompt_version", "enrichment_model"):
            changed = json.loads(json.dumps(current))
            if field == "enrichment_model" and current["enriched"][key][field].startswith(
                "codex-cli:"
            ):
                changed["enriched"][key][field] = "codex-cli:old@xhigh"
            else:
                changed["enriched"][key][field] = "old"
            with self.subTest(field=field):
                self.assertTrue(
                    enricher_pipeline._needs_enrichment(changed, key, "source-fingerprint")
                )

        changed_fingerprint = json.loads(json.dumps(current))
        changed_fingerprint["enriched"][key]["source_fingerprint"] = "new-fingerprint"
        self.assertTrue(
            enricher_pipeline._needs_enrichment(
                changed_fingerprint,
                key,
                "source-fingerprint",
            )
        )

    def test_progress_does_not_replace_missing_output_card(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized_dir = root / "normalized" / "Channel"
            enriched_dir = root / "enriched"
            normalized_dir.mkdir(parents=True)
            (normalized_dir / "1.txt").write_text(
                "Useful normalized text with enough content.",
                encoding="utf-8",
            )
            (normalized_dir / "1.meta.json").write_text(
                json.dumps({"channel_name": "Channel", "message_id": 1}),
                encoding="utf-8",
            )
            source_fingerprint = enricher_pipeline._source_fingerprint(
                normalized_dir / "1.txt",
                normalized_dir / "1.meta.json",
            )
            progress = {
                "enriched": {
                    "Channel/1": {
                        "source_fingerprint": source_fingerprint,
                        "schema_version": enricher_pipeline.config.ENRICHMENT_SCHEMA_VERSION,
                        "prompt_version": enricher_pipeline.config.ENRICHMENT_PROMPT_VERSION,
                        "enrichment_model": enricher_pipeline.llm_backend.active_model_for(
                            "enrichment"
                        ),
                    "media_contract_version": enricher_pipeline._MEDIA_CONTRACT_VERSION,
                    }
                }
            }
            stats = enricher_pipeline.EnrichmentStats()
            with patch.object(enricher_pipeline.config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(enricher_pipeline.config, "ENRICHED_DIR", enriched_dir):
                jobs = enricher_pipeline._collect_enrichment_jobs(
                    progress=progress,
                    channel_filter="Channel",
                    force=False,
                    stats=stats,
                )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(stats.skipped_up_to_date, 0)

    def test_metadata_only_change_invalidates_enrichment_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized_dir = root / "normalized" / "Channel"
            enriched_dir = root / "enriched" / "Channel"
            normalized_dir.mkdir(parents=True)
            enriched_dir.mkdir(parents=True)
            txt_path = normalized_dir / "1.txt"
            meta_path = normalized_dir / "1.meta.json"
            txt_path.write_text("Stable normalized source text.", encoding="utf-8")
            meta_path.write_text(
                json.dumps({"channel_name": "Channel", "message_id": 1}),
                encoding="utf-8",
            )
            valid_card = EnrichedCardV2(
                schema_version=enricher_pipeline.config.ENRICHMENT_SCHEMA_VERSION,
                prompt_version=enricher_pipeline.config.ENRICHMENT_PROMPT_VERSION,
                enrichment_model=enricher_pipeline.llm_backend.active_model_for(
                    "enrichment"
                ),
                media_contract_version=enricher_pipeline._MEDIA_CONTRACT_VERSION,
                enriched_at="2026-01-01T00:00:00+00:00",
                provenance={"source_id": "telegram:Channel:1"},
            )
            (enriched_dir / "1.enriched.json").write_text(
                valid_card.model_dump_json(),
                encoding="utf-8",
            )
            fingerprint = enricher_pipeline._source_fingerprint(txt_path, meta_path)
            progress = {
                "enriched": {
                    "Channel/1": {
                        "source_fingerprint": fingerprint,
                        "schema_version": enricher_pipeline.config.ENRICHMENT_SCHEMA_VERSION,
                        "prompt_version": enricher_pipeline.config.ENRICHMENT_PROMPT_VERSION,
                        "enrichment_model": enricher_pipeline.llm_backend.active_model_for(
                            "enrichment"
                        ),
                        "media_contract_version": enricher_pipeline._MEDIA_CONTRACT_VERSION,
                    }
                }
            }

            with patch.object(enricher_pipeline.config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(enricher_pipeline.config, "ENRICHED_DIR", root / "enriched"):
                before_stats = enricher_pipeline.EnrichmentStats()
                before = enricher_pipeline._collect_enrichment_jobs(
                    progress=progress,
                    channel_filter="Channel",
                    force=False,
                    stats=before_stats,
                )
                meta_path.write_text(
                    json.dumps({
                        "channel_name": "Channel",
                        "message_id": 1,
                        "telegram_channel_id": 42,
                    }),
                    encoding="utf-8",
                )
                after_stats = enricher_pipeline.EnrichmentStats()
                after = enricher_pipeline._collect_enrichment_jobs(
                    progress=progress,
                    channel_filter="Channel",
                    force=False,
                    stats=after_stats,
                )

        self.assertEqual(before, [])
        self.assertEqual(before_stats.skipped_up_to_date, 1)
        self.assertEqual(len(after), 1)
        self.assertEqual(after_stats.skipped_up_to_date, 0)

    def test_malformed_output_card_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized_dir = root / "normalized" / "Channel"
            enriched_dir = root / "enriched" / "Channel"
            normalized_dir.mkdir(parents=True)
            enriched_dir.mkdir(parents=True)
            txt_path = normalized_dir / "1.txt"
            meta_path = normalized_dir / "1.meta.json"
            txt_path.write_text("Substantive normalized text.", encoding="utf-8")
            meta_path.write_text(
                json.dumps({"channel_name": "Channel", "message_id": 1}),
                encoding="utf-8",
            )
            (enriched_dir / "1.enriched.json").write_text("{", encoding="utf-8")
            fingerprint = enricher_pipeline._source_fingerprint(txt_path, meta_path)
            progress = {
                "enriched": {
                    "Channel/1": {
                        "source_fingerprint": fingerprint,
                        "schema_version": enricher_pipeline.config.ENRICHMENT_SCHEMA_VERSION,
                        "prompt_version": enricher_pipeline.config.ENRICHMENT_PROMPT_VERSION,
                        "enrichment_model": enricher_pipeline.llm_backend.active_model_for(
                            "enrichment"
                        ),
                    }
                }
            }
            with patch.object(enricher_pipeline.config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(enricher_pipeline.config, "ENRICHED_DIR", root / "enriched"):
                jobs = enricher_pipeline._collect_enrichment_jobs(
                    progress=progress,
                    channel_filter="Channel",
                    force=False,
                    stats=enricher_pipeline.EnrichmentStats(),
                )

        self.assertEqual(len(jobs), 1)

    def test_malformed_metadata_cannot_skip_card_with_unknown_source_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized_dir = root / "normalized" / "Channel"
            enriched_dir = root / "enriched" / "Channel"
            normalized_dir.mkdir(parents=True)
            enriched_dir.mkdir(parents=True)
            txt_path = normalized_dir / "1.txt"
            meta_path = normalized_dir / "1.meta.json"
            txt_path.write_text("Substantive normalized text.", encoding="utf-8")
            meta_path.write_text("[]", encoding="utf-8")
            valid_card = EnrichedCardV2(
                schema_version=enricher_pipeline.config.ENRICHMENT_SCHEMA_VERSION,
                prompt_version=enricher_pipeline.config.ENRICHMENT_PROMPT_VERSION,
                enrichment_model=enricher_pipeline.llm_backend.active_model_for(
                    "enrichment"
                ),
                enriched_at="2026-01-01T00:00:00+00:00",
                provenance={"source_id": "telegram:someone-else:1"},
            )
            (enriched_dir / "1.enriched.json").write_text(
                valid_card.model_dump_json(),
                encoding="utf-8",
            )
            fingerprint = enricher_pipeline._source_fingerprint(txt_path, meta_path)
            progress = {
                "enriched": {
                    "Channel/1": {
                        "source_fingerprint": fingerprint,
                        "schema_version": enricher_pipeline.config.ENRICHMENT_SCHEMA_VERSION,
                        "prompt_version": enricher_pipeline.config.ENRICHMENT_PROMPT_VERSION,
                        "enrichment_model": enricher_pipeline.llm_backend.active_model_for(
                            "enrichment"
                        ),
                    }
                }
            }
            with patch.object(enricher_pipeline.config, "NORMALIZED_DIR", root / "normalized"), \
                 patch.object(enricher_pipeline.config, "ENRICHED_DIR", root / "enriched"):
                jobs = enricher_pipeline._collect_enrichment_jobs(
                    progress=progress,
                    channel_filter="Channel",
                    force=False,
                    stats=enricher_pipeline.EnrichmentStats(),
                )

        self.assertEqual(len(jobs), 1)

    def test_structural_repair_is_wired_and_counted_when_final_payload_is_valid(self):
        result, stats, repair_calls = self._run_job_with_raw(
            {"summary": "fixed", "query_aliases": ["legacy"]},
            {"summary": "fixed"},
        )

        self.assertEqual(result["summary"], "fixed")
        self.assertEqual(repair_calls, 1)
        self.assertEqual(stats.repaired, 1)

    def test_empty_raw_uses_one_structural_repair_attempt(self):
        result, stats, repair_calls = self._run_job_with_raw({}, {"summary": "fixed"})

        self.assertEqual(result["summary"], "fixed")
        self.assertEqual(repair_calls, 1)
        self.assertEqual(stats.repaired, 1)

    def test_failed_structural_repair_raises_domain_error(self):
        with self.assertRaises(StructuralRepairError):
            self._run_job_with_raw(
                {"summary": "original", "query_aliases": ["legacy"]},
                {"query_aliases": ["still invalid"]},
            )

    def test_structural_repair_uses_the_only_attempt_before_semantic_validation(self):
        result, stats, repair_calls = self._run_job_with_raw(
            {"summary": "original", "query_aliases": ["legacy"]},
            {
                "summary": "fixed",
                "quotes": [{"text": "not in source"}],
            },
        )

        self.assertEqual(repair_calls, 1)
        self.assertEqual(stats.repaired, 0)
        self.assertEqual(result["summary"], "fixed")
        self.assertNotIn("extraction_unstable", result["quality_flags"])
        self.assertEqual(result["quotes"], [])

    def test_successful_semantic_repair_increments_repaired_stats(self):
        result, stats, repair_calls = self._run_job_with_raw(
            {"summary": "original", "quotes": [{"text": "not in source"}]},
            {"summary": "fixed"},
        )

        self.assertEqual(repair_calls, 1)
        self.assertEqual(stats.repaired, 1)
        self.assertEqual(result["summary"], "fixed")

    def test_key_points_only_card_is_saved_instead_of_marked_partial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            txt_path = root / "1.txt"
            txt_path.write_text("x" * 60, encoding="utf-8")
            out_path = root / "1.enriched.json"
            job = enricher_pipeline._EnrichmentJob(
                txt_path=txt_path,
                meta_path=root / "1.meta.json",
                channel_name="Channel",
                msg_id="1",
                progress_key="Channel/1",
                source_fingerprint="test-fingerprint",
                out_path=out_path,
            )
            progress = {"enriched": {}}
            stats = enricher_pipeline.EnrichmentStats()
            card = {
                "summary": "",
                "key_points": [{"text": "A useful point"}],
                "content_type": "telegram_post",
            }

            state_dir = root / "state"
            state_dir.mkdir()
            progress_path = state_dir / "enrichment_progress.json"
            with patch.object(enricher_pipeline, "_PROGRESS_FILE", progress_path):
                enricher_pipeline._handle_enrichment_result(job, card, progress, stats)

            self.assertTrue(out_path.exists())
            self.assertTrue(progress_path.exists())
            self.assertEqual(stats.enriched, 1)
            self.assertEqual(stats.partial, 0)
            self.assertIn("Channel/1", progress["enriched"])

    def test_code_owned_ignored_blocks_survive_card_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            txt_path = root / "1.txt"
            txt_path.write_text("source", encoding="utf-8")
            preprocessed = PreprocessedText(
                header="header",
                clean_text="source",
                body_char_count=10_000,
                ignored_blocks=[IgnoredBlock(type="video", text="video placeholder")],
            )
            meta = NormalizedMeta.model_validate(
                {"source_id": "custom:1", "channel_name": "Channel", "message_id": 1}
            )

            with patch.object(enricher_pipeline.config, "PROJECT_ROOT", root):
                card = enricher_pipeline._assemble_card(
                    payload=LLMPayload(summary="summary"),
                    meta=meta,
                    content_type="telegram_post",
                    channel_name="Channel",
                    msg_id="1",
                    txt_path=txt_path,
                    preprocessed=preprocessed,
                )

        dumped = card.model_dump(mode="json")
        self.assertEqual(dumped["ignored_blocks"][0]["text"], "video placeholder")
        self.assertNotIn("chunks", dumped)

    def test_invalid_repair_response_is_not_retried(self):
        payload = LLMPayload(summary="original")
        validation = ValidationResult(is_valid=False, violations=["bad"], should_repair=True)
        with patch("enricher.repair._call_llm", return_value={"query_aliases": ["legacy"]}) as call:
            repaired, succeeded = repair_if_needed(payload, validation, "source text")

        self.assertFalse(succeeded)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(repaired.summary, "original")
        self.assertIn("extraction_unstable", repaired.quality_flags)

if __name__ == "__main__":
    unittest.main()
