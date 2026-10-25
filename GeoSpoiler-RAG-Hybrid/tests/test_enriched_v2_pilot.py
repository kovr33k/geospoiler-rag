import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from models import EnrichedCardV2
from retrieval.card_fts import rebuild_card_index
from scripts import enriched_v2_pilot as pilot


class EnrichedV2PilotTests(unittest.TestCase):
    def test_selection_is_deterministic_representative_and_has_one_youtube(self):
        with tempfile.TemporaryDirectory() as tmp:
            normalized = Path(tmp) / "normalized"
            self._build_sample(normalized)

            first = pilot.select_representative_posts(normalized, limit=10)
            second = pilot.select_representative_posts(normalized, limit=10)

            self.assertEqual(
                [item.relative_txt for item in first],
                [item.relative_txt for item in second],
            )
            traits = {trait for item in first for trait in item.traits}
            self.assertTrue(
                {
                    "telegram_short",
                    "telegram_medium",
                    "telegram_long",
                    "forward",
                    "youtube",
                    "instagram",
                    "web",
                    "ignored_media",
                }.issubset(traits)
            )
            self.assertEqual(sum("youtube" in item.traits for item in first), 1)

    def test_selection_excludes_other_youtube_candidates_from_all_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            normalized = Path(tmp) / "normalized"
            self._build_sample(normalized)
            self._write_post(
                normalized,
                13,
                "Hybrid forwarded Instagram YouTube transcript. " * 120,
                is_forward=True,
                youtube_urls=["https://www.youtube.com/watch?v=hybrid"],
                instagram_urls=["https://instagram.example/hybrid"],
            )

            selected = pilot.select_representative_posts(normalized, limit=10)

            self.assertEqual(sum("youtube" in item.traits for item in selected), 1)

    def test_selection_requires_an_eligible_youtube(self):
        with tempfile.TemporaryDirectory() as tmp:
            normalized = Path(tmp) / "normalized"
            self._write_post(normalized, 1, "Substantive policy statement. " * 20)
            with self.assertRaisesRegex(ValueError, "YouTube"):
                pilot.select_representative_posts(normalized, limit=1)

    def test_selection_rejects_youtube_link_without_dedicated_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            normalized = Path(tmp) / "normalized"
            self._write_post(
                normalized,
                1,
                "Telegram post with a link but no extracted transcript. " * 20,
                youtube_urls=["https://www.youtube.com/watch?v=missing"],
            )
            with self.assertRaisesRegex(ValueError, "dedicated source artifact"):
                pilot.select_representative_posts(normalized, limit=1)

    def test_selection_rejects_transcript_without_artifact_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            transcript = root / "transcript.txt"
            transcript.write_text("Dedicated transcript without metadata. " * 30, encoding="utf-8")
            self._write_post(
                normalized,
                1,
                "Telegram post with an incomplete YouTube artifact. " * 20,
                youtube_urls=["https://www.youtube.com/watch?v=incomplete"],
                youtube_sources=[
                    {
                        "video_id": "incomplete",
                        "text_path": str(transcript),
                    }
                ],
            )
            with self.assertRaisesRegex(ValueError, "dedicated source artifact"):
                pilot.select_representative_posts(normalized, limit=1)

    def test_selection_rejects_legacy_youtube_without_dedicated_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            normalized = Path(tmp) / "normalized"
            self._write_post(
                normalized,
                1,
                "[YouTube]\nURL: https://www.youtube.com/watch?v=legacy123\n\n"
                + "Legacy normalized transcript. " * 30,
                has_body_text=False,
                youtube_urls=["https://www.youtube.com/watch?v=legacy123"],
            )
            with self.assertRaisesRegex(ValueError, "dedicated source artifact"):
                pilot.select_representative_posts(normalized, limit=1)

    def test_selection_rejects_artifact_not_linked_to_post_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            source = self._youtube_source(root, "artifact")
            self._write_post(
                normalized,
                1,
                "Telegram post with the wrong YouTube artifact. " * 20,
                youtube_urls=["https://www.youtube.com/watch?v=declared"],
                youtube_sources=source,
            )
            with self.assertRaisesRegex(ValueError, "dedicated source artifact"):
                pilot.select_representative_posts(normalized, limit=1)

    def test_selection_rejects_invalid_cues_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            source = self._youtube_source(root, "cues")
            cues = root / "cues.json"
            cues.write_text("{broken", encoding="utf-8")
            source[0]["cues_path"] = str(cues)
            self._write_post(
                normalized,
                1,
                "Telegram post with invalid YouTube cues. " * 20,
                youtube_urls=["https://www.youtube.com/watch?v=cues"],
                youtube_sources=source,
            )
            with self.assertRaisesRegex(ValueError, "dedicated source artifact"):
                pilot.select_representative_posts(normalized, limit=1)

    def test_selection_rejects_invalid_dedicated_metadata_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            source = self._youtube_source(root, "contract")
            metadata = Path(source[0]["metadata_path"])
            invalid_metadata = metadata.with_name("contract-artifact.json")
            invalid_metadata.write_text(
                json.dumps(
                    {
                        "url": "https://www.youtube.com/watch?v=contract",
                        "video_id": "contract",
                        "transcript_source": "subtitles",
                        "chapters": {},
                    }
                ),
                encoding="utf-8",
            )
            source[0]["metadata_path"] = str(invalid_metadata)
            self._write_post(
                normalized,
                1,
                "Telegram post with invalid YouTube metadata. " * 20,
                youtube_urls=["https://www.youtube.com/watch?v=contract"],
                youtube_sources=source,
            )
            with self.assertRaisesRegex(ValueError, "dedicated source artifact"):
                pilot.select_representative_posts(normalized, limit=1)

    def test_selection_can_require_a_real_long_form_youtube_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            normalized = Path(tmp) / "normalized"
            transcript = Path(tmp) / "dedicated-transcript.txt"
            transcript.write_text("Dedicated YouTube transcript. " * 300, encoding="utf-8")
            self._write_post(
                normalized,
                1,
                "Telegram post containing a YouTube link.",
                youtube_urls=["https://www.youtube.com/watch?v=long"],
                youtube_sources=[self._youtube_source_entry(transcript, "long", 1_000)],
            )
            selected = pilot.select_representative_posts(
                normalized,
                limit=1,
                require_long_youtube=True,
            )

            self.assertEqual(len(selected), 1)
            self.assertIn("youtube_long", selected[0].traits)

    def test_mixed_long_telegram_body_and_short_youtube_artifact_is_not_long(self):
        with tempfile.TemporaryDirectory() as tmp:
            normalized = Path(tmp) / "normalized"
            transcript = Path(tmp) / "short-transcript.txt"
            transcript.write_text("Short dedicated transcript.", encoding="utf-8")
            self._write_post(
                normalized,
                1,
                "Long Telegram commentary. " * 700,
                youtube_urls=["https://www.youtube.com/watch?v=short"],
                youtube_sources=[self._youtube_source_entry(transcript, "short", 30)],
            )

            candidates = pilot.scan_candidates(normalized)

        self.assertEqual(len(candidates), 1)
        self.assertIn("youtube", candidates[0].traits)
        self.assertNotIn("youtube_long", candidates[0].traits)

    def test_multiple_youtube_sources_mark_the_qualifying_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            normalized = Path(tmp) / "normalized"
            first = Path(tmp) / "first-transcript.txt"
            second = Path(tmp) / "second-transcript.txt"
            first.write_text("Short transcript.", encoding="utf-8")
            second.write_text("Long transcript. " * 900, encoding="utf-8")
            self._write_post(
                normalized,
                1,
                "Telegram post containing two YouTube links.",
                youtube_urls=[
                    "https://www.youtube.com/watch?v=short",
                    "https://www.youtube.com/watch?v=long",
                ],
                youtube_sources=[
                    self._youtube_source_entry(first, "short", 30),
                    self._youtube_source_entry(second, "long", 30),
                ],
            )

            candidates = pilot.scan_candidates(normalized)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].youtube_video_ids, ("short", "long"))
        self.assertEqual(candidates[0].youtube_long_video_ids, ("long",))
        self.assertIn("youtube_long", candidates[0].traits)

    def test_recall_checks_every_card_file_for_one_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = pilot.PilotPaths.build(Path(tmp) / "pilot")
            first = paths.pilot_dir / "workspace" / "output" / "enriched" / "first.json"
            second = paths.pilot_dir / "workspace" / "output" / "enriched" / "second.json"
            first.parent.mkdir(parents=True)
            first.write_text(
                json.dumps(self._card_dict(source_id="source:first")),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps(self._card_dict(source_id="source:second")),
                encoding="utf-8",
            )
            card_result = {
                "status": "valid",
                "card_file": "workspace/output/enriched/first.json",
                "card_files": [
                    "workspace/output/enriched/first.json",
                    "workspace/output/enriched/second.json",
                ],
                "source_id": "source:first",
            }
            with patch.object(
                pilot,
                "_union_source_ids",
                side_effect=[["source:first"], ["source:second"]],
            ):
                result = pilot._run_recall_checks([card_result], paths)

        self.assertEqual(len(result["self_recall"]["checks"]), 2)
        self.assertTrue(result["self_recall"]["passed"])

    def test_copy_skips_non_object_artifact_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            transcript = root / "video.youtube.txt"
            artifact_meta = root / "video.youtube.meta.json"
            transcript.write_text("Dedicated transcript. " * 50, encoding="utf-8")
            artifact_meta.write_text("[]", encoding="utf-8")
            self._write_post(
                normalized,
                1,
                "Telegram post with a dedicated YouTube artifact. " * 20,
                youtube_urls=["https://www.youtube.com/watch?v=abc"],
                youtube_sources=[
                    {
                        "video_id": "abc",
                        "text_path": str(transcript),
                        "metadata_path": str(artifact_meta),
                    }
                ],
            )
            candidate = pilot.scan_candidates(normalized)[0]
            paths = pilot.PilotPaths.build(root / "pilot")

            copied = pilot._copy_youtube_artifacts(candidate, paths)

        self.assertEqual(copied, 0)

    def test_prepare_workspace_copies_complete_youtube_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "Telegram post with a complete YouTube artifact. " * 20,
                youtube_urls=["https://www.youtube.com/watch?v=complete"],
                youtube_sources=self._youtube_source(root, "complete"),
            )
            candidate = pilot.select_representative_posts(normalized, limit=1)[0]
            paths = pilot.PilotPaths.build(root / "pilot")

            copied = pilot._prepare_workspace([candidate], paths)
            copied_metadata = list(paths.youtube_normalized_dir.rglob("*.youtube.meta.json"))

        self.assertEqual(copied, 1)
        self.assertTrue(copied_metadata)

    def test_media_audit_rejects_silent_caption_only_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "Caption with downloaded media. " * 20,
                media=[
                    {
                        "media_type": "video",
                        "message_id": 1,
                        "file_path": "media_cache/channel/video/msg_1.mp4",
                        "download_status": "downloaded",
                        "transcription_status": "failed",
                    }
                ],
            )
            candidate = pilot.scan_candidates(normalized)[0]
            paths = pilot.PilotPaths.build(root / "pilot")
            pilot._prepare_workspace([candidate], paths)

            audit = pilot._audit_media([candidate], paths)

        self.assertFalse(audit["passed"])
        self.assertIn("transcription_failed", audit["issues"][0])

    def test_media_audit_accepts_successful_native_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "Caption with successfully processed media. " * 20,
                media=[
                    {
                        "media_type": "video",
                        "message_id": 1,
                        "file_path": "media_cache/channel/video/msg_1.mp4",
                        "download_status": "downloaded",
                        "transcription_status": "transcribed",
                        "visual_status": "success",
                    }
                ],
            )
            candidate = pilot.scan_candidates(normalized)[0]
            paths = pilot.PilotPaths.build(root / "pilot")
            pilot._prepare_workspace([candidate], paths)

            audit = pilot._audit_media([candidate], paths)

        self.assertTrue(audit["passed"])
        self.assertEqual(audit["native_media_items"], 1)

    def test_relative_artifact_paths_resolve_for_selection_and_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            channel = normalized / "channel"
            channel.mkdir(parents=True)
            transcript = channel / "relative.youtube.txt"
            metadata = channel / "relative.youtube.meta.json"
            transcript.write_text("Relative dedicated transcript. " * 30, encoding="utf-8")
            metadata.write_text(
                json.dumps(
                    {
                        "video_id": "relative",
                        "url": "https://www.youtube.com/watch?v=relative",
                        "transcript_source": "subtitles",
                        "duration_seconds": 300,
                    }
                ),
                encoding="utf-8",
            )
            self._write_post(
                normalized,
                1,
                "Telegram post with relative artifact paths. " * 20,
                youtube_urls=["https://www.youtube.com/watch?v=relative"],
                youtube_sources=[
                    {
                        "video_id": "relative",
                        "text_path": transcript.name,
                        "metadata_path": metadata.name,
                    }
                ],
            )
            candidate = pilot.select_representative_posts(normalized, limit=1)[0]
            paths = pilot.PilotPaths.build(root / "pilot")

            copied = pilot._prepare_workspace([candidate], paths)

        self.assertEqual(copied, 1)

    def test_youtube_copy_sanitizes_message_id_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "Telegram post with a traversal-shaped message ID. " * 20,
                message_id="../../../../../escape",
                youtube_urls=["https://www.youtube.com/watch?v=safeid"],
                youtube_sources=self._youtube_source(root, "safeid"),
            )
            candidate = pilot.select_representative_posts(normalized, limit=1)[0]
            paths = pilot.PilotPaths.build(root / "pilot")

            pilot._prepare_workspace([candidate], paths)

            copied_files = list(paths.youtube_normalized_dir.rglob("*"))
            self.assertTrue(copied_files)
            self.assertTrue(
                all(
                    path.resolve().is_relative_to(paths.youtube_normalized_dir.resolve())
                    for path in copied_files
                )
            )

    def test_youtube_copy_keeps_same_named_artifacts_for_multiple_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            entries = []
            for folder, video_id in (("first", "aaa"), ("second", "bbb")):
                artifact_dir = root / folder
                artifact_dir.mkdir()
                transcript = artifact_dir / "artifact.youtube.txt"
                metadata = artifact_dir / "artifact.youtube.meta.json"
                transcript.write_text(f"Transcript for {video_id}. " * 30, encoding="utf-8")
                metadata.write_text(
                    json.dumps(
                        {
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "video_id": video_id,
                            "transcript_source": "subtitles",
                        }
                    ),
                    encoding="utf-8",
                )
                entries.append(
                    {
                        "video_id": video_id,
                        "text_path": str(transcript),
                        "metadata_path": str(metadata),
                    }
                )
            self._write_post(
                normalized,
                1,
                "Telegram post with two YouTube artifacts. " * 20,
                youtube_urls=[
                    "https://www.youtube.com/watch?v=aaa",
                    "https://www.youtube.com/watch?v=bbb",
                ],
                youtube_sources=entries,
            )
            candidate = pilot.select_representative_posts(normalized, limit=1)[0]
            paths = pilot.PilotPaths.build(root / "pilot")

            copied = pilot._prepare_workspace([candidate], paths)
            with pilot.isolated_pipeline_config(paths):
                discovered = list(pilot._iter_dedicated_sources(None))

        self.assertEqual(copied, 2)
        self.assertEqual({source["video_id"] for source in discovered}, {"aaa", "bbb"})

    def test_selection_rejects_ambiguous_relative_artifact_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            channel = normalized / "channel"
            channel.mkdir(parents=True)
            project_root = root / "project"
            project_root.mkdir()
            (project_root / "same.txt").write_text("Wrong transcript.", encoding="utf-8")
            (channel / "same.txt").write_text("Correct transcript. " * 30, encoding="utf-8")
            metadata = channel / "same.youtube.meta.json"
            metadata.write_text(
                json.dumps(
                    {
                        "url": "https://www.youtube.com/watch?v=ambiguous",
                        "video_id": "ambiguous",
                        "transcript_source": "subtitles",
                    }
                ),
                encoding="utf-8",
            )
            self._write_post(
                normalized,
                1,
                "Telegram post with an ambiguous relative artifact path. " * 20,
                youtube_urls=["https://www.youtube.com/watch?v=ambiguous"],
                youtube_sources=[
                    {
                        "video_id": "ambiguous",
                        "text_path": "same.txt",
                        "metadata_path": metadata.name,
                    }
                ],
            )
            with patch.object(pilot.config, "PROJECT_ROOT", project_root):
                with self.assertRaisesRegex(ValueError, "dedicated source artifact"):
                    pilot.select_representative_posts(normalized, limit=1)

    def test_card_validation_reports_missing_episode_source_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "Telegram post with a YouTube link but no source. " * 20,
                youtube_urls=["https://www.youtube.com/watch?v=missing"],
            )
            candidate = pilot.scan_candidates(normalized)[0]
            paths = pilot.PilotPaths.build(root / "pilot")

            result = pilot._validate_produced_cards([candidate], paths)[0]

        self.assertEqual(result["status"], "failed_checks")
        self.assertIn(
            "YouTube episode expected, but no dedicated transcript source was resolved",
            result["errors"],
        )

    def test_isolation_guard_rejects_every_live_root_and_sqlite_parent(self):
        protected = [
            Path(config.OUTPUT_DIR),
            Path(config.NORMALIZED_DIR),
            Path(config.ENRICHED_DIR),
            Path(config.STATE_DIR),
            Path(config.RAG_STORAGE_DIR),
            Path(config.CARD_FTS_DB_PATH),
            Path(config.SOURCE_REGISTRY_DB_PATH),
            Path(config.CARD_FTS_DB_PATH).parent,
        ]
        for root in protected:
            with self.subTest(root=root):
                paths = pilot.PilotPaths.build(root / "pilot" if root.suffix else root)
                with self.assertRaises(ValueError):
                    pilot.assert_isolated_paths(paths, Path(config.NORMALIZED_DIR))

    def test_default_artifacts_pilot_location_is_allowed(self):
        paths = pilot.PilotPaths.build(pilot.PILOT_BASE_DIR / "safe-test-run")
        pilot.assert_isolated_paths(paths, Path(config.NORMALIZED_DIR))

    def test_isolated_pipeline_redirects_media_and_runtime_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = pilot.PilotPaths.build(Path(tmp) / "pilot")
            names = (
                "MEDIA_CACHE_DIR",
                "MEDIA_VISUAL_DIR",
                "TRANSCRIPTION_DIR",
                "INSTAGRAM_CACHE_DIR",
                "CODEX_RUNTIME_DIR",
                "LOG_DIR",
            )
            expected = {
                "MEDIA_CACHE_DIR": paths.media_cache_dir,
                "MEDIA_VISUAL_DIR": paths.media_visual_dir,
                "TRANSCRIPTION_DIR": paths.transcription_dir,
                "INSTAGRAM_CACHE_DIR": paths.instagram_cache_dir,
                "CODEX_RUNTIME_DIR": paths.codex_runtime_dir,
                "LOG_DIR": paths.log_dir,
            }
            previous = {name: getattr(config, name) for name in names}

            with pilot.isolated_pipeline_config(paths):
                for name in names:
                    self.assertEqual(getattr(config, name), expected[name])
                    self.assertTrue(
                        Path(getattr(config, name)).resolve().is_relative_to(paths.pilot_dir.resolve())
                    )

            for name in names:
                self.assertEqual(getattr(config, name), previous[name])
    def test_isolation_guard_resolves_path_traversal_before_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_output = root / "output"
            traversing = root / "safe" / ".." / "output" / "pilot"
            with patch.object(config, "OUTPUT_DIR", live_output):
                with self.assertRaises(ValueError):
                    pilot.assert_isolated_paths(
                        pilot.PilotPaths.build(traversing),
                        root / "normalized-source",
                    )

    def test_main_rejects_output_pilot_before_mkdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._build_sample(normalized)
            live_output = root / "output"
            forbidden = live_output / "pilot"
            self.assertFalse(forbidden.exists())

            with (
                patch.object(config, "OUTPUT_DIR", live_output),
                patch.object(pilot, "run_live_pilot") as live,
            ):
                exit_code = pilot.main(
                    [
                        "--limit",
                        "10",
                        "--normalized-dir",
                        str(normalized),
                        "--output-dir",
                        str(forbidden),
                    ]
                )

            self.assertEqual(exit_code, 2)
            live.assert_not_called()
            self.assertFalse(forbidden.exists())

    def test_report_writes_separate_self_and_curated_sections_without_source_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = pilot.PilotPaths.build(Path(tmp) / "pilot")
            report = {
                "run_id": "test-run",
                "passed": True,
                "versions": {
                    "schema_version": "enriched_v2",
                    "prompt_version": "prompt-v1",
                    "model": "test-model",
                },
                "selected": [
                    {
                        "normalized_file": "channel/001.txt",
                        "content_type": "telegram_post",
                        "body_char_count": 100,
                        "traits": ["telegram_short"],
                        "nominal_model_calls": 1,
                    }
                ],
                "call_estimates": {
                    "nominal_model_calls": 1,
                    "theoretical_max_model_calls_with_one_repair_per_card": 2,
                    "theoretical_max_http_requests_including_400_fallback": 4,
                },
                "enrichment_stats": {
                    "enriched": 1,
                    "failed": 0,
                    "partial": 0,
                    "repaired": 0,
                },
                "cards": [],
                "recall": {
                    "self_recall": {"top_k": 10, "checks": []},
                    "curated_golden": {"configured": False, "checks": []},
                },
                "errors": [],
            }

            pilot.write_report(report, paths)

            loaded = json.loads(paths.report_json.read_text(encoding="utf-8"))
            self.assertEqual(loaded["run_id"], "test-run")
            markdown = paths.report_md.read_text(encoding="utf-8")
            self.assertIn("Self recall@10", markdown)
            self.assertIn("Curated golden", markdown)
            self.assertIn("Not configured", markdown)
            self.assertNotIn("secret source body", markdown)

    def test_card_fixture_matches_real_enriched_v2_entities_structure(self):
        card = EnrichedCardV2.model_validate(self._card_dict())
        self.assertEqual(card.entities.equipment, [])
        self.assertEqual(card.entities.weapons, [])

    def test_card_identity_checks_fail_wrong_source_path_and_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "YouTube transcript about a concrete policy decision. " * 100,
                youtube_urls=["https://www.youtube.com/watch?v=identity"],
                youtube_sources=self._youtube_source(root, "identity"),
            )
            selected = pilot.select_representative_posts(normalized, limit=1)
            paths = pilot.PilotPaths.build(root / "pilot")
            pilot._prepare_workspace(selected, paths)
            expected_id, expected_path, expected_type = pilot._expected_card_identity(
                selected[0], paths
            )

            cases = (
                ("source", {"source_id": "telegram:wrong:999"}),
                ("path", {"normalized_path": "output/normalized/channel/wrong.txt"}),
                ("type", {"content_type": "telegram_post"}),
            )
            for label, mutation in cases:
                with self.subTest(label=label):
                    card = self._card_dict(
                        source_id=expected_id,
                        normalized_path=expected_path,
                        content_type=expected_type,
                    )
                    if "content_type" in mutation:
                        card["content_type"] = mutation["content_type"]
                    else:
                        card["provenance"].update(mutation)
                    checks = pilot._static_card_checks(
                        EnrichedCardV2.model_validate(card),
                        card,
                        selected[0],
                        paths,
                    )
                    self.assertFalse(all(checks.values()))
                    self.assertFalse(checks[f"{'content_type' if label == 'type' else 'provenance_' + ('source_id' if label == 'source' else 'normalized_path')}_exact"])

    def test_self_recall_is_top_ten_and_uses_specific_multiword_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "YouTube transcript about North Korea missile negotiations. " * 80,
                youtube_urls=["https://www.youtube.com/watch?v=recall"],
                youtube_sources=self._youtube_source(root, "recall"),
            )
            selected = pilot.select_representative_posts(normalized, limit=1)
            paths = pilot.PilotPaths.build(root / "pilot")
            pilot._prepare_workspace(selected, paths)
            source_id, normalized_path, content_type = pilot._expected_card_identity(selected[0], paths)
            card_path = paths.enriched_dir / "channel" / "001.enriched.json"
            card_path.parent.mkdir(parents=True, exist_ok=True)
            card = self._card_dict(
                source_id=source_id,
                normalized_path=normalized_path,
                content_type=content_type,
            )
            card["topics"] = [
                {"label": "politics", "salience": "primary", "type": "policy_topic"}
            ]
            card["search_phrases"] = [
                {"text": "North Korea missile negotiations", "source": "phrase_from_text"}
            ]
            card["search_text"] = "North Korea missile negotiations"
            card_path.write_text(json.dumps(card), encoding="utf-8")
            rebuild_card_index(paths.enriched_dir, paths.card_fts_db)
            card_results = [
                {
                    "status": "valid",
                    "source_id": source_id,
                    "card_file": pilot._relative_or_string(card_path, paths.pilot_dir),
                }
            ]

            with pilot.isolated_pipeline_config(paths):
                recall = pilot._run_recall_checks(card_results, paths)

            self_check = recall["self_recall"]["checks"][0]
            self.assertEqual(recall["self_recall"]["top_k"], 10)
            self.assertEqual(self_check["query"], "North Korea missile negotiations")
            self.assertEqual(self_check["rank"], 1)
            self.assertTrue(self_check["passed"])
            self.assertFalse(recall["curated_golden"]["configured"])

    def test_curated_golden_manifest_resolves_selected_files_and_fails_missing_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "YouTube transcript about Alpha Treaty. " * 80,
                youtube_urls=["https://www.youtube.com/watch?v=golden"],
                youtube_sources=self._youtube_source(root, "golden"),
            )
            selected = pilot.select_representative_posts(normalized, limit=1)
            paths = pilot.PilotPaths.build(root / "pilot")
            source_id, _, _ = pilot._expected_card_identity(selected[0], paths)
            manifest_path = root / "golden.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "queries": [
                            {
                                "query": "query with no indexed match",
                                "must_find_normalized_files": [selected[0].relative_txt],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            manifest = pilot._load_curated_golden_manifest(manifest_path, selected, paths)
            with pilot.isolated_pipeline_config(paths):
                recall = pilot._run_recall_checks(
                    [], paths, curated_golden=manifest, golden_top_k=20
                )

            golden = recall["curated_golden"]
            self.assertTrue(golden["configured"])
            self.assertEqual(golden["checks"][0]["must_find_source_ids"], [source_id])
            self.assertFalse(golden["checks"][0]["passed"])
            self.assertFalse(golden["passed"])

    def test_golden_manifest_rejects_expectations_outside_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "YouTube transcript about a policy. " * 80,
                youtube_urls=["https://www.youtube.com/watch?v=manifest"],
                youtube_sources=self._youtube_source(root, "manifest"),
            )
            selected = pilot.select_representative_posts(normalized, limit=1)
            paths = pilot.PilotPaths.build(root / "pilot")
            manifest_path = root / "golden.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "queries": [
                            {
                                "query": "policy",
                                "must_find_source_ids": ["telegram:not:selected"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "outside the selected pilot"):
                pilot._load_curated_golden_manifest(manifest_path, selected, paths)

    def test_call_estimates_label_model_calls_and_http_upper_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            normalized = Path(tmp) / "normalized"
            self._build_sample(normalized)
            selected = pilot.select_representative_posts(normalized, limit=10)
            paths = pilot.PilotPaths.build(Path(tmp) / "pilot")

            estimates = pilot._base_report("counts", selected, paths)["call_estimates"]

            self.assertEqual(
                estimates["theoretical_max_http_requests_including_400_fallback"],
                2 * estimates["theoretical_max_model_calls_with_one_repair_per_card"],
            )
            self.assertGreaterEqual(
                estimates["theoretical_max_model_calls_with_one_repair_per_card"],
                estimates["nominal_model_calls"],
            )

    def test_default_mode_never_calls_live_pipeline_or_creates_pilot_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._build_sample(normalized)
            output_dir = root / "planned-pilot"

            with patch.object(pilot, "run_live_pilot") as live:
                exit_code = pilot.main(
                    [
                        "--limit",
                        "10",
                        "--run-id",
                        "dry-test",
                        "--normalized-dir",
                        str(normalized),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            live.assert_not_called()
            self.assertFalse(output_dir.exists())

    def test_live_harness_is_fully_redirected_when_pipeline_is_mocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "YouTube transcript about a policy. " * 80,
                youtube_urls=["https://www.youtube.com/watch?v=mock"],
                youtube_sources=self._youtube_source(root, "mock"),
            )
            selected = pilot.select_representative_posts(normalized, limit=1)
            paths = pilot.PilotPaths.build(root / "pilot")

            def fake_enrich_all(*, force):
                self.assertTrue(force)
                self.assertEqual(Path(config.NORMALIZED_DIR), paths.normalized_dir)
                self.assertEqual(Path(config.ENRICHED_DIR), paths.enriched_dir)
                self.assertEqual(Path(config.CARD_FTS_DB_PATH), paths.card_fts_db)
                return pilot.EnrichmentStats(scanned=1, enriched=1)

            source_id, _, content_type = pilot._expected_card_identity(selected[0], paths)
            card_result = {
                "normalized_file": selected[0].relative_txt,
                "card_file": "workspace/output/enriched/channel/001.enriched.json",
                "source_id": source_id,
                "content_type": content_type,
                "status": "valid",
                "checks": {"strict_enriched_v2": True},
                "errors": [],
            }
            recall_result = {
                "self_recall": {
                    "configured": True,
                    "top_k": 10,
                    "checks": [{"passed": True}],
                    "passed": True,
                },
                "curated_golden": {
                    "configured": False,
                    "top_k": 20,
                    "checks": [],
                    "passed": None,
                },
            }
            with (
                patch.object(pilot, "enrich_all", side_effect=fake_enrich_all),
                patch.object(pilot, "_rebuild_isolated_indexes", return_value=({}, [])),
                patch.object(pilot, "_validate_produced_cards", return_value=[card_result]),
                patch.object(pilot, "_run_recall_checks", return_value=recall_result),
            ):
                report, passed = pilot.run_live_pilot(
                    selected, paths, normalized, "mock-live"
                )

            self.assertTrue(passed)
            self.assertTrue(report["passed"])
            self.assertTrue(paths.report_json.exists())
            self.assertTrue((paths.normalized_dir / selected[0].relative_txt).exists())
            self.assertNotEqual(paths.enriched_dir.resolve(), Path(config.ENRICHED_DIR).resolve())

    def test_live_harness_fails_before_enrichment_when_discovery_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized"
            self._write_post(
                normalized,
                1,
                "YouTube transcript about a policy. " * 80,
                youtube_urls=["https://www.youtube.com/watch?v=preflight"],
                youtube_sources=self._youtube_source(root, "preflight"),
            )
            selected = pilot.select_representative_posts(normalized, limit=1)
            paths = pilot.PilotPaths.build(root / "pilot")
            enrich = patch.object(pilot, "enrich_all")

            with (
                enrich as enrich_mock,
                patch.object(pilot, "_iter_dedicated_sources", return_value=[]),
            ):
                report, passed = pilot.run_live_pilot(
                    selected, paths, normalized, "missing-discovery"
                )

            self.assertFalse(passed)
            self.assertFalse(report["passed"])
            enrich_mock.assert_not_called()
            self.assertTrue(any("discovery preflight failed" in error for error in report["errors"]))

    def test_preflight_rejects_duplicate_discovered_video_id(self):
        selected = [
            pilot.PilotCandidate(
                txt_path=Path("post.txt"),
                meta_path=Path("post.meta.json"),
                relative_txt="post.txt",
                relative_meta="post.meta.json",
                content_type="telegram_post",
                char_count=100,
                body_char_count=100,
                traits=("youtube",),
                youtube_video_ids=("duplicate",),
                youtube_long_video_ids=(),
                estimated_llm_calls=1,
                estimated_llm_calls_with_repair=2,
            )
        ]
        source = {"video_id": "duplicate"}
        with patch.object(pilot, "_iter_dedicated_sources", return_value=[source, source]):
            with self.assertRaisesRegex(ValueError, "not_exactly_once=duplicate"):
                pilot._preflight_youtube_discovery(selected)

    @staticmethod
    def _write_post(
        normalized: Path,
        index: int,
        body: str,
        **meta_overrides,
    ) -> None:
        channel_dir = normalized / "channel"
        channel_dir.mkdir(parents=True, exist_ok=True)
        txt_path = channel_dir / f"{index:03d}.txt"
        txt_path.write_text(body, encoding="utf-8")
        meta = {
            "channel_name": "channel",
            "channel_id": 100,
            "message_id": index,
            "date": "2026-01-01T00:00:00+00:00",
            "has_text": True,
        }
        meta.update(meta_overrides)
        txt_path.with_suffix(".meta.json").write_text(json.dumps(meta), encoding="utf-8")

    @staticmethod
    def _youtube_source(root: Path, video_id: str) -> list[dict]:
        transcript = root / f"{video_id}-transcript.txt"
        transcript.write_text(f"Dedicated transcript for {video_id}. " * 30, encoding="utf-8")
        return [EnrichedV2PilotTests._youtube_source_entry(transcript, video_id, 300)]

    @staticmethod
    def _youtube_source_entry(transcript: Path, video_id: str, duration_seconds: int) -> dict:
        metadata = transcript.with_name(f"{transcript.stem}.youtube.meta.json")
        metadata.write_text(
            json.dumps(
                {
                    "video_id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "transcript_source": "subtitles",
                    "duration_seconds": duration_seconds,
                }
            ),
            encoding="utf-8",
        )
        return {
            "video_id": video_id,
            "text_path": str(transcript),
            "metadata_path": str(metadata),
            "duration_seconds": duration_seconds,
        }

    @classmethod
    def _build_sample(cls, normalized: Path) -> None:
        cls._write_post(normalized, 1, "Short concrete political statement " * 8)
        cls._write_post(normalized, 2, "Medium policy report. " * 80)
        cls._write_post(normalized, 3, "Long diplomatic transcript. " * 180)
        cls._write_post(normalized, 4, "Forwarded minister statement. " * 50, is_forward=True)
        youtube_transcript = normalized.parent / "youtube-sample.txt"
        youtube_transcript.write_text(
            "YouTube transcript about a concrete decision. " * 90,
            encoding="utf-8",
        )
        cls._write_post(
            normalized,
            5,
            "YouTube transcript about a concrete decision. " * 90,
            youtube_urls=["https://www.youtube.com/watch?v=sample-video"],
            youtube_sources=[cls._youtube_source_entry(youtube_transcript, "sample-video", 300)],
        )
        cls._write_post(
            normalized,
            6,
            "Instagram transcript about parliament. " * 45,
            instagram_urls=["https://instagram.example/post"],
        )
        cls._write_post(
            normalized,
            7,
            "[Web article: https://example.test/article]\nWeb article text about sanctions. " * 70,
            web_urls=["https://example.test/article"],
        )
        cls._write_post(normalized, 8, "[Media omitted]\nConcrete caption about NATO. " * 30, has_images=True)
        cls._write_post(normalized, 9, "Additional short report. " * 15, is_forward=True)
        cls._write_post(normalized, 10, "Additional medium report. " * 65)
        cls._write_post(normalized, 11, "Another policy statement. " * 75)
        cls._write_post(
            normalized,
            12,
            "Another long YouTube transcript. " * 170,
            youtube_urls=["https://www.youtube.com/watch?v=second-video"],
        )

    @staticmethod
    def _card_dict(
        *,
        source_id: str = "telegram:100:1",
        normalized_path: str = "output/normalized/channel/001.txt",
        content_type: str = "telegram_post",
    ) -> dict:
        return {
            "schema_version": "enriched_v2",
            "prompt_version": "test-prompt",
            "enrichment_model": "test-model",
            "enriched_at": "2026-01-01T00:00:00+00:00",
            "provenance": {
                "source_id": source_id,
                "source_type": content_type,
                "channel": "channel",
                "date": "2026-01-01T00:00:00+00:00",
                "post_url": "",
                "message_id": 1,
                "forwarded_from": None,
                "normalized_path": normalized_path,
            },
            "content_type": content_type,
            "language": "ru",
            "summary": "North Korea reported a concrete policy decision.",
            "key_points": [],
            "entities": {
                "people": [],
                "organizations": [],
                "countries": [
                    {"text": "North Korea", "role": "actor", "salience": "primary"}
                ],
                "locations": [],
                "military_units": [],
                "equipment": [],
                "weapons": [],
                "programs_projects": [],
                "media_sources": [],
                "other": [],
            },
            "topics": [],
            "theses": [],
            "quotes": [],
            "events": [],
            "search_phrases": [
                {"text": "North Korea policy decision", "source": "surface_form"}
            ],
            "source_chain": {
                "original_source": None,
                "forwarded_from": None,
                "mentioned_sources": [],
                "external_links": [],
            },
            "graph_text": "North Korea reported a concrete policy decision.",
            "search_text": "North Korea policy decision",
            "ignored_blocks": [],
            "quality_flags": [],
        }


if __name__ == "__main__":
    unittest.main()
