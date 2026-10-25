import asyncio as py_asyncio
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli_app  # noqa: E402
import cli_pipeline  # noqa: E402
import cli_query  # noqa: E402
import main  # noqa: E402


class _FakeRag:
    def __init__(self):
        self.finalized = False

    async def finalize_storages(self):
        self.finalized = True


class MainQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_query_exits_nonzero_when_rag_returns_no_answer(self):
        rag = _FakeRag()
        output = io.StringIO()

        with patch.object(cli_query, "create_rag", AsyncMock(return_value=rag)):
            with patch.object(cli_query, "query_rag_result", AsyncMock(return_value={"llm_response": {"content": None}})):
                with patch("sys.stdout", output):
                    with self.assertRaises(SystemExit) as ctx:
                        await cli_app.cmd_query("test question")

        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(rag.finalized)
        self.assertIn("Query failed: LightRAG returned no answer.", output.getvalue())

    async def test_cmd_query_prints_sources_only_when_requested(self):
        rag = _FakeRag()
        output = io.StringIO()
        result = {
            "llm_response": {"content": "Answer body"},
            "data": {
                "references": [
                    {"reference_id": "ref-1", "file_path": str(Path("D:/topic/1.txt").resolve(strict=False))}
                ]
            },
        }
        source_index = {
            str(Path("D:/topic/1.txt").resolve(strict=False)): {
                "post_url": "https://t.me/example/1",
                "channel_name": "Topic",
                "date": "2026-04-30 12:00",
            }
        }

        with patch.object(cli_query, "create_rag", AsyncMock(return_value=rag)):
            with patch.object(cli_query, "query_rag_result", AsyncMock(return_value=result)):
                with patch.object(cli_query, "load_source_metadata_index", return_value=source_index):
                    with patch("sys.stdout", output):
                        await cli_app.cmd_query("Откуда эта информация? Дай ссылку")

        text = output.getvalue()
        self.assertTrue(rag.finalized)
        self.assertIn("Answer body", text)
        self.assertIn("Источники:", text)
        self.assertIn("https://t.me/example/1", text)

    async def test_cmd_query_skips_sources_when_not_requested(self):
        rag = _FakeRag()
        output = io.StringIO()
        result = {
            "llm_response": {"content": "Answer body"},
            "data": {"references": [{"reference_id": "ref-1", "file_path": "D:/topic/1.txt"}]},
        }

        with patch.object(cli_query, "create_rag", AsyncMock(return_value=rag)):
            with patch.object(cli_query, "query_rag_result", AsyncMock(return_value=result)):
                with patch("sys.stdout", output):
                    await cli_app.cmd_query("Что в базе говорится про AfD?")

        text = output.getvalue()
        self.assertTrue(rag.finalized)
        self.assertIn("Answer body", text)
        self.assertNotIn("Источники:", text)

    def test_extract_query_sources_prefers_cited_references(self):
        result = {
            "llm_response": {"content": "Answer\n\n### References\n- [2] Some cited chunk"},
            "data": {
                "references": [
                    {"reference_id": "ref-a", "file_path": str(Path("D:/topic/1.txt").resolve(strict=False))},
                    {"reference_id": "ref-b", "file_path": str(Path("D:/topic/2.txt").resolve(strict=False))},
                ]
            },
        }
        source_index = {
            str(Path("D:/topic/1.txt").resolve(strict=False)): {"post_url": "https://t.me/example/1"},
            str(Path("D:/topic/2.txt").resolve(strict=False)): {"post_url": "https://t.me/example/2"},
        }

        with patch.object(cli_query, "load_source_metadata_index", return_value=source_index):
            sources = cli_query._extract_query_sources(result)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["post_url"], "https://t.me/example/2")

    def test_extract_query_sources_accepts_explicit_reference_id_tokens(self):
        result = {
            "llm_response": {"content": "Answer cites [card-2]."},
            "data": {
                "references": [
                    {"reference_id": "card-1", "file_path": str(Path("D:/topic/1.txt").resolve(strict=False))},
                    {"reference_id": "card-2", "file_path": str(Path("D:/topic/2.txt").resolve(strict=False))},
                ]
            },
        }
        source_index = {
            str(Path("D:/topic/1.txt").resolve(strict=False)): {"post_url": "https://t.me/example/1"},
            str(Path("D:/topic/2.txt").resolve(strict=False)): {"post_url": "https://t.me/example/2"},
        }

        with patch.object(cli_query, "load_source_metadata_index", return_value=source_index):
            sources = cli_query._extract_query_sources(result)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["post_url"], "https://t.me/example/2")

    def test_extract_query_sources_uses_direct_reference_metadata(self):
        result = {
            "llm_response": {"content": "Answer\n\n### References\n- [1] Direct source"},
            "data": {
                "references": [
                    {
                        "reference_id": "direct-1",
                        "file_path": "output/normalized/test/10.txt",
                        "post_url": "https://t.me/c/1/10",
                        "channel": "Test",
                        "date": "2026-05-27T00:00:00+00:00",
                    }
                ]
            },
        }

        with patch.object(cli_query, "load_source_metadata_index", return_value={}):
            sources = cli_query._extract_query_sources(result)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["post_url"], "https://t.me/c/1/10")
        self.assertEqual(sources[0]["channel"], "Test")

    def test_extract_query_sources_reads_adjacent_meta_when_index_is_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "topic" / "148.txt"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text("Body", encoding="utf-8")
            source_path.with_suffix(".meta.json").write_text(
                json.dumps(
                    {
                        "post_url": "https://t.me/c/3328128766/148",
                        "channel_name": "Hungary",
                        "date": "2026-04-10T16:41:09+00:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = {
                "llm_response": {"content": "Answer\n\n### References\n- [1] Source"},
                "data": {"references": [{"reference_id": "card-1", "file_path": str(source_path)}]},
            }

            with patch.object(cli_query, "load_source_metadata_index", return_value={}):
                sources = cli_query._extract_query_sources(result)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["post_url"], "https://t.me/c/3328128766/148")
        self.assertEqual(sources[0]["channel"], "Hungary")

    def test_mark_contiguous_processed_stops_at_first_failed_message(self):
        messages = [
            SimpleNamespace(channel_id=1, channel_name="Channel", message_id=101),
            SimpleNamespace(channel_id=1, channel_name="Channel", message_id=102),
            SimpleNamespace(channel_id=1, channel_name="Channel", message_id=103),
        ]

        with patch.object(cli_pipeline, "mark_message_processed") as mark:
            count = cli_pipeline._mark_contiguous_processed(messages, {101, 103})

        self.assertEqual(count, 1)
        mark.assert_called_once_with(1, "Channel", 101)

    def test_mark_contiguous_processed_does_not_advance_when_first_message_failed(self):
        messages = [
            SimpleNamespace(channel_id=1, channel_name="Channel", message_id=101),
            SimpleNamespace(channel_id=1, channel_name="Channel", message_id=102),
        ]

        with patch.object(cli_pipeline, "mark_message_processed") as mark:
            count = cli_pipeline._mark_contiguous_processed(messages, {102})

        self.assertEqual(count, 0)
        mark.assert_not_called()

    async def test_cmd_run_stops_after_enrich_and_local_index_refresh(self):
        calls = []

        async def fake_fetch(limit=None):
            calls.append("fetch")
            return []

        def fake_enrich():
            calls.append("enrich")
            return SimpleNamespace()

        async def forbidden_load(*_args, **_kwargs):
            calls.append("load")

        with patch.object(cli_pipeline, "cmd_fetch", side_effect=fake_fetch):
            with patch.object(cli_pipeline, "cmd_enrich", side_effect=fake_enrich):
                with patch.object(cli_pipeline, "cmd_load", side_effect=forbidden_load):
                    with patch.object(cli_pipeline, "_print_pipeline_summary"):
                        await cli_pipeline.cmd_run()

        self.assertEqual(calls, ["fetch", "enrich"])

    def test_maybe_launch_reviewer_opens_web_when_pending_and_enabled(self):
        with patch.object(cli_pipeline, "get_pending_reviews", return_value=[{"id": "review-1"}]):
            with patch.object(cli_pipeline.config, "AUTO_OPEN_REVIEWER_AFTER_RUN", True, create=True):
                with patch.object(cli_pipeline, "launch_reviewer_web") as launch:
                    with patch("sys.stdout", io.StringIO()):
                        cli_pipeline._maybe_launch_reviewer()

        launch.assert_called_once()

    def test_run_pipeline_review_command_launches_web_reviewer(self):
        script = Path("run_pipeline.ps1").read_text(encoding="utf-8")

        self.assertIn('Invoke-Python main.py review --web', script)

    def test_main_query_cli_joins_full_question_and_mode(self):
        captured = {}
        original_run = py_asyncio.run

        async def fake_cmd_query(question, mode, query_profile=None):
            captured["question"] = question
            captured["mode"] = mode
            captured["query_profile"] = query_profile

        def fake_run(coro):
            original_run(coro)

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_query", fake_cmd_query):
                with patch.object(cli_app.asyncio, "run", side_effect=fake_run):
                    with patch.object(
                        sys,
                        "argv",
                        ["main.py", "query", "Откуда", "в", "базе", "тезис", "hybrid"],
                    ):
                        main.main()

        self.assertEqual(captured.get("question"), "Откуда в базе тезис")
        self.assertEqual(captured.get("mode"), "hybrid")
        self.assertIsNone(captured.get("query_profile"))

    def test_main_query_cli_parses_query_profile(self):
        captured = {}
        original_run = py_asyncio.run

        async def fake_cmd_query(question, mode, query_profile=None):
            captured["question"] = question
            captured["mode"] = mode
            captured["query_profile"] = query_profile

        def fake_run(coro):
            original_run(coro)

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_query", fake_cmd_query):
                with patch.object(cli_app.asyncio, "run", side_effect=fake_run):
                    with patch.object(
                        sys,
                        "argv",
                        ["main.py", "query", "Откуда", "это?", "mix", "source"],
                    ):
                        main.main()

        self.assertEqual(captured.get("question"), "Откуда это?")
        self.assertEqual(captured.get("mode"), "mix")
        self.assertEqual(captured.get("query_profile"), "source")

    def test_main_query_cli_uses_configured_default_when_mode_is_omitted(self):
        captured = {}
        original_run = py_asyncio.run

        async def fake_cmd_query(question, mode, query_profile=None):
            captured["question"] = question
            captured["mode"] = mode
            captured["query_profile"] = query_profile

        def fake_run(coro):
            original_run(coro)

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_query", fake_cmd_query):
                with patch.object(cli_app.asyncio, "run", side_effect=fake_run):
                    with patch.object(
                        sys,
                        "argv",
                        ["main.py", "query", "Ð§Ñ‚Ð¾", "Ð²", "Ð±Ð°Ð·Ðµ", "Ð¿Ñ€Ð¾", "ÐšÑƒÐ±Ñƒ"],
                    ):
                        main.main()

        self.assertEqual(captured.get("question"), "Ð§Ñ‚Ð¾ Ð² Ð±Ð°Ð·Ðµ Ð¿Ñ€Ð¾ ÐšÑƒÐ±Ñƒ")
        self.assertEqual(captured.get("mode"), cli_app._default_query_mode())
        self.assertIsNone(captured.get("query_profile"))

    def test_main_rebuild_cli_uses_explicit_enriched_graph_command(self):
        captured = {"called": False}
        original_run = py_asyncio.run

        async def fake_cmd_rebuild():
            captured["called"] = True

        def fake_run(coro):
            original_run(coro)

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_rebuild", fake_cmd_rebuild):
                with patch.object(cli_app.asyncio, "run", side_effect=fake_run):
                    with patch.object(sys, "argv", ["main.py", "rebuild"]):
                        main.main()

        self.assertTrue(captured["called"])

    def test_main_load_cli_defaults_to_enriched_graph(self):
        captured = {"called": False}
        original_run = py_asyncio.run

        async def fake_cmd_load():
            captured["called"] = True
            return cli_pipeline.LoadStats()

        def fake_run(coro):
            return original_run(coro)

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_load", fake_cmd_load):
                with patch.object(cli_app, "_print_load_summary"):
                    with patch.object(cli_app.asyncio, "run", side_effect=fake_run):
                        with patch.object(sys, "argv", ["main.py", "load"]):
                            main.main()

        self.assertTrue(captured["called"])

    def test_main_fts_search_cli_parses_flags(self):
        captured = {}

        def fake_cmd_fts_search(query, top_k=10, compare_shadow=False):
            captured["query"] = query
            captured["top_k"] = top_k
            captured["compare_shadow"] = compare_shadow

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_fts_search", fake_cmd_fts_search):
                with patch.object(
                    sys,
                    "argv",
                    [
                        "main.py",
                        "fts",
                        "search",
                        "Trump",
                        "Orban",
                        "--top-k",
                        "3",
                        "--compare-shadow",
                    ],
                ):
                    main.main()

        self.assertEqual(captured.get("query"), "Trump Orban")
        self.assertEqual(captured.get("top_k"), 3)
        self.assertTrue(captured.get("compare_shadow"))

    def test_main_fts_search_cli_all_passes_unbounded_limit(self):
        captured = {}

        def fake_cmd_fts_search(query, top_k=10, compare_shadow=False):
            captured["query"] = query
            captured["top_k"] = top_k

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_fts_search", fake_cmd_fts_search):
                with patch.object(sys, "argv", ["main.py", "fts", "search", "Cuba", "--all"]):
                    main.main()

        self.assertEqual(captured, {"query": "Cuba", "top_k": None})

    def test_fts_search_cli_rejects_all_with_top_k(self):
        parser = cli_app.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["fts", "search", "Cuba", "--all", "--top-k", "3"])

    def test_main_fts_rebuild_cli_dispatches(self):
        captured = {}

        def fake_cmd_fts_rebuild():
            captured["called"] = True

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_fts_rebuild", fake_cmd_fts_rebuild):
                with patch.object(sys, "argv", ["main.py", "fts", "rebuild"]):
                    main.main()

        self.assertTrue(captured.get("called"))

    def test_main_registry_resolve_cli_passes_source_id(self):
        captured = {}

        def fake_cmd_registry_resolve(source_id):
            captured["source_id"] = source_id

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_registry_resolve", fake_cmd_registry_resolve):
                with patch.object(
                    sys,
                    "argv",
                    ["main.py", "registry", "resolve", "telegram:1:10"],
                ):
                    main.main()

        self.assertEqual(captured.get("source_id"), "telegram:1:10")

    def test_main_registry_rebuild_cli_dispatches(self):
        captured = {}

        def fake_cmd_registry_rebuild():
            captured["called"] = True

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_registry_rebuild", fake_cmd_registry_rebuild):
                with patch.object(sys, "argv", ["main.py", "registry", "rebuild"]):
                    main.main()

        self.assertTrue(captured.get("called"))

    def test_main_transcribe_backfill_cli_parses_flags(self):
        captured = {}

        def fake_cmd_transcribe_backfill(limit=3, channel=None, media_type=None, dry_run=False):
            captured["limit"] = limit
            captured["channel"] = channel
            captured["media_type"] = media_type
            captured["dry_run"] = dry_run

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_transcribe_backfill", fake_cmd_transcribe_backfill):
                with patch.object(
                    sys,
                    "argv",
                    [
                        "main.py",
                        "transcribe",
                        "backfill",
                        "--limit",
                        "2",
                        "--channel",
                        "Channel",
                        "--media-type",
                        "voice",
                        "--dry-run",
                    ],
                ):
                    main.main()

        self.assertEqual(captured.get("limit"), 2)
        self.assertEqual(captured.get("channel"), "Channel")
        self.assertEqual(captured.get("media_type"), "voice")
        self.assertTrue(captured.get("dry_run"))

    def test_main_validate_enriched_cli_parses_fail_flag(self):
        captured = {}

        def fake_cmd_validate_enriched(fail_on_error=False):
            captured["fail_on_error"] = fail_on_error

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_validate_enriched", fake_cmd_validate_enriched):
                with patch.object(sys, "argv", ["main.py", "validate", "enriched", "--fail-on-error"]):
                    main.main()

        self.assertTrue(captured.get("fail_on_error"))

    def test_main_experiments_index_cli_dispatches(self):
        captured = {}

        def fake_cmd_experiments_index():
            captured["called"] = True

        with patch.object(cli_app, "cmd_experiments_index", fake_cmd_experiments_index):
            with patch.object(sys, "argv", ["main.py", "experiments", "index"]):
                main.main()

        self.assertTrue(captured.get("called"))

    def test_wiki_run_parser_accepts_paths_profile_and_no_luna(self):
        args = cli_app.build_parser().parse_args(
            [
                "wiki",
                "run",
                "first.enriched.json",
                "second.enriched.json",
                "--llm-profile",
                "luna",
                "--no-luna",
            ]
        )

        self.assertEqual(args.command, "wiki")
        self.assertEqual(args.subcommand, "run")
        self.assertEqual(
            args.paths,
            ["first.enriched.json", "second.enriched.json"],
        )
        self.assertEqual(args.llm_profile, "luna")
        self.assertTrue(args.no_luna)

    @patch.object(cli_app.config, "WIKI_ENABLED", True)
    def test_main_wiki_run_dispatches_and_opens_pending_review(self):
        captured = {}
        stats = SimpleNamespace(review_counts=SimpleNamespace(total=3))

        def fake_cmd_wiki_run(paths, *, use_luna=None):
            captured["paths"] = paths
            captured["use_luna"] = use_luna
            return stats

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_wiki_run", fake_cmd_wiki_run):
                with patch.object(cli_app, "_maybe_launch_reviewer") as launch:
                    with patch.object(
                        sys,
                        "argv",
                        [
                            "main.py",
                            "wiki",
                            "run",
                            "card.enriched.json",
                            "--no-luna",
                        ],
                    ):
                        main.main()

        self.assertEqual(captured["paths"], ["card.enriched.json"])
        self.assertFalse(captured["use_luna"])
        launch.assert_called_once_with()

    @patch.object(cli_app.config, "WIKI_ENABLED", True)
    def test_main_wiki_search_joins_query_and_passes_limit(self):
        captured = {}

        def fake_cmd_wiki_search(query, *, limit=12):
            captured["query"] = query
            captured["limit"] = limit

        with patch.object(cli_app, "setup_logging"):
            with patch.object(cli_app, "cmd_wiki_search", fake_cmd_wiki_search):
                with patch.object(
                    sys,
                    "argv",
                    [
                        "main.py",
                        "wiki",
                        "search",
                        "Китай",
                        "ракеты",
                        "--limit",
                        "4",
                    ],
                ):
                    main.main()

        self.assertEqual(captured, {"query": "Китай ракеты", "limit": 4})

    def test_main_wiki_commands_are_blocked_when_master_switch_is_off(self):
        with patch.object(cli_app.config, "WIKI_ENABLED", False):
            with patch.object(cli_app, "setup_logging"):
                with patch.object(cli_app, "cmd_wiki_run") as wiki_run:
                    with patch.object(
                        sys,
                        "argv",
                        ["main.py", "wiki", "run", "card.enriched.json"],
                    ):
                        main.main()

        wiki_run.assert_not_called()
