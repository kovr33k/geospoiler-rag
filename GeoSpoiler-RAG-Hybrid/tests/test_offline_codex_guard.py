import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from geospoiler.providers import llm_backend as backend


def test_offline_mode_blocks_sync_codex_before_process_start(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOSPOILER_NO_NETWORK", "1")
    process = Mock(returncode=0, communicate=Mock(return_value=("controlled response", "")))
    start = Mock(return_value=process)
    monkeypatch.setattr(backend.subprocess, "Popen", start)
    with pytest.raises(backend.LLMBackendError, match="offline"):
        backend._run_codex(["codex", "-C", str(tmp_path)], "prompt", tmp_path / "output.txt", 1)
    start.assert_not_called()


def test_offline_mode_blocks_async_codex_before_process_start(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOSPOILER_NO_NETWORK", "1")
    process = Mock(returncode=0, communicate=AsyncMock(return_value=(b"controlled response", b"")))
    start = AsyncMock(return_value=process)
    monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", start)
    with pytest.raises(backend.LLMBackendError, match="offline"):
        asyncio.run(backend._run_codex_async(["codex", "-C", str(tmp_path)], "prompt", tmp_path / "output.txt"))
    start.assert_not_called()
