import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reranker  # noqa: E402


class RerankerTests(unittest.TestCase):
    def test_sync_wrapper_returns_model_order(self):
        async def fake_rerank(query, passages, top_n):
            return [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.8},
            ]

        with patch.object(reranker.config, "RERANKER_ENABLED", True):
            with patch.object(reranker, "lightrag_rerank_func", fake_rerank):
                result = reranker.rerank("q", ["a", "b"])

        self.assertEqual(result, ["b", "a"])

    def test_sync_wrapper_returns_original_passages_when_disabled(self):
        with patch.object(reranker.config, "RERANKER_ENABLED", False):
            result = reranker.rerank("q", ["a", "b"])

        self.assertEqual(result, ["a", "b"])


class OpenRouterRerankerTests(unittest.IsolatedAsyncioTestCase):
    async def test_openrouter_request_and_response_contract(self):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "results": [
                        {"index": 1, "relevance_score": 0.95},
                        {"index": 0, "relevance_score": 0.75},
                    ]
                }

        class FakeClient:
            def __init__(self, *, timeout):
                captured["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url, *, json, headers):
                captured.update(url=url, payload=json, headers=headers)
                return FakeResponse()

        with (
            patch.object(reranker.config, "RERANKER_BASE_URL", "https://openrouter.ai/api/v1"),
            patch.object(reranker.config, "RERANKER_MODEL", "voyageai/rerank-2.5"),
            patch.object(reranker.config, "RERANKER_API_KEY", "test-key"),
            patch.object(reranker.httpx, "AsyncClient", FakeClient),
        ):
            result = await reranker._rerank_openrouter_async("query", ["a", "b"], 2)

        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/rerank")
        self.assertEqual(
            captured["payload"],
            {
                "model": "voyageai/rerank-2.5",
                "query": "query",
                "documents": ["a", "b"],
                "top_n": 2,
            },
        )
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(result[0], {"index": 1, "relevance_score": 0.95})

    async def test_enabled_openrouter_call_updates_success_stats(self):
        async def fake_call(query, documents, top_n):
            return [{"index": 0, "relevance_score": 0.9}]

        reranker.reset_reranker_stats()
        with (
            patch.object(reranker.config, "RERANKER_ENABLED", True),
            patch.object(reranker.config, "RERANKER_PROVIDER", "openrouter"),
            patch.object(reranker, "_rerank_openrouter_async", fake_call),
        ):
            result = await reranker.lightrag_rerank_func("q", ["a"], 1)

        stats = reranker.get_reranker_stats()
        self.assertEqual(result, [{"index": 0, "relevance_score": 0.9}])
        self.assertEqual(stats["attempted"], 1)
        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(stats["failed"], 0)

    async def test_empty_response_falls_back_and_records_failure(self):
        async def fake_call(query, documents, top_n):
            return []

        reranker.reset_reranker_stats()
        with (
            patch.object(reranker.config, "RERANKER_ENABLED", True),
            patch.object(reranker.config, "RERANKER_PROVIDER", "openrouter"),
            patch.object(reranker, "_rerank_openrouter_async", fake_call),
        ):
            result = await reranker.lightrag_rerank_func("q", ["a", "b"], 1)

        stats = reranker.get_reranker_stats()
        self.assertEqual(result, [{"index": 0, "relevance_score": 1.0}])
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["last_error_type"], "ValueError")

    def test_result_validation_rejects_duplicate_or_out_of_range_indexes(self):
        with self.assertRaisesRegex(ValueError, "duplicated"):
            reranker._validated_results(
                [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.8},
                ],
                document_count=2,
                top_n=2,
            )
        with self.assertRaisesRegex(ValueError, "invalid"):
            reranker._validated_results(
                [{"index": 2, "relevance_score": 0.9}],
                document_count=2,
                top_n=1,
            )
