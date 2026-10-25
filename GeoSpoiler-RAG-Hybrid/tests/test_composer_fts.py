import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrieval import composer  # noqa: E402
from retrieval.card_fts import CardFtsMatch  # noqa: E402
from retrieval.shadow_search import ShadowMatch  # noqa: E402
from retrieval.source_registry import rebuild_source_registry  # noqa: E402


class ComposerFtsTests(unittest.TestCase):
    def test_thesis_mode_handles_v2_thesis_objects(self):
        card = {
            "_path": "output/enriched/Channel/1.enriched.json",
            "provenance": {
                "source_id": "telegram:1:1",
                "normalized_path": "output/normalized/Channel/1.txt",
                "channel": "Channel",
                "date": "2026-01-01T00:00:00+00:00",
            },
            "theses": [{"text": "China supports the policy", "speaker": "Source"}],
            "entities": {},
        }
        with (
            patch.object(composer, "_load_all_cards", return_value=[card]),
            patch.object(composer, "search_card_index", return_value=[]),
            patch.object(composer.shadow_search, "search", return_value=[]),
            patch.object(composer, "_search_youtube_segment_hits", return_value=[]),
            patch.object(
                composer,
                "query_rag_result",
                new=AsyncMock(return_value={"response": ""}),
            ),
        ):
            package = asyncio.run(composer.search(object(), "China supports", mode="thesis"))

        self.assertEqual(len(package.primary_results), 1)
        self.assertIn("China supports the policy", package.primary_results[0].snippets)

    def test_cards_only_runs_fts_and_shadow_with_fts_first(self):
        fts_match = CardFtsMatch(
            source_id="telegram:1:10",
            card_path="output/enriched/Hungary/10.enriched.json",
            normalized_file="output/normalized/Hungary/10.txt",
            post_url="https://t.me/c/1/10",
            title="Hungary - 2026-05-27",
            score=4.2,
            snippet="Trump supported Orban.",
        )

        with patch.object(composer, "_load_all_cards", return_value=[]):
            with patch.object(composer, "search_card_index", return_value=[fts_match]) as fts_mock:
                with patch.object(composer.shadow_search, "search", return_value=[]) as shadow_mock:
                    package = asyncio.run(composer.search(None, "Trump Orban", mode="cards"))

        fts_mock.assert_called_once()
        shadow_mock.assert_called_once()
        self.assertEqual(package.llm_answer, "Cards-only search: LightRAG/LLM query was not run.")
        self.assertEqual(len(package.primary_results), 1)
        self.assertEqual(package.primary_results[0].url, "https://t.me/c/1/10")
        self.assertIn("FTS Match", package.primary_results[0].relevance_reason)
        self.assertIn("Trump supported Orban", package.primary_results[0].snippets[0])

    def test_cards_only_keeps_shadow_results_when_fts_empty(self):
        shadow_match = ShadowMatch(
            source_path="output/normalized/Hungary/10.txt",
            card_path="output/enriched/Hungary/10.enriched.json",
            score=2.0,
            snippet="Shadow match for Orban.",
            title="Hungary - 2026-05-27",
        )

        with patch.object(composer, "_load_all_cards", return_value=[]):
            with patch.object(composer, "search_card_index", return_value=[]):
                with patch.object(composer.shadow_search, "search", return_value=[shadow_match]) as shadow_mock:
                    package = asyncio.run(composer.search(None, "Trump Orban", mode="cards"))

        shadow_mock.assert_called_once()
        self.assertEqual(len(package.primary_results), 1)
        self.assertIn("Shadow Match", package.primary_results[0].relevance_reason)
        self.assertIn("Shadow match", package.primary_results[0].snippets[0])

    def test_card_hits_union_dedupes_same_card_and_keeps_unique_shadow(self):
        fts_match = CardFtsMatch(
            source_id="telegram:1:10",
            card_path="output/enriched/Hungary/10.enriched.json",
            normalized_file="output/normalized/Hungary/10.txt",
            post_url="https://t.me/c/1/10",
            title="FTS title",
            score=4.2,
            snippet="FTS snippet",
        )
        duplicate_shadow = ShadowMatch(
            source_path="output/normalized/Hungary/10.txt",
            card_path="output/enriched/Hungary/10.enriched.json",
            score=9.0,
            snippet="duplicate shadow",
            title="Shadow duplicate",
        )
        unique_shadow = ShadowMatch(
            source_path="output/normalized/Hungary/11.txt",
            card_path="output/enriched/Hungary/11.enriched.json",
            score=2.0,
            snippet="unique shadow",
            title="Shadow unique",
        )

        with patch.object(composer, "search_card_index", return_value=[fts_match]):
            with patch.object(
                composer.shadow_search,
                "search",
                return_value=[duplicate_shadow, unique_shadow],
            ):
                hits = composer._search_card_hits("Orban", top_k=10)

        self.assertEqual([hit.backend for hit in hits], ["fts", "shadow"])
        self.assertEqual(hits[0].snippet, "FTS snippet")
        self.assertEqual(hits[1].source_path, "output/normalized/Hungary/11.txt")

    def test_card_hits_applies_stable_top_k_after_union(self):
        fts_matches = [
            CardFtsMatch(str(i), f"card-{i}", f"source-{i}", "", str(i), 1.0, str(i))
            for i in range(2)
        ]
        shadow_matches = [
            ShadowMatch(f"shadow-{i}", f"shadow-card-{i}", 1.0, str(i), str(i))
            for i in range(2)
        ]
        with patch.object(composer, "search_card_index", return_value=fts_matches):
            with patch.object(composer.shadow_search, "search", return_value=shadow_matches):
                hits = composer._search_card_hits("query", top_k=3)

        self.assertEqual(
            [(hit.backend, hit.source_path) for hit in hits],
            [("fts", "source-0"), ("fts", "source-1"), ("shadow", "shadow-0")],
        )

    def test_recall_uses_fts_without_extra_lightrag_calls(self):
        fts_match = CardFtsMatch(
            source_id="telegram:1:10",
            card_path="output/enriched/Hungary/10.enriched.json",
            normalized_file="output/normalized/Hungary/10.txt",
            post_url="https://t.me/c/1/10",
            title="Hungary - 2026-05-27",
            score=4.2,
            snippet="Trump supported Orban.",
        )
        query_mock = AsyncMock(return_value={"response": "Graph answer."})

        with patch.object(composer.config, "RERANKER_ENABLED", False):
            with patch.object(composer, "_load_all_cards", return_value=[]):
                with patch.object(composer, "search_card_index", return_value=[fts_match]):
                    with patch.object(composer.shadow_search, "search", return_value=[]):
                        with patch.object(composer, "query_rag_result", query_mock):
                            package = asyncio.run(composer.search(object(), "Trump Orban", mode="recall"))

        query_mock.assert_awaited_once()
        self.assertEqual(package.llm_answer, "Graph answer.")
        self.assertEqual(len(package.primary_results), 1)
        self.assertIn("FTS Match", package.primary_results[0].relevance_reason)

    def test_cards_only_enriches_card_result_from_source_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            enriched_dir = root / "output" / "enriched" / "Hungary"
            normalized_dir = root / "output" / "normalized" / "Hungary"
            enriched_dir.mkdir(parents=True)
            normalized_dir.mkdir(parents=True)

            source_path = normalized_dir / "10.txt"
            source_path.write_text("Trump supported Orban.", encoding="utf-8")
            card_path = enriched_dir / "10.enriched.json"
            card = {
                "schema_version": "enriched_v2",
                "summary": "Trump supported Orban.",
                "content_type": "news",
                "language": "ru",
                "provenance": {
                    "source_id": "telegram:1:10",
                    "channel": "Hungary",
                    "message_id": 10,
                    "date": "2026-05-27T00:00:00+00:00",
                    "post_url": "https://t.me/c/1/10",
                    "normalized_path": str(source_path.relative_to(root)),
                },
                "source_chain": {
                    "external_links": [
                        {
                            "url": "https://www.youtube.com/watch?v=abc",
                            "label": "YouTube video",
                        }
                    ]
                },
            }
            card_path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
            registry_db = root / "state" / "source_registry.sqlite"
            rebuild_source_registry(
                normalized_dir=root / "output" / "normalized",
                enriched_dir=root / "output" / "enriched",
                db_path=registry_db,
            )

            card["_path"] = str(card_path)
            fts_match = CardFtsMatch(
                source_id="telegram:1:10",
                card_path=str(card_path),
                normalized_file=str(source_path.relative_to(root)),
                post_url="https://t.me/c/1/10",
                title="Hungary - 2026-05-27",
                score=4.2,
                snippet="Trump supported Orban.",
            )

            with patch.object(composer.config, "SOURCE_REGISTRY_DB_PATH", registry_db):
                with patch.object(composer, "_load_all_cards", return_value=[card]):
                    with patch.object(composer, "search_card_index", return_value=[fts_match]):
                        package = asyncio.run(composer.search(None, "Trump Orban", mode="cards"))

            result = package.primary_results[0]
            self.assertEqual(result.source_id, "telegram:1:10")
            self.assertEqual(result.url, "https://www.youtube.com/watch?v=abc")
            self.assertEqual(result.primary_url, "https://www.youtube.com/watch?v=abc")
            self.assertEqual(result.source_path, str(source_path.relative_to(root)))


if __name__ == "__main__":
    unittest.main()
