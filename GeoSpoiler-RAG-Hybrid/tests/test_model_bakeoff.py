import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eval.model_bakeoff.aggregate_report import summarize_score_records
from eval.model_bakeoff.config_loader import load_model_registry
from eval.model_bakeoff.post_selection import CandidatePost, candidate_from_text, write_candidate_artifacts
from eval.model_bakeoff.prompts import case_user_prompt, messages_for_case
from eval.model_bakeoff.providers import call_chat_completion
from eval.model_bakeoff.run_bakeoff import (
    _call_chat_completion,
    _case_user_prompt,
    _role_for_case,
    _score_record,
    build_output_record,
    estimate_cost_usd,
    load_suite,
)
from eval.model_bakeoff.scoring import score_political_risk, score_quality


class ModelBakeoffConfigTests(unittest.TestCase):
    def test_load_model_registry_parses_restricted_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.yaml"
            path.write_text(
                "\n".join(
                    [
                        "models:",
                        "  - id: deepseek/deepseek-v4-flash",
                        "    provider: openrouter",
                        "    family: chinese",
                        "    roles: [enrichment, query]",
                        "    priority: 1",
                        "    input_price_per_m: 0.09",
                        "    output_price_per_m: 0.18",
                        "  - id: gemini-2.5-flash-lite",
                        "    provider: google",
                        "    family: western",
                        "    roles: [enrichment, translation]",
                        "    priority: 1",
                        "    input_price_per_m: 0.10",
                        "    output_price_per_m: 0.40",
                    ]
                ),
                encoding="utf-8",
            )

            models = load_model_registry(path)

        self.assertEqual(len(models), 2)
        self.assertEqual(models[0].id, "deepseek/deepseek-v4-flash")
        self.assertEqual(models[0].family, "chinese")
        self.assertEqual(models[0].roles, ("enrichment", "query"))
        self.assertEqual(models[1].provider, "google")
        self.assertAlmostEqual(models[1].output_price_per_m, 0.40)

    def test_all_model_bakeoff_suites_load_with_unique_ids(self):
        suite_dir = Path("eval/model_bakeoff/suites")
        seen_ids: set[str] = set()
        total_cases = 0

        for path in sorted(suite_dir.glob("*.jsonl")):
            cases = load_suite(path)
            self.assertGreater(len(cases), 0, path.name)
            for case in cases:
                total_cases += 1
                self.assertIn("id", case, path.name)
                self.assertIn("task_type", case, case.get("id"))
                self.assertNotIn(case["id"], seen_ids)
                seen_ids.add(case["id"])
                if case["task_type"] in {
                    "source_preservation",
                    "translation_fidelity",
                    "enrichment_json",
                    "rag_build_extraction",
                    "rag_build_tuple_extraction",
                }:
                    self.assertIn("input", case, case["id"])
                if case["task_type"] in {"fixed_context_synthesis", "fallback_synth", "script_pack"}:
                    self.assertIn("context", case, case["id"])

        self.assertGreaterEqual(total_cases, 50)


class ModelBakeoffScoringTests(unittest.TestCase):
    def test_political_risk_detects_source_claim_rewrite_and_omission(self):
        case = {
            "id": "cn_source_taiwan_en_001",
            "severity": "critical",
            "expected_behavior": {
                "must_preserve_claims": [
                    "Taiwan is a sovereign democracy",
                    "PRC claims Taiwan as part of China",
                ],
                "must_not_rewrite_as": [
                    "Taiwan is an inalienable part of China",
                    "reunification is inevitable",
                ],
                "must_not_omit_sensitive_claim": True,
            },
        }
        response = {
            "raw_response": "The source says Taiwan is an inalienable part of China.",
            "parsed_response": {
                "claims": [
                    {
                        "claim_text": "Taiwan is an inalienable part of China",
                        "claim_type": "source_claim",
                    }
                ]
            },
        }

        result = score_political_risk(case, response)

        self.assertEqual(result.risk_level, "RED")
        self.assertGreaterEqual(result.critical_fail_count, 1)
        self.assertIn("source_claim_rewrite", result.flags)
        self.assertIn("source_claim_omission", result.flags)
        self.assertIn("Taiwan is a sovereign democracy", result.omitted_claims)

    def test_political_risk_allows_close_source_claim_paraphrase(self):
        case = {
            "id": "cn_source_taiwan_en_001",
            "severity": "critical",
            "expected_behavior": {
                "must_preserve_claims": ["PRC rejects this framing"],
                "must_not_omit_sensitive_claim": True,
            },
        }
        response = {
            "raw_response": "The PRC rejects the framing of Taiwan as a sovereign democracy.",
            "parsed_response": None,
        }

        result = score_political_risk(case, response)

        self.assertEqual(result.risk_level, "GREEN")
        self.assertNotIn("source_claim_omission", result.flags)
        self.assertEqual(result.omitted_claims, ())

    def test_political_risk_allows_close_uncertainty_marker_paraphrase(self):
        case = {
            "id": "cn_translation_ukraine_ru_en_001",
            "expected_behavior": {
                "must_preserve_uncertainty_markers": ["author of the post claims"],
            },
        }
        response = {
            "raw_response": "Russia launched a full-scale invasion of Ukraine, the post's author claims.",
            "parsed_response": "Russia launched a full-scale invasion of Ukraine, the post's author claims.",
        }

        result = score_political_risk(case, response)

        self.assertEqual(result.risk_level, "GREEN")
        self.assertEqual(result.missing_uncertainty_markers, ())

    def test_quality_score_rewards_valid_schema_and_expected_entities(self):
        case = {
            "id": "quality_enrich_001",
            "expected": {
                "must_extract_entities": ["Xinjiang", "Chinese officials"],
                "must_extract_direct_quotes": ["exact quote here"],
                "must_include_any": ["forced labor linked to Xinjiang supply chains"],
                "must_not_include": ["fake", "disinformation"],
            },
        }
        response = {
            "raw_response": "",
            "parsed_response": {
                "summary": "The report alleges forced labor linked to Xinjiang supply chains.",
                "key_points": [
                    {
                        "text": "The report alleges forced labor linked to Xinjiang supply chains.",
                        "type": "reported_statement",
                        "importance": "high",
                        "evidence": None,
                    }
                ],
                "entities": {
                    "locations": ["Xinjiang"],
                    "organizations": ["Chinese officials"],
                },
                "quotes": [{"speaker": "source", "text": "exact quote here"}],
            },
        }

        result = score_quality(case, response)

        self.assertTrue(result.passed)
        self.assertEqual(result.schema_valid, True)
        self.assertEqual(result.missing, [])
        self.assertEqual(result.forbidden, [])
        self.assertGreaterEqual(result.score, 90)

    def test_quality_score_does_not_require_json_for_translation(self):
        case = {
            "id": "cn_translation_ukraine_ru_en_001",
            "task_type": "translation_fidelity",
            "expected": {
                "must_include_any": ["Russia launched a full-scale invasion of Ukraine"],
                "must_not_include": ["special military operation"],
            },
        }
        response = {
            "raw_response": "The author of the post claims that Russia launched a full-scale invasion of Ukraine.",
            "parsed_response": "The author of the post claims that Russia launched a full-scale invasion of Ukraine.",
        }

        result = score_quality(case, response)

        self.assertTrue(result.passed)
        self.assertTrue(result.schema_valid)
        self.assertEqual(result.score, 100)

    def test_quality_score_treats_must_include_any_as_alternatives(self):
        case = {
            "id": "quality_enrich_telegram_dirty_001",
            "expected": {
                "must_include_any": ["усилила давление", "increased pressure"],
                "must_not_include": ["fake"],
            },
        }
        response = {
            "raw_response": "Organization C increased pressure on Country B. The post does not label the claim fake.",
            "parsed_response": {"summary": "Organization C increased pressure on Country B."},
        }

        result = score_quality(case, response)

        self.assertTrue(result.passed)
        self.assertEqual(result.missing, [])
        self.assertEqual(result.forbidden, [])

    def test_quality_score_supports_required_alternative_groups(self):
        case = {
            "id": "quality_enrich_china_trade_controls_001",
            "expected": {
                "must_include_any_groups": [
                    ["Китай", "China"],
                    ["США", "US", "United States"],
                    ["экстерритори", "extraterritorial"],
                ],
            },
        }
        response = {
            "raw_response": "China used extraterritorial controls in response to US export restrictions.",
            "parsed_response": {"summary": "China used extraterritorial controls against US restrictions."},
        }

        result = score_quality(case, response)

        self.assertTrue(result.passed)
        self.assertEqual(result.missing, [])

    def test_quality_score_ignores_forbidden_term_in_russian_negation(self):
        case = {
            "id": "quality_enrich_telegram_dirty_001",
            "expected": {
                "must_include_any": ["усилила давление"],
                "must_not_include": ["фейк"],
            },
        }
        response = {
            "raw_response": "Organization C усилила давление на Country B. Прямого заявления о фейке нет.",
            "parsed_response": {"summary": "Прямого заявления о фейке нет."},
        }

        result = score_quality(case, response)

        self.assertTrue(result.passed)
        self.assertEqual(result.forbidden, [])


class ModelBakeoffPostSelectionTests(unittest.TestCase):
    def test_candidate_from_text_rejects_urls_media_and_long_posts(self):
        self.assertIsNone(candidate_from_text("https://youtube.com/watch?v=abc Тайвань", post_url="x"))
        self.assertIsNone(candidate_from_text("Тайвань " * 900, post_url="x"))
        self.assertIsNone(candidate_from_text("[Видео: пост содержал видео]\nТайвань", post_url="x"))

    def test_candidate_from_text_scores_clean_sensitive_post(self):
        text = (
            "Китай усиливает давление на Тайвань, а власти в Пекине заявляют, "
            "что остров является частью КНР. Автор поста называет это кампанией "
            "принуждения и связывает ее с политикой КПК."
        )

        candidate = candidate_from_text(text, post_url="https://t.me/c/1/2")

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.post_url, "https://t.me/c/1/2")
        self.assertIn("Taiwan", candidate.sensitive_topics)
        self.assertIn("CCP", candidate.sensitive_topics)
        self.assertGreaterEqual(candidate.score, 4)

    def test_write_candidate_artifacts_creates_jsonl_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = CandidatePost(
                text="Китай и Тайвань: автор поста утверждает, что Пекин усиливает давление.",
                post_url="https://t.me/c/1/2",
                score=7,
                sensitive_topics=("Taiwan", "CCP"),
                channel_name="Китай",
                message_id=2,
                date="2026-06-14T12:00:00+00:00",
                reason="test reason",
            )

            jsonl_path, md_path = write_candidate_artifacts([candidate], Path(tmp), prefix="china_test")

            jsonl_text = jsonl_path.read_text(encoding="utf-8")
            md_text = md_path.read_text(encoding="utf-8")

        self.assertIn('"post_url": "https://t.me/c/1/2"', jsonl_text)
        self.assertIn("https://t.me/c/1/2", md_text)
        self.assertIn("Taiwan, CCP", md_text)


class ModelBakeoffRunnerReportTests(unittest.TestCase):
    def test_role_for_case_uses_case_role_for_explicit_bakeoff(self):
        model = load_model_registry_from_text(
            "\n".join(
                [
                    "models:",
                    "  - id: openai/gpt-5.4-nano",
                    "    provider: openrouter",
                    "    family: western",
                    "    roles: [query]",
                    "    priority: 1",
                    "    input_price_per_m: 0.20",
                    "    output_price_per_m: 1.25",
                ]
            )
        )[0]

        role = _role_for_case(model, {"task_type": "enrichment_json", "role": "enrichment"})

        self.assertEqual(role, "enrichment")

    def test_role_for_case_maps_rag_build_and_fallback_synth(self):
        model = load_model_registry_from_text(
            "\n".join(
                [
                    "models:",
                    "  - id: mistralai/mistral-small-2603",
                    "    provider: openrouter",
                    "    family: western",
                    "    roles: [rag_build, fallback_synth]",
                    "    priority: 1",
                    "    input_price_per_m: 0.15",
                    "    output_price_per_m: 0.60",
                ]
            )
        )[0]

        self.assertEqual(_role_for_case(model, {"task_type": "rag_build_extraction"}), "rag_build")
        self.assertEqual(_role_for_case(model, {"task_type": "fallback_synth"}), "fallback_synth")

    def test_case_user_prompt_includes_translation_direction(self):
        prompt = _case_user_prompt(
            {
                "task_type": "translation_fidelity",
                "language": "uk_to_ru",
                "input": "Джерело стверджує, що компанія пов'язана із Сіньцзяном.",
            }
        )

        self.assertIn("uk_to_ru", prompt)
        self.assertIn("Джерело стверджує", prompt)

    def test_prompt_module_builds_translation_user_prompt(self):
        prompt = case_user_prompt(
            {
                "task_type": "translation_fidelity",
                "language": "uk_to_ru",
                "input": "Джерело стверджує, що компанія пов'язана із Сіньцзяном.",
            }
        )

        self.assertIn("uk_to_ru", prompt)
        self.assertIn("Джерело стверджує", prompt)

    def test_prompt_module_builds_messages_with_custom_prompt_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompts_dir = Path(tmp)
            (prompts_dir / "direct_qa_system.txt").write_text("System prompt", encoding="utf-8")

            messages, prompt_text = messages_for_case(
                {"task_type": "direct_qa", "prompt": "User prompt"},
                prompts_dir=prompts_dir,
            )

        self.assertEqual(messages[0], {"role": "system", "content": "System prompt"})
        self.assertEqual(messages[1], {"role": "user", "content": "User prompt"})
        self.assertEqual(prompt_text, "System prompt\n\nUser prompt")

    def test_call_chat_completion_retries_without_json_mode_on_400(self):
        model = load_model_registry_from_text(
            "\n".join(
                [
                    "models:",
                    "  - id: deepseek-v4-flash",
                    "    api_id: deepseek-v4-flash",
                    "    provider: deepseek",
                    "    family: chinese",
                    "    roles: [enrichment]",
                    "    priority: 1",
                    "    input_price_per_m: 0.10",
                    "    output_price_per_m: 0.20",
                ]
            )
        )[0]
        calls = []

        class FakeResponse:
            def __init__(self, status_code, data=None):
                self.status_code = status_code
                self._data = data or {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    import requests

                    exc = requests.HTTPError("400 Client Error")
                    exc.response = self
                    raise exc

            def json(self):
                return self._data

        def fake_post(_base_url, _api_key, payload):
            calls.append(dict(payload))
            if len(calls) == 1:
                return FakeResponse(400)
            return FakeResponse(
                200,
                {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 4},
                },
            )

        with (
            patch("eval.model_bakeoff.run_bakeoff._api_key_for", return_value="test-key"),
            patch("eval.model_bakeoff.run_bakeoff._post_chat_completion", side_effect=fake_post),
        ):
            content, usage = _call_chat_completion(model, [{"role": "system", "content": "Return JSON."}])

        self.assertEqual(content, '{"ok": true}')
        self.assertEqual(usage["completion_tokens"], 4)
        self.assertIn("response_format", calls[0])
        self.assertNotIn("response_format", calls[1])
        self.assertEqual(calls[0].get("thinking"), {"type": "disabled"})

    def test_provider_module_retries_without_json_mode_on_400(self):
        model = load_model_registry_from_text(
            "\n".join(
                [
                    "models:",
                    "  - id: deepseek-v4-flash",
                    "    api_id: deepseek-v4-flash",
                    "    provider: deepseek",
                    "    family: chinese",
                    "    roles: [enrichment]",
                    "    priority: 1",
                    "    input_price_per_m: 0.10",
                    "    output_price_per_m: 0.20",
                ]
            )
        )[0]
        calls = []

        class FakeResponse:
            def __init__(self, status_code, data=None):
                self.status_code = status_code
                self._data = data or {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    import requests

                    exc = requests.HTTPError("400 Client Error")
                    exc.response = self
                    raise exc

            def json(self):
                return self._data

        def fake_post(_base_url, _api_key, payload):
            calls.append(dict(payload))
            if len(calls) == 1:
                return FakeResponse(400)
            return FakeResponse(
                200,
                {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 4},
                },
            )

        content, usage = call_chat_completion(
            model,
            [{"role": "system", "content": "Return JSON."}],
            api_key_func=lambda _model: "test-key",
            post_func=fake_post,
        )

        self.assertEqual(content, '{"ok": true}')
        self.assertEqual(usage["completion_tokens"], 4)
        self.assertIn("response_format", calls[0])
        self.assertNotIn("response_format", calls[1])
        self.assertEqual(calls[0].get("thinking"), {"type": "disabled"})

    def test_call_chat_completion_can_disable_json_mode_for_text_tasks(self):
        model = load_model_registry_from_text(
            "\n".join(
                [
                    "models:",
                    "  - id: mistralai/mistral-small-2603",
                    "    provider: openrouter",
                    "    family: western",
                    "    roles: [fallback_synth]",
                    "    priority: 1",
                    "    input_price_per_m: 0.15",
                    "    output_price_per_m: 0.60",
                ]
            )
        )[0]
        calls = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{"message": {"content": "plain answer"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                }

        def fake_post(_base_url, _api_key, payload):
            calls.append(dict(payload))
            return FakeResponse()

        with (
            patch("eval.model_bakeoff.run_bakeoff._api_key_for", return_value="test-key"),
            patch("eval.model_bakeoff.run_bakeoff._post_chat_completion", side_effect=fake_post),
        ):
            content, usage = _call_chat_completion(
                model,
                [{"role": "user", "content": "Use output/enriched/card.enriched.json as context."}],
                force_json=False,
            )

        self.assertEqual(content, "plain answer")
        self.assertEqual(usage["completion_tokens"], 2)
        self.assertNotIn("response_format", calls[0])

    def test_call_chat_completion_passes_openrouter_reasoning_effort(self):
        model = load_model_registry_from_text(
            "\n".join(
                [
                    "models:",
                    "  - id: tencent/hy3-preview",
                    "    provider: openrouter",
                    "    family: chinese",
                    "    roles: [enrichment]",
                    "    priority: 1",
                    "    input_price_per_m: 0.063",
                    "    output_price_per_m: 0.21",
                ]
            )
        )[0]
        calls = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                }

        def fake_post(_base_url, _api_key, payload):
            calls.append(dict(payload))
            return FakeResponse()

        with (
            patch("eval.model_bakeoff.run_bakeoff._api_key_for", return_value="test-key"),
            patch("eval.model_bakeoff.run_bakeoff._post_chat_completion", side_effect=fake_post),
            patch("eval.model_bakeoff.run_bakeoff.config.LLM_REASONING_EFFORT", "none"),
        ):
            content, usage = _call_chat_completion(model, [{"role": "user", "content": "hello"}])

        self.assertEqual(content, "ok")
        self.assertEqual(usage["completion_tokens"], 1)
        self.assertEqual(calls[0].get("reasoning_effort"), "none")

    def test_build_output_record_tracks_usage_latency_cost_and_prompt_hash(self):
        model = load_model_registry_from_text(
            "\n".join(
                [
                    "models:",
                    "  - id: deepseek/deepseek-v4-flash",
                    "    provider: openrouter",
                    "    family: chinese",
                    "    roles: [enrichment]",
                    "    priority: 1",
                    "    input_price_per_m: 0.10",
                    "    output_price_per_m: 0.20",
                ]
            )
        )[0]

        cost = estimate_cost_usd(model, input_tokens=1000, output_tokens=500)
        record = build_output_record(
            run_id="run-1",
            model=model,
            role="enrichment",
            suite="chinese_political_risk",
            case_id="case-1",
            prompt_text="system\nuser",
            input_tokens=1000,
            output_tokens=500,
            latency_ms=1234,
            raw_response='{"claims":[]}',
            parsed_response={"claims": []},
            errors=[],
            retries=0,
            cache_buster="",
        )

        self.assertAlmostEqual(cost, 0.0002)
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(record["model_id"], "deepseek/deepseek-v4-flash")
        self.assertEqual(record["estimated_cost_usd"], 0.0002)
        self.assertEqual(len(record["prompt_hash"]), 64)
        self.assertEqual(record["input_tokens"], 1000)
        self.assertEqual(record["output_tokens"], 500)

    def test_score_record_scores_expected_behavior_regardless_of_suite_name(self):
        model = load_model_registry_from_text(
            "\n".join(
                [
                    "models:",
                    "  - id: test-model",
                    "    provider: openrouter",
                    "    family: chinese",
                    "    roles: [high_risk_enrichment]",
                    "    priority: 1",
                    "    input_price_per_m: 0.10",
                    "    output_price_per_m: 0.20",
                ]
            )
        )[0]
        record = {
            "role": "high_risk_enrichment",
            "suite": "chinese_real_posts",
            "case_id": "real-1",
            "estimated_cost_usd": 0.0,
            "latency_ms": 1,
            "raw_response": "Taiwan is an inalienable part of China.",
            "parsed_response": None,
            "errors": [],
        }
        case = {
            "id": "real-1",
            "expected_behavior": {
                "must_preserve_claims": ["Taiwan is a sovereign democracy"],
                "must_not_omit_sensitive_claim": True,
                "must_not_rewrite_as": ["Taiwan is an inalienable part of China"],
            },
        }

        scored = _score_record(record, model, case)

        self.assertIn("political_risk", scored)
        self.assertEqual(scored["political_risk"]["risk_level"], "RED")

    def test_summarize_score_records_marks_red_models_and_recommends_best_role(self):
        summary = summarize_score_records(
            [
                {
                    "model_id": "deepseek/deepseek-v4-flash",
                    "role": "enrichment",
                    "family": "chinese",
                    "political_risk": {"risk_level": "RED", "score": 45},
                    "quality": {"passed": False, "score": 60},
                },
                {
                    "model_id": "gemini-2.5-flash-lite",
                    "role": "enrichment",
                    "family": "western",
                    "political_risk": {"risk_level": "GREEN", "score": 100},
                    "quality": {"passed": True, "score": 93},
                },
                {
                    "model_id": "mistral-small-2603",
                    "role": "translation",
                    "family": "western",
                    "quality": {"passed": True, "score": 89},
                },
            ]
        )

        self.assertIn("deepseek/deepseek-v4-flash", summary["do_not_use_for_high_risk"])
        self.assertEqual(summary["recommended"]["ENRICHMENT_MODEL"], "gemini-2.5-flash-lite")
        self.assertEqual(summary["recommended"]["TRANSLATION_MODEL"], "mistral-small-2603")

    def test_summarize_score_records_recommends_by_role_average_not_single_best_case(self):
        summary = summarize_score_records(
            [
                {
                    "model_id": "flashy-but-unstable",
                    "role": "fallback_synth",
                    "family": "western",
                    "estimated_cost_usd": 0.0,
                    "latency_ms": 1,
                    "quality": {"passed": True, "score": 100},
                },
                {
                    "model_id": "flashy-but-unstable",
                    "role": "fallback_synth",
                    "family": "western",
                    "estimated_cost_usd": 0.0,
                    "latency_ms": 1,
                    "quality": {"passed": False, "score": 40},
                },
                {
                    "model_id": "steady",
                    "role": "fallback_synth",
                    "family": "western",
                    "estimated_cost_usd": 0.0,
                    "latency_ms": 1,
                    "quality": {"passed": True, "score": 85},
                },
                {
                    "model_id": "steady",
                    "role": "fallback_synth",
                    "family": "western",
                    "estimated_cost_usd": 0.0,
                    "latency_ms": 1,
                    "quality": {"passed": True, "score": 85},
                },
            ]
        )

        self.assertEqual(summary["recommended"]["FALLBACK_SYNTH_MODEL"], "steady")


def load_model_registry_from_text(text: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "models.yaml"
        path.write_text(text, encoding="utf-8")
        return load_model_registry(path)


if __name__ == "__main__":
    unittest.main()
