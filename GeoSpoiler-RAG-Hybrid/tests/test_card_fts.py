import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from retrieval import card_fts
from retrieval.card_fts import rebuild_card_index, search_card_index


def _fts5_available() -> bool:
    try:
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False


@unittest.skipUnless(_fts5_available(), "SQLite FTS5 is not available")
class CardFtsTests(unittest.TestCase):
    def test_rebuild_indexes_keep_cards_and_searches_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            enriched_dir = root / "enriched" / "Hungary"
            enriched_dir.mkdir(parents=True)
            db_path = root / "card_fts.sqlite"

            self._write_card(
                enriched_dir / "10.enriched.json",
                {
                    "provenance": {
                        "source_id": "telegram:1:10",
                        "channel": "Hungary",
                        "message_id": 10,
                        "date": "2026-05-27T00:00:00+00:00",
                        "post_url": "https://t.me/c/1/10",
                        "normalized_path": "output/normalized/Hungary/10.txt",
                    },
                    "search_text": "Trump supported Orban before the Hungarian election.",
                    "entities": {"people": ["Trump", "Viktor Orban"], "countries": ["Hungary"]},
                    "topics": ["elections", "support"],
                    "key_points": [{"text": "Trump supported Orban.", "type": "reported_statement", "importance": "high", "evidence": None}],
                },
            )
            self._write_card(
                enriched_dir / "11.enriched.json",
                {
                    "provenance": {
                        "source_id": "telegram:1:11",
                        "channel": "Hungary",
                        "message_id": 11,
                        "normalized_path": "output/normalized/Hungary/11.txt",
                    },
                    "summary": "",
                    "key_points": [],
                    "search_text": "",
                },
            )
            (enriched_dir / "legacy.enriched.json").write_text(
                json.dumps(
                    {
                        "schema_version": "enriched_v1",
                        "search_text": "Orban support legacy card",
                        "provenance": {"source_id": "legacy:1"},
                    }
                ),
                encoding="utf-8",
            )

            stats = rebuild_card_index(enriched_dir=enriched_dir, db_path=db_path)
            matches = search_card_index("Orban support", top_k=5, db_path=db_path)
            self.assertEqual(stats.cards_seen, 3)
            self.assertEqual(stats.cards_indexed, 1)
            self.assertEqual(stats.cards_skipped, 2)
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].source_id, "telegram:1:10")
            self.assertEqual(matches[0].post_url, "https://t.me/c/1/10")
            self.assertIn("Orban", matches[0].snippet)

    def test_search_missing_index_or_empty_query_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "missing.sqlite"

            self.assertEqual(search_card_index("Orban", db_path=db_path), [])
            self.assertEqual(search_card_index("и в на", db_path=db_path), [])

    def test_search_top_k_none_returns_all_matches_without_limit_100(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            enriched_dir = root / "enriched"
            enriched_dir.mkdir()
            db_path = root / "card_fts.sqlite"
            for index in range(125):
                self._write_card(
                    enriched_dir / f"{index}.enriched.json",
                    {
                        "provenance": {
                            "source_id": f"telegram:1:{index}",
                            "channel": "Cuba",
                            "message_id": index,
                            "normalized_path": f"normalized/{index}.txt",
                        },
                        "search_text": f"Cuba policy report number {index}",
                    },
                )

            rebuild_card_index(enriched_dir=enriched_dir, db_path=db_path)
            limited = search_card_index("Cuba policy", top_k=10, db_path=db_path)
            all_matches = search_card_index("Cuba policy", top_k=None, db_path=db_path)

        self.assertEqual(len(limited), 10)
        self.assertEqual(len(all_matches), 125)

    def test_equal_coverage_and_rank_use_stable_identity_tiebreakers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            enriched_dir = root / "enriched"
            enriched_dir.mkdir()
            db_path = root / "card_fts.sqlite"
            for filename, source_id in (
                ("z.enriched.json", "telegram:1:2"),
                ("a.enriched.json", "telegram:1:1"),
            ):
                self._write_card(
                    enriched_dir / filename,
                    {
                        "provenance": {
                            "source_id": source_id,
                            "channel": "Test",
                            "normalized_path": f"normalized/{source_id.rsplit(':', 1)[-1]}.txt",
                        },
                        "search_text": "identical stable ranking phrase",
                    },
                )

            rebuild_card_index(enriched_dir=enriched_dir, db_path=db_path)
            first = search_card_index("stable ranking", top_k=None, db_path=db_path)
            second = search_card_index("stable ranking", top_k=None, db_path=db_path)

        expected = ["telegram:1:1", "telegram:1:2"]
        self.assertEqual([match.source_id for match in first], expected)
        self.assertEqual([match.source_id for match in second], expected)

    def test_query_path_is_read_only_and_does_not_create_missing_schema(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            enriched_dir = root / "enriched"
            enriched_dir.mkdir()
            db_path = root / "card_fts.sqlite"
            self._write_card(
                enriched_dir / "one.enriched.json",
                {
                    "provenance": {"source_id": "telegram:1:1", "normalized_path": "n/1.txt"},
                    "search_text": "read only query marker",
                },
            )
            rebuild_card_index(enriched_dir=enriched_dir, db_path=db_path)
            before = (db_path.stat().st_mtime_ns, db_path.read_bytes())
            self.assertEqual(len(search_card_index("read only", db_path=db_path)), 1)
            with self.assertRaises(sqlite3.OperationalError):
                card_fts.list_youtube_segment_ids("telegram:1:1", db_path=db_path)
            after = (db_path.stat().st_mtime_ns, db_path.read_bytes())
            self.assertEqual(after, before)

            empty_db = root / "empty.sqlite"
            sqlite3.connect(empty_db).close()
            with self.assertRaises(sqlite3.OperationalError):
                search_card_index("read only", db_path=empty_db)
            with self.assertRaises(sqlite3.OperationalError):
                card_fts.search_youtube_segments("read only", db_path=empty_db)
            with self.assertRaises(sqlite3.OperationalError):
                card_fts.list_youtube_segment_ids("telegram:1:1", db_path=empty_db)
            with closing(sqlite3.connect(empty_db)) as conn:
                tables = conn.execute("SELECT name FROM sqlite_master").fetchall()
            self.assertEqual(tables, [])

    def test_rebuild_uses_normalized_text_for_thin_similarity_cards(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            enriched_dir = root / "output" / "enriched" / "Ultra"
            normalized_dir = root / "output" / "normalized" / "Ultra"
            enriched_dir.mkdir(parents=True)
            normalized_dir.mkdir(parents=True)
            db_path = root / "card_fts.sqlite"

            canonical_source = normalized_dir / "11.txt"
            canonical_source.write_text("Ультра-левые и ультра-правые совпадают.", encoding="utf-8")
            broad_source = normalized_dir / "20.txt"
            broad_source.write_text(
                "Урсула фон дер Ляйен вызывает ненависть у ультралевых и ультраправых сил.",
                encoding="utf-8",
            )
            self._write_card(
                enriched_dir / "11.enriched.json",
                {
                    "provenance": {
                        "source_id": "telegram:3299898370:11",
                        "channel": "Ultra",
                        "message_id": 11,
                        "post_url": "https://t.me/c/3299898370/11",
                        "normalized_path": str(canonical_source.relative_to(root)),
                    },
                    "summary": "",
                    "key_points": [],
                    "quotes": [],
                    "theses": [],
                    "events": [],
                    "search_text": "Источник: Ultra",
                    "graph_text": "Источник: Ultra",
                },
            )
            self._write_card(
                enriched_dir / "20.enriched.json",
                {
                    "provenance": {
                        "source_id": "telegram:3299898370:20",
                        "channel": "Ultra",
                        "message_id": 20,
                        "post_url": "https://t.me/c/3299898370/20",
                        "normalized_path": str(broad_source.relative_to(root)),
                    },
                    "summary": "Материал про совпадение идеологии ультраправых групп с джихадистами.",
                    "key_points": [{"text": "Ультраправые группы совпадают с джихадистами по отдельным установкам.", "type": "reported_statement", "importance": "high", "evidence": None}],
                    "search_text": (
                        "[Канал: Ультра левые и ультра правые]\n\n"
                        "Ультраправые группы совпадают с джихадистами по отдельным установкам."
                    ),
                },
            )

            with patch.object(card_fts.config, "PROJECT_ROOT", root):
                rebuild_card_index(enriched_dir=enriched_dir, db_path=db_path)
                matches = search_card_index(
                    "сходство ультралевых ультраправых",
                    top_k=5,
                    db_path=db_path,
                )

            self.assertGreaterEqual(len(matches), 1)
            self.assertEqual(matches[0].source_id, "telegram:3299898370:11")
            self.assertIn("совпадают", matches[0].snippet)

    def _write_card(self, path: Path, data: dict) -> None:
        payload = dict(data)
        payload.setdefault("schema_version", "enriched_v2")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
