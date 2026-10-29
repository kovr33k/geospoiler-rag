import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import loader as loader_package  # noqa: E402
import loader.lightrag_loader as lightrag_loader  # noqa: E402
from loader import clients as lightrag_clients  # noqa: E402
from loader import query as lightrag_query  # noqa: E402
from loader.answer_postprocess import (  # noqa: E402
    _answer_looks_corrupt,
    _postprocess_answer_text,
)
from loader.card_context import (  # noqa: E402
    _attach_card_context,
    _card_context_for_query,
    _card_fact_lines,
    _content_query_terms,
    _shadow_fallback_result,
    _synthesize_shadow_fallback_result,
)
from loader.clients import _chat_completion_options  # noqa: E402
from loader.entity_merge import plan_safe_entity_merges  # noqa: E402
from loader.extraction import PROMPTS, _postprocess_extraction_response  # noqa: E402
from loader.ingest import _existing_processed_matches, load_texts  # noqa: E402
from loader.profiles import get_query_profile  # noqa: E402
from loader.query import _synthesize_hybrid_result, query_rag_result  # noqa: E402
from loader.reference_hints import _attach_reference_hints  # noqa: E402
from loader.storage import (  # noqa: E402
    _doc_status_value,
    _remove_source_metadata_index,
    _source_doc_id,
    _sync_source_metadata_index,
    _wait_for_doc_terminal_status,
    load_source_metadata_index,
    rebuild_rag_storage,
)
from retrieval import shadow_search  # noqa: E402
from retrieval.source_registry import rebuild_source_registry  # noqa: E402


def _v2_provenance(
    root: Path,
    source_path: Path,
    *,
    source_id: str | None = None,
    channel: str | None = None,
    **extra,
) -> dict:
    return {
        "source_id": source_id or f"test:{source_path.parent.name}:{source_path.stem}",
        "normalized_path": str(source_path.relative_to(root)),
        "channel": channel or source_path.parent.name,
        **extra,
    }


class LightragLoaderFacadeTests(unittest.TestCase):
    def test_doc_status_enum_uses_persisted_value(self):
        status = SimpleNamespace(value="processed")
        self.assertEqual(_doc_status_value({"status": status}), "processed")

    def test_doc_status_dotted_value_uses_terminal_name(self):
        self.assertEqual(_doc_status_value({"status": "docstatus.processed"}), "processed")

    def test_loader_package_documents_explicit_import_policy(self):
        doc = loader_package.__doc__ or ""

        self.assertIn("explicit imports", doc.casefold())
        self.assertIn("lightrag_loader", doc)

    def test_facade_only_exposes_public_loader_api(self):
        visible_names = {
            name
            for name in vars(lightrag_loader)
            if not name.startswith("__")
        }

        self.assertEqual(visible_names, set(lightrag_loader.__all__))

    def test_facade_does_not_export_experimental_enriched_loader(self):
        self.assertNotIn("load_from_enriched", lightrag_loader.__all__)
        self.assertFalse(hasattr(lightrag_loader, "load_from_enriched"))


class QueryRagWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_rag_returns_content_from_query_rag_result(self):
        calls = []

        async def fake_query_rag_result(rag, question, mode=None, query_profile=None):
            calls.append(
                {
                    "rag": rag,
                    "question": question,
                    "mode": mode,
                    "query_profile": query_profile,
                }
            )
            return {
                "response": "Fallback response",
                "llm_response": {"content": "Structured answer"},
                "data": {"references": []},
            }

        rag = object()
        with patch.object(lightrag_query, "query_rag_result", side_effect=fake_query_rag_result):
            with patch.object(lightrag_query, "_try_shadow_fallback_result", return_value=None):
                answer = await lightrag_query.query_rag(
                    rag,
                    "What is in the corpus?",
                    mode="hybrid",
                    query_profile="source",
                )

        self.assertEqual(answer, "Structured answer")
        self.assertEqual(
            calls,
            [
                {
                    "rag": rag,
                    "question": "What is in the corpus?",
                    "mode": "hybrid",
                    "query_profile": "source",
                }
            ],
        )


class _FakeDocStatus:
    def __init__(self, existing_ids=None):
        self._existing_ids = {doc_id: {"status": "processed"} for doc_id in (existing_ids or [])}

    async def get_by_id(self, doc_id: str):
        return self._existing_ids.get(doc_id)

    def remove(self, doc_id: str):
        self._existing_ids.pop(doc_id, None)


class _FakeRag:
    def __init__(self, existing_ids=None):
        self.doc_status = _FakeDocStatus(existing_ids)
        self.deleted = []
        self.inserted = []

    async def adelete_by_doc_id(self, doc_id: str):
        self.deleted.append(doc_id)
        self.doc_status.remove(doc_id)
        return SimpleNamespace(status="success", message="deleted")

    async def ainsert(self, texts, ids=None, file_paths=None):
        self.inserted.append(
            {
                "texts": texts,
                "ids": ids,
                "file_paths": file_paths,
            }
        )
        for doc_id in ids or []:
            self.doc_status._existing_ids[doc_id] = {"status": "processed"}


class _ReusableFakeRag(_FakeRag):
    def __init__(self, doc_id: str, content: str):
        super().__init__([doc_id])

        async def get_by_id(requested_id):
            return {"content": content} if requested_id == doc_id else None

        self.full_docs = SimpleNamespace(get_by_id=get_by_id)


class _HangingRag(_FakeRag):
    async def ainsert(self, texts, ids=None, file_paths=None):
        self.inserted.append(
            {
                "texts": texts,
                "ids": ids,
                "file_paths": file_paths,
            }
        )
        for doc_id in ids or []:
            self.doc_status._existing_ids[doc_id] = {"status": "processed"}
        await asyncio.Event().wait()


class _FailedStatusRag(_FakeRag):
    async def ainsert(self, texts, ids=None, file_paths=None):
        await super().ainsert(texts, ids=ids, file_paths=file_paths)
        for doc_id in ids or []:
            self.doc_status._existing_ids[doc_id] = {"status": "failed"}


class _FailedStatusBasenameRag(_FakeRag):
    async def ainsert(self, texts, ids=None, file_paths=None):
        await super().ainsert(texts, ids=ids, file_paths=file_paths)
        for doc_id in ids or []:
            self.doc_status._existing_ids[doc_id] = {
                "status": "failed",
                "file_path": "wrong-basename.txt",
            }


class LoadTextsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self._rag_storage_temp = tempfile.TemporaryDirectory()
        self._rag_storage_patch = patch.object(
            config,
            "RAG_STORAGE_DIR",
            Path(self._rag_storage_temp.name) / "rag_storage",
        )
        self._rag_storage_patch.start()

    def tearDown(self):
        self._rag_storage_patch.stop()
        self._rag_storage_temp.cleanup()
        super().tearDown()

    async def test_load_texts_uses_stable_path_based_doc_ids(self):
        path = str((Path(__file__).resolve().parents[1] / "output" / "normalized" / "topic" / "1.txt").resolve())
        rag = _FakeRag()

        # Legacy layout: a local data/.layout-v2.json must not remap this path.
        with patch("geospoiler.config.DATA_LAYOUT_VERSION", 1):
            inserted = await load_texts(rag, [(path, "hello world")], batch_size=5)
            expected_id = _source_doc_id(path)

        self.assertEqual(inserted, 1)
        self.assertEqual(rag.deleted, [])
        self.assertEqual(len(rag.inserted), 1)
        self.assertEqual(rag.inserted[0]["ids"], [expected_id])
        self.assertEqual(rag.inserted[0]["file_paths"], [path])
        self.assertEqual(rag.inserted[0]["texts"], ["hello world"])

    async def test_load_texts_disambiguates_duplicate_normalized_basenames(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "duplicate_basename_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        (temp_root / "normalized" / "alpha").mkdir(parents=True, exist_ok=True)
        (temp_root / "normalized" / "beta").mkdir(parents=True, exist_ok=True)

        try:
            first = temp_root / "normalized" / "alpha" / "21.txt"
            second = temp_root / "normalized" / "beta" / "21.txt"
            first.write_text("Alpha body", encoding="utf-8")
            second.write_text("Beta body", encoding="utf-8")
            rag_storage_dir = temp_root / "rag_storage"
            rag = _FakeRag()

            with patch.object(config, "NORMALIZED_DIR", temp_root / "normalized"):
                with patch.object(config, "RAG_STORAGE_DIR", rag_storage_dir):
                    inserted = await load_texts(
                        rag,
                        [(str(first), "Alpha body"), (str(second), "Beta body")],
                        batch_size=5,
                    )

            self.assertEqual(inserted, 2)
            file_paths = [item["file_paths"][0] for item in rag.inserted]
            self.assertEqual(len(set(file_paths)), 2)
            self.assertTrue(all(Path(path).name.startswith("__geospoiler__doc-") for path in file_paths))
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    async def test_load_texts_strips_headers_and_placeholders_before_insert(self):
        path = str((Path(__file__).resolve().parents[1] / "output" / "normalized" / "topic" / "3.txt").resolve())
        rag = _FakeRag()
        text = (
            "[Канал: Topic | Дата: 2026-04-30 12:00 | Пост: https://t.me/example/3]\n\n"
            "Useful body.\n\n"
            "[Видео: пост содержал видео - не обработано]\n"
            "[Аудио: пост содержал аудио - не обработано | status=downloaded | path=media_cache/topic/audio/msg_3.ogg]\n"
            "[AI-диалог: https://chatgpt.com/share/abc]\n"
            "[Внешняя ссылка: https://example.com/story]\n"
            "[Малоинформативный пост: Uninformative post]\n"
            "[Отправлено в очередь на ручной просмотр: Channel_3_external.json]\n"
        )

        inserted = await load_texts(rag, [(path, text)], batch_size=5)

        self.assertEqual(inserted, 1)
        self.assertEqual(rag.inserted[0]["texts"], ["Useful body."])

    async def test_load_texts_strips_long_instagram_reel_review_wrappers(self):
        path = str((Path(__file__).resolve().parents[1] / "output" / "normalized" / "topic" / "5.txt").resolve())
        rag = _FakeRag()
        text = (
            "[\u041a\u0430\u043d\u0430\u043b: Topic | \u0414\u0430\u0442\u0430: 2026-04-30 12:00 | "
            "\u041f\u043e\u0441\u0442: https://t.me/example/5]\n\n"
            "[Instagram Reel: https://www.instagram.com/reel/ABC/ - @source]\n\n"
            "Useful caption.\n\n"
            "[\u0414\u043b\u0438\u043d\u043d\u044b\u0439 Instagram Reel: https://www.instagram.com/reel/ABC/]\n"
            "[\u041e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e \u0432 \u043e\u0447\u0435\u0440\u0435\u0434\u044c "
            "\u043d\u0430 \u0440\u0443\u0447\u043d\u043e\u0439 \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440: "
            "Channel_5_instagram_long_reel.json]\n"
        )

        inserted = await load_texts(rag, [(path, text)], batch_size=5)

        self.assertEqual(inserted, 1)
        self.assertEqual(rag.inserted[0]["texts"], ["Useful caption."])


    async def test_load_texts_writes_source_metadata_index(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "source_index_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            rag_storage_dir = temp_root / "rag_storage"
            rag_storage_dir.mkdir(parents=True, exist_ok=True)
            topic_dir = temp_root / "normalized" / "topic"
            topic_dir.mkdir(parents=True, exist_ok=True)

            txt_path = topic_dir / "4.txt"
            txt_path.write_text("Body", encoding="utf-8")
            meta_path = topic_dir / "4.meta.json"
            meta_path.write_text(
                json.dumps({"post_url": "https://t.me/example/4", "channel_name": "Topic"}, ensure_ascii=False),
                encoding="utf-8",
            )

            rag = _FakeRag()
            with patch.object(config, "RAG_STORAGE_DIR", rag_storage_dir):
                inserted = await load_texts(rag, [(str(txt_path), "Body")], batch_size=5)

            self.assertEqual(inserted, 1)
            index_path = rag_storage_dir / "doc_metadata_index.sqlite"
            self.assertTrue(index_path.exists())
            with patch.object(config, "RAG_STORAGE_DIR", rag_storage_dir):
                data = load_source_metadata_index()
            self.assertEqual(len(data), 1)
            only_value = next(iter(data.values()))
            self.assertEqual(only_value["post_url"], "https://t.me/example/4")
            self.assertEqual(only_value["channel_name"], "Topic")
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    async def test_reused_processed_document_keeps_canonical_provenance_mapping(self):
        temp_root = Path(self._rag_storage_temp.name)
        normalized_root = temp_root / "normalized"
        topic_dir = normalized_root / "topic"
        topic_dir.mkdir(parents=True, exist_ok=True)
        txt_path = topic_dir / "102.txt"
        txt_path.write_text("Body", encoding="utf-8")
        canonical_path = str(txt_path.resolve())
        rag = _ReusableFakeRag(_source_doc_id(canonical_path), "Body")

        with patch.object(config, "NORMALIZED_DIR", normalized_root):
            reused = await _existing_processed_matches(
                rag,
                canonical_path,
                "Body",
            )

        self.assertTrue(reused)
        metadata = load_source_metadata_index()
        self.assertTrue(metadata)
        self.assertTrue(
            all(value.get("canonical_path") == canonical_path for value in metadata.values())
        )

    def test_source_metadata_index_migrates_legacy_json_to_sqlite(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "source_index_migration_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            rag_storage_dir = temp_root / "rag_storage"
            rag_storage_dir.mkdir(parents=True, exist_ok=True)
            source_path = str((temp_root / "normalized" / "topic" / "4.txt").resolve())
            legacy_index = {
                source_path: {
                    "post_url": "https://t.me/example/4",
                    "channel_name": "Topic",
                }
            }
            (rag_storage_dir / "doc_metadata_index.json").write_text(
                json.dumps(legacy_index, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(config, "RAG_STORAGE_DIR", rag_storage_dir):
                data = load_source_metadata_index()

            self.assertEqual(data, legacy_index)
            self.assertTrue((rag_storage_dir / "doc_metadata_index.sqlite").exists())
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_source_metadata_index_removes_sqlite_row(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "source_index_remove_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            rag_storage_dir = temp_root / "rag_storage"
            rag_storage_dir.mkdir(parents=True, exist_ok=True)
            source_path = str((temp_root / "normalized" / "topic" / "4.txt").resolve())

            with patch.object(config, "RAG_STORAGE_DIR", rag_storage_dir):
                _sync_source_metadata_index(source_path, {"post_url": "https://t.me/example/4"})
                self.assertIn(source_path, load_source_metadata_index())
                _remove_source_metadata_index(source_path)
                data = load_source_metadata_index()

            self.assertEqual(data, {})
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    async def test_load_texts_replaces_existing_document_for_same_path(self):
        path = str((Path(__file__).resolve().parents[1] / "output" / "normalized" / "topic" / "2.txt").resolve())
        existing_doc_id = _source_doc_id(path)
        rag = _FakeRag(existing_ids=[existing_doc_id])

        inserted = await load_texts(rag, [(path, "updated text")], batch_size=5)

        self.assertEqual(inserted, 1)
        self.assertEqual(rag.deleted, [existing_doc_id])
        self.assertEqual(rag.inserted[0]["ids"], [existing_doc_id])
        self.assertEqual(rag.inserted[0]["texts"], ["updated text"])

    async def test_load_texts_skips_timed_out_document_and_writes_report(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "timeout_insert_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            rag_storage_dir = temp_root / "rag_storage"
            rag_storage_dir.mkdir(parents=True, exist_ok=True)
            path = str((temp_root / "normalized" / "topic" / "slow.txt").resolve())
            rag = _HangingRag()

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "RAG_STORAGE_DIR", rag_storage_dir):
                    with patch.object(config, "RAG_INSERT_TIMEOUT_SECONDS", 0.01):
                        inserted = await load_texts(rag, [(path, "slow body")], batch_size=1)

            self.assertEqual(inserted, 0)
            self.assertEqual(rag.deleted, [_source_doc_id(path)])
            report_path = temp_root / "artifacts" / "rag_insert_skipped.md"
            self.assertTrue(report_path.exists())
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("insert timeout after", report)
            self.assertIn("slow.txt", report)
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    async def test_wait_for_doc_terminal_status_fails_closed_when_missing(self):
        with self.assertRaises(TimeoutError):
            await _wait_for_doc_terminal_status(
                _FakeRag(),
                "missing-doc",
                timeout_seconds=0,
            )

    async def test_load_texts_rejects_missing_lightrag_status(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "missing_status_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            rag_storage_dir = temp_root / "rag_storage"
            rag_storage_dir.mkdir(parents=True, exist_ok=True)
            path = str((temp_root / "normalized" / "topic" / "missing.txt").resolve())
            rag = _FakeRag()

            async def missing_status(*args, **kwargs):
                return None

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "RAG_STORAGE_DIR", rag_storage_dir):
                    with patch("loader.ingest._wait_for_doc_terminal_status", side_effect=missing_status):
                        inserted = await load_texts(rag, [(path, "body")], batch_size=1)

            self.assertEqual(inserted, 0)
            self.assertEqual(rag.deleted, [_source_doc_id(path)])
            report = (temp_root / "artifacts" / "rag_insert_skipped.md").read_text(encoding="utf-8")
            self.assertIn("status missing", report)
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)
    async def test_load_texts_skips_lightrag_failed_status(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "failed_status_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            rag_storage_dir = temp_root / "rag_storage"
            rag_storage_dir.mkdir(parents=True, exist_ok=True)
            path = str((temp_root / "normalized" / "topic" / "failed.txt").resolve())
            rag = _FailedStatusRag()

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "RAG_STORAGE_DIR", rag_storage_dir):
                    inserted = await load_texts(rag, [(path, "body")], batch_size=1)

            self.assertEqual(inserted, 0)
            self.assertEqual(rag.deleted, [_source_doc_id(path)])
            report_path = temp_root / "artifacts" / "rag_insert_skipped.md"
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("LightRAG marked document as failed", report)
            self.assertIn("failed.txt", report)
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    async def test_late_failed_status_cleans_attempted_canonical_path_not_status_basename(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "failed_status_basename_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)
        try:
            path = str((temp_root / "normalized" / "topic" / "failed.txt").resolve())
            rag = _FailedStatusBasenameRag()
            with patch("loader.ingest._remove_source_metadata_index") as remove_index:
                inserted = await load_texts(rag, [(path, "body")], batch_size=1)
            self.assertEqual(inserted, 0)
            remove_index.assert_called_once_with(path)
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)



class QueryProfileTests(unittest.TestCase):
    def test_answer_profile_uses_top_k_15(self):
        profile = get_query_profile("answer")

        self.assertEqual(profile["top_k"], 15)

    def test_source_profile_uses_top_k_15(self):
        profile = get_query_profile("source")

        self.assertEqual(profile["top_k"], 15)

    def test_overview_profile_is_explicit_top_k_30(self):
        profile = get_query_profile("overview")

        self.assertEqual(profile["top_k"], 30)


class ChatCompletionOptionsTests(unittest.TestCase):
    def test_chat_completion_options_uses_reasoning_when_configured(self):
        with patch.multiple(
            config,
            LLM_REASONING_EFFORT="low",
        ):
            options = _chat_completion_options(max_tokens=2048)

        self.assertEqual(options["reasoning_effort"], "low")

    def test_chat_completion_options_disables_deepseek_v4_thinking_by_default(self):
        with patch.multiple(
            config,
            LLM_BASE_URL="https://api.deepseek.com",
            LLM_MODEL="deepseek-v4-flash",
            RAG_BUILD_BASE_URL="https://api.deepseek.com",
            RAG_BUILD_MODEL="deepseek-v4-flash",
            QUERY_BASE_URL="https://api.deepseek.com",
            QUERY_MODEL="deepseek-v4-flash",
            FALLBACK_SYNTH_BASE_URL="https://api.deepseek.com",
            FALLBACK_SYNTH_MODEL="deepseek-v4-flash",
            LLM_REASONING_EFFORT="",
        ):
            options = _chat_completion_options(max_tokens=2048)

        self.assertEqual(options["extra_body"], {"thinking": {"type": "disabled"}})

    def test_embedding_input_type_detects_nvidia_embed_models(self):
        with patch.multiple(
            config,
            EMBEDDING_BASE_URL="https://integrate.api.nvidia.com/v1",
            EMBEDDING_MODEL="nvidia/llama-nemotron-embed-vl-1b-v2",
        ):
            self.assertTrue(lightrag_clients._needs_embedding_input_type())

        with patch.multiple(
            config,
            EMBEDDING_BASE_URL="https://api.openai.com/v1",
            EMBEDDING_MODEL="text-embedding-3-large",
        ):
            self.assertFalse(lightrag_clients._needs_embedding_input_type())

    def test_default_embedding_input_type_tracks_lightrag_role(self):
        self.assertEqual(lightrag_clients._default_embedding_input_type(), "query")

        token = lightrag_clients._LLM_ROLE.set("build")
        try:
            self.assertEqual(lightrag_clients._default_embedding_input_type(), "passage")
        finally:
            lightrag_clients._LLM_ROLE.reset(token)

    def test_embedding_dimensions_override_tracks_nvidia_endpoint(self):
        with patch.multiple(
            config,
            EMBEDDING_BASE_URL="https://integrate.api.nvidia.com/v1",
            EMBEDDING_DIM=1024,
        ):
            self.assertEqual(lightrag_clients._embedding_dimensions_override(), 1024)

        with patch.multiple(
            config,
            EMBEDDING_BASE_URL="https://api.openai.com/v1",
            EMBEDDING_DIM=3072,
        ):
            self.assertIsNone(lightrag_clients._embedding_dimensions_override())

    def test_embedding_client_is_lazy_and_config_keyed(self):
        self.assertFalse(hasattr(lightrag_clients, "_embed_client"))
        self.assertFalse(hasattr(lightrag_clients, "_embed_semaphore"))

        with patch.object(lightrag_clients, "AsyncOpenAI", autospec=True) as openai_cls:
            first_client = object()
            second_client = object()
            openai_cls.side_effect = [first_client, second_client]
            with patch.multiple(
                config,
                EMBEDDING_API_KEY="first-key",
                EMBEDDING_BASE_URL="https://first.example/v1",
                EMBEDDING_TIMEOUT_SECONDS=11,
            ):
                first = lightrag_clients._get_embed_client()
                again = lightrag_clients._get_embed_client()
            with patch.multiple(
                config,
                EMBEDDING_API_KEY="second-key",
                EMBEDDING_BASE_URL="https://second.example/v1",
                EMBEDDING_TIMEOUT_SECONDS=22,
            ):
                second = lightrag_clients._get_embed_client()

        self.assertIs(first, again)
        self.assertIsNot(first, second)
        self.assertIs(first, first_client)
        self.assertIs(second, second_client)
        self.assertEqual(openai_cls.call_count, 2)

    def test_embedding_semaphore_is_lazy_and_config_keyed(self):
        with patch.object(config, "EMBEDDING_CONCURRENCY", 2):
            first = lightrag_clients._get_embed_semaphore()
            again = lightrag_clients._get_embed_semaphore()
        with patch.object(config, "EMBEDDING_CONCURRENCY", 3):
            second = lightrag_clients._get_embed_semaphore()

        self.assertIs(first, again)
        self.assertIsNot(first, second)


class ShadowFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self._runtime_config_patch = patch.multiple(
            config,
            LATE_FUSION_ENABLED=False,
            LLM_PROFILE="current",
        )
        self._runtime_config_patch.start()

    def tearDown(self):
        self._runtime_config_patch.stop()
        super().tearDown()

    def test_content_query_terms_drop_source_request_wording(self):
        terms = shadow_search.query_terms("Трамп реально поддерживал Орбана? Дай источник.")
        content_terms = _content_query_terms(terms, shadow_search)

        self.assertIn("трамп", content_terms)
        self.assertIn("орбана", content_terms)
        self.assertIn("поддерживал", content_terms)
        self.assertNotIn("реально", content_terms)
        self.assertNotIn("дай", content_terms)

    def test_shadow_fallback_matches_inflected_russian_terms(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "shadow_fallback_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            enriched_dir = temp_root / "output" / "enriched" / "Куба"
            normalized_dir = temp_root / "output" / "normalized" / "Куба"
            enriched_dir.mkdir(parents=True, exist_ok=True)
            normalized_dir.mkdir(parents=True, exist_ok=True)

            source_path = normalized_dir / "8.txt"
            source_path.write_text("США провели переговоры с Кубой в Гаване.", encoding="utf-8")
            card = {
                "schema_version": "enriched_v2",
                "summary": "США провели тайные переговоры с Кубой в Гаване.",
                "key_points": [{"text": "Это первые прямые переговоры США и Кубы за 10 лет.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, source_path),
                "search_text": "США провели переговоры с Кубой в Гаване.",
            }
            (enriched_dir / "8.enriched.json").write_text(
                json.dumps(card, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    result = _shadow_fallback_result(
                        "Что в базе говорится о Кубе и переговорах с США?",
                        "answer",
                    )

            self.assertIsNotNone(result)
            answer = result["llm_response"]["content"]
            self.assertIn("Куб", answer)
            self.assertIn("переговор", answer)
            self.assertIn("8.txt", answer)
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_shadow_fallback_keeps_results_in_top_topic(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "shadow_topic_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            cuba_enriched = temp_root / "output" / "enriched" / "Cuba"
            cuba_normalized = temp_root / "output" / "normalized" / "Cuba"
            other_enriched = temp_root / "output" / "enriched" / "Other"
            other_normalized = temp_root / "output" / "normalized" / "Other"
            for directory in (cuba_enriched, cuba_normalized, other_enriched, other_normalized):
                directory.mkdir(parents=True, exist_ok=True)

            cuba_source = cuba_normalized / "5.txt"
            cuba_source.write_text("Cuba protests followed an economic crisis.", encoding="utf-8")
            other_source = other_normalized / "20.txt"
            other_source.write_text("Ultraright protests happened elsewhere.", encoding="utf-8")

            cuba_card = {
                "schema_version": "enriched_v2",
                "summary": "Cuba protests followed an economic crisis.",
                "key_points": [{"text": "The relevant item is about protests in Cuba.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, cuba_source),
                "search_text": "Cuba protests economic crisis",
            }
            other_card = {
                "schema_version": "enriched_v2",
                "summary": "Ultraright protests happened elsewhere.",
                "key_points": [{"text": "This card is not about Cuba.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, other_source),
                "search_text": "ultraright protests protests protests",
            }
            (cuba_enriched / "5.enriched.json").write_text(
                json.dumps(cuba_card, ensure_ascii=False),
                encoding="utf-8",
            )
            (other_enriched / "20.enriched.json").write_text(
                json.dumps(other_card, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    result = _shadow_fallback_result("Cuba protests", "answer")

            self.assertIsNotNone(result)
            answer = result["llm_response"]["content"]
            self.assertIn("Cuba", answer)
            self.assertIn("5.txt", answer)
            self.assertNotIn("ultraright", answer.casefold())
            self.assertNotIn("20.txt", answer)
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_card_context_treats_connection_word_as_generic_for_topic_ranking(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "shadow_connection_generic_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            ultra_enriched = temp_root / "output" / "enriched" / "Ultra"
            ultra_normalized = temp_root / "output" / "normalized" / "Ultra"
            other_enriched = temp_root / "output" / "enriched" / "Other"
            other_normalized = temp_root / "output" / "normalized" / "Other"
            for directory in (ultra_enriched, ultra_normalized, other_enriched, other_normalized):
                directory.mkdir(parents=True, exist_ok=True)

            ultra_source = ultra_normalized / "9.txt"
            ultra_source.write_text("Европейские ультраправых политиков связывают с Россией.", encoding="utf-8")
            other_source = other_normalized / "135.txt"
            other_source.write_text("Связь связь связь Венгрии с Россией.", encoding="utf-8")

            ultra_card = {
                "schema_version": "enriched_v2",
                "summary": "Европейские ультраправых политиков связывают с Россией.",
                "key_points": [{"text": "Ультраправых политиков связывают с Россией.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, ultra_source),
                "search_text": "ультраправых Россией",
            }
            other_card = {
                "schema_version": "enriched_v2",
                "summary": "Связь Венгрии с Россией.",
                "key_points": [{"text": "Связь связь связь с Россией.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, other_source),
                "search_text": "связь связь связь Россией",
            }
            (ultra_enriched / "9.enriched.json").write_text(
                json.dumps(ultra_card, ensure_ascii=False),
                encoding="utf-8",
            )
            (other_enriched / "135.enriched.json").write_text(
                json.dumps(other_card, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    result = _shadow_fallback_result(
                        "Что в базе говорится про связь ультраправых с Россией?",
                        "answer",
                    )

            self.assertIsNotNone(result)
            answer = result["llm_response"]["content"]
            self.assertIn("9.txt", answer)
            self.assertIn("ультраправ", answer.casefold())
            self.assertNotIn("135.txt", answer)
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_shadow_fallback_uses_normalized_text_for_thin_similarity_cards(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "shadow_thin_similarity_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            enriched_dir = temp_root / "output" / "enriched" / "Ultra"
            normalized_dir = temp_root / "output" / "normalized" / "Ultra"
            enriched_dir.mkdir(parents=True, exist_ok=True)
            normalized_dir.mkdir(parents=True, exist_ok=True)

            canonical_source = normalized_dir / "11.txt"
            canonical_source.write_text("Ультра-левые и ультра-правые совпадают.", encoding="utf-8")
            broad_source = normalized_dir / "20.txt"
            broad_source.write_text(
                "Урсула фон дер Ляйен вызывает ненависть у ультралевых и ультраправых сил.",
                encoding="utf-8",
            )

            thin_card = {
                "schema_version": "enriched_v2",
                "summary": "",
                "key_points": [],
                "quotes": [],
                "theses": [],
                "events": [],
                "provenance": _v2_provenance(temp_root, canonical_source),
                "search_text": "Источник: Ultra",
                "graph_text": "Источник: Ultra",
            }
            broad_card = {
                "schema_version": "enriched_v2",
                "summary": "Материал про совпадение идеологии ультраправых групп с джихадистами.",
                "key_points": [{"text": "Ультраправые группы совпадают с джихадистами по отдельным установкам.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, broad_source),
                "search_text": (
                    "[Канал: Ультра левые и ультра правые]\n\n"
                    "Ультраправые группы совпадают с джихадистами по отдельным установкам."
                ),
            }
            (enriched_dir / "11.enriched.json").write_text(
                json.dumps(thin_card, ensure_ascii=False),
                encoding="utf-8",
            )
            (enriched_dir / "20.enriched.json").write_text(
                json.dumps(broad_card, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    result = _shadow_fallback_result(
                        "Что в базе говорится о сходстве ультралевых и ультраправых?",
                        "answer",
                    )

            self.assertIsNotNone(result)
            references = result["data"]["references"]
            self.assertEqual(Path(references[0]["file_path"]).name, "11.txt")
            self.assertIn("совпадают", "\n".join(result["data"]["shadow_context"][0]["facts"]))
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_card_context_expands_afd_alias_before_ranking(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "afd_alias_context_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            direct_enriched = temp_root / "output" / "enriched" / "Ultra"
            broad_enriched = temp_root / "output" / "enriched" / "Hungary"
            direct_normalized = temp_root / "output" / "normalized" / "Ultra"
            broad_normalized = temp_root / "output" / "normalized" / "Hungary"
            for directory in (direct_enriched, broad_enriched, direct_normalized, broad_normalized):
                directory.mkdir(parents=True, exist_ok=True)

            direct_source = direct_normalized / "12.txt"
            direct_source.write_text("АдГ выступает против военной помощи Украине.", encoding="utf-8")
            direct_card = {
                "schema_version": "enriched_v2",
                "summary": "АдГ выступает против военной помощи Украине.",
                "key_points": [{"text": "АдГ выступает против военной помощи Украине.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, direct_source),
                "search_text": "АдГ войне Украине помощь",
            }
            (direct_enriched / "12.enriched.json").write_text(
                json.dumps(direct_card, ensure_ascii=False),
                encoding="utf-8",
            )
            for message_id in range(140, 150):
                broad_source = broad_normalized / f"{message_id}.txt"
                broad_source.write_text("Украине нужна помощь в войне.", encoding="utf-8")
                broad_card = {
                    "schema_version": "enriched_v2",
                    "summary": "Общий материал про войну и Украину без партии.",
                    "key_points": [{"text": "Войне в Украине посвящён общий материал без партии.", "type": "reported_statement", "importance": "high", "evidence": None}],
                    "provenance": _v2_provenance(temp_root, broad_source),
                    "search_text": "войне Украине войне Украине войне Украине",
                }
                (broad_enriched / f"{message_id}.enriched.json").write_text(
                    json.dumps(broad_card, ensure_ascii=False),
                    encoding="utf-8",
                )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    with patch.object(config, "HYBRID_QUERY_CARDS_ENABLED", True):
                        context = _card_context_for_query(
                            "Что в базе говорится про отношение AfD к войне в Украине?",
                            "answer",
                        )

            self.assertIsNotNone(context)
            self.assertEqual(Path(context["references"][0]["file_path"]).name, "12.txt")
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_shadow_fallback_uses_summary_and_key_points_for_regular_questions(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "shadow_visual_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            enriched_dir = temp_root / "output" / "enriched" / "Slovakia"
            normalized_dir = temp_root / "output" / "normalized" / "Slovakia"
            enriched_dir.mkdir(parents=True, exist_ok=True)
            normalized_dir.mkdir(parents=True, exist_ok=True)

            source_path = normalized_dir / "7.txt"
            source_path.write_text("Фицо стремится посетить Москву.", encoding="utf-8")
            card = {
                "schema_version": "enriched_v2",
                "summary": "Фицо стремится посетить Москву на фоне политического кризиса.",
                "key_points": [{"text": "Страны Балтии отказали самолёту Фицо в пролёте.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, source_path),
                "search_text": "Фицо Москва политический кризис самолёт карта Европы",
            }
            (enriched_dir / "7.enriched.json").write_text(
                json.dumps(card, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    result = _shadow_fallback_result(
                        "Что в базе говорится о Фицо и политическом кризисе?",
                        "answer",
                    )

            self.assertIsNotNone(result)
            answer = result["llm_response"]["content"]
            facts = result["data"]["shadow_context"][0]["facts"]
            self.assertEqual(facts, [card["summary"], card["key_points"][0]["text"]])
            self.assertIn("Фицо", answer)
            self.assertNotIn("B-roll", answer)
            self.assertNotIn("LightRAG не поднял", answer)
            self.assertNotIn("Точный поиск по карточкам", answer)
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_shadow_fallback_uses_summary_and_key_points_for_visual_questions(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "shadow_visual_requested_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            enriched_dir = temp_root / "output" / "enriched" / "Baltic"
            normalized_dir = temp_root / "output" / "normalized" / "Baltic"
            enriched_dir.mkdir(parents=True, exist_ok=True)
            normalized_dir.mkdir(parents=True, exist_ok=True)

            source_path = normalized_dir / "2.txt"
            source_path.write_text("Нарва и Эстония фигурируют в сценарии давления.", encoding="utf-8")
            card = {
                "schema_version": "enriched_v2",
                "summary": "Нарва упоминается в контексте давления на Эстонию.",
                "key_points": [{"text": "Ида-Вирумаа и Нарва выделяются как чувствительные регионы.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, source_path),
                "search_text": "Нарва Эстония визуалы карта",
            }
            (enriched_dir / "2.enriched.json").write_text(
                json.dumps(card, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    result = _shadow_fallback_result(
                        "Какие кадры или визуалы можно использовать для ролика про Нарву и Эстонию?",
                        "answer",
                    )

            self.assertIsNotNone(result)
            answer = result["llm_response"]["content"]
            facts = result["data"]["shadow_context"][0]["facts"]
            self.assertEqual(facts, [card["summary"], card["key_points"][0]["text"]])
            self.assertIn("Нарв", answer)
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_card_context_for_visual_query_keeps_focused_entity_source(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "visual_context_focus_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            enriched_dir = temp_root / "output" / "enriched" / "Baltic"
            normalized_dir = temp_root / "output" / "normalized" / "Baltic"
            enriched_dir.mkdir(parents=True, exist_ok=True)
            normalized_dir.mkdir(parents=True, exist_ok=True)

            direct_source = normalized_dir / "2.txt"
            broad_lithuania_source = normalized_dir / "9.txt"
            broad_baltic_source = normalized_dir / "6.txt"
            direct_source.write_text("Нарва и Эстония: прямой визуальный источник.", encoding="utf-8")
            broad_lithuania_source.write_text("Литва и страны Балтии: общий визуальный материал.", encoding="utf-8")
            broad_baltic_source.write_text("Общий балтийский сценарий с картой.", encoding="utf-8")

            cards = [
                (
                    "9.enriched.json",
                    broad_lithuania_source,
                    "Эстония визуалы кадры можно использовать для ролика Литва страны Балтии",
                    "Широкая балтийская карточка без прямой темы города.",
                ),
                (
                    "6.enriched.json",
                    broad_baltic_source,
                    "Эстония визуалы кадры можно использовать карта Балтии",
                    "Широкая балтийская карточка.",
                ),
                (
                    "2.enriched.json",
                    direct_source,
                    "Нарва Эстония визуалы карта",
                    "Прямая карточка про Нарву и Эстонию.",
                ),
            ]
            for filename, source_path, search_text, summary in cards:
                card = {
                    "schema_version": "enriched_v2",
                    "summary": summary,
                    "key_points": [{"text": summary, "type": "reported_statement", "importance": "high", "evidence": None}],
                    "provenance": _v2_provenance(temp_root, source_path),
                    "search_text": search_text,
                }
                (enriched_dir / filename).write_text(
                    json.dumps(card, ensure_ascii=False),
                    encoding="utf-8",
                )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    with patch.object(config, "HYBRID_QUERY_CARDS_ENABLED", True):
                        context = _card_context_for_query(
                            "Какие кадры или визуалы можно использовать для ролика про Нарву и Эстонию?",
                            "answer",
                        )

            self.assertIsNotNone(context)
            reference_paths = [Path(ref["file_path"]).name for ref in context["references"]]
            self.assertEqual(reference_paths, ["2.txt"])
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_card_context_prioritizes_specific_entity_terms_over_generic_overlap(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "entity_specificity_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            generic_enriched = temp_root / "output" / "enriched" / "Generic"
            direct_enriched = temp_root / "output" / "enriched" / "Ultra"
            generic_normalized = temp_root / "output" / "normalized" / "Generic"
            direct_normalized = temp_root / "output" / "normalized" / "Ultra"
            for directory in (generic_enriched, direct_enriched, generic_normalized, direct_normalized):
                directory.mkdir(parents=True, exist_ok=True)

            generic_source = generic_normalized / "9.txt"
            direct_source = direct_normalized / "12.txt"
            generic_source.write_text("Общий материал про отношение к войне в Украине.", encoding="utf-8")
            direct_source.write_text("AfD и война в Украине.", encoding="utf-8")

            generic_card = {
                "schema_version": "enriched_v2",
                "summary": "Отношение к войне в Украине без упоминания нужной партии.",
                "key_points": [{"text": "Отношение к войне в Украине описано общими словами.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, generic_source),
                "search_text": "отношение отношение отношение войне войне Украине Украине",
            }
            direct_card = {
                "schema_version": "enriched_v2",
                "summary": "AfD выступает против военной помощи Украине.",
                "key_points": [{"text": "AfD выступает против военной помощи Украине.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, direct_source),
                "search_text": "AfD войне Украине",
            }
            (generic_enriched / "9.enriched.json").write_text(
                json.dumps(generic_card, ensure_ascii=False),
                encoding="utf-8",
            )
            (direct_enriched / "12.enriched.json").write_text(
                json.dumps(direct_card, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    with patch.object(config, "HYBRID_QUERY_CARDS_ENABLED", True):
                        context = _card_context_for_query(
                            "Что в базе говорится про отношение AfD к войне в Украине?",
                            "answer",
                        )

            self.assertIsNotNone(context)
            self.assertEqual(Path(context["references"][0]["file_path"]).name, "12.txt")
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_card_context_for_query_returns_card_references(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "hybrid_card_context_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            enriched_dir = temp_root / "output" / "enriched" / "Hungary"
            normalized_dir = temp_root / "output" / "normalized" / "Hungary"
            enriched_dir.mkdir(parents=True, exist_ok=True)
            normalized_dir.mkdir(parents=True, exist_ok=True)

            source_path = normalized_dir / "148.txt"
            source_path.write_text("Trump supported Orban in Hungary.", encoding="utf-8")
            card = {
                "schema_version": "enriched_v2",
                "summary": "Trump publicly supported Orban before Hungarian elections.",
                "key_points": [{"text": "The post frames this as explicit political support.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(temp_root, source_path),
                "search_text": "Trump Orban Hungary explicit political support election",
            }
            (enriched_dir / "148.enriched.json").write_text(
                json.dumps(card, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    with patch.object(config, "HYBRID_QUERY_CARDS_ENABLED", True):
                        context = _card_context_for_query("Trump Orban Hungary", "answer")

            self.assertIsNotNone(context)
            self.assertEqual(context["references"][0]["reference_id"], "card-1")
            self.assertIn("148.txt", context["references"][0]["file_path"])
            self.assertIn("explicit political support", "\n".join(context["shadow_context"][0]["facts"]))
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_card_context_references_use_source_registry_passport(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "hybrid_card_context_registry_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            enriched_dir = temp_root / "output" / "enriched" / "Hungary"
            normalized_dir = temp_root / "output" / "normalized" / "Hungary"
            enriched_dir.mkdir(parents=True, exist_ok=True)
            normalized_dir.mkdir(parents=True, exist_ok=True)

            source_path = normalized_dir / "148.txt"
            source_path.write_text("Trump supported Orban in Hungary.", encoding="utf-8")
            card = {
                "schema_version": "enriched_v2",
                "summary": "Trump publicly supported Orban before Hungarian elections.",
                "key_points": [{"text": "The post frames this as explicit political support.", "type": "reported_statement", "importance": "high", "evidence": None}],
                "provenance": _v2_provenance(
                    temp_root,
                    source_path,
                    source_id="telegram:1:148",
                    channel="Hungary",
                    message_id=148,
                    date="2026-05-27T00:00:00+00:00",
                    post_url="https://t.me/c/1/148",
                ),
                "search_text": "Trump Orban Hungary explicit political support election",
            }
            (enriched_dir / "148.enriched.json").write_text(
                json.dumps(card, ensure_ascii=False),
                encoding="utf-8",
            )
            registry_db = temp_root / "state" / "source_registry.sqlite"
            rebuild_source_registry(
                normalized_dir=temp_root / "output" / "normalized",
                enriched_dir=temp_root / "output" / "enriched",
                db_path=registry_db,
            )

            with patch.object(config, "PROJECT_ROOT", temp_root):
                with patch.object(config, "ENRICHED_DIR", temp_root / "output" / "enriched"):
                    with patch.object(config, "SOURCE_REGISTRY_DB_PATH", registry_db):
                        with patch.object(config, "HYBRID_QUERY_CARDS_ENABLED", True):
                            context = _card_context_for_query("Trump Orban Hungary", "answer")

            self.assertIsNotNone(context)
            reference = context["references"][0]
            self.assertEqual(reference["source_id"], "telegram:1:148")
            self.assertEqual(reference["post_url"], "https://t.me/c/1/148")
            self.assertEqual(reference["channel"], "Hungary")
            self.assertEqual(reference["date"], "2026-05-27T00:00:00+00:00")
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)

    def test_attach_card_context_merges_references_without_duplicates(self):
        result = {
            "response": "Answer",
            "llm_response": {"content": "Answer"},
            "data": {"references": [{"reference_id": "1", "file_path": "D:/topic/1.txt"}]},
        }
        card_context = {
            "references": [
                {"reference_id": "card-1", "file_path": "D:/topic/1.txt"},
                {"reference_id": "card-2", "file_path": "D:/topic/2.txt"},
            ],
            "shadow_context": [{"reference_id": "card-2", "file_path": "D:/topic/2.txt", "facts": ["Fact"]}],
        }

        fixed = _attach_card_context(result, card_context)

        self.assertEqual(len(fixed["data"]["references"]), 2)
        self.assertEqual(fixed["data"]["references"][1]["reference_id"], "card-2")
        self.assertEqual(fixed["data"]["shadow_context"], card_context["shadow_context"])

    def test_attach_card_context_can_prioritize_card_references(self):
        result = {
            "response": "Answer",
            "llm_response": {"content": "Answer"},
            "data": {
                "references": [
                    {"reference_id": "graph-1", "file_path": "D:/topic/1.txt"},
                    {"reference_id": "graph-2", "file_path": "D:/topic/2.txt"},
                ]
            },
        }
        card_context = {
            "references": [
                {"reference_id": "card-1", "file_path": "D:/topic/1.txt"},
                {"reference_id": "card-2", "file_path": "D:/topic/3.txt"},
            ],
            "shadow_context": [{"reference_id": "card-1", "file_path": "D:/topic/1.txt", "facts": ["Fact"]}],
        }

        fixed = _attach_card_context(result, card_context, prefer_card_references=True)

        reference_ids = [item["reference_id"] for item in fixed["data"]["references"]]
        self.assertEqual(reference_ids, ["card-1", "card-2", "graph-2"])

    def test_card_reference_first_rule_is_named_and_documented(self):
        self.assertTrue(lightrag_query._card_references_should_be_first("Any question", "answer"))
        doc = lightrag_query._card_references_should_be_first.__doc__ or ""
        self.assertIn("card references", doc.casefold())
        self.assertFalse(hasattr(lightrag_query, "_should_prefer_card_references"))

    def test_card_fact_lines_preserve_claim_type_as_reader_visible_tags(self):
        card = {
            "key_points": [
                {"text": "Plain source fact.", "type": "reported_statement", "importance": "high", "evidence": None},
                {"text": "Claimed by the source.", "type": "reported_statement", "importance": "high", "evidence": None},
                {"text": "Analytical hypothesis.", "type": "reported_statement", "importance": "medium", "evidence": None},
                {"text": "Quoted wording.", "type": "reported_statement", "importance": "high", "evidence": None},
                {"text": "Thesis wording.", "type": "reported_statement", "importance": "high", "evidence": None},
                {"text": "Unknown cautious wording.", "type": "reported_statement", "importance": "high", "evidence": None},
            ]
        }

        facts = _card_fact_lines(card, limit=6)

        self.assertEqual(facts[0], "Plain source fact.")
        self.assertEqual(facts[1], "Claimed by the source.")
        self.assertEqual(facts[2], "Analytical hypothesis.")
        self.assertEqual(facts[3], "Quoted wording.")
        self.assertEqual(facts[4], "Thesis wording.")
        self.assertEqual(facts[5], "Unknown cautious wording.")

    async def test_hybrid_synthesis_can_attach_context_without_second_llm(self):
        result = {
            "response": "Graph answer",
            "llm_response": {"content": "Graph answer"},
            "data": {"references": []},
        }
        card_context = {
            "references": [{"reference_id": "card-1", "file_path": "D:/topic/1.txt"}],
            "shadow_context": [{"reference_id": "card-1", "file_path": "D:/topic/1.txt", "facts": ["Card fact"]}],
        }

        with patch.object(config, "HYBRID_SYNTH_ENABLED", False):
            fixed = await _synthesize_hybrid_result("Question", "answer", result, card_context)

        self.assertEqual(fixed["llm_response"]["content"], "Graph answer")
        self.assertEqual(fixed["data"]["references"][0]["reference_id"], "card-1")

    async def test_shadow_synthesis_prompt_requires_naming_confirmed_question_subject(self):
        calls = []

        class FakeCompletions:
            async def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="Синтезированный ответ про ультраправых и Россию.")
                        )
                    ]
                )

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        fallback = {
            "response": "Card answer",
            "llm_response": {"content": "Card answer"},
            "data": {
                "references": [{"reference_id": "card-1", "file_path": "D:/topic/103.txt"}],
                "shadow_context": [
                    {
                        "reference_id": "card-1",
                        "file_path": "D:/topic/103.txt",
                        "facts": ["Источник описывает связи ультраправых политических сил с Россией."],
                    }
                ],
            },
            "fallback": "shadow_search",
        }

        with patch.multiple(
            config,
            HYBRID_SYNTH_ENABLED=True,
            FALLBACK_SYNTH_API_KEY="test-key",
            FALLBACK_SYNTH_BASE_URL="https://example.invalid/v1",
            FALLBACK_SYNTH_MODEL="test-model",
            FALLBACK_SYNTH_MAX_TOKENS=4096,
            QUERY_DELAY_SECONDS=0,
            FALLBACK_SYNTH_TIMEOUT_SECONDS=5,
            LLM_TIMEOUT_SECONDS=5,
        ):
            with patch.object(lightrag_clients, "AsyncOpenAI", FakeClient):
                fixed = await _synthesize_shadow_fallback_result(
                    "Что в базе говорится про связь ультраправых с Россией?",
                    "answer",
                    fallback,
                )

        self.assertEqual(fixed["fallback"], "shadow_search_llm")
        system_prompt = calls[0]["messages"][0]["content"]
        self.assertIn("ключевой предмет вопроса", system_prompt.casefold())
        self.assertIn("[утверждение источника]", system_prompt)
        self.assertIn("[гипотеза/оценка источника]", system_prompt)
        self.assertIn("не выводи сами теги", system_prompt.casefold())
        self.assertIn("формулировку пользователя", system_prompt.casefold())

    async def test_shadow_synthesis_overview_prompt_preserves_country_region_terms(self):
        calls = []

        class FakeCompletions:
            async def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="Синтезированный обзор: Россия, Германия, Италия.")
                        )
                    ]
                )

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        fallback = {
            "response": "Card answer",
            "llm_response": {"content": "Card answer"},
            "data": {
                "references": [{"reference_id": "card-1", "file_path": "D:/topic/4.txt"}],
                "shadow_context": [
                    {
                        "reference_id": "card-1",
                        "file_path": "D:/topic/4.txt",
                        "facts": [
                            "В контексте названы Россия, Германия, Италия и Европа как регион."
                        ],
                    }
                ],
            },
            "fallback": "shadow_search",
        }

        with patch.multiple(
            config,
            HYBRID_SYNTH_ENABLED=True,
            FALLBACK_SYNTH_API_KEY="test-key",
            FALLBACK_SYNTH_BASE_URL="https://example.invalid/v1",
            FALLBACK_SYNTH_MODEL="test-model",
            FALLBACK_SYNTH_MAX_TOKENS=4096,
            QUERY_DELAY_SECONDS=0,
            FALLBACK_SYNTH_TIMEOUT_SECONDS=5,
            LLM_TIMEOUT_SECONDS=5,
        ):
            with patch.object(lightrag_clients, "AsyncOpenAI", FakeClient):
                fixed = await _synthesize_shadow_fallback_result(
                    "Какие страны или регионы чаще всего фигурируют в теме ультраправых?",
                    "overview",
                    fallback,
                )

        self.assertEqual(fixed["fallback"], "shadow_search_llm")
        system_prompt = calls[0]["messages"][0]["content"].casefold()
        user_prompt = calls[0]["messages"][1]["content"].casefold()
        self.assertIn("страны", system_prompt)
        self.assertIn("регионы", system_prompt)
        self.assertIn("страны/регионы", user_prompt)

    async def test_query_rag_result_hybridizes_normal_lightrag_answer(self):
        class FakeQueryRag:
            async def aquery_llm(self, *args, **kwargs):
                return {
                    "response": "Graph answer about Orban.",
                    "llm_response": {"content": "Graph answer about Orban."},
                    "data": {"references": []},
                }

        card_context = {
            "references": [{"reference_id": "card-1", "file_path": "D:/topic/148.txt"}],
            "shadow_context": [{"reference_id": "card-1", "file_path": "D:/topic/148.txt", "facts": ["Card fact"]}],
        }

        async def fake_synth(question, query_profile, result, context):
            fixed = _attach_card_context(result, context)
            fixed["response"] = "Hybrid answer"
            fixed["llm_response"] = {"content": "Hybrid answer"}
            fixed["hybrid_context"] = "cards"
            return fixed

        with patch.object(lightrag_query, "_card_context_for_query", return_value=card_context):
            with patch.object(lightrag_query, "_synthesize_hybrid_result", side_effect=fake_synth):
                result = await query_rag_result(
                    FakeQueryRag(),
                    "Что в базе говорится про Трампа и Орбана?",
                    mode="hybrid",
                    query_profile="answer",
                )

        self.assertEqual(result["llm_response"]["content"], "Hybrid answer")
        self.assertEqual(result["hybrid_context"], "cards")
        self.assertEqual(result["data"]["references"][0]["reference_id"], "card-1")

    async def test_query_rag_result_uses_shadow_fallback_on_lightrag_error(self):
        class FailingQueryRag:
            async def aquery_llm(self, *args, **kwargs):
                raise RuntimeError("degraded endpoint")

        fallback = {
            "response": "Card fallback answer",
            "llm_response": {"content": "Card fallback answer"},
            "data": {"references": [{"reference_id": "card-1", "file_path": "D:/topic/1.txt"}]},
            "fallback": "shadow_search",
        }

        with patch.object(lightrag_query, "_shadow_fallback_result", return_value=fallback):
            with patch.object(config, "HYBRID_SYNTH_ENABLED", False):
                result = await query_rag_result(
                    FailingQueryRag(),
                    "Что в базе говорится про Трампа и Орбана?",
                    mode="hybrid",
                    query_profile="answer",
                )

        self.assertEqual(result["llm_response"]["content"], "Card fallback answer")
        self.assertEqual(result["fallback"], "shadow_search")

    async def test_shadow_fallback_helper_skips_funding_questions(self):
        with patch.object(lightrag_query, "_shadow_fallback_result") as fallback_mock:
            result = await lightrag_query._try_shadow_fallback_result(
                "Кто финансирует AfD?",
                "answer",
            )

        self.assertIsNone(result)
        fallback_mock.assert_not_called()


class AnswerPostprocessTests(unittest.TestCase):
    def test_postprocess_uses_explicit_absent_word_for_unanswered_funding_questions(self):
        answer = _postprocess_answer_text(
            "В контексте не указано, кто напрямую финансирует AfD.",
            "Кто финансирует AfD?",
            "answer",
        )

        self.assertIn("отсутств", answer.casefold())
        self.assertIn("нельзя определить", answer.casefold())

    def test_postprocess_handles_deepseek_funding_absence_wording(self):
        answer = _postprocess_answer_text(
            (
                "В предоставленной базе данных нет прямого ответа на вопрос о том, "
                "кто финансирует партию AfD. Имеющиеся сведения не содержат информации "
                "о конкретных источниках финансирования."
            ),
            "Кто финансирует AfD?",
            "answer",
        )

        self.assertIn("отсутств", answer.casefold())
        self.assertIn("нельзя определить", answer.casefold())

    def test_postprocess_handles_english_no_context_funding_answer(self):
        answer = _postprocess_answer_text(
            "Sorry, I'm not able to provide an answer to that question.[no-context]",
            "Кто финансирует AfD?",
            "answer",
        )

        self.assertIn("\u043e\u0442\u0441\u0443\u0442\u0441\u0442\u0432", answer.casefold())
        self.assertIn(
            "\u043d\u0435\u043b\u044c\u0437\u044f "
            "\u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c",
            answer.casefold(),
        )

    def test_postprocess_compacts_ultra_prefix_spelling(self):
        answer = _postprocess_answer_text(
            "Тезис про ультра-левых и ультра-правых дан в источнике.",
            "Откуда тезис про ультралевых и ультраправых?",
            "source",
        )

        self.assertIn("ультралев", answer.casefold())
        self.assertIn("ультраправ", answer.casefold())
        self.assertNotIn("ультра-лев", answer.casefold())

    def test_postprocess_neutralizes_trump_ultraright_unsupported_hedge(self):
        answer = _postprocess_answer_text(
            (
                "Как сообщается в отчете Bloomberg, это связано с тем, "
                "что Трамп якобы применил военную силу."
            ),
            "Что в базе говорится про связи европейских ультраправых с Трампом?",
            "answer",
        )

        self.assertNotIn("якобы", answer.casefold())
        self.assertIn("по утверждению источника", answer.casefold())
        self.assertIn("трамп", answer.casefold())

    def test_postprocess_neutralizes_donald_trump_hedge_without_breaking_name(self):
        answer = _postprocess_answer_text(
            "Дональд Трамп якобы угрожал аннексией Гренландии.",
            "Что в базе говорится про связи европейских ультраправых с Трампом?",
            "answer",
        )

        self.assertNotIn("якобы", answer.casefold())
        self.assertIn("дональд трамп", answer.casefold())
        self.assertNotIn("дональд по утверждению", answer.casefold())

    def test_postprocess_neutralizes_afd_leak_proof_wording(self):
        answer = _postprocess_answer_text(
            "Речь идет именно о подозрениях, а не о доказанных фактах.",
            "Что в базе говорится о риске утечки информации от AfD к России?",
            "answer",
        )

        self.assertNotIn("доказан", answer.casefold())
        self.assertIn("подтвержден", answer.casefold())
        self.assertIn("подозр", answer.casefold())

    def test_answer_looks_corrupt_detects_model_garbage(self):
        self.assertTrue(
            _answer_looks_corrupt(
                "контекстеmalloc успешного применения qqball ин‌م저 поддержки"
            )
        )
        self.assertFalse(
            _answer_looks_corrupt(
                "В базе говорится, что европейские ультраправые связаны с Трампом риторически."
            )
        )

    def test_postprocess_does_not_add_missing_ukraine_topic_fact(self):
        original = "AfD выглядит проблемной из-за подозрений в связях с Россией."
        answer = _postprocess_answer_text(
            original,
            "Почему в базе AfD выглядит проблемной партией?",
            "answer",
        )

        self.assertEqual(answer, original)
        self.assertNotIn("украин", answer.casefold())
        self.assertNotIn("помощ", answer.casefold())

    def test_postprocess_does_not_force_afd_alias_when_answer_uses_adg(self):
        original = "Партию «Альтернатива для Германии» (АдГ) подозревают в передаче данных России."
        answer = _postprocess_answer_text(
            original,
            "Что в базе говорится о риске утечки информации от AfD к России?",
            "answer",
        )

        self.assertEqual(answer, original)
        self.assertNotIn("afd", answer.casefold())
        self.assertIn("адг", answer.casefold())

    def test_postprocess_does_not_add_missing_economy_topic_label(self):
        original = "Автор говорит о Кубе и последствиях кризиса."
        answer = _postprocess_answer_text(
            original,
            "Какой главный тезис о Кубе и её экономике продвигает автор?",
            "answer",
        )

        self.assertEqual(answer, original)
        self.assertNotIn("экономика:", answer.casefold())

    def test_postprocess_does_not_add_missing_ultraright_topic_label(self):
        original = "В базе описаны политические связи с Россией."
        answer = _postprocess_answer_text(
            original,
            "Что в базе говорится про связь ультраправых с Россией?",
            "answer",
        )

        self.assertEqual(answer, original)
        self.assertNotIn("ультраправ", answer.casefold())
        self.assertIn("росси", answer.casefold())

    def test_postprocess_does_not_add_missing_russia_overview_fact(self):
        original = "Германия и Венгрия чаще всего фигурируют в теме ультраправых."
        answer = _postprocess_answer_text(
            original,
            "Какие страны или регионы чаще всего фигурируют в теме ультраправых?",
            "overview",
        )

        self.assertEqual(answer, original)
        self.assertNotIn("росси", answer.casefold())

    def test_postprocess_does_not_add_missing_germany_overview_fact(self):
        original = "Венгрия часто фигурирует в теме ультраправых."
        answer = _postprocess_answer_text(
            original,
            "Какие страны или регионы чаще всего фигурируют в теме ультраправых?",
            "overview",
        )

        self.assertEqual(answer, original)
        self.assertNotIn("герман", answer.casefold())
        self.assertNotIn("afd", answer.casefold())

    def test_postprocess_preserves_region_mentions_from_ultraright_overview(self):
        original = "Германия фигурирует часто. Молдова упоминается как побочный пример. Швеция тоже случайно попала."
        answer = _postprocess_answer_text(
            original,
            "Какие страны или регионы чаще всего фигурируют в теме ультраправых?",
            "overview",
        )

        self.assertEqual(answer, original)
        self.assertIn("герман", answer.casefold())
        self.assertIn("молдова", answer.casefold())
        self.assertIn("швеция", answer.casefold())

    def test_reference_hints_no_longer_inject_ultra_similarity_source(self):
        result = {
            "response": "Answer",
            "llm_response": {"content": "Answer"},
            "data": {
                "references": [
                    {"reference_id": "graph-1", "file_path": "D:/other/20.txt"},
                ]
            },
        }

        fixed = _attach_reference_hints(
            result,
            "Что в базе говорится о сходстве ультралевых и ультраправых?",
        )

        self.assertIs(fixed, result)
        self.assertEqual(fixed["data"]["references"], result["data"]["references"])


class ExtractionPostprocessTests(unittest.TestCase):
    def test_postprocess_drops_relations_that_reference_missing_entities(self):
        tuple_delimiter = PROMPTS["DEFAULT_TUPLE_DELIMITER"]
        completion_delimiter = PROMPTS["DEFAULT_COMPLETION_DELIMITER"]
        raw = "\n".join(
            [
                f"entity{tuple_delimiter}Alice Weidel{tuple_delimiter}person{tuple_delimiter}Leader of AfD.",
                f"relation{tuple_delimiter}Alice Weidel{tuple_delimiter}Western states{tuple_delimiter}criticizes{tuple_delimiter}Alice Weidel criticizes Western states.",
                completion_delimiter,
            ]
        )

        cleaned = _postprocess_extraction_response(raw)

        self.assertIn("Alice Weidel", cleaned)
        self.assertNotIn("Western states", cleaned)
        self.assertNotIn("relation", cleaned)

    def test_postprocess_sanitizes_delimiters_inside_fields(self):
        tuple_delimiter = PROMPTS["DEFAULT_TUPLE_DELIMITER"]
        completion_delimiter = PROMPTS["DEFAULT_COMPLETION_DELIMITER"]
        raw = "\n".join(
            [
                f"entity{tuple_delimiter}Western states{tuple_delimiter}other{tuple_delimiter}Led by Great Britain {tuple_delimiter} and opposing peace.",
                completion_delimiter,
            ]
        )

        cleaned = _postprocess_extraction_response(raw)

        self.assertIn("Western states", cleaned)
        self.assertIn("Led by Great Britain and opposing peace.", cleaned)
        self.assertEqual(cleaned.count(tuple_delimiter), 3)


class RebuildStorageTests(unittest.TestCase):
    def test_rebuild_rag_storage_moves_existing_files_to_backup_and_recreates_dir(self):
        temp_root = Path(__file__).resolve().parents[1] / ".tmp-tests" / "rebuild_rag_storage_case"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            root = temp_root
            storage_dir = root / "rag_storage"
            storage_dir.mkdir()
            original_file = storage_dir / "data.json"
            original_file.write_text("hello", encoding="utf-8")

            with patch.object(config, "PROJECT_ROOT", root):
                with patch.object(config, "RAG_STORAGE_DIR", storage_dir):
                    backup_path = rebuild_rag_storage()

            self.assertIsNotNone(backup_path)
            self.assertTrue(backup_path.exists())
            self.assertTrue((backup_path / "data.json").exists())
            self.assertTrue(storage_dir.exists())
            self.assertEqual(list(storage_dir.iterdir()), [])
        finally:
            if temp_root.exists():
                shutil.rmtree(temp_root)


class SafeEntityMergePlanTests(unittest.TestCase):
    def test_plan_safe_entity_merges_handles_case_only_duplicates(self):
        plans = plan_safe_entity_merges(["HAMAS", "Hamas", "Russia"])

        self.assertEqual(plans, [{"target": "Hamas", "sources": ["HAMAS"]}])

    def test_plan_safe_entity_merges_uses_explicit_alias_map(self):
        plans = plan_safe_entity_merges(["USA", "United States", "Russia"])

        self.assertEqual(plans, [{"target": "United States", "sources": ["USA"]}])
