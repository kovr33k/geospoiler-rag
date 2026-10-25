import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from loader import ingest
from retrieval import card_fts, card_text, composer, shadow_search
from retrieval.source_registry import rebuild_source_registry, resolve_source


class EnrichedV2ProvenanceTests(unittest.TestCase):
    def test_card_text_reads_only_normalized_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.txt"
            source.write_text("normalized source", encoding="utf-8")

            with patch.object(card_text.config, "PROJECT_ROOT", root):
                legacy = card_text._read_normalized_text(
                    {"provenance": {"normalized_file": "source.txt"}}
                )
                current = card_text._read_normalized_text(
                    {"provenance": {"normalized_path": "source.txt"}}
                )

        self.assertEqual(legacy, "")
        self.assertEqual(current, "normalized source")

    def test_fts_record_ignores_legacy_provenance_names(self):
        card_path = Path("card.enriched.json")
        legacy = card_fts.card_to_fts_record(
            {
                "schema_version": "enriched_v2",
                "provenance": {
                    "channel_name": "Legacy",
                    "channel_id": 1,
                    "message_id": 2,
                    "normalized_file": "legacy.txt",
                },
                "search_text": "searchable text",
            },
            card_path,
        )
        current = card_fts.card_to_fts_record(
            {
                "schema_version": "enriched_v2",
                "provenance": {
                    "source_id": "telegram:1:2",
                    "channel": "Current",
                    "normalized_path": "current.txt",
                },
                "search_text": "searchable text",
            },
            card_path,
        )

        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.source_id, "")
        self.assertEqual(legacy.normalized_file, "")
        self.assertTrue(legacy.title.startswith("? -"))
        self.assertEqual(current.source_id, "telegram:1:2")
        self.assertEqual(current.normalized_file, "current.txt")
        self.assertTrue(current.title.startswith("Current -"))

    def test_composer_and_shadow_ignore_legacy_names(self):
        legacy_card = {
            "schema_version": "enriched_v2",
            "provenance": {
                "source_id": "legacy:1",
                "channel_name": "Legacy",
                "normalized_file": "legacy.txt",
            },
            "search_text": "distinctive searchable phrase",
            "_path": "legacy.enriched.json",
        }
        with patch.object(composer, "_resolve_source_passport", return_value=None):
            result = composer._card_to_result(legacy_card, "test")

        self.assertEqual(result.source_path, "")
        self.assertTrue(result.title.startswith("? -"))

        with tempfile.TemporaryDirectory() as tmpdir:
            enriched_dir = Path(tmpdir) / "Channel"
            enriched_dir.mkdir(parents=True)
            (enriched_dir / "1.enriched.json").write_text(
                json.dumps(legacy_card), encoding="utf-8"
            )
            with patch.object(shadow_search.config, "ENRICHED_DIR", Path(tmpdir)):
                matches = shadow_search.search("distinctive phrase")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].source_path, "")
        self.assertTrue(matches[0].title.startswith("? -"))

    def test_shadow_equal_scores_use_stable_path_tiebreakers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            channel_dir = root / "Channel"
            channel_dir.mkdir(parents=True)
            for filename, normalized_path in (
                ("z.enriched.json", "normalized/z.txt"),
                ("a.enriched.json", "normalized/a.txt"),
            ):
                (channel_dir / filename).write_text(
                    json.dumps(
                        {
                            "schema_version": "enriched_v2",
                            "provenance": {
                                "source_id": f"source:{filename}",
                                "channel": "Channel",
                                "normalized_path": normalized_path,
                            },
                            "search_text": "identical stable shadow phrase",
                        }
                    ),
                    encoding="utf-8",
                )

            with patch.object(shadow_search.config, "ENRICHED_DIR", root):
                first = shadow_search.search("stable shadow", top_k=None)
                second = shadow_search.search("stable shadow", top_k=None)

        expected = ["normalized/a.txt", "normalized/z.txt"]
        self.assertEqual([match.source_path for match in first], expected)
        self.assertEqual([match.source_path for match in second], expected)

    def test_source_registry_maps_only_v2_enriched_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized_dir = root / "normalized"
            enriched_dir = root / "enriched" / "Channel"
            normalized_dir.mkdir()
            enriched_dir.mkdir(parents=True)
            db_path = root / "source_registry.sqlite"

            cards = {
                "1.enriched.json": {
                    "schema_version": "enriched_v2",
                    "provenance": {
                        "source_id": "current:1",
                        "channel": "Current",
                        "message_id": 1,
                        "normalized_path": "current.txt",
                    },
                    "summary": "current",
                },
                "2.enriched.json": {
                    "schema_version": "enriched_v2",
                    "provenance": {
                        "source_id": "legacy:2",
                        "channel_name": "Legacy",
                        "message_id": 2,
                        "normalized_file": "legacy.txt",
                    },
                    "summary": "legacy",
                },
            }
            for name, payload in cards.items():
                (enriched_dir / name).write_text(json.dumps(payload), encoding="utf-8")

            rebuild_source_registry(normalized_dir, root / "enriched", db_path)
            current = resolve_source("current:1", db_path)
            legacy = resolve_source("legacy:2", db_path)

        self.assertIsNotNone(current)
        self.assertEqual(current.channel_name, "Current")
        self.assertEqual(current.normalized_file, "current.txt")
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.channel_name, "")
        self.assertEqual(legacy.normalized_file, "")

    def test_lightrag_loader_skips_legacy_path_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            enriched_dir = Path(tmpdir)
            (enriched_dir / "1.enriched.json").write_text(
                json.dumps(
                    {
                        "schema_version": "enriched_v2",
                        "graph_text": "legacy graph",
                        "provenance": {"normalized_file": "legacy.txt"},
                    }
                ),
                encoding="utf-8",
            )
            (enriched_dir / "2.enriched.json").write_text(
                json.dumps(
                    {
                        "schema_version": "enriched_v2",
                        "graph_text": "current graph",
                        "provenance": {"normalized_path": "current.txt"},
                    }
                ),
                encoding="utf-8",
            )
            load_texts = AsyncMock(return_value=1)
            rag = object()
            with patch.object(ingest, "load_texts", load_texts):
                inserted = asyncio.run(ingest.load_from_enriched(rag, enriched_dir))

        self.assertEqual(inserted, 1)
        load_texts.assert_awaited_once_with(rag, [("current.txt", "current graph")])

    def test_lightrag_loader_never_falls_back_to_normalized_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized = root / "normalized.txt"
            normalized.write_text("raw normalized text", encoding="utf-8")
            (root / "card.enriched.json").write_text(
                json.dumps(
                    {
                        "schema_version": "enriched_v2",
                        "graph_text": "",
                        "provenance": {"normalized_path": str(normalized)},
                    }
                ),
                encoding="utf-8",
            )
            load_texts = AsyncMock(return_value=0)
            with patch.object(ingest, "load_texts", load_texts):
                inserted = asyncio.run(ingest.load_from_enriched(object(), root))

        self.assertEqual(inserted, 0)
        load_texts.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
