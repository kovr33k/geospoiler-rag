import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cli_app
import cli_pipeline
import config
import llm_backend
import models
from enricher import llm_enricher
from loader import factory
from models import LLMPayload
from scripts import luna_smoke


class LlmBackendTests(unittest.TestCase):
    def setUp(self):
        self.original_profile = config.LLM_PROFILE
        self.original_model = config.CODEX_LUNA_MODEL
        self.original_reasoning_effort = config.CODEX_LUNA_REASONING_EFFORT
        self.original_runtime_dir = config.CODEX_RUNTIME_DIR
        self.original_regenerate = config.REGENERATE_ON_PROFILE_CHANGE
        self.original_concurrency = config.CODEX_LLM_MAX_CONCURRENCY
        self.original_fallback = config.CODEX_FALLBACK_TO_API

    def tearDown(self):
        config.LLM_PROFILE = self.original_profile
        config.CODEX_LUNA_MODEL = self.original_model
        config.CODEX_LUNA_REASONING_EFFORT = self.original_reasoning_effort
        config.CODEX_RUNTIME_DIR = self.original_runtime_dir
        config.REGENERATE_ON_PROFILE_CHANGE = self.original_regenerate
        config.CODEX_LLM_MAX_CONCURRENCY = self.original_concurrency
        config.CODEX_FALLBACK_TO_API = self.original_fallback

    def test_current_profile_uses_existing_model_identity(self):
        config.LLM_PROFILE = "current"
        self.assertFalse(llm_backend.is_luna_role("enrichment"))
        self.assertEqual(llm_backend.active_model_for("query"), config.QUERY_MODEL)

    def test_luna_profile_routes_text_roles_and_preserves_specialized_roles(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        config.CODEX_LUNA_REASONING_EFFORT = "xhigh"
        for role in llm_backend.TEXT_ROLES:
            self.assertTrue(llm_backend.is_luna_role(role))
            self.assertEqual(
                llm_backend.active_model_for(role),
                "codex-cli:codex-test-model@xhigh",
            )
        self.assertFalse(llm_backend.is_luna_role("transcription"))

    def test_profile_switch_does_not_force_regeneration_by_default(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        config.REGENERATE_ON_PROFILE_CHANGE = False
        self.assertFalse(llm_backend.model_change_requires_regeneration("old-api-model", "enrichment"))

    def test_missing_previous_model_still_requires_regeneration(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        self.assertTrue(llm_backend.model_change_requires_regeneration("", "enrichment"))

    def test_same_backend_model_change_keeps_valid_provenance(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        config.REGENERATE_ON_PROFILE_CHANGE = False
        self.assertFalse(llm_backend.model_change_requires_regeneration("codex-cli:previous", "enrichment"))

    def test_luna_requires_explicit_model(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = ""
        with self.assertRaises(llm_backend.LLMBackendError):
            llm_backend.validate_luna_configuration()

    def test_codex_command_is_ephemeral_read_only_and_schema_aware(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir)
            output = runtime / "last_message.txt"
            schema = runtime / "schema.json"
            with patch.object(llm_backend, "_resolve_codex_executable", return_value="codex.exe"):
                command = llm_backend._build_codex_command(runtime, output, schema)
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)
        self.assertIn("--output-schema", command)
        self.assertEqual(command[-1], "-")

    def test_codex_schema_requires_all_properties_and_removes_defaults(self):
        schema = LLMPayload.model_json_schema()

        prepared = llm_backend._prepare_codex_output_schema(schema)

        self.assertEqual(
            set(prepared["required"]),
            set(prepared["properties"]),
        )
        entities = prepared["$defs"]["Entities"]
        self.assertEqual(set(entities["required"]), set(entities["properties"]))
        self.assertNotIn("default", entities["properties"]["people"])
        entity_item = prepared["$defs"]["EntityItem"]
        self.assertEqual(set(entity_item["required"]), set(entity_item["properties"]))
        self.assertNotIn("default", entity_item["properties"]["role"])
        self.assertEqual(
            set(prepared["$defs"]["KeyPoint"]["properties"]["type"]["enum"]),
            set(models.ALLOWED_POINT_TYPES),
        )
        self.assertEqual(
            set(entity_item["properties"]["salience"]["enum"]),
            set(models.ALLOWED_SALIENCE),
        )
        self.assertEqual(
            set(prepared["properties"]["quality_flags"]["items"]["enum"]),
            set(models.ALLOWED_QUALITY_FLAGS),
        )

    def test_complete_json_reads_codex_final_message_without_network(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        with tempfile.TemporaryDirectory() as tmpdir:
            config.CODEX_RUNTIME_DIR = Path(tmpdir)
            with patch.object(llm_backend, "_resolve_codex_executable", return_value="codex.exe"), patch.object(
                llm_backend,
                "_run_codex",
                return_value='```json\n{"ok": true}\n```',
            ):
                result = llm_backend.complete_json_sync(
                    [{"role": "user", "content": "return json"}],
                    role="enrichment",
                    schema={"type": "object"},
                )
        self.assertEqual(result, {"ok": True})

    def test_media_analysis_stages_images_in_read_only_runtime(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config.CODEX_RUNTIME_DIR = root / "runtime"
            config.CODEX_RUNTIME_DIR.mkdir()
            image = root / "source.png"
            image.write_bytes(b"fake-png")
            observed = {}

            def fake_run(command, prompt, output_path, timeout):
                observed["command"] = command
                observed["prompt"] = prompt
                observed["staged"] = output_path.parent / "media_01.png"
                self.assertTrue(observed["staged"].exists())
                return "visual result"

            with patch.object(llm_backend, "_resolve_codex_executable", return_value="codex.exe"), patch.object(
                llm_backend, "_run_codex", side_effect=fake_run
            ):
                result = llm_backend.complete_media_analysis_sync(
                    [image], prompt="Extract visible text", timeout_seconds=10
                )

        self.assertEqual(result, "visual result")
        self.assertIn("media_01.png", observed["prompt"])
        self.assertIn("read-only", observed["prompt"])
        self.assertIn("read-only", observed["command"])
        self.assertFalse(observed["staged"].exists())

    def test_media_analysis_rejects_audio_files(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        with tempfile.TemporaryDirectory() as tmpdir:
            audio = Path(tmpdir) / "sound.mp3"
            audio.write_bytes(b"audio")
            with self.assertRaises(llm_backend.LLMBackendError):
                llm_backend.complete_media_analysis_sync([audio], prompt="inspect")

    def test_enrichment_role_uses_codex_without_api_key(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        with patch.object(
            llm_backend,
            "complete_json_sync",
            return_value={"summary": "from luna"},
        ) as complete:
            result = llm_enricher._call_llm("system", "user")
        self.assertEqual(result, {"summary": "from luna"})
        self.assertEqual(complete.call_args.kwargs["role"], "enrichment")

    def test_enrichment_retries_codex_timeout_once(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        with patch.object(
            llm_backend,
            "complete_json_sync",
            side_effect=[
                llm_backend.LLMBackendError("Codex CLI timed out after 300s"),
                {"summary": "После повтора результат получен."},
            ],
        ) as complete:
            result = llm_enricher._call_llm("system", "user")

        self.assertEqual(result, {"summary": "После повтора результат получен."})
        self.assertEqual(complete.call_count, 2)

    def test_cli_accepts_profile_after_command(self):
        args = cli_app.build_parser().parse_args(["enrich", "--llm-profile", "luna"])
        self.assertEqual(args.llm_profile, "luna")

    def test_reviewer_process_receives_effective_profile_and_codex_settings(self):
        with (
            patch.object(config, "LLM_PROFILE", "luna"),
            patch.object(config, "CODEX_CLI_PATH", "codex-test"),
            patch.object(config, "CODEX_LUNA_MODEL", "codex-test-model"),
            patch.object(config, "CODEX_LUNA_REASONING_EFFORT", "xhigh"),
            patch.object(config, "CODEX_LLM_TIMEOUT_SECONDS", 17.0),
            patch.object(config, "CODEX_LLM_MAX_CONCURRENCY", 2),
            patch.object(config, "CODEX_FALLBACK_TO_API", False),
            patch.object(config, "REGENERATE_ON_PROFILE_CHANGE", True),
            patch("subprocess.run") as run,
            patch("threading.Thread"),
            patch("builtins.print"),
        ):
            cli_pipeline.launch_reviewer_web()

        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["LLM_PROFILE"], "luna")
        self.assertEqual(child_env["CODEX_CLI_PATH"], "codex-test")
        self.assertEqual(child_env["CODEX_LUNA_MODEL"], "codex-test-model")
        self.assertEqual(child_env["CODEX_LUNA_REASONING_EFFORT"], "xhigh")
        self.assertEqual(child_env["CODEX_LLM_TIMEOUT_SECONDS"], "17.0")
        self.assertEqual(child_env["CODEX_LLM_MAX_CONCURRENCY"], "2")
        self.assertEqual(child_env["CODEX_FALLBACK_TO_API"], "false")
        self.assertEqual(child_env["REGENERATE_ON_PROFILE_CHANGE"], "true")

    @patch.dict(os.environ, {"GEOSPOILER_NO_NETWORK": "0"})
    def test_async_codex_runner_terminates_process_on_cancellation(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as tmpdir:
                pid_path = Path(tmpdir) / "child.pid"
                child_code = (
                    "import os, pathlib, sys, time; "
                    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                    "time.sleep(30)"
                )
                command = [
                    sys.executable,
                    "-c",
                    child_code,
                    str(pid_path),
                    "-C",
                    str(Path.cwd()),
                ]
                task = asyncio.create_task(
                    llm_backend._run_codex_async(command, "prompt", Path("missing-output.txt"))
                )
                pid = None
                for _ in range(100):
                    try:
                        raw_pid = pid_path.read_text(encoding="utf-8").strip()
                        pid = int(raw_pid) if raw_pid else None
                    except (OSError, ValueError):
                        pid = None
                    if pid is not None:
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(pid, "child process did not publish a valid PID")

                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                self.assertFalse(self._pid_is_running(pid))

        asyncio.run(exercise())

    @patch.dict(os.environ, {"GEOSPOILER_NO_NETWORK": "0"})
    def test_sync_codex_timeout_invokes_tree_cleanup(self):
        process = Mock()
        process.pid = 12345
        process.communicate.side_effect = subprocess.TimeoutExpired("codex", 1)
        command = [sys.executable, "-c", "", "-C", str(Path.cwd())]

        with patch.object(llm_backend.subprocess, "Popen", return_value=process), patch.object(
            llm_backend,
            "_terminate_process_tree",
        ) as terminate:
            with self.assertRaises(llm_backend.LLMBackendError):
                llm_backend._run_codex(command, "", Path("missing-output.txt"), timeout=1)

        terminate.assert_called_once_with(process)

    @patch.dict(os.environ, {"GEOSPOILER_NO_NETWORK": "0"})
    def test_sync_codex_cleanup_preserves_keyboard_interrupt(self):
        process = Mock()
        process.pid = 12345
        process.communicate.side_effect = KeyboardInterrupt
        command = [sys.executable, "-c", "", "-C", str(Path.cwd())]

        with patch.object(llm_backend.subprocess, "Popen", return_value=process), patch.object(
            llm_backend,
            "_terminate_process_tree",
        ) as terminate:
            with self.assertRaises(KeyboardInterrupt):
                llm_backend._run_codex(command, "", Path("missing-output.txt"), timeout=1)

        terminate.assert_called_once_with(process)

    def test_sync_termination_kills_parent_and_child(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir)
            parent_pid_path = runtime / "parent.pid"
            child_pid_path = runtime / "child.pid"
            child_code = (
                "import os, pathlib, sys, time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(30)"
            )
            parent_code = (
                "import os, pathlib, subprocess, sys, time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]]); "
                "time.sleep(30)"
            )
            command = [
                sys.executable,
                "-c",
                parent_code,
                str(parent_pid_path),
                str(child_pid_path),
                child_code,
                "-C",
                str(runtime),
            ]
            process_kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "cwd": str(runtime),
            }
            if os.name == "nt":
                process_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                process_kwargs["start_new_session"] = True

            process = subprocess.Popen(command, **process_kwargs)
            try:
                parent_pid = None
                child_pid = None
                for _ in range(150):
                    try:
                        raw_parent_pid = parent_pid_path.read_text(encoding="utf-8").strip()
                        raw_child_pid = child_pid_path.read_text(encoding="utf-8").strip()
                        parent_pid = int(raw_parent_pid) if raw_parent_pid else None
                        child_pid = int(raw_child_pid) if raw_child_pid else None
                    except (OSError, ValueError):
                        parent_pid = None
                        child_pid = None
                    if parent_pid is not None and child_pid is not None:
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(parent_pid, "parent process did not publish a valid PID")
                self.assertIsNotNone(child_pid, "child process did not publish a valid PID")

                llm_backend._terminate_process_tree(process)
                for _ in range(150):
                    if not self._pid_is_running(parent_pid) and not self._pid_is_running(child_pid):
                        break
                    time.sleep(0.02)
                self.assertFalse(self._pid_is_running(parent_pid))
                self.assertFalse(self._pid_is_running(child_pid))
            finally:
                if process.poll() is None:
                    llm_backend._terminate_process_tree(process)

    def test_async_cancellation_releases_semaphore_for_next_call(self):
        async def exercise():
            config.LLM_PROFILE = "luna"
            config.CODEX_LUNA_MODEL = "codex-test-model"
            config.CODEX_LLM_MAX_CONCURRENCY = 1
            entered = asyncio.Event()
            calls = 0

            async def fake_runner(_command, _prompt, _output_path):
                nonlocal calls
                calls += 1
                entered.set()
                await asyncio.Event().wait()

            with tempfile.TemporaryDirectory() as tmpdir, patch.object(
                llm_backend,
                "_resolve_codex_executable",
                return_value="codex.exe",
            ), patch.object(llm_backend, "_run_codex_async", side_effect=fake_runner):
                config.CODEX_RUNTIME_DIR = Path(tmpdir)
                first = asyncio.create_task(
                    llm_backend.complete_text_async(
                        [{"role": "user", "content": "first"}],
                        role="query",
                        timeout_seconds=10,
                    )
                )
                await asyncio.wait_for(entered.wait(), timeout=1)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first

                with patch.object(llm_backend, "_run_codex_async", return_value="second"):
                    result = await llm_backend.complete_text_async(
                        [{"role": "user", "content": "second"}],
                        role="query",
                        timeout_seconds=1,
                    )
                self.assertEqual(result, "second")
                self.assertEqual(calls, 1)

        asyncio.run(exercise())

    def test_async_timeout_while_waiting_for_semaphore(self):
        async def exercise():
            config.LLM_PROFILE = "luna"
            config.CODEX_LUNA_MODEL = "codex-test-model"
            config.CODEX_LLM_MAX_CONCURRENCY = 1
            entered = asyncio.Event()
            release = asyncio.Event()

            async def fake_runner(_command, _prompt, _output_path):
                entered.set()
                await release.wait()
                return "first"

            with tempfile.TemporaryDirectory() as tmpdir, patch.object(
                llm_backend,
                "_resolve_codex_executable",
                return_value="codex.exe",
            ), patch.object(llm_backend, "_run_codex_async", side_effect=fake_runner):
                config.CODEX_RUNTIME_DIR = Path(tmpdir)
                first = asyncio.create_task(
                    llm_backend.complete_text_async(
                        [{"role": "user", "content": "first"}],
                        role="query",
                        timeout_seconds=10,
                    )
                )
                await asyncio.wait_for(entered.wait(), timeout=1)
                with self.assertRaises(llm_backend.LLMBackendError):
                    await llm_backend.complete_text_async(
                        [{"role": "user", "content": "second"}],
                        role="query",
                        timeout_seconds=0.05,
                    )
                release.set()
                self.assertEqual(await first, "first")

        asyncio.run(exercise())

    def test_factory_uses_api_fallback_after_async_codex_error(self):
        class FakeRag:
            async def initialize_storages(self):
                return None

        class FakeCompletions:
            async def create(self, **_kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="api fallback"))]
                )

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

        async def exercise():
            config.LLM_PROFILE = "luna"
            config.CODEX_LUNA_MODEL = "codex-test-model"
            config.CODEX_FALLBACK_TO_API = True
            with patch.object(factory, "LightRAG", return_value=FakeRag()) as rag_ctor, patch.object(
                llm_backend,
                "complete_text_async",
                side_effect=llm_backend.LLMBackendError("codex unavailable"),
            ), patch.object(factory, "_openai_client", return_value=fake_client), patch.object(
                factory,
                "_is_extraction_prompt",
                return_value=False,
            ):
                await factory.create_rag()
                llm_func = rag_ctor.call_args.kwargs["llm_model_func"]
                result = await llm_func("answer this", system_prompt="system")
            self.assertEqual(result, "api fallback")

        asyncio.run(exercise())

    def test_factory_binds_embedding_queue_to_project_contract(self):
        class FakeRag:
            async def initialize_storages(self):
                return None

        async def exercise():
            with patch.object(factory, "LightRAG", return_value=FakeRag()) as rag_ctor:
                await factory.create_rag()
            kwargs = rag_ctor.call_args.kwargs
            self.assertEqual(
                kwargs["embedding_func_max_async"],
                max(1, config.EMBEDDING_CONCURRENCY),
            )
            self.assertEqual(
                kwargs["default_embedding_timeout"],
                max(1, int(config.EMBEDDING_TIMEOUT_SECONDS)),
            )
            self.assertEqual(
                kwargs["embedding_batch_num"],
                max(1, config.EMBEDDING_BATCH_SIZE),
            )

        asyncio.run(exercise())

    def test_factory_does_not_use_api_fallback_when_disabled(self):
        class FakeRag:
            async def initialize_storages(self):
                return None

        async def exercise():
            config.LLM_PROFILE = "luna"
            config.CODEX_LUNA_MODEL = "codex-test-model"
            config.CODEX_FALLBACK_TO_API = False
            with patch.object(factory, "LightRAG", return_value=FakeRag()) as rag_ctor, patch.object(
                llm_backend,
                "complete_text_async",
                side_effect=llm_backend.LLMBackendError("codex unavailable"),
            ), patch.object(factory, "_openai_client") as api_client, patch.object(
                factory,
                "_is_extraction_prompt",
                return_value=False,
            ):
                await factory.create_rag()
                llm_func = rag_ctor.call_args.kwargs["llm_model_func"]
                with self.assertRaises(llm_backend.LLMBackendError):
                    await llm_func("answer this", system_prompt="system")
            api_client.assert_not_called()

        asyncio.run(exercise())

    def test_live_smoke_without_confirmation_makes_no_calls(self):
        with patch.object(luna_smoke.llm_backend, "complete_text_sync") as text_call, patch.object(
            luna_smoke.llm_backend,
            "complete_json_sync",
        ) as json_call:
            result = luna_smoke.main([])

        self.assertEqual(result, 2)
        text_call.assert_not_called()
        json_call.assert_not_called()

    def test_live_smoke_success_makes_exactly_two_calls(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        payload = LLMPayload(summary="The source reports a meeting.").model_dump()
        with patch.object(
            luna_smoke.llm_backend,
            "complete_text_sync",
            return_value="LUNA_READY",
        ) as text_call, patch.object(
            luna_smoke.llm_backend,
            "complete_json_sync",
            return_value=payload,
        ) as json_call, patch.object(
            luna_smoke.llm_backend,
            "_resolve_codex_executable",
            return_value="codex.exe",
        ):
            result = luna_smoke.main(["--confirm-live"])

        self.assertEqual(result, 0)
        text_call.assert_called_once()
        json_call.assert_called_once()

    def test_live_smoke_requires_codex_executable(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"
        with patch.object(
            luna_smoke.llm_backend,
            "_resolve_codex_executable",
            return_value=None,
        ), patch.object(luna_smoke.llm_backend, "complete_text_sync") as text_call, patch.object(
            luna_smoke.llm_backend,
            "complete_json_sync",
        ) as json_call:
            result = luna_smoke.main(["--confirm-live"])

        self.assertEqual(result, 2)
        text_call.assert_not_called()
        json_call.assert_not_called()

    def test_live_smoke_requires_luna_profile_and_makes_no_calls(self):
        config.LLM_PROFILE = "current"
        with patch.object(luna_smoke.llm_backend, "complete_text_sync") as text_call, patch.object(
            luna_smoke.llm_backend,
            "complete_json_sync",
        ) as json_call:
            self.assertFalse(luna_smoke._check_live_preconditions(True))
        text_call.assert_not_called()
        json_call.assert_not_called()

    def test_live_smoke_requires_model_id(self):
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = ""
        with patch.object(luna_smoke.llm_backend, "complete_text_sync") as text_call, patch.object(
            luna_smoke.llm_backend,
            "complete_json_sync",
        ) as json_call:
            self.assertFalse(luna_smoke._check_live_preconditions(True))
        text_call.assert_not_called()
        json_call.assert_not_called()

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in result.stdout
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


if __name__ == "__main__":
    unittest.main()
