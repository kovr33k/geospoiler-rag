import json
import tempfile
import unittest
from pathlib import Path

from experiment_registry import collect_experiment_records, write_experiment_registry


class ExperimentRegistryTests(unittest.TestCase):
    def test_collects_golden_and_probe_score_summaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts_dir = Path(tmpdir)
            (artifacts_dir / "sample_golden_set_scores.json").write_text(
                json.dumps(
                    {
                        "checked_at": "2026-05-31T10:00:00+00:00",
                        "query_model": "deepseek-v4-flash",
                        "mode": "hybrid",
                        "config_flags": {
                            "RERANKER_ENABLED": False,
                            "HYBRID_SYNTH_ENABLED": True,
                        },
                        "total": 4,
                        "passed": 3,
                        "average_score": 87.5,
                    }
                ),
                encoding="utf-8",
            )
            (artifacts_dir / "llm_probe_sample_scores.json").write_text(
                json.dumps(
                    {
                        "checked_at": "2026-05-31T11:00:00+00:00",
                        "query_model": "deepseek-v4-flash",
                        "mode": "hybrid",
                        "config_flags": {
                            "RERANKER_ENABLED": "false",
                            "HYBRID_SYNTH_ENABLED": "true",
                        },
                        "total": 6,
                        "passed": 6,
                        "average_score": 100.0,
                        "average_duration_seconds": 9.5,
                    }
                ),
                encoding="utf-8",
            )
            (artifacts_dir / "broken_scores.json").write_text("{", encoding="utf-8")
            (artifacts_dir / "not_a_summary_scores.json").write_text("{}", encoding="utf-8")

            records = collect_experiment_records(artifacts_dir)

        by_run_id = {record.run_id: record for record in records}
        self.assertEqual(set(by_run_id), {"sample_golden_set", "llm_probe_sample"})
        self.assertEqual(by_run_id["sample_golden_set"].kind, "golden")
        self.assertEqual(by_run_id["sample_golden_set"].pass_rate, 75.0)
        self.assertFalse(by_run_id["sample_golden_set"].reranker_enabled)
        self.assertTrue(by_run_id["sample_golden_set"].hybrid_synth_enabled)
        self.assertEqual(by_run_id["llm_probe_sample"].kind, "focused_probe")
        self.assertEqual(by_run_id["llm_probe_sample"].average_duration_seconds, 9.5)
        self.assertEqual(
            Path(by_run_id["llm_probe_sample"].results_path).name,
            "llm_probe_sample_results.md",
        )

    def test_writes_manifest_and_markdown_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts_dir = Path(tmpdir)
            (artifacts_dir / "sample_golden_set_scores.json").write_text(
                json.dumps(
                    {
                        "checked_at": "2026-05-31T10:00:00+00:00",
                        "query_model": "deepseek-v4-flash",
                        "mode": "hybrid",
                        "config_flags": {"RERANKER_ENABLED": False},
                        "total": 2,
                        "passed": 2,
                        "average_score": 100.0,
                    }
                ),
                encoding="utf-8",
            )

            registry = write_experiment_registry(artifacts_dir)
            manifest = json.loads(registry.manifest_path.read_text(encoding="utf-8"))
            report = registry.report_path.read_text(encoding="utf-8")

        self.assertEqual(len(registry.records), 1)
        self.assertEqual(len(manifest["records"]), 1)
        self.assertEqual(manifest["records"][0]["pass_rate"], 100.0)
        self.assertIn("| Checked At | Kind | Model | Mode | Passed | Avg |", report)
        self.assertIn("sample_golden_set_scores.json", report)
