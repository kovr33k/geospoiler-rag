import tempfile
import unittest
from pathlib import Path

import golden_compare


class GoldenCompareTests(unittest.TestCase):
    def test_compare_summaries_flags_regressions_and_focus_cases(self):
        baseline = {
            "checked_at": "2026-05-27T02:03:18+00:00",
            "query_model": "baseline-model",
            "mode": "hybrid",
            "passed": 2,
            "total": 2,
            "average_score": 100,
            "config_flags": {"RERANKER_ENABLED": False},
            "cases": [
                {
                    "question": "Откуда тезис? Дай ссылку.",
                    "score": 100,
                    "pass": True,
                    "source_ok": True,
                    "source_any_ok": True,
                },
                {
                    "question": "Обычный вопрос",
                    "score": 90,
                    "pass": True,
                    "source_ok": True,
                    "source_any_ok": True,
                },
            ],
        }
        candidate = {
            "checked_at": "2026-05-27T03:00:00+00:00",
            "query_model": "candidate-model",
            "mode": "hybrid",
            "passed": 1,
            "total": 2,
            "average_score": 80,
            "config_flags": {"RERANKER_ENABLED": True},
            "cases": [
                {
                    "question": "Откуда тезис? Дай ссылку.",
                    "score": 70,
                    "pass": False,
                    "source_ok": True,
                    "source_any_ok": False,
                },
                {
                    "question": "Обычный вопрос",
                    "score": 90,
                    "pass": True,
                    "source_ok": True,
                    "source_any_ok": True,
                },
            ],
        }

        comparison = golden_compare.compare_summaries(baseline, candidate)

        self.assertEqual(comparison["regressions"], 1)
        self.assertEqual(comparison["focus_cases"], 1)
        self.assertEqual(comparison["focus_regressions"], 1)
        self.assertEqual(comparison["average_delta"], -15.0)

    def test_write_markdown_report_contains_per_case_table(self):
        comparison = golden_compare.compare_summaries(
            {
                "cases": [
                    {
                        "question": "Q|1",
                        "score": 100,
                        "pass": True,
                        "source_ok": True,
                        "source_any_ok": True,
                    }
                ]
            },
            {
                "cases": [
                    {
                        "question": "Q|1",
                        "score": 100,
                        "pass": True,
                        "source_ok": True,
                        "source_any_ok": True,
                    }
                ]
            },
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "comparison.md"

            golden_compare.write_markdown_report(comparison, output)

            text = output.read_text(encoding="utf-8")
            self.assertIn("# Golden Set Comparison", text)
            self.assertIn("Q\\|1", text)


if __name__ == "__main__":
    unittest.main()
