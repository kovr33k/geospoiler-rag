# Close Architecture Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the architecture-review follow-up by removing query orchestration duplication, extending source registry usage only where safe, and splitting model bakeoff orchestration helpers.

**Architecture:** Keep existing public APIs stable. Make `query_rag_result()` the canonical query path and keep `query_rag()` as a string-returning wrapper. Expand source passport resolution only where a reliable `source_id` exists, and split bakeoff prompt/provider helpers without changing result formats.

**Tech Stack:** Python 3.11, unittest, LightRAG, Pydantic contracts, local golden runners.

---

### Task 1: Baseline

**Files:**
- Read: `GeoSpoiler-RAG-Hybrid/loader/query.py`
- Read: `GeoSpoiler-RAG-Hybrid/loader/lightrag_loader.py`
- Test: `GeoSpoiler-RAG-Hybrid/tests/test_loader.py`

- [ ] **Step 1: Create branch**

Run from repo root:

```powershell
git checkout -b refactor/close-architecture-review
```

Expected: branch is no longer `master`.

- [ ] **Step 2: Run baseline tests**

Run from `GeoSpoiler-RAG-Hybrid`:

```powershell
$env:GEOSPOILER_NO_NETWORK='1'; python -m unittest discover -s tests -p 'test_*.py'
```

Expected: `Ran 239 tests` and `OK`.

### Task 2: Make query_rag a wrapper

**Files:**
- Modify: `GeoSpoiler-RAG-Hybrid/loader/query.py`
- Read: `GeoSpoiler-RAG-Hybrid/loader/lightrag_loader.py`
- Test: `GeoSpoiler-RAG-Hybrid/tests/test_loader.py`

- [ ] **Step 1: Add regression test for wrapper behavior**

Add a test that patches `loader.query.query_rag_result`, calls `query_rag()`, and asserts that the returned value is the structured result content.

- [ ] **Step 2: Verify the regression test fails**

Run from `GeoSpoiler-RAG-Hybrid`:

```powershell
python -m unittest tests.test_loader.QueryRagWrapperTests
```

Expected: the new wrapper-specific assertion fails before production code is changed.

- [ ] **Step 3: Replace duplicated query_rag implementation**

Keep `query_rag_result()` unchanged as the canonical implementation. Replace `query_rag()` with a thin wrapper that awaits `query_rag_result()` and returns `llm_response.content`, then `response`, then an empty string.

- [ ] **Step 4: Verify query tests**

Run from `GeoSpoiler-RAG-Hybrid`:

```powershell
$env:GEOSPOILER_NO_NETWORK='1'; python -m unittest tests.test_loader
```

Expected: loader tests pass.

### Task 3: Source registry audit and safe usage

**Files:**
- Modify: `GeoSpoiler-RAG-Hybrid/loader/card_context.py`
- Modify: `GeoSpoiler-RAG-Hybrid/retrieval/composer.py`
- Read: `GeoSpoiler-RAG-Hybrid/retrieval/source_registry.py`
- Read: `GeoSpoiler-RAG-Hybrid/retrieval/wiki_index.py`
- Test: `GeoSpoiler-RAG-Hybrid/tests/test_source_registry.py`
- Test: `GeoSpoiler-RAG-Hybrid/tests/test_source_selection_golden.py` if present

- [ ] **Step 1: Audit reference construction sites**

List all reference construction paths in `card_context.py` and `composer.py`. For each path, identify whether a stable `source_id` is present, can be extracted from card `provenance`, or is unavailable.

- [ ] **Step 2: Add tests for passport-enriched references**

Add targeted tests covering paths where a card already has provenance sufficient for `extract_source_id()`.

- [ ] **Step 3: Verify tests fail**

Run the targeted tests and confirm they fail because references do not yet include registry-resolved passport fields.

- [ ] **Step 4: Implement safe registry enrichment**

Use `retrieval.wiki_index.extract_source_id()` to derive `source_id` from card/provenance where possible. Use `retrieval.source_registry.resolve_source()` only when a stable id exists. Preserve current fallback behavior for paths that only have `source_path` or `card_path`.

- [ ] **Step 5: Verify source tests**

Run from `GeoSpoiler-RAG-Hybrid`:

```powershell
$env:GEOSPOILER_NO_NETWORK='1'; python -m unittest tests.test_source_registry tests.test_composer_wiki tests.test_loader
```

Expected: tests pass.

### Task 4: Split model bakeoff helper responsibilities

**Files:**
- Modify: `GeoSpoiler-RAG-Hybrid/eval/model_bakeoff/run_bakeoff.py`
- Create: `GeoSpoiler-RAG-Hybrid/eval/model_bakeoff/prompts.py`
- Create: `GeoSpoiler-RAG-Hybrid/eval/model_bakeoff/providers.py`
- Test: `GeoSpoiler-RAG-Hybrid/tests/test_model_bakeoff.py`

- [ ] **Step 1: Add tests for extracted helpers**

Add tests that import prompt-building and provider-routing helpers from their new modules and assert the same behavior currently provided by `run_bakeoff.py`.

- [ ] **Step 2: Verify tests fail**

Run from `GeoSpoiler-RAG-Hybrid`:

```powershell
python -m unittest tests.test_model_bakeoff
```

Expected: imports fail before the new modules exist.

- [ ] **Step 3: Move prompt helpers**

Move `_case_user_prompt()` and `_messages_for_case()` from `run_bakeoff.py` into `eval/model_bakeoff/prompts.py`. Re-export or import them in `run_bakeoff.py` so behavior remains unchanged.

- [ ] **Step 4: Move provider helpers**

Move `_base_url_for()`, `_api_key_for()`, `_post_chat_completion()`, and `_call_chat_completion()` into `eval/model_bakeoff/providers.py`. Keep dependency injection for HTTP posting so tests do not require network.

- [ ] **Step 5: Verify bakeoff tests**

Run from `GeoSpoiler-RAG-Hybrid`:

```powershell
python -m unittest tests.test_model_bakeoff
```

Expected: bakeoff tests pass.

### Task 5: Full verification and commit

**Files:**
- Verify all changed files.

- [ ] **Step 1: Run full unittest suite**

Run from `GeoSpoiler-RAG-Hybrid`:

```powershell
$env:GEOSPOILER_NO_NETWORK='1'; python -m unittest discover -s tests -p 'test_*.py'
```

Expected: all tests pass.

- [ ] **Step 2: Run golden checks if credentials/local data permit**

Run from `GeoSpoiler-RAG-Hybrid`:

```powershell
python tests/test_golden_set.py
python source_selection_golden.py
```

Expected: pass rate does not drop. If environment blocks network/model calls, record that explicitly.

- [ ] **Step 3: Check whitespace and git status**

Run from repo root:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only intentional files changed.

- [ ] **Step 4: Commit**

Run from repo root:

```powershell
git add GeoSpoiler-RAG-Hybrid/loader/query.py GeoSpoiler-RAG-Hybrid/loader/card_context.py GeoSpoiler-RAG-Hybrid/retrieval/composer.py GeoSpoiler-RAG-Hybrid/eval/model_bakeoff GeoSpoiler-RAG-Hybrid/tests GeoSpoiler-RAG-Hybrid/docs/superpowers/plans/2026-06-18-close-architecture-review.md
git commit -m "refactor: close architecture review findings"
```
