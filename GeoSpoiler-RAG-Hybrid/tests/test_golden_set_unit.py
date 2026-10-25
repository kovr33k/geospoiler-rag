import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import test_golden_set  # noqa: E402


class GoldenSetUnitTests(unittest.TestCase):
    def test_golden_query_mode_matches_reranker_flag(self):
        with patch.object(test_golden_set.config, "RERANKER_ENABLED", False):
            self.assertEqual(test_golden_set._golden_query_mode(), "hybrid")

        with patch.object(test_golden_set.config, "RERANKER_ENABLED", True):
            self.assertEqual(test_golden_set._golden_query_mode(), "mix")

    def test_score_answer_requires_requested_source(self):
        case = {
            "question": "Откуда тезис? Дай ссылку.",
            "must": ["куб"],
            "source_required": True,
            "source_any": ["3841808641/8"],
        }
        answer = "В базе есть тезис про Кубу."
        sources = [{"post_url": "https://t.me/c/3841808641/8", "file_path": ""}]

        score = test_golden_set._score_answer(answer, sources, case)

        self.assertTrue(score["pass"])
        self.assertTrue(score["source_ok"])
        self.assertTrue(score["source_any_ok"])

    def test_score_answer_flags_missing_and_forbidden_terms(self):
        case = {
            "question": "Что в базе говорится о Кубе?",
            "must": ["куб", "переговор"],
            "must_not": ["нет информации"],
        }

        score = test_golden_set._score_answer("Нет информации про Кубу.", [], case)

        self.assertFalse(score["pass"])
        self.assertIn("переговор", score["missing"])
        self.assertIn("нет информации", score["forbidden"])

    def test_retryable_query_error_detection(self):
        self.assertTrue(test_golden_set._is_retryable_query_error(RuntimeError("Error code: 429")))
        self.assertTrue(test_golden_set._is_retryable_query_error(TimeoutError("timed out")))
        self.assertFalse(test_golden_set._is_retryable_query_error(ValueError("bad request")))

    def test_golden_query_delay_uses_env_with_safe_default(self):
        with patch.dict("os.environ", {"GOLDEN_QUERY_DELAY_SECONDS": "0.25"}):
            self.assertEqual(test_golden_set._golden_query_delay_seconds(), 0.25)

        with patch.dict("os.environ", {"GOLDEN_QUERY_DELAY_SECONDS": "nope"}):
            self.assertEqual(test_golden_set._golden_query_delay_seconds(), 5.0)

    def test_golden_output_paths_can_be_overridden(self):
        with patch.dict(
            "os.environ",
            {
                "GOLDEN_RESULTS_FILE": "artifacts/custom_results.md",
                "GOLDEN_SCORES_FILE": "artifacts/custom_scores.json",
            },
        ):
            self.assertEqual(test_golden_set._golden_results_file(), Path("artifacts/custom_results.md"))
            self.assertEqual(test_golden_set._golden_scores_file(), Path("artifacts/custom_scores.json"))

    def test_golden_case_limit_can_bound_live_runs(self):
        with patch.dict("os.environ", {"GOLDEN_CASE_LIMIT": "2"}):
            self.assertEqual(len(test_golden_set._golden_cases_for_run()), 2)

        with patch.dict("os.environ", {"GOLDEN_CASE_LIMIT": "0"}):
            self.assertEqual(test_golden_set._golden_cases_for_run(), test_golden_set.GOLDEN_CASES)

        with patch.dict("os.environ", {"GOLDEN_CASE_LIMIT": "bad"}):
            self.assertEqual(test_golden_set._golden_cases_for_run(), test_golden_set.GOLDEN_CASES)


if __name__ == "__main__":
    unittest.main()
