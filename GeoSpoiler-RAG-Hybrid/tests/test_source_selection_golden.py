import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import source_selection_golden as source_golden  # noqa: E402


class SourceSelectionGoldenUnitTests(unittest.TestCase):
    def test_score_source_selection_accepts_canonical_source_within_rank(self):
        case = {
            "answer_must": ["куб", "протест"],
            "source_any": ["3841808641/5", "Куба\\5.txt"],
            "max_rank": 1,
        }
        sources = [
            {
                "post_url": "https://t.me/c/3841808641/5",
                "file_path": "C:/repo/output/normalized/Куба/5.txt",
            }
        ]

        score = source_golden._score_source_selection("Куба: протесты и электричество.", sources, case)

        self.assertTrue(score["passed"])
        self.assertEqual(score["source_rank"], 1)
        self.assertTrue(score["rank_ok"])

    def test_score_source_selection_flags_late_canonical_source(self):
        case = {
            "answer_must": ["нарв"],
            "source_any": ["3889026624/2"],
            "max_rank": 1,
        }
        sources = [
            {"post_url": "https://t.me/c/3889026624/9", "file_path": "Балтийские страны/9.txt"},
            {"post_url": "https://t.me/c/3889026624/2", "file_path": "Балтийские страны/2.txt"},
        ]

        score = source_golden._score_source_selection("Нарва и Эстония.", sources, case)

        self.assertFalse(score["passed"])
        self.assertEqual(score["source_rank"], 2)
        self.assertFalse(score["rank_ok"])

    def test_score_source_selection_flags_forbidden_top_source(self):
        case = {
            "answer_must": ["нарв"],
            "source_any": ["3889026624/2"],
            "max_rank": 2,
            "forbidden_top_any": ["3889026624/9"],
            "forbidden_top_n": 1,
        }
        sources = [
            {"post_url": "https://t.me/c/3889026624/9", "file_path": "Балтийские страны/9.txt"},
            {"post_url": "https://t.me/c/3889026624/2", "file_path": "Балтийские страны/2.txt"},
        ]

        score = source_golden._score_source_selection("Нарва и Эстония.", sources, case)

        self.assertFalse(score["passed"])
        self.assertEqual(score["forbidden_top_hits"], ["3889026624/9"])

    def test_source_blob_matches_slash_and_backslash_variants(self):
        source = {"post_url": "", "file_path": "C:/repo/output/normalized/Куба/5.txt"}
        blob = source_golden._source_blob(source)

        self.assertTrue(source_golden._term_matches_source_blob("Куба\\5.txt", blob))
        self.assertTrue(source_golden._term_matches_source_blob("Куба/5.txt", blob))

    def test_source_cases_can_be_selected_by_ids(self):
        with patch.dict(os.environ, {"SOURCE_GOLDEN_CASE_IDS": "q9_cuba_protests_source,north_korea_troops_source"}):
            cases = source_golden._source_cases_for_run()

        self.assertEqual([case["id"] for case in cases], ["q9_cuba_protests_source", "north_korea_troops_source"])

    def test_source_case_limit_can_bound_live_runs(self):
        with patch.dict(os.environ, {"SOURCE_GOLDEN_CASE_LIMIT": "2"}):
            cases = source_golden._source_cases_for_run()

        self.assertEqual(len(cases), 2)


if __name__ == "__main__":
    unittest.main()
