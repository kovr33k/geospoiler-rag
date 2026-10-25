import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import baseline_probe  # noqa: E402


class BaselineProbeTests(unittest.TestCase):
    def test_baseline_metadata_records_models_without_api_keys(self):
        metadata = baseline_probe.collect_baseline_metadata()
        blob = json.dumps(metadata, ensure_ascii=False)

        self.assertIn("query", metadata)
        self.assertIn("candidate_models_to_check", metadata)
        self.assertIn("recommended_flags", metadata)
        self.assertNotIn("API_KEY", blob)
        self.assertNotIn("api_key", blob)

    def test_baseline_probe_writes_report_with_mock_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts_dir = Path(tmpdir) / "artifacts"
            query_mock = AsyncMock(
                return_value={
                    "response": "Stable answer about the corpus.",
                    "llm_response": {"content": "Stable answer about the corpus."},
                    "data": {"references": [{"file_path": "output/normalized/test.txt"}]},
                }
            )

            with patch.object(baseline_probe, "create_rag", AsyncMock(return_value=_DummyRag())):
                with patch.object(baseline_probe, "query_rag_result", query_mock):
                    report = asyncio.run(baseline_probe.run_baseline_probe(limit=1, mode="hybrid"))
            metadata_path, report_path = baseline_probe.write_probe_report(report, artifacts_dir=artifacts_dir)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            rendered = report_path.read_text(encoding="utf-8")

        query_mock.assert_awaited_once()
        self.assertEqual(report.stable_count, 1)
        self.assertIn("probe", metadata)
        self.assertIn("# Baseline Model Probe", rendered)
        self.assertIn("Stable cases: 1/1", rendered)

    def test_baseline_probe_cache_buster_changes_query_not_report_question(self):
        query_mock = AsyncMock(
            return_value={
                "response": "Stable answer about the corpus.",
                "llm_response": {"content": "Stable answer about the corpus."},
                "data": {"references": []},
            }
        )

        with patch.dict("os.environ", {"BASELINE_PROBE_CACHE_BUSTER": "fresh-run"}):
            with patch.object(baseline_probe, "create_rag", AsyncMock(return_value=_DummyRag())):
                with patch.object(baseline_probe, "query_rag_result", query_mock):
                    report = asyncio.run(baseline_probe.run_baseline_probe(limit=1, mode="hybrid"))

        sent_question = query_mock.await_args.args[1]
        self.assertIn("\u2063fresh-run:1", sent_question)
        self.assertEqual(report.results[0].question, baseline_probe.BASELINE_QUERY_CASES[0]["question"])
        self.assertIn("Cache buster: enabled", baseline_probe.format_probe_report(report))
        self.assertTrue(baseline_probe._report_to_dict(report)["cache_buster"])

    def test_baseline_probe_records_create_rag_failure_without_crashing(self):
        with patch.object(baseline_probe, "create_rag", AsyncMock(side_effect=RuntimeError("network blocked"))):
            report = asyncio.run(baseline_probe.run_baseline_probe(limit=2, mode="hybrid"))

        self.assertEqual(len(report.results), 2)
        self.assertEqual(report.stable_count, 0)
        self.assertEqual({result.status for result in report.results}, {"error"})
        self.assertIn("network blocked", report.results[0].error)

    def test_baseline_probe_flags_diff_when_current_config_not_recommended(self):
        report = baseline_probe.BaselineProbeReport(
            checked_at="2026-05-27T00:00:00+00:00",
            mode="mix",
            query_model="model",
            query_base_url="https://example.test/v1",
            query_profile_default="answer",
            config_flags={
                "RERANKER_ENABLED": True,
                "HYBRID_SYNTH_ENABLED": True,
                "HYBRID_QUERY_CARDS_ENABLED": True,
            },
            recommended_flags=baseline_probe.RECOMMENDED_FLAGS,
            results=[],
        )

        rendered = baseline_probe.format_probe_report(report)

        self.assertIn("RERANKER_ENABLED: True (recommended for E1: False) [DIFF]", rendered)
        self.assertIn("HYBRID_SYNTH_ENABLED: True (recommended for E1: False) [DIFF]", rendered)

    def test_baseline_probe_detects_mixed_language_garbage(self):
        self.assertTrue(baseline_probe._answer_looks_corrupt("Valid start benten picker all-France garbage"))
        self.assertTrue(baseline_probe._answer_looks_corrupt("Ответ с внезапным 中文 мусором"))


    def test_baseline_probe_cases_are_readable_utf8_questions(self):
        questions = [case["question"] for case in baseline_probe.BASELINE_QUERY_CASES]

        self.assertIn("Что в базе говорится", questions[0])
        self.assertFalse(any("Ð" in question or "Ñ" in question for question in questions))


class _DummyRag:
    async def finalize_storages(self):
        return None


if __name__ == "__main__":
    unittest.main()
