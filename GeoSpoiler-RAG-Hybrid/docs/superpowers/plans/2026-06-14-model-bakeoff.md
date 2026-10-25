# Model Bakeoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only model bakeoff layer that can test Chinese political source-preservation risk, cheap western model quality, and select clean China Telegram posts for real-corpus checks.

**Architecture:** Add a self-contained `eval/model_bakeoff` package with deterministic suite loading, OpenAI-compatible model calls, scoring, aggregation, and Telegram post candidate selection. Keep production RAG, retrieval, wiki, reranker, and LightRAG storage unchanged; all outputs go under `artifacts/model_bakeoff`.

**Tech Stack:** Python 3.11, `unittest`, existing `requests`, `telethon`, `.env`/`config.py`, OpenAI-compatible chat completions.

---

### Task 1: Deterministic Core

**Files:**
- Create: `tests/test_model_bakeoff.py`
- Create: `eval/model_bakeoff/__init__.py`
- Create: `eval/model_bakeoff/config_loader.py`
- Create: `eval/model_bakeoff/scoring.py`
- Create: `eval/model_bakeoff/post_selection.py`

- [ ] **Step 1: Write failing tests**

Add tests for restricted YAML model parsing, political-risk scoring, quality scoring, and Telegram post filtering.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_model_bakeoff.py" -v`
Expected: import failures for missing `eval.model_bakeoff` modules.

- [ ] **Step 3: Implement minimal deterministic code**

Implement only local parsing/scoring/filtering code; no network calls in tests.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -p "test_model_bakeoff.py" -v`
Expected: all tests pass.

### Task 2: Bakeoff Runner And Artifacts

**Files:**
- Create: `eval/model_bakeoff/run_bakeoff.py`
- Create: `eval/model_bakeoff/aggregate_report.py`
- Create: `eval/model_bakeoff/models.yaml`
- Create: `eval/model_bakeoff/suites/*.jsonl`
- Create: `eval/model_bakeoff/prompts/*.txt`
- Create: `eval/model_bakeoff/README.md`

- [ ] **Step 1: Write failing tests for artifact formatting**

Extend `tests/test_model_bakeoff.py` with output-record and report checks.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_model_bakeoff.py" -v`
Expected: missing runner/report helpers.

- [ ] **Step 3: Implement runner/report helpers**

Implement dry-run capable runner, per-output JSON files, CSV scores, role recommendations, report, and failures report.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -p "test_model_bakeoff.py" -v`
Expected: all tests pass.

### Task 3: China Telegram Candidate Selector

**Files:**
- Create: `eval/model_bakeoff/select_china_posts.py`
- Modify: `eval/model_bakeoff/post_selection.py`
- Create: `artifacts/model_bakeoff/china_candidate_posts_*.md`

- [ ] **Step 1: Write filtering tests**

Add tests for excluding URLs, media, long posts, and low-signal text.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest discover -s tests -p "test_model_bakeoff.py" -v`
Expected: missing selector behavior.

- [ ] **Step 3: Implement read-only Telethon selector**

Scan Telegram folder `GeoSpoiler`, choose channels named China/Kitay, skip media and URL posts, rank by political/source-risk keywords, and write candidate artifacts.

- [ ] **Step 4: Run selector**

Run: `python -m eval.model_bakeoff.select_china_posts --limit-per-channel 200 --max-posts 30`
Expected: Markdown and JSONL artifacts under `artifacts/model_bakeoff`.

### Task 4: Verification

**Files:**
- Test: `tests/test_model_bakeoff.py`
- Read-only check: generated artifacts

- [ ] **Step 1: Run focused tests**

Run: `python -m unittest discover -s tests -p "test_model_bakeoff.py" -v`
Expected: all tests pass.

- [ ] **Step 2: Run lint on new package**

Run: `python -m ruff check eval/model_bakeoff tests/test_model_bakeoff.py --config pyproject.toml`
Expected: no new lint errors.

- [ ] **Step 3: Summarize next run options**

Report generated files, candidate China posts, and exact command for Round 0 smoke.
