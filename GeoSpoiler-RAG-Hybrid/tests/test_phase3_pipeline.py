import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import cli_pipeline
from enricher.pipeline import EnrichmentStats
from retrieval.card_fts import CardFtsBuildStats, YouTubeSegmentFtsBuildStats
from retrieval.source_registry import SourceRegistryStats


class _FakeRag:
    async def finalize_storages(self):
        return None


class Phase3PipelineTests(unittest.TestCase):
    def _index_stats(self, root: Path):
        return (
            CardFtsBuildStats(
                db_path=root / "card_fts.sqlite",
                cards_seen=4,
                cards_indexed=3,
                cards_skipped=1,
            ),
            SourceRegistryStats(
                db_path=root / "source_registry.sqlite",
                run_id="test-run",
                sources=4,
                normalized_docs=4,
                enriched_cards=3,
                references=2,
            ),
        )

    def _wiki_stats(self, root: Path):
        return SimpleNamespace(
            review_counts=SimpleNamespace(
                concepts=0,
                hierarchy=0,
                ambiguities=0,
            ),
            projections=SimpleNamespace(hubs_built=0, fts_documents=0),
            database_path=root / "wiki.sqlite",
        )

    @patch.object(cli_pipeline.config, "WIKI_ENABLED", True)
    def test_cmd_enrich_refreshes_local_indexes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fts_stats, registry_stats = self._index_stats(root)
            fts_rebuild = Mock(return_value=fts_stats)
            registry_rebuild = Mock(return_value=registry_stats)
            youtube_rebuild = Mock(return_value=YouTubeSegmentFtsBuildStats(Path(tmpdir) / "youtube.sqlite", 0, 0, 0))
            wiki_refresh = Mock(return_value=self._wiki_stats(root))
            enrichment = EnrichmentStats(scanned=4, enriched=3)

            with patch.object(cli_pipeline, "enrich_all", return_value=enrichment):
                with patch.object(cli_pipeline, "rebuild_card_index", fts_rebuild):
                    with patch.object(cli_pipeline, "rebuild_source_registry", registry_rebuild):
                        with patch.object(cli_pipeline, "rebuild_youtube_segment_index", youtube_rebuild):
                            with patch.object(
                                cli_pipeline,
                                "run_configured_wiki_pipeline",
                                wiki_refresh,
                            ):
                                with patch.object(cli_pipeline, "_print_enrich_summary"):
                                    result = cli_pipeline.cmd_enrich()

        fts_rebuild.assert_called_once_with()
        registry_rebuild.assert_called_once_with()
        youtube_rebuild.assert_called_once_with()
        wiki_refresh.assert_called_once_with()
        self.assertIs(result.enrichment, enrichment)
        self.assertIs(result.retrieval.card_fts, fts_stats)
        self.assertIs(result.retrieval.source_registry, registry_stats)

    @patch.object(cli_pipeline.config, "WIKI_ENABLED", True)
    def test_cmd_enrich_refreshes_indexes_even_when_enrichment_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fts_stats, registry_stats = self._index_stats(root)
            fts_rebuild = Mock(return_value=fts_stats)
            registry_rebuild = Mock(return_value=registry_stats)
            youtube_rebuild = Mock(return_value=YouTubeSegmentFtsBuildStats(Path(tmpdir) / "youtube.sqlite", 0, 0, 0))
            wiki_refresh = Mock(return_value=self._wiki_stats(root))

            with patch.object(cli_pipeline, "enrich_all", side_effect=RuntimeError("batch failed")):
                with patch.object(cli_pipeline, "rebuild_card_index", fts_rebuild):
                    with patch.object(cli_pipeline, "rebuild_source_registry", registry_rebuild):
                        with patch.object(cli_pipeline, "rebuild_youtube_segment_index", youtube_rebuild):
                            with patch.object(
                                cli_pipeline,
                                "run_configured_wiki_pipeline",
                                wiki_refresh,
                            ):
                                with self.assertRaisesRegex(RuntimeError, "batch failed"):
                                    cli_pipeline.cmd_enrich()

        fts_rebuild.assert_called_once_with()
        registry_rebuild.assert_called_once_with()
        youtube_rebuild.assert_called_once_with()
        wiki_refresh.assert_called_once_with()

    @patch.object(cli_pipeline.config, "WIKI_ENABLED", True)
    def test_refresh_attempts_registry_when_fts_rebuild_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _, registry_stats = self._index_stats(root)
            registry_rebuild = Mock(return_value=registry_stats)
            youtube_rebuild = Mock(return_value=YouTubeSegmentFtsBuildStats(Path(tmpdir) / "youtube.sqlite", 0, 0, 0))
            wiki_refresh = Mock(return_value=self._wiki_stats(root))

            with self.assertRaises(cli_pipeline.RetrievalRefreshError) as raised:
                cli_pipeline.refresh_enriched_retrieval(
                    rebuild_fts=Mock(side_effect=RuntimeError("fts failed")),
                    rebuild_registry=registry_rebuild,
                    rebuild_youtube_segments=youtube_rebuild,
                    refresh_wiki=wiki_refresh,
                )

        registry_rebuild.assert_called_once_with()
        youtube_rebuild.assert_called_once_with()
        wiki_refresh.assert_called_once_with()
        self.assertIsNone(raised.exception.stats.card_fts)
        self.assertIs(raised.exception.stats.source_registry, registry_stats)
        self.assertEqual(len(raised.exception.stats.errors), 1)

    @patch.object(cli_pipeline.config, "WIKI_ENABLED", True)
    def test_cmd_enrich_fails_after_attempting_both_indexes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _, registry_stats = self._index_stats(root)
            fts_rebuild = Mock(side_effect=RuntimeError("fts failed"))
            registry_rebuild = Mock(return_value=registry_stats)
            youtube_rebuild = Mock(return_value=YouTubeSegmentFtsBuildStats(Path(tmpdir) / "youtube.sqlite", 0, 0, 0))
            wiki_refresh = Mock(return_value=self._wiki_stats(root))

            with patch.object(cli_pipeline, "enrich_all", return_value=EnrichmentStats()):
                with patch.object(cli_pipeline, "rebuild_card_index", fts_rebuild):
                    with patch.object(cli_pipeline, "rebuild_source_registry", registry_rebuild):
                        with patch.object(cli_pipeline, "rebuild_youtube_segment_index", youtube_rebuild):
                            with patch.object(
                                cli_pipeline,
                                "run_configured_wiki_pipeline",
                                wiki_refresh,
                            ):
                                with self.assertRaises(cli_pipeline.RetrievalRefreshError):
                                    cli_pipeline.cmd_enrich()

        fts_rebuild.assert_called_once_with()
        registry_rebuild.assert_called_once_with()
        youtube_rebuild.assert_called_once_with()
        wiki_refresh.assert_called_once_with()

    def test_refresh_skips_wiki_completely_when_master_switch_is_off(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fts_stats, registry_stats = self._index_stats(root)
            youtube_stats = YouTubeSegmentFtsBuildStats(
                root / "youtube.sqlite",
                0,
                0,
                0,
            )
            wiki_refresh = Mock(side_effect=AssertionError("Wiki must stay off"))

            with patch.object(cli_pipeline.config, "WIKI_ENABLED", False):
                stats = cli_pipeline.refresh_enriched_retrieval(
                    rebuild_fts=Mock(return_value=fts_stats),
                    rebuild_registry=Mock(return_value=registry_stats),
                    rebuild_youtube_segments=Mock(return_value=youtube_stats),
                    refresh_wiki=wiki_refresh,
                )

        wiki_refresh.assert_not_called()
        self.assertIsNone(stats.wiki)
        self.assertEqual(stats.errors, ())

    def test_cmd_run_propagates_index_refresh_failure(self):
        refresh_error = cli_pipeline.RetrievalRefreshError(
            cli_pipeline.RetrievalRefreshStats(
                card_fts=None,
                source_registry=None,
                errors=("card FTS rebuild failed",),
            )
        )

        with patch.object(cli_pipeline, "cmd_fetch", AsyncMock(return_value=[])):
            with patch.object(cli_pipeline, "cmd_enrich", side_effect=refresh_error):
                with patch.object(cli_pipeline, "_print_pipeline_summary") as summary:
                    with self.assertRaises(cli_pipeline.RetrievalRefreshError):
                        asyncio.run(cli_pipeline.cmd_run())

        summary.assert_not_called()


class Phase3LoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_load_uses_enriched_loader_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            enriched_dir = Path(tmpdir) / "enriched"
            enriched_dir.mkdir()
            (enriched_dir / "one.enriched.json").write_text("{}", encoding="utf-8")
            rag = _FakeRag()

            with patch.object(cli_pipeline.config, "ENRICHED_DIR", enriched_dir):
                with patch.object(cli_pipeline, "create_rag", AsyncMock(return_value=rag)):
                    with patch.object(cli_pipeline, "load_from_enriched", AsyncMock(return_value=1)) as load:
                        with patch.object(
                            cli_pipeline,
                            "auto_fix_safe_entity_merges",
                            AsyncMock(return_value=[]),
                        ):
                            stats = await cli_pipeline.cmd_load()

        load.assert_awaited_once_with(rag)
        self.assertEqual(stats.enriched_cards_seen, 1)
        self.assertEqual(stats.graph_texts_loaded, 1)
        self.assertFalse(hasattr(stats, "reviewed_loaded"))


if __name__ == "__main__":
    unittest.main()
