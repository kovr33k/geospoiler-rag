import importlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli_query
import cli_runtime
import config
from cli import (
    cmd_experiments_index,
    cmd_fts_rebuild,
    cmd_fts_search,
    cmd_registry_rebuild,
    cmd_registry_resolve,
    cmd_transcribe_backfill,
    cmd_validate_enriched,
)
from data_validation import ContractIssue, EnrichedValidationReport
from experiment_registry import ExperimentRegistry
from normalizer.transcription_backfill import BackfillItemResult, BackfillStats
from retrieval.card_fts import CardFtsBuildStats, CardFtsMatch
from retrieval.shadow_search import ShadowMatch
from retrieval.source_registry import SourcePassport, SourceRegistryStats


class CliCommandTests(unittest.TestCase):
    def test_default_query_mode_lives_with_query_cli(self):
        self.assertFalse(hasattr(cli_runtime, "_default_query_mode"))

        with patch.object(config, "RERANKER_ENABLED", False):
            self.assertEqual(cli_query._default_query_mode(), "hybrid")
        with patch.object(config, "RERANKER_ENABLED", True):
            self.assertEqual(cli_query._default_query_mode(), "mix")

    def test_cli_wrapper_reexports_tools_module(self):
        compat_cli = importlib.import_module("cli")
        cli_tools = importlib.import_module("cli_tools")

        self.assertIs(compat_cli.cmd_fts_search, cli_tools.cmd_fts_search)
        self.assertIs(compat_cli.cmd_registry_resolve, cli_tools.cmd_registry_resolve)

    def test_cmd_experiments_index_prints_registry_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = ExperimentRegistry(
                generated_at="2026-05-31T00:00:00+00:00",
                records=[],
                manifest_path=Path(tmpdir) / "experiment_registry.json",
                report_path=Path(tmpdir) / "experiment_registry.md",
            )
            output = io.StringIO()

            with patch("sys.stdout", output):
                returned = cmd_experiments_index(write_registry=lambda: registry)

        text = output.getvalue()
        self.assertIs(returned, registry)
        self.assertIn("Experiment registry complete.", text)
        self.assertIn("Records: 0", text)
        self.assertIn("experiment_registry.json", text)
        self.assertIn("experiment_registry.md", text)

    def test_cmd_validate_enriched_prints_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "validation.md"
            report = EnrichedValidationReport(
                cards_seen=3,
                cards_valid=2,
                cards_invalid=1,
                warnings=[
                    ContractIssue(
                        severity="warning",
                        code="sample_warning",
                        path="card.json",
                        message="sample",
                    )
                ],
            )
            output = io.StringIO()

            with patch("sys.stdout", output):
                returned = cmd_validate_enriched(
                    scan_cards=lambda _path: report,
                    write_report=lambda _report: report_path,
                )

        text = output.getvalue()
        self.assertIs(returned, report)
        self.assertIn("Enriched validation complete.", text)
        self.assertIn("Cards seen: 3", text)
        self.assertIn("Errors: 0", text)
        self.assertIn("Warnings: 1", text)
        self.assertIn("validation.md", text)

    def test_cmd_validate_enriched_can_fail_on_errors(self):
        report = EnrichedValidationReport(
            cards_seen=1,
            cards_invalid=1,
            errors=[
                ContractIssue(
                    severity="error",
                    code="schema_error",
                    path="card.json",
                    message="bad card",
                )
            ],
        )

        with patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit):
                cmd_validate_enriched(
                    fail_on_error=True,
                    scan_cards=lambda _path: report,
                    write_report=lambda _report: Path("validation.md"),
                )

    def test_cmd_registry_rebuild_prints_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stats = SourceRegistryStats(
                db_path=Path(tmpdir) / "source_registry.sqlite",
                run_id="2026-05-31T00:00:00+00:00",
                sources=2,
                normalized_docs=3,
                enriched_cards=4,
                references=5,
            )
            output = io.StringIO()

            with patch("sys.stdout", output):
                returned = cmd_registry_rebuild(rebuild_registry=lambda **_kwargs: stats)

        text = output.getvalue()
        self.assertIs(returned, stats)
        self.assertIn("Source registry rebuild complete.", text)
        self.assertIn("Sources: 2", text)
        self.assertIn("Normalized docs: 3", text)
        self.assertIn("References: 5", text)

    def test_cmd_registry_resolve_prints_passport(self):
        passport = SourcePassport(
            source_id="telegram:1:10",
            post_url="https://t.me/c/1/10",
            primary_url="https://www.youtube.com/watch?v=abc",
            normalized_file="output/normalized/Hungary/10.txt",
            meta_file="output/normalized/Hungary/10.meta.json",
            card_path="output/enriched/Hungary/10.enriched.json",
            channel_name="Hungary",
            channel_id="1",
            message_id="10",
            date="2026-05-31T00:00:00+00:00",
            content_type="news",
            language="ru",
            youtube_url="https://www.youtube.com/watch?v=abc",
            original_source="Telegram",
        )
        output = io.StringIO()

        with patch("sys.stdout", output):
            returned = cmd_registry_resolve(
                "telegram:1:10",
                resolve_registry_source=lambda *_args, **_kwargs: passport,
            )

        text = output.getvalue()
        self.assertIs(returned, passport)
        self.assertIn("Source: telegram:1:10", text)
        self.assertIn("primary_url: https://www.youtube.com/watch?v=abc", text)
        self.assertIn("youtube_url: https://www.youtube.com/watch?v=abc", text)
        self.assertIn("message_id: 10", text)

    def test_cmd_registry_resolve_prints_missing_source(self):
        output = io.StringIO()

        with patch("sys.stdout", output):
            returned = cmd_registry_resolve(
                "telegram:missing:1",
                resolve_registry_source=lambda *_args, **_kwargs: None,
            )

        self.assertIsNone(returned)
        self.assertIn("Source not found: telegram:missing:1", output.getvalue())

    def test_cmd_fts_rebuild_prints_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stats = CardFtsBuildStats(
                db_path=Path(tmpdir) / "card_fts.sqlite",
                cards_seen=5,
                cards_indexed=4,
                cards_skipped=1,
            )
            output = io.StringIO()

            with patch("sys.stdout", output):
                returned = cmd_fts_rebuild(rebuild_index=lambda: stats)

        text = output.getvalue()
        self.assertIs(returned, stats)
        self.assertIn("Card FTS rebuild complete.", text)
        self.assertIn("Cards seen: 5", text)
        self.assertIn("Cards indexed: 4", text)
        self.assertIn("Cards skipped: 1", text)

    def test_cmd_fts_search_prints_matches_and_shadow_comparison(self):
        match = CardFtsMatch(
            source_id="telegram:1:10",
            card_path="card.json",
            normalized_file="normalized/10.txt",
            post_url="https://t.me/c/1/10",
            title="Hungary - 2026-05-31",
            score=1.25,
            snippet="Trump Orban snippet",
        )
        shadow = ShadowMatch(
            source_path="normalized/10.txt",
            card_path="card.json",
            score=2.0,
            snippet="shadow snippet",
            title="Shadow title",
        )
        output = io.StringIO()

        with patch("sys.stdout", output):
            returned = cmd_fts_search(
                "Trump Orban",
                top_k=3,
                compare_shadow=True,
                search_index=lambda *_args, **_kwargs: [match],
                search_shadow=lambda *_args, **_kwargs: [shadow],
            )

        text = output.getvalue()
        self.assertEqual(returned, [match])
        self.assertIn("Card FTS search: Trump Orban", text)
        self.assertIn("Results: 1", text)
        self.assertIn("Hungary - 2026-05-31", text)
        self.assertIn("source_id: telegram:1:10", text)
        self.assertIn("Shadow search comparison: 1", text)
        self.assertIn("Shadow title", text)

    def test_cmd_fts_search_prints_empty_hint(self):
        output = io.StringIO()

        with patch("sys.stdout", output):
            returned = cmd_fts_search(
                "missing",
                search_index=lambda *_args, **_kwargs: [],
            )

        self.assertEqual(returned, [])
        self.assertIn("No FTS matches.", output.getvalue())

    def test_cmd_transcribe_backfill_prints_summary_and_items(self):
        stats = BackfillStats(
            candidates_seen=2,
            attempted=1,
            transcribed=1,
            skipped=0,
            failed=0,
            normalized_updated=1,
            dry_run=False,
            items=[
                BackfillItemResult(
                    normalized_file="output/normalized/Channel/10.txt",
                    media_type="voice",
                    message_id=10,
                    status="transcribed",
                    updated=True,
                )
            ],
        )
        captured = {}
        output = io.StringIO()

        def fake_backfill(**kwargs):
            captured.update(kwargs)
            return stats

        with patch("sys.stdout", output):
            returned = cmd_transcribe_backfill(
                limit=2,
                channel="Channel",
                media_type="voice",
                dry_run=True,
                backfill=fake_backfill,
            )

        text = output.getvalue()
        self.assertIs(returned, stats)
        self.assertEqual(captured["limit"], 2)
        self.assertEqual(captured["channel"], "Channel")
        self.assertEqual(captured["media_type"], "voice")
        self.assertTrue(captured["dry_run"])
        self.assertIn("Transcription backfill complete.", text)
        self.assertIn("Attempted: 1", text)
        self.assertIn("Transcribed/cached: 1", text)
        self.assertIn("Normalized files updated: 1", text)
        self.assertIn("transcribed voice msg=10 updated", text)

    def test_cmd_transcribe_backfill_prints_item_errors(self):
        stats = BackfillStats(
            attempted=1,
            failed=1,
            items=[
                BackfillItemResult(
                    normalized_file="output/normalized/Channel/11.txt",
                    media_type="video",
                    message_id=11,
                    status="failed",
                    error="provider timeout",
                )
            ],
        )
        output = io.StringIO()

        with patch("sys.stdout", output):
            cmd_transcribe_backfill(backfill=lambda **_kwargs: stats)

        self.assertIn("failed video msg=11 error=provider timeout", output.getvalue())
