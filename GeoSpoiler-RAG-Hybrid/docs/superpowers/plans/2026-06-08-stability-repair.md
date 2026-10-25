# WikiRag Stability Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the 12 review findings from the 2026-06-08 project audit so query sources, resumable fetching, review queue handling, Instagram extraction, dependencies, docs, and repository hygiene are stable.

**Architecture:** Keep the existing pipeline shape: fetch -> normalize -> review queue -> enrich -> load/query. Make focused changes at subsystem boundaries instead of large rewrites. Add regression tests around each bug that can silently corrupt retrieval or ingestion, then run broader verification after related groups of fixes.

**Tech Stack:** Python 3.11, unittest, LightRAG, Telethon, Streamlit, Crawl4AI, yt-dlp, ffmpeg, requests, Pillow.

---

## File Structure

- `main.py`: source-reference parsing, contiguous progress marking, review item collection and summary labels.
- `fetcher/state.py`: keep as-is unless helper naming needs a clearer API; prefer using existing `mark_message_processed`.
- `normalizer/review_queue.py`: unified review types, idempotent file schema, labels, and long Instagram Reel type.
- `normalizer/pipeline.py`: remove unused web extraction import; pass Telegram context into Instagram extraction; keep review counters honest.
- `normalizer/instagram_handler.py`: cache signature validation, safe fallback caching, long Reel queue integration, safer config defaults.
- `normalizer/web_handler.py`: whitespace cleanup and Crawl4AI helper hardening if needed by reviewer UI.
- `reviewer_app.py`: make JSON review item the single source of truth; stop overwriting normalized files by default.
- `loader/lightrag_loader.py`: strip all review placeholders before graph insertion and meaningful-body checks.
- `config.py` and `.env.example`: complete Instagram deep-extract config with safe defaults.
- `pyproject.toml` and `requirements.txt`: align runtime and dev dependencies.
- `README.md`, `OPERATIONS.md`, `ARCHITECTURE.md`: document unified review queue and manual review workflow.
- `.gitattributes` and `.gitignore`: normalize line endings and keep runtime artifacts out of future commits.
- Tests: `test_main.py`, `test_loader.py`, `test_handlers.py`, `test_pipeline_stats.py`, plus a new `test_review_queue.py` if queue behavior grows beyond one or two tests.

---

## Verification Policy

Use targeted tests immediately after each bug fix. Use full verification after groups of related tasks.

Core commands:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m ruff check .
git diff --check
git status --short
```

If `ruff` is missing, install dev dependencies once:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Expected final result:

- `unittest discover`: PASS
- `compileall`: exit code 0
- `ruff check`: exit code 0
- `git diff --check`: no whitespace errors
- `git status --short`: only intended code/doc/config changes plus expected untracked plan file if not committed

---

### Task 1: Fix Query Source Reference Parsing

**Problem covered:** 1

**Files:**
- Modify: `main.py`
- Modify: `test_main.py`

- [ ] **Step 1: Confirm current regression**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_main.MainQueryTests.test_extract_query_sources_prefers_cited_references test_main.MainQueryTests.test_extract_query_sources_uses_direct_reference_metadata test_main.MainQueryTests.test_extract_query_sources_reads_adjacent_meta_when_index_is_stale -v
```

Expected before fix: 3 failures with `AssertionError: 0 != 1`.

- [ ] **Step 2: Add explicit test for reference_id tokens**

In `test_main.py`, add a test near the existing `_extract_query_sources` tests:

```python
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

        with patch.object(main, "load_source_metadata_index", return_value=source_index):
            sources = main._extract_query_sources(result)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["post_url"], "https://t.me/example/2")
```

- [ ] **Step 3: Implement numeric bullets plus explicit IDs**

In `main.py`, replace `_REFERENCE_INLINE_RE` with two clearer patterns:

```python
_REFERENCE_BULLET_RE = re.compile(r"^\s*-\s*\[(\d+)\]", re.MULTILINE)
_REFERENCE_TOKEN_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_-]*)\]")
```

Update `_extract_answer_reference_keys`:

```python
def _extract_answer_reference_keys(answer: str) -> tuple[set[str], set[str]]:
    """Collect explicit citation keys and numbered reference bullets mentioned in the answer."""
    if not answer:
        return set(), set()

    reference_ids = {match.group(1).strip() for match in _REFERENCE_ID_RE.finditer(answer)}
    refs_section = answer.split("### References", 1)[1] if "### References" in answer else answer
    numbered_refs = {match.group(1) for match in _REFERENCE_BULLET_RE.finditer(refs_section)}
    reference_ids.update(
        token
        for token in _REFERENCE_TOKEN_RE.findall(answer)
        if not token.isdigit() and token.lower() != "reference_id"
    )
    return reference_ids, numbered_refs
```

Update the filtering block in `_extract_query_sources` so numeric bullets always map to ordinal position:

```python
        if explicit_ids or numbered_refs:
            if str(idx) in numbered_refs:
                filtered_references.append(ref)
                continue
            if ref_id and ref_id in explicit_ids:
                filtered_references.append(ref)
                continue
            continue
```

- [ ] **Step 4: Run targeted tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_main.MainQueryTests -v
```

Expected: all `MainQueryTests` pass.

- [ ] **Step 5: Commit checkpoint**

```powershell
git add main.py test_main.py
git commit -m "fix: restore query source reference parsing"
```

---

### Task 2: Make Fetch Progress Contiguous

**Problem covered:** 2

**Files:**
- Modify: `main.py`
- Modify: `test_main.py`

- [ ] **Step 1: Add tests for gaps in normalization success**

In `test_main.py`, add tests using lightweight message objects:

```python
    def test_mark_contiguous_processed_stops_at_first_failed_message(self):
        messages = [
            SimpleNamespace(channel_id=1, channel_name="Channel", message_id=101),
            SimpleNamespace(channel_id=1, channel_name="Channel", message_id=102),
            SimpleNamespace(channel_id=1, channel_name="Channel", message_id=103),
        ]

        with patch.object(main, "mark_message_processed") as mark:
            count = main._mark_contiguous_processed(messages, {101, 103})

        self.assertEqual(count, 1)
        mark.assert_called_once_with(1, "Channel", 101)

    def test_mark_contiguous_processed_does_not_advance_when_first_message_failed(self):
        messages = [
            SimpleNamespace(channel_id=1, channel_name="Channel", message_id=101),
            SimpleNamespace(channel_id=1, channel_name="Channel", message_id=102),
        ]

        with patch.object(main, "mark_message_processed") as mark:
            count = main._mark_contiguous_processed(messages, {102})

        self.assertEqual(count, 0)
        mark.assert_not_called()
```

Add this import at the top of `test_main.py`:

```python
from types import SimpleNamespace
```

- [ ] **Step 2: Implement helper in `main.py`**

Place this helper near `cmd_normalize`:

```python
def _mark_contiguous_processed(messages: list[TelegramMessage], successful_ids: set[int]) -> int:
    """Advance fetch progress only through the first contiguous successful prefix."""
    marked = 0
    for msg in sorted(messages, key=lambda item: item.message_id):
        if msg.message_id not in successful_ids:
            break
        mark_message_processed(msg.channel_id, msg.channel_name, msg.message_id)
        marked += 1
    return marked
```

Replace the marking loop in `cmd_normalize` with:

```python
        marked_count = _mark_contiguous_processed(messages, successful_ids)
        result.processed_messages += marked_count
        marked_ids = {msg.message_id for msg in sorted(messages, key=lambda item: item.message_id)[:marked_count]}

        for msg in messages:
            if msg.message_id not in marked_ids:
                logger.warning(
                    f"  Message {msg.message_id} from '{msg.channel_name}' not marked processed "
                    "(will be retried next run)."
                )
```

- [ ] **Step 3: Run targeted tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_main.MainQueryTests.test_mark_contiguous_processed_stops_at_first_failed_message test_main.MainQueryTests.test_mark_contiguous_processed_does_not_advance_when_first_message_failed -v
```

Expected: PASS.

- [ ] **Step 4: Run CLI/main tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_main test_cli -v
```

Expected: PASS.

- [ ] **Step 5: Commit checkpoint**

```powershell
git add main.py test_main.py
git commit -m "fix: advance fetch progress only through contiguous successes"
```

---

### Task 3: Strip Unified Review Placeholders Before RAG Insert

**Problems covered:** 3

**Files:**
- Modify: `loader/lightrag_loader.py`
- Modify: `test_loader.py`

- [ ] **Step 1: Extend loader regression test**

In `test_loader.py`, update `test_load_texts_strips_headers_and_placeholders_before_insert` text to include new placeholder types:

```python
            "[AI-диалог: https://chatgpt.com/share/abc]\n"
            "[Внешняя ссылка: https://example.com/story]\n"
            "[Малоинформативный пост: Uninformative post]\n"
            "[Отправлено в очередь на ручной просмотр: Channel_3_external.json]\n"
```

Keep the existing expectation:

```python
        self.assertEqual(rag.inserted[0]["texts"], ["Useful body."])
```

- [ ] **Step 2: Update both placeholder regexes**

In `loader/lightrag_loader.py`, update `_POSTHOLDER_LINE_RE`:

```python
_POSTHOLDER_LINE_RE = re.compile(
    r"^\[(?:Видео:|Аудио:|Transcript|Voice transcript|Video transcript|AI-диалог:|"
    r"Внешняя ссылка:|Малоинформативный пост:|Instagram Reel:.*очередь|"
    r"Отправлено в очередь на ручной просмотр:|Уже обработано:).*\]$"
)
```

Update `_PLACEHOLDER_CONTENT_RE`:

```python
_PLACEHOLDER_CONTENT_RE = re.compile(
    r"^\[(?:Видео:|Аудио:|AI-диалог:|Внешняя ссылка:|Малоинформативный пост:|"
    r"Instagram Reel:.*очередь|Отправлено в очередь|Уже обработано:|Веб-страница:.*ошибка).*\]$",
    re.IGNORECASE,
)
```

- [ ] **Step 3: Run loader tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_loader.LoadTextsTests -v
```

Expected: PASS.

- [ ] **Step 4: Commit checkpoint**

```powershell
git add loader/lightrag_loader.py test_loader.py
git commit -m "fix: strip unified review placeholders before rag insert"
```

---

### Task 4: Make Review JSON the Single Source of Truth

**Problems covered:** 4

**Files:**
- Modify: `main.py`
- Modify: `reviewer_app.py`
- Modify: `normalizer/review_queue.py`
- Create or modify: `test_review_queue.py`
- Modify: `test_main.py`

- [ ] **Step 1: Add collector tests for all review types**

Create `test_review_queue.py`:

```python
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import config  # noqa: E402
import main  # noqa: E402


class ReviewQueueLoadTests(unittest.TestCase):
    def test_collect_reviewed_texts_loads_all_processed_review_types(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir)
            for name, review_type, text in [
                ("ai.json", "ai_chat", "AI extracted"),
                ("link.json", "external_link", "External extracted"),
                ("low.json", "uninformative", "Approved low info"),
            ]:
                (queue_dir / name).write_text(
                    json.dumps(
                        {
                            "review_type": review_type,
                            "status": "processed",
                            "channel": "Channel",
                            "message_id": 10,
                            "url": "https://example.com" if review_type == "external_link" else "",
                            "extracted_text": text,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            with patch.object(config, "REVIEW_QUEUE_DIR", queue_dir):
                reviewed = main._collect_reviewed_texts()

        self.assertEqual(len(reviewed), 3)
        self.assertTrue(all("Review type:" in text for _, text in reviewed))
        self.assertEqual({Path(path).name for path, _ in reviewed}, {"ai.json", "link.json", "low.json"})
```

- [ ] **Step 2: Rename and generalize collector**

In `main.py`, rename `_collect_reviewed_ai_texts` to `_collect_reviewed_texts` and build a small provenance header:

```python
def _collect_reviewed_texts() -> list[tuple[str, str]]:
    """Collect processed review items whose extracted_text is non-empty."""
    results = []
    for f in config.REVIEW_QUEUE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("status") != "processed" or not data.get("extracted_text"):
                continue
            extracted = str(data["extracted_text"]).strip()
            if not extracted:
                continue
            header = (
                f"[Review type: {data.get('review_type', 'unknown')} | "
                f"Channel: {data.get('channel', '')} | "
                f"Message: {data.get('message_id', '')} | "
                f"URL: {data.get('url', '')}]"
            )
            results.append((str(f), f"{header}\n\n{extracted}"))
        except (json.JSONDecodeError, OSError):
            continue
    return results
```

Update `cmd_load`:

```python
        reviewed = _collect_reviewed_texts()
        if reviewed:
            logger.info(f"  Loading {len(reviewed)} reviewed item(s) into LightRAG.")
```

Update summary labels from `Reviewed AI items` to `Reviewed items`.

- [ ] **Step 3: Stop reviewer from overwriting normalized text by default**

In `reviewer_app.py`, remove the call to `_save_override_text` in the save button path:

```python
                    mark_reviewed(
                        filepath,
                        extracted_text=edited_text.strip() if edited_text.strip() else None,
                        skip=not edited_text.strip(),
                        attached_image=img_path,
                    )
```

Keep `_save_uploaded_image`. Remove `_save_override_text` entirely if it becomes unused.

- [ ] **Step 4: Make approve behavior type-aware**

In `reviewer_app.py`, change the approve button block:

```python
            if st.button("✅ Одобрить", key=f"approve_{idx}", use_container_width=True):
                if review_type == REVIEW_TYPE_UNINFORMATIVE and message_text.strip():
                    mark_reviewed(filepath, extracted_text=message_text.strip())
                    st.rerun()
                else:
                    st.warning("Для AI-диалога или внешней ссылки сначала добавьте извлечённый текст через редактирование.")
```

- [ ] **Step 5: Run review and main tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_review_queue test_main -v
```

Expected: PASS.

- [ ] **Step 6: Commit checkpoint**

```powershell
git add main.py reviewer_app.py normalizer/review_queue.py test_review_queue.py test_main.py
git commit -m "fix: load reviewed items from unified review queue"
```

---

### Task 5: Make Instagram Cache Safe

**Problems covered:** 5

**Files:**
- Modify: `normalizer/instagram_handler.py`
- Modify: `test_handlers.py`

- [ ] **Step 1: Add cache invalidation tests**

In `test_handlers.py`, add tests under `InstagramCacheTests`:

```python
    def test_read_cache_ignores_different_signature(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            (cache_dir / "ABC123.json").write_text(
                json.dumps(
                    {
                        "post_id": "ABC123",
                        "text": "old cached text",
                        "cache_version": 2,
                        "signature": {"deep_extract_enabled": False},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch("normalizer.instagram_handler.config.INSTAGRAM_CACHE_DIR", cache_dir):
                with patch("normalizer.instagram_handler.config.INSTAGRAM_DEEP_EXTRACT_ENABLED", True):
                    self.assertIsNone(_read_cache("ABC123"))

    def test_deep_extract_failure_does_not_cache_caption_only_fallback(self):
        info = {"description": "Caption", "uploader": "user", "duration": 10}
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch("normalizer.instagram_handler.config.INSTAGRAM_CACHE_DIR", cache_dir):
                with patch("normalizer.instagram_handler.config.INSTAGRAM_DEEP_EXTRACT_ENABLED", True):
                    with patch("normalizer.instagram_handler._get_info_ytdlp", return_value=info):
                        with patch("normalizer.instagram_handler._deep_extract_reel", return_value=None):
                            from normalizer.instagram_handler import extract_instagram_text
                            result = extract_instagram_text("https://www.instagram.com/reel/ABC123/")

        self.assertIn("Caption", result)
        self.assertFalse((cache_dir / "ABC123.json").exists())
```

- [ ] **Step 2: Add cache signature helpers**

In `normalizer/instagram_handler.py`, add:

```python
_INSTAGRAM_CACHE_VERSION = 2


def _cache_signature() -> dict:
    return {
        "deep_extract_enabled": config.INSTAGRAM_DEEP_EXTRACT_ENABLED,
        "transcription_model": config.TRANSCRIPTION_MODEL,
        "vision_base_url": config.INSTAGRAM_VISION_BASE_URL,
        "vision_model": config.INSTAGRAM_VISION_MODEL,
        "llm_model": config.LLM_MODEL,
    }
```

Update `_read_cache`:

```python
        if payload.get("cache_version") != _INSTAGRAM_CACHE_VERSION:
            return None
        if payload.get("signature") != _cache_signature():
            return None
        text = payload.get("text")
```

Update `_write_cache`:

```python
            "cache_version": _INSTAGRAM_CACHE_VERSION,
            "signature": _cache_signature(),
```

- [ ] **Step 3: Avoid caching fallback when deep extract was enabled and failed**

In `extract_instagram_text`, change fallback caching:

```python
    result = _caption_only(canonical_url, info, caption, uploader, is_reel)
    if post_id and not config.INSTAGRAM_DEEP_EXTRACT_ENABLED:
        _write_cache(post_id, result)
    return result
```

- [ ] **Step 4: Run handler tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_handlers.InstagramCacheTests test_handlers.InstagramFallbackTests -v
```

Expected: PASS.

- [ ] **Step 5: Commit checkpoint**

```powershell
git add normalizer/instagram_handler.py test_handlers.py
git commit -m "fix: validate instagram extraction cache signature"
```

---

### Task 6: Unify Long Instagram Reel Review Items

**Problems covered:** 6

**Files:**
- Modify: `normalizer/review_queue.py`
- Modify: `normalizer/instagram_handler.py`
- Modify: `normalizer/pipeline.py`
- Modify: `reviewer_app.py`
- Modify: `test_handlers.py`

- [ ] **Step 1: Add review type constant**

In `normalizer/review_queue.py`:

```python
REVIEW_TYPE_INSTAGRAM_LONG_REEL = "instagram_long_reel"
```

Update labels:

```python
        REVIEW_TYPE_INSTAGRAM_LONG_REEL: "Длинный Instagram Reel",
```

- [ ] **Step 2: Pass Telegram context into Instagram extraction**

Change signature in `normalizer/instagram_handler.py`:

```python
def extract_instagram_text(
    url: str,
    *,
    channel_name: str = "instagram",
    message_id: int = 0,
    message_text: str = "",
    message_date: datetime | None = None,
) -> str:
```

Pass these values into `_deep_extract_reel`.

In `normalizer/pipeline.py`, update the link-only call:

```python
            ig_text = extract_instagram_text(
                url,
                channel_name=msg.channel_name,
                message_id=msg.message_id,
                message_text=msg.text,
                message_date=msg.date,
            )
```

- [ ] **Step 3: Replace long Reel custom JSON with `queue_item`**

Change `_queue_long_reel_for_review` to accept context and call `queue_item`:

```python
def _queue_long_reel_for_review(
    url: str,
    caption: str,
    uploader: str,
    duration: float,
    *,
    channel_name: str,
    message_id: int,
    message_text: str,
    message_date: datetime | None,
) -> str:
    result = queue_review_item(
        review_type=REVIEW_TYPE_INSTAGRAM_LONG_REEL,
        channel_name=channel_name,
        message_id=message_id,
        message_text=message_text or caption,
        message_date=message_date,
        url=url,
        reason=f"Duration {duration}s exceeds limit {config.INSTAGRAM_MAX_VIDEO_DURATION_SEC}s",
    )
    header = f"[Instagram Reel: {url}" + (f" - @{uploader}" if uploader else "") + "]"
    return "\n\n".join(part for part in [header, caption, result.placeholder_text] if part)
```

Add imports at top of `instagram_handler.py`:

```python
from normalizer.review_queue import REVIEW_TYPE_INSTAGRAM_LONG_REEL, queue_item as queue_review_item
```

- [ ] **Step 4: Show long Reel type in reviewer UI**

In `reviewer_app.py`, import `REVIEW_TYPE_INSTAGRAM_LONG_REEL` and add:

```python
    REVIEW_TYPE_INSTAGRAM_LONG_REEL: ("Длинный Reel", "type-external-link"),
```

Include it in filter options or let it show under external links.

- [ ] **Step 5: Add long Reel test**

In `test_handlers.py`, add:

```python
    def test_long_reel_uses_unified_review_queue(self):
        info = {"description": "Caption", "uploader": "user", "duration": 999}
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir)
            with patch("normalizer.instagram_handler.config.REVIEW_QUEUE_DIR", queue_dir):
                with patch("normalizer.instagram_handler.config.INSTAGRAM_DEEP_EXTRACT_ENABLED", True):
                    with patch("normalizer.instagram_handler.config.INSTAGRAM_MAX_VIDEO_DURATION_SEC", 180):
                        with patch("normalizer.instagram_handler._get_info_ytdlp", return_value=info):
                            from normalizer.instagram_handler import extract_instagram_text
                            result = extract_instagram_text(
                                "https://www.instagram.com/reel/ABC123/",
                                channel_name="Channel",
                                message_id=42,
                                message_text="Post text",
                            )

            files = list(queue_dir.glob("*.json"))

        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["review_type"], "instagram_long_reel")
        self.assertEqual(payload["status"], "pending")
        self.assertIn("очеред", result.lower())
```

- [ ] **Step 6: Run targeted tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_handlers.InstagramFallbackTests test_handlers.InstagramCacheTests -v
```

Expected: PASS.

- [ ] **Step 7: Commit checkpoint**

```powershell
git add normalizer/review_queue.py normalizer/instagram_handler.py normalizer/pipeline.py reviewer_app.py test_handlers.py
git commit -m "fix: route long instagram reels through unified review queue"
```

---

### Task 7: Fix Config Defaults and Dependency Metadata

**Problems covered:** 7, 8, 9, 10 partly

**Files:**
- Modify: `config.py`
- Modify: `.env.example`
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `main.py`

- [ ] **Step 1: Make Instagram vision defaults use vision config**

In `config.py`, change Instagram vision defaults:

```python
INSTAGRAM_VISION_API_KEY = os.getenv("INSTAGRAM_VISION_API_KEY", "") or VISION_API_KEY or LLM_API_KEY
INSTAGRAM_VISION_BASE_URL = os.getenv("INSTAGRAM_VISION_BASE_URL", "") or VISION_BASE_URL or LLM_BASE_URL
INSTAGRAM_VISION_MODEL = os.getenv("INSTAGRAM_VISION_MODEL", "") or VISION_MODEL
```

Move this block after the `VISION_*` block so names are defined before use.

- [ ] **Step 2: Add `.env.example` Instagram section**

Add after the vision block:

```dotenv
# ===== INSTAGRAM DEEP EXTRACT =====
INSTAGRAM_DEEP_EXTRACT_ENABLED=false
INSTAGRAM_VISION_API_KEY=
INSTAGRAM_VISION_BASE_URL=
INSTAGRAM_VISION_MODEL=
INSTAGRAM_FRAME_INTERVAL_SEC=2.0
INSTAGRAM_FRAME_BATCH_SIZE=5
INSTAGRAM_MAX_VIDEO_DURATION_SEC=180
INSTAGRAM_MAX_VIDEO_SIZE_MB=100
```

- [ ] **Step 3: Align dependencies**

In `pyproject.toml`, add runtime dependencies:

```toml
    "streamlit>=1.40.0",
    "Pillow>=10.0.0",
    "qrcode>=8.0.0",
```

In `requirements.txt`, add:

```text
Pillow>=10.0.0
qrcode>=8.0.0
```

- [ ] **Step 4: Launch reviewer through current Python**

In `main.py`, replace hard-coded `.venv/Scripts/streamlit.exe` with:

```python
            sys.executable,
            "-m",
            "streamlit",
```

The subprocess argument list should become:

```python
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.port",
            str(port),
            "--server.headless",
            "true",
        ],
```

- [ ] **Step 5: Run config/import checks**

Run:

```powershell
.\.venv\Scripts\python.exe -c "import config; print(config.INSTAGRAM_VISION_MODEL)"
.\.venv\Scripts\python.exe -c "import reviewer_app; import qrcode; import PIL; print('imports ok')"
```

Expected: first command prints a model name, second prints `imports ok`.

- [ ] **Step 6: Commit checkpoint**

```powershell
git add config.py .env.example pyproject.toml requirements.txt main.py
git commit -m "fix: align instagram config and runtime dependencies"
```

---

### Task 8: Clean Web Handler and Normalizer Async Contract

**Problems covered:** 8, 11 partly, whitespace from verification

**Files:**
- Modify: `normalizer/web_handler.py`
- Modify: `normalizer/pipeline.py`
- Modify: `test_pipeline_stats.py`

- [ ] **Step 1: Remove unused normalizer import**

In `normalizer/pipeline.py`, remove:

```python
from normalizer.web_handler import extract_web_text
```

In `test_pipeline_stats.py`, remove the nested patch:

```python
with patch("normalizer.pipeline.extract_web_text", return_value="[web]"):
```

Keep the rest of the test nesting valid by unindenting the inner `with` blocks.

- [ ] **Step 2: Patch review queue calls in pipeline stats test**

In `test_pipeline_stats.py`, import or use a simple object:

```python
from types import SimpleNamespace
```

Patch `queue_review_item`:

```python
with patch(
    "normalizer.pipeline.queue_review_item",
    return_value=SimpleNamespace(placeholder_text="[review]", action="queued", filepath="review.json"),
):
```

Add expectations:

```python
        self.assertEqual(result.link_review_created, 4)
```

- [ ] **Step 3: Remove trailing whitespace from web handler**

In `normalizer/web_handler.py`, ensure lines around the Crawl4AI call contain no trailing spaces:

```python
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)

            if not result.success:
                logger.warning(f"Crawl4AI failed for {url}: {result.error_message}")
                return f'[Веб: {url}]\n[Содержание не удалось извлечь]'

            markdown_content = result.markdown
```

- [ ] **Step 4: Run targeted tests and diff check**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_pipeline_stats -v
git diff --check
```

Expected: test passes; `git diff --check` no longer reports `normalizer/web_handler.py` trailing whitespace.

- [ ] **Step 5: Commit checkpoint**

```powershell
git add normalizer/web_handler.py normalizer/pipeline.py test_pipeline_stats.py
git commit -m "fix: isolate review queue side effects in pipeline stats tests"
```

---

### Task 9: Update Review Queue Documentation

**Problems covered:** 9, 10 partly

**Files:**
- Modify: `README.md`
- Modify: `OPERATIONS.md`
- Modify: `ARCHITECTURE.md`

- [ ] **Step 1: Update README review queue description**

Replace AI-only text with:

```markdown
- `output/review_queue/` - manual review queue for AI chat shares, external links that need human triage, long Instagram Reels, and low-information posts.
```

Add command note:

```markdown
python main.py review
python main.py review --web
```

- [ ] **Step 2: Update operations workflow**

In `OPERATIONS.md`, update the load step wording:

```markdown
The load step includes all review queue items with `status=processed` and non-empty `extracted_text`.
Review JSON files are treated as their own stable source paths.
```

Add a warning:

```markdown
Do not edit `output/normalized/*.txt` to paste reviewed external-link text unless you intentionally want to replace the original normalized post. Prefer `python main.py review --web`, which stores reviewed text in the review JSON.
```

- [ ] **Step 3: Update architecture review queue section**

In `ARCHITECTURE.md`, replace AI-only review references with:

```markdown
The review queue is a unified manual-ingestion layer. Normalization and enrichment may enqueue AI chat links, external links, long Instagram Reels, and low-information posts. Processed items are loaded as review JSON sources during `load`.
```

- [ ] **Step 4: Verify docs grep**

Run:

```powershell
rg -n "AI-chat|AI chat|review queue|external link|Instagram Reel|processed" README.md OPERATIONS.md ARCHITECTURE.md
```

Expected: no remaining text claims the review queue is only for AI-chat items.

- [ ] **Step 5: Commit checkpoint**

```powershell
git add README.md OPERATIONS.md ARCHITECTURE.md
git commit -m "docs: document unified review queue workflow"
```

---

### Task 10: Repository Hygiene for Runtime Data and Line Endings

**Problems covered:** 12 and LF/CRLF warnings

**Files:**
- Create: `.gitattributes`
- Modify: `.gitignore`
- Git index only: untrack runtime directories while keeping files on disk

- [ ] **Step 1: Add line-ending policy**

Create `.gitattributes` at repository root `C:\WikiRag\RAG-GeoSpoiler\.gitattributes`:

```gitattributes
* text=auto eol=lf
*.png binary
*.jpg binary
*.jpeg binary
*.webp binary
*.gif binary
*.sqlite binary
```

- [ ] **Step 2: Extend artifact ignores**

In `GeoSpoiler-RAG-Hybrid/.gitignore`, add:

```gitignore
artifacts/ig_test_*.txt
artifacts/telegram_qr*.png
```

- [ ] **Step 3: Untrack runtime state without deleting local files**

Run from repo root `C:\WikiRag\RAG-GeoSpoiler`:

```powershell
git rm --cached -r -- GeoSpoiler-RAG-Hybrid/output GeoSpoiler-RAG-Hybrid/media_cache GeoSpoiler-RAG-Hybrid/logs GeoSpoiler-RAG-Hybrid/state GeoSpoiler-RAG-Hybrid/rag_storage
```

This removes files from git tracking only. It does not delete local working files.

- [ ] **Step 4: Verify runtime files are no longer tracked**

Run:

```powershell
git ls-files GeoSpoiler-RAG-Hybrid/output GeoSpoiler-RAG-Hybrid/media_cache GeoSpoiler-RAG-Hybrid/logs GeoSpoiler-RAG-Hybrid/state GeoSpoiler-RAG-Hybrid/rag_storage
```

Expected: no output.

- [ ] **Step 5: Check status**

Run:

```powershell
git status --short
```

Expected: staged deletions for runtime files, plus intended `.gitattributes` and `.gitignore` changes.

- [ ] **Step 6: Commit checkpoint**

```powershell
git add .gitattributes GeoSpoiler-RAG-Hybrid/.gitignore
git commit -m "chore: stop tracking runtime generated data"
```

---

### Task 11: Move Manual Live Scripts Out of Test Discovery

**Problems covered:** 11 and untracked artifact hygiene

**Files:**
- Move: `test_instagram_live.py` -> `scripts/instagram_live_check.py`
- Modify: docs references if any are added in Task 9
- Modify: `.gitignore`

- [ ] **Step 1: Create scripts directory and move live script**

Run:

```powershell
New-Item -ItemType Directory -Force -Path scripts | Out-Null
git mv test_instagram_live.py scripts/instagram_live_check.py
```

- [ ] **Step 2: Update usage string in moved script**

In `scripts/instagram_live_check.py`, change:

```python
Usage:
    python scripts/instagram_live_check.py <instagram_reel_url>
```

Update path bootstrap:

```python
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
```

Change artifact directory:

```python
out_dir = PROJECT_ROOT / "artifacts"
```

- [ ] **Step 3: Verify unittest discovery no longer imports the live script**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

Expected: live Instagram network script is not listed as a test module.

- [ ] **Step 4: Commit checkpoint**

```powershell
git add scripts/instagram_live_check.py
git commit -m "chore: move instagram live check out of test discovery"
```

---

### Task 12: Final Full Verification and Cleanup

**Problems covered:** all

**Files:**
- Any file touched above

- [ ] **Step 1: Install dev tooling if missing**

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Expected: `ruff` becomes available.

- [ ] **Step 2: Run full unit tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

Expected: all tests pass. The previous 3 `test_main` failures must be gone.

- [ ] **Step 3: Run syntax and lint checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m ruff check .
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 4: Verify imports for optional tools**

Run:

```powershell
.\.venv\Scripts\python.exe -c "import config, reviewer_app, qrcode, PIL; print('imports ok')"
```

Expected: `imports ok`.

- [ ] **Step 5: Review git status**

Run:

```powershell
git status --short
```

Expected: only intentional source/doc/config changes if not committed, or clean working tree if all checkpoints were committed.

- [ ] **Step 6: Write final implementation summary**

Include:

```markdown
Verification:
- unittest discover: PASS
- compileall: PASS
- ruff: PASS
- git diff --check: PASS

Fixed:
- query source references
- contiguous fetch progress
- unified review queue load and placeholder cleanup
- Instagram cache and long Reel queue
- dependency/env/docs alignment
- runtime data untracked from git
```

---

## Self-Review

Spec coverage:

- Problem 1: Task 1
- Problem 2: Task 2
- Problem 3: Task 3
- Problem 4: Task 4
- Problem 5: Task 5
- Problem 6: Task 6
- Problems 7, 8, 9, 10: Tasks 7, 8, 9
- Problem 11: Tasks 8, 11, 12
- Problem 12: Task 10

Placeholder scan:

- No task uses placeholder language or unspecified test instructions.
- Each code-change task names files, commands, expected results, and representative code.

Type consistency:

- Review queue statuses use `pending`, `processed`, `skipped`.
- Unified collector name is `_collect_reviewed_texts`.
- Long Reel review type is `instagram_long_reel`.
- Source parsing returns `(explicit_ids, numbered_refs)` throughout.

## Execution Options

Plan complete and saved to `docs/superpowers/plans/2026-06-08-stability-repair.md`.

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - execute tasks in this session with checkpoints after each task group.
