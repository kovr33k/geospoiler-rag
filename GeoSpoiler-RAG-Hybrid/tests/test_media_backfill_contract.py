import json
import tempfile
import unittest
from pathlib import Path

from enricher import pipeline as enricher_pipeline
from normalizer.media_paths import portable_media_path
from normalizer.transcription_backfill import _iter_candidates


class MediaBackfillContractTests(unittest.TestCase):
    def test_portable_path_reanchors_stale_media_cache(self):
        value = portable_media_path(r"C:\WikiRag\media_cache\Channel\clip.mp4")
        self.assertEqual(value, "media_cache/Channel/clip.mp4")

    def test_transcription_candidates_fail_closed_on_malformed_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "normalized" / "Channel"
            root.mkdir(parents=True)
            (root / "1.txt").write_text("body", encoding="utf-8")
            (root / "1.meta.json").write_text(
                json.dumps({"channel_name": "Channel", "message_id": "bad", "media": []}),
                encoding="utf-8",
            )
            self.assertEqual(_iter_candidates(root.parent), [])

    def test_media_contract_version_invalidates_legacy_progress(self):
        key = "Channel/1"
        progress = {
            "enriched": {
                key: {
                    "source_fingerprint": "same",
                    "schema_version": enricher_pipeline.config.ENRICHMENT_SCHEMA_VERSION,
                    "prompt_version": enricher_pipeline.config.ENRICHMENT_PROMPT_VERSION,
                    "enrichment_model": enricher_pipeline.llm_backend.active_model_for("enrichment"),
                }
            }
        }
        self.assertTrue(enricher_pipeline._needs_enrichment(progress, key, "same"))
        progress["enriched"][key]["media_contract_version"] = enricher_pipeline._MEDIA_CONTRACT_VERSION
        self.assertFalse(enricher_pipeline._needs_enrichment(progress, key, "same"))


if __name__ == "__main__":
    unittest.main()
