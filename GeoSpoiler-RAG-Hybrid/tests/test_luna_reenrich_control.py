import unittest

from scripts import luna_reenrich_control as control


class LunaReenrichControlTests(unittest.TestCase):
    def _identity(self, counts):
        return {
            "input": {"counts": counts},
            "runtime": {
                "active_enrichment_model": control.EXPECTED_MODEL,
                "CODEX_FALLBACK_TO_API": False,
                "WIKI_ENABLED": False,
                "RERANKER_ENABLED": False,
            },
        }

    def test_preflight_accepts_current_dynamic_corpus(self):
        control._assert_preflight(
            self._identity(
                {
                    "normalized_documents": 129,
                    "generic_jobs": 127,
                    "youtube_only_documents": 2,
                    "youtube_sources": 4,
                    "unique_source_ids": 129,
                    "unique_video_ids": 4,
                }
            )
        )

    def test_preflight_rejects_accounting_drift(self):
        with self.assertRaisesRegex(RuntimeError, "accounting mismatch"):
            control._assert_preflight(
                self._identity(
                    {
                        "normalized_documents": 129,
                        "generic_jobs": 126,
                        "youtube_only_documents": 2,
                        "youtube_sources": 4,
                        "unique_source_ids": 129,
                        "unique_video_ids": 4,
                    }
                )
            )

    def test_preflight_rejects_non_four_youtube_sources(self):
        with self.assertRaisesRegex(RuntimeError, "YouTube input identity"):
            control._assert_preflight(
                self._identity(
                    {
                        "normalized_documents": 129,
                        "generic_jobs": 127,
                        "youtube_only_documents": 2,
                        "youtube_sources": 3,
                        "unique_source_ids": 129,
                        "unique_video_ids": 3,
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
