"""Offline contract tests for the fail-closed Late-Fusion A/B harness."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import late_fusion_ab


def _green_checks(output_dir: Path) -> list[dict]:
    checks = []
    for name in (
            "late_fusion_tests",
            "affected_regression_tests",
            "full_suite_flag_false",
            "full_suite_flag_true",
            "ruff",
            "compile_import",
            "git_diff_check",
        ):
        log_path = output_dir / "logs" / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"{name}: passed\n", encoding="utf-8")
        check = {
            "name": name,
            "command": f"python -m pytest # {name}",
            "exit_code": 0,
            "log_path": str(log_path.relative_to(output_dir)),
            "log_sha256": late_fusion_ab._sha256_path(log_path),
        }
        if name in {"full_suite_flag_false", "full_suite_flag_true"}:
            check["environment"] = {
                "LATE_FUSION_ENABLED": "false" if name.endswith("false") else "true",
                "LLM_PROFILE": "current",
                "LLM_MODEL": "test-model",
            }
        checks.append(check)
    return checks


def _valid_late_variant() -> dict:
    return {
        "answer": "Проверенный ответ [S1].",
        "references": [
            {
                "reference_id": "S1",
                "source_id": "source:1",
                "title": "Источник",
                "url": "https://example.com/source",
                "content_type": "article",
                "start_url": "",
            }
        ],
        "late_fusion": {
            "pipeline": "late_fusion",
            "prompt_source_ids": ["source:1"],
            "estimated_input_tokens": 100,
            "max_input_tokens": 1000,
            "output_token_reserve": 100,
            "runtime_context_limit": 1100,
            "source_block_tokens": {"S1": {"full": 80, "final": 80}},
            "dropped_source_ids": [],
            "channel_statuses": {
                "lightrag": {"status": "success", "duration_ms": 1.0},
                "card_fts": {"status": "empty", "duration_ms": 1.0},
                "youtube_fts": {"status": "empty", "duration_ms": 1.0},
            },
        },
        "fallback": None,
    }


def _make_valid_run(output_dir: Path, *, seed: str = "test") -> None:
    identity = late_fusion_ab.prepare_run(output_dir, resume=False, seed=seed)
    mapping = late_fusion_ab._read_json(output_dir / "blind_mapping.json")
    reviews = []
    hashes = {}
    for index, case in enumerate(late_fusion_ab.FROZEN_CASES):
        case_id = case["id"]
        artifact = {
            "id": case_id,
            "question": case["question"],
            "profile": case["profile"],
            "legacy": {"answer": "Legacy", "references": [], "late_fusion": {}, "fallback": None},
            "late_fusion": _valid_late_variant(),
            "completed_at": "2026-08-09T00:00:00+00:00",
        }
        case_path = output_dir / "cases" / f"{case_id}.json"
        late_fusion_ab._write_json(case_path, artifact)
        hashes[case_id] = late_fusion_ab._sha256_path(case_path)
        late_label = next(label for label, variant in mapping[case_id].items() if variant == "late_fusion")
        better = index < 5
        reviews.append(
            {
                "id": case_id,
                "completeness": late_label if better else "tie",
                "specificity": late_label if better else "tie",
                "relevance": "tie",
                "no_unsupported": "tie",
                "citations": "tie",
                "rating_entry_mode": "per_criterion",
                "apply_to_all_source_choice": None,
                "unsupported_financing_claim": False,
            }
        )
    late_fusion_ab._write_json(
        output_dir / "run_state.json",
        {
            "run_id": identity["run_id"],
            "run_seed": seed,
            "started_at": identity["started_at"],
            "completed": [case["id"] for case in late_fusion_ab.FROZEN_CASES],
            "case_sha256": hashes,
        },
    )
    late_fusion_ab._write_json(output_dir / "reviews.json", {"cases": reviews})
    late_fusion_ab.record_automatic_gates(output_dir, _green_checks(output_dir))
    late_fusion_ab.finalize_reviews(output_dir)


class LateFusionAbTests(unittest.TestCase):
    def test_prepare_run_rejects_changed_identity_on_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with patch.object(late_fusion_ab, "build_identity", return_value={"identity": "one"}):
                late_fusion_ab.prepare_run(output_dir, resume=False, seed="seed")
                self.assertEqual(
                    late_fusion_ab.prepare_run(output_dir, resume=True, seed="seed"),
                    {"identity": "one"},
                )
            with patch.object(late_fusion_ab, "build_identity", return_value={"identity": "two"}):
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    late_fusion_ab.prepare_run(output_dir, resume=True, seed="seed")

    def test_resume_with_different_seed_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            late_fusion_ab.prepare_run(output_dir, resume=False, seed="one")
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                late_fusion_ab.prepare_run(output_dir, resume=True, seed="two")

    def test_blind_mapping_is_deterministic_and_bound_to_identity(self):
        self.assertEqual(late_fusion_ab._blind_mapping("stable"), late_fusion_ab._blind_mapping("stable"))
        identity = late_fusion_ab.build_identity(seed="stable", started_at="t", run_id="r")
        self.assertEqual(
            identity["blind_mapping_sha256"],
            late_fusion_ab._sha256_text(late_fusion_ab._canonical_json(late_fusion_ab._blind_mapping("stable"))),
        )

    def test_content_manifest_hashes_untracked_file_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "new.py"
            path.write_text("first", encoding="utf-8")
            with (
                patch.object(late_fusion_ab, "PROJECT_ROOT", root),
                patch.object(late_fusion_ab, "SCOPED_PATHS", ("new.py",)),
            ):
                first = late_fusion_ab._implementation_manifest()
                path.write_text("second", encoding="utf-8")
                second = late_fusion_ab._implementation_manifest()
        self.assertNotEqual(first[0]["sha256"], second[0]["sha256"])

    def test_rag_manifest_can_exclude_runtime_llm_cache_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            corpus = root / "vdb_chunks.json"
            cache = root / "kv_store_llm_response_cache.json"
            corpus.write_text("corpus-one", encoding="utf-8")
            cache.write_text("cache-one", encoding="utf-8")
            first = late_fusion_ab._directory_manifest_hash(
                root,
                ignored_relative_paths={cache.name},
            )
            cache.write_text("cache-two", encoding="utf-8")
            second = late_fusion_ab._directory_manifest_hash(
                root,
                ignored_relative_paths={cache.name},
            )
            corpus.write_text("corpus-two", encoding="utf-8")
            third = late_fusion_ab._directory_manifest_hash(
                root,
                ignored_relative_paths={cache.name},
            )

        self.assertEqual(first, second)
        self.assertNotEqual(second, third)

    def test_scoring_applies_all_acceptance_gates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            _make_valid_run(output_dir)
            report = late_fusion_ab.score_reviews(output_dir)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["acceptance_status"], "accepted")
        self.assertEqual(report["non_worse_count"], 10)
        self.assertEqual(report["materially_better_count"], 5)

    def test_pipeline_other_than_exact_late_fusion_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            _make_valid_run(output_dir)
            case_path = output_dir / "cases" / "LF01.json"
            artifact = late_fusion_ab._read_json(case_path)
            artifact["late_fusion"]["late_fusion"]["pipeline"] = "mix"
            late_fusion_ab._write_json(case_path, artifact)
            state = late_fusion_ab._read_json(output_dir / "run_state.json")
            state["case_sha256"]["LF01"] = late_fusion_ab._sha256_path(case_path)
            late_fusion_ab._write_json(output_dir / "run_state.json", state)
            report = late_fusion_ab.score_reviews(output_dir)
        self.assertFalse(report["accepted"])
        self.assertIn("LF01:pipeline_not_late_fusion", report["validation_errors"])

    def test_case_hash_tampering_and_partial_run_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            _make_valid_run(output_dir)
            case_path = output_dir / "cases" / "LF02.json"
            case_path.write_text("{}", encoding="utf-8")
            report = late_fusion_ab.score_reviews(output_dir)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["acceptance_status"], "invalid_run")

    def test_invalid_url_and_unknown_citation_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            _make_valid_run(output_dir)
            case_path = output_dir / "cases" / "LF03.json"
            artifact = late_fusion_ab._read_json(case_path)
            artifact["late_fusion"]["answer"] = "Ответ [S999]."
            artifact["late_fusion"]["references"][0]["url"] = "javascript:alert(1)"
            late_fusion_ab._write_json(case_path, artifact)
            state = late_fusion_ab._read_json(output_dir / "run_state.json")
            state["case_sha256"]["LF03"] = late_fusion_ab._sha256_path(case_path)
            late_fusion_ab._write_json(output_dir / "run_state.json", state)
            report = late_fusion_ab.score_reviews(output_dir)
        self.assertFalse(report["accepted"])
        self.assertIn("LF03:unknown_citation", report["validation_errors"])
        self.assertIn("LF03:invalid_reference_url:S1", report["validation_errors"])

    def test_lf10_requires_explicit_no_unsupported_financing_claim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            _make_valid_run(output_dir)
            reviews = late_fusion_ab._read_json(output_dir / "reviews.json")
            next(item for item in reviews["cases"] if item["id"] == "LF10")["unsupported_financing_claim"] = True
            late_fusion_ab._write_json(output_dir / "reviews.json", reviews)
            report = late_fusion_ab.score_reviews(output_dir)
        self.assertFalse(report["accepted"])
        self.assertTrue(next(item for item in report["cases"] if item["id"] == "LF10")["lf10_automatic_fail"])

    def test_blind_packet_contains_clickable_reference_links_without_pipeline_labels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            _make_valid_run(output_dir)
            packet = late_fusion_ab.write_blind_review_packet(output_dir).read_text(encoding="utf-8")
        self.assertIn("(https://example.com/source)", packet)
        self.assertIn("content type: article", packet)
        self.assertNotIn("pipeline", packet.casefold())

    def test_blind_packet_keeps_uncited_context_sources_and_timestamp_metadata(self):
        rendered = late_fusion_ab._render_blind_references(
            {
                "answer": "Answer [S1]",
                "references": [
                    {
                        "reference_id": "S1",
                        "title": "Cited",
                        "url": "https://example.com/cited",
                        "content_type": "article",
                    },
                    {
                        "reference_id": "S2",
                        "title": "Context",
                        "url": "https://example.com/context",
                        "content_type": "youtube_transcript",
                        "start_url": "https://youtube.com/watch?v=x&t=90s",
                    },
                ],
            }
        )
        packet = "\n".join(rendered)
        self.assertIn("[S1]", packet)
        self.assertIn("[S2]", packet)
        self.assertIn("content type: youtube_transcript", packet)
        self.assertIn("timestamp/start URL", packet)
        self.assertIn("does not mean the sources are unverified", packet)

    def test_blind_packet_uses_legacy_canonical_url_fields(self):
        rendered = late_fusion_ab._render_blind_references(
            {
                "answer": "Legacy [L1]",
                "references": [
                    {
                        "reference_id": "L1",
                        "title": "Legacy source",
                        "primary_url": "https://example.com/legacy",
                    }
                ],
            }
        )
        self.assertIn("(https://example.com/legacy)", "\n".join(rendered))
        self.assertNotIn("URL missing", "\n".join(rendered))

    def test_blind_packet_does_not_describe_unlinked_context_as_unverified(self):
        rendered = late_fusion_ab._render_blind_references(
            {
                "answer": "Legacy answer",
                "references": [{"reference_id": "1", "title": "Context source"}],
            }
        )
        packet = "\n".join(rendered)
        self.assertIn("no canonical link was exported", packet)
        self.assertIn("not an unsupported-claim marker", packet)
        self.assertNotIn("URL missing", packet)

    def test_scoring_rejects_reviews_changed_after_finalization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            _make_valid_run(output_dir)
            reviews = late_fusion_ab._read_json(output_dir / "reviews.json")
            reviews["cases"][0]["relevance"] = "A"
            late_fusion_ab._write_json(output_dir / "reviews.json", reviews)
            report = late_fusion_ab.score_reviews(output_dir)
        self.assertFalse(report["accepted"])
        self.assertIn("rating_artifact_hash_mismatch", report["validation_errors"])

    def test_finalize_reviews_rejects_incomplete_human_ratings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            late_fusion_ab.prepare_run(output_dir, resume=False, seed="test")
            late_fusion_ab._write_json(output_dir / "reviews.json", late_fusion_ab._review_template())
            with self.assertRaisesRegex(RuntimeError, "LF01/completeness must be A, B, or tie"):
                late_fusion_ab.finalize_reviews(output_dir)
            self.assertFalse((output_dir / "reviews_manifest.json").exists())

    def test_scoring_revalidates_automatic_gate_log_hashes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            _make_valid_run(output_dir)
            gates = late_fusion_ab._read_json(output_dir / "automatic_gates.json")
            first_check = gates["checks"][0]
            (output_dir / first_check["log_path"]).write_text("tampered\n", encoding="utf-8")
            report = late_fusion_ab.score_reviews(output_dir)
        self.assertFalse(report["accepted"])
        self.assertIn(
            "automatic_gate_check_invalid:late_fusion_tests:log_hash_mismatch",
            report["validation_errors"],
        )

    def test_automatic_gate_requires_hashed_log_and_explicit_full_suite_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            late_fusion_ab.prepare_run(output_dir, resume=False, seed="test")
            checks = _green_checks(output_dir)
            next(item for item in checks if item["name"] == "full_suite_flag_true")["environment"].pop(
                "LLM_MODEL"
            )
            report = late_fusion_ab.record_automatic_gates(output_dir, checks)
        self.assertFalse(report["passed"])
        self.assertIn("full_suite_flag_true", report["failed"])

    def test_atomic_json_write_and_corrupt_checkpoint_rejection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            late_fusion_ab._write_json(path, {"ok": True})
            self.assertEqual(late_fusion_ab._read_json(path), {"ok": True})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Cannot read"):
                late_fusion_ab._read_json(path)

    def test_git_output_decodes_utf8_independently_of_windows_locale(self):
        completed = subprocess.CompletedProcess(["git"], 0, stdout="файл\n".encode(), stderr=b"")
        with patch.object(late_fusion_ab.subprocess, "run", return_value=completed):
            self.assertEqual(late_fusion_ab._git_output("status"), "файл")


if __name__ == "__main__":
    unittest.main()
