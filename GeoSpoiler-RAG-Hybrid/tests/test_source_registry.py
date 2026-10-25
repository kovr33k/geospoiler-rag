import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from retrieval.source_registry import rebuild_source_registry, resolve_source


class SourceRegistryTests(unittest.TestCase):
    def test_rebuild_registry_resolves_source_paths_and_urls(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            normalized_dir = root / "normalized" / "Hungary"
            enriched_dir = root / "enriched" / "Hungary"
            normalized_dir.mkdir(parents=True)
            enriched_dir.mkdir(parents=True)
            db_path = root / "source_registry.sqlite"

            (normalized_dir / "10.txt").write_text("Trump supported Orban.", encoding="utf-8")
            (normalized_dir / "10.meta.json").write_text(
                json.dumps(
                    {
                        "channel_name": "Hungary",
                        "channel_id": 1,
                        "channel_username": "hungary",
                        "message_id": 10,
                        "date": "2026-05-27T00:00:00+00:00",
                        "post_url": "https://t.me/c/1/10",
                        "has_text": True,
                        "has_images": False,
                        "youtube_urls": ["https://youtu.be/example"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (enriched_dir / "10.enriched.json").write_text(
                json.dumps(
                    {
                        "schema_version": "enriched_v2",
                        "content_type": "telegram_post",
                        "language": "ru",
                        "summary": "Trump supported Orban.",
                        "provenance": {
                            "source_id": "telegram:1:10",
                            "channel": "Hungary",
                            "message_id": 10,
                            "date": "2026-05-27T00:00:00+00:00",
                            "post_url": "https://t.me/c/1/10",
                            "normalized_path": "output/normalized/Hungary/10.txt",
                        },
                        "source_chain": {
                            "original_source": "Telegram",
                            "external_links": [
                                {
                                    "url": "https://www.youtube.com/watch?v=abc",
                                    "label": "YouTube video",
                                },
                                {
                                    "url": "https://example.com/source",
                                    "label": "External source",
                                },
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (enriched_dir / "legacy.enriched.json").write_text(
                json.dumps(
                    {
                        "schema_version": "enriched_v1",
                        "provenance": {
                            "source_id": "legacy:1",
                            "channel": "Legacy",
                            "normalized_path": "legacy.txt",
                        },
                    }
                ),
                encoding="utf-8",
            )

            stats = rebuild_source_registry(
                normalized_dir=root / "normalized",
                enriched_dir=root / "enriched",
                db_path=db_path,
            )
            passport = resolve_source("telegram:1:10", db_path=db_path)
            legacy = resolve_source("legacy:1", db_path=db_path)

            self.assertEqual(stats.sources, 1)
            self.assertEqual(stats.normalized_docs, 1)
            self.assertEqual(stats.enriched_cards, 1)
            self.assertEqual(stats.references, 3)
            self.assertIsNotNone(passport)
            self.assertIsNone(legacy)
            self.assertEqual(passport.post_url, "https://t.me/c/1/10")
            self.assertEqual(passport.primary_url, "https://www.youtube.com/watch?v=abc")
            self.assertEqual(passport.normalized_file, "output/normalized/Hungary/10.txt")
            self.assertIn("10.enriched.json", passport.card_path)

            with closing(sqlite3.connect(db_path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                reference_count = conn.execute('SELECT COUNT(*) FROM "references"').fetchone()[0]

            self.assertTrue({"sources", "normalized_docs", "enriched_cards", "processing_runs", "references"} <= tables)
            self.assertEqual(reference_count, 3)

    def test_resolve_missing_source_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertIsNone(resolve_source("telegram:missing:1", db_path=Path(tmp_dir) / "missing.sqlite"))


if __name__ == "__main__":
    unittest.main()
