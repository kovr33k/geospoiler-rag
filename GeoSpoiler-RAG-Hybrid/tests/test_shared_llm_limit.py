import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from geospoiler import config
from geospoiler.providers import llm_backend as backend


@pytest.mark.parametrize("failure", [None, "error", "cancel"])
def test_sync_async_media_share_three_slots(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(config, "LLM_PROFILE", "luna")
    monkeypatch.setattr(config, "CODEX_LLM_MAX_CONCURRENCY", 3)
    monkeypatch.setattr(config, "CODEX_RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(backend, "_resolve_codex_executable", lambda: "codex")
    image = tmp_path / "image.png"
    image.write_bytes(b"controlled-media")
    lock = threading.Lock()
    release = threading.Event()
    three_entered = threading.Event()
    active = maximum = entered = 0

    def enter():
        nonlocal active, maximum, entered
        with lock:
            active += 1
            entered += 1
            maximum = max(maximum, active)
            if active == 3:
                three_entered.set()

    def leave():
        nonlocal active
        with lock:
            active -= 1

    def sync_runner(*args):
        enter()
        try:
            assert release.wait(5)
            return "ok"
        finally:
            leave()

    async def exercise():
        gate = asyncio.Event()
        async_entered = asyncio.Event()

        async def async_runner(*args):
            enter()
            async_entered.set()
            try:
                await gate.wait()
                if failure == "error":
                    raise backend.LLMBackendError("controlled failure")
                return "ok"
            finally:
                leave()

        monkeypatch.setattr(backend, "_run_codex", sync_runner)
        monkeypatch.setattr(backend, "_run_codex_async", async_runner)
        with ThreadPoolExecutor(max_workers=4) as executor:
            first = asyncio.create_task(backend.complete_text_async([], role="enrichment"))
            await asyncio.wait_for(async_entered.wait(), 2)
            sync_calls = [executor.submit(backend.complete_text_sync, [], role="default"),
                          executor.submit(backend.complete_media_analysis_sync, [image], prompt="media")]
            assert await asyncio.to_thread(three_entered.wait, 2)
            fourth = asyncio.create_task(backend.complete_text_async([], role="query", timeout_seconds=0.05))
            with pytest.raises(backend.LLMBackendError, match="timed out"):
                await fourth
            assert entered == 3
            if failure == "cancel":
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            else:
                gate.set()
                if failure == "error":
                    with pytest.raises(backend.LLMBackendError):
                        await first
                else:
                    assert await first == "ok"
            # A queued call timed out without stealing a later permit.
            gate.set()
            try:
                if failure == "error":
                    with pytest.raises(backend.LLMBackendError):
                        await backend.complete_text_async([], role="query", timeout_seconds=1)
                else:
                    assert await backend.complete_text_async([], role="query", timeout_seconds=1) == "ok"
            finally:
                release.set()
            assert [future.result(2) for future in sync_calls] == ["ok", "ok"]

    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert maximum == 3
    assert active == 0
    assert entered == 4
