# Review Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce fragile project areas highlighted by external review while preserving the existing public CLI/RAG behavior.

**Architecture:** Start with the highest semantic risk: answer postprocessing must not inject topic facts after retrieval. Then move tests out of the project root, migrate the hot source metadata index to SQLite behind the existing API, split Instagram extraction by failure boundary, and add bounded enrichment concurrency without changing artifact contracts.

**Tech Stack:** Python 3.11, unittest, ruff, sqlite3, existing RAG/normalizer/enricher packages.

---

### Task 1: Remove Fact-Injecting Answer Postprocess Rules

**Files:**
- Modify: `loader/answer_postprocess.py`
- Modify: `tests/test_loader.py`
- Check: `tests/test_golden_set.py`, `source_selection_golden.py`

- [x] Write tests that assert `_postprocess_answer_text()` does not add missing Ukraine, Russia, or Germany topic facts to an answer.
- [x] Run the targeted postprocess tests and verify they fail on current behavior.
- [x] Remove only the fact-injecting thematic append/prepend rules.
- [x] Keep wording guardrails that normalize spelling, neutralize overclaiming, and clarify absent funding data.
- [x] Run `python -m unittest tests.test_loader.AnswerPostprocessTests -v`.
- [x] Run broader loader/query tests.
- [x] Run live golden checks and repair retrieval/prompting if quality drops.

### Task 2: Move Tests Into `tests/`

**Files:**
- Move: root `test_*.py` to `tests/`
- Modify: `pyproject.toml`
- Modify: `README.md`, `OPERATIONS.md`
- Add: `tests/__init__.py`

- [x] Move all root test modules into `tests/`.
- [x] Replace per-test `sys.path.insert(0, str(Path(__file__).parent))` with project-root insertion.
- [x] Update unittest discovery config and docs.
- [x] Update ruff per-file ignore from `test_*.py` to `tests/test_*.py`.
- [x] Run `python -m unittest discover -s tests -p "test_*.py" -v`.

### Task 3: SQLite Source Metadata Index

**Files:**
- Modify: `loader/storage.py`
- Modify: `loader/ingest.py` only if call signatures need cleanup
- Modify: `cli_query.py` only if lookup can become narrower
- Modify: `tests/test_loader.py`, `tests/test_main.py`

- [x] Add tests for SQLite-backed upsert, removal, JSON migration, and unchanged `load_source_metadata_index()` return shape.
- [x] Implement `rag_storage/doc_metadata_index.sqlite` with `source_path TEXT PRIMARY KEY` and `metadata_json TEXT NOT NULL`.
- [x] On first load, migrate existing `doc_metadata_index.json` if SQLite is absent.
- [x] Keep `load_source_metadata_index()` as public compatibility API.
- [x] Run loader and query-source tests.

### Task 4: Split Instagram Handler By Failure Boundary

**Files:**
- Create package: `normalizer/instagram/`
- Keep wrapper: `normalizer/instagram_handler.py`
- Modify: `test_handlers.py`

- [x] Move cache helpers into `normalizer/instagram/cache.py`.
- [x] Move URL/info/download helpers into `normalizer/instagram/downloader.py`.
- [x] Move audio/transcription helpers into `normalizer/instagram/audio.py`.
- [x] Move frame extraction, phash, and empty-frame filtering into `normalizer/instagram/frames.py`.
- [x] Move OCR/image description/summary helpers into `normalizer/instagram/vision.py`.
- [x] Move final text assembly and review queue handoff into `normalizer/instagram/builder.py`.
- [x] Keep `extract_instagram_text()` and tested private wrappers compatible until tests migrate.

### Task 5: Bounded Async Enrichment

**Files:**
- Modify: `enricher/llm_enricher.py`
- Modify: `enricher/pipeline.py`
- Modify: `config.py`
- Modify: `test_enricher.py`, `test_pipeline_stats.py`

- [x] Add bounded worker boundary while keeping sync wrappers.
- [x] Add configurable enrichment concurrency.
- [x] Process independent posts concurrently, but serialize progress/output writes.
- [x] Preserve hard timeout semantics.
- [x] Run enrichment and pipeline stats tests.

### Final Verification

- [x] `python -m unittest discover -s tests -p "test_*.py" -v`.
- [x] `python -m compileall -q .`
- [x] `python -m ruff check . --config pyproject.toml`
- [x] `python source_selection_golden.py`
- [x] `python tests/test_golden_set.py`
