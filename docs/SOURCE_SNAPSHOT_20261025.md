# Source snapshot, 25 October 2026

Repository: `kovr33k/geospoiler-rag`, branch `main`. It was initialized private;
the user explicitly requested public visibility on 26 October 2026 so standard
GitHub-hosted Actions runners can execute without private-repository minute charges.
The application remains in `GeoSpoiler-RAG-Hybrid/`.

This is a new history built from the working files at
`D:/RAG-GEOSP/RAG-GeoSpoiler`, old branch `codex/project-structure`, commit
`5ba0754a85a0f63b8d4a78e78254844f8ae865c5`. The old checkout, its Git history,
uncommitted files and local runtime data are preserved in place.

## Snapshot composition

- Copy tracked project source, tests, fixtures, workflow and documentation.
- Include the five inspected local modifications: application `.env.example`,
  `README.md`, `src/geospoiler/config.py`, `docs/README.md`, `docs/specs/README.md`.
  The first three align defaults and examples with the existing working
  `gpt-6-luna` model; the latter two link the agreed specification and agent rules.
  The working profile remains `luna`, reasoning effort `xhigh`.
- Include the agreed root `AGENTS.md`, `docs/agents/`, the published ingestion
  specification and decision record, and the local copies/publication mapping
  of issues 2–10. GitHub issue bodies remain the tracker authority.
- Exclude tracked `.claude/settings.local.json`, which is workstation state.
- Preserve but exclude the unconfirmed architecture visualization and its QA
  artifacts, and `AGENT_SKILLS_SETUP_DRAFT_20261025.md`. Their original local
  files are untouched.
- Never copy `.git`, `.env`, databases, source corpora, sessions, runtime output,
  caches, logs, media, backups, virtual environments or local credentials.

Each copied file was checked byte for byte with SHA-256 against the original.
The local execution evidence outside both checkouts contains the per-file hash
manifest, original status and original uncommitted patch.

## Verification boundary

The earlier 1185 passed / 4 skipped / 44 subtests result belongs to the old
baseline and is historical evidence. Fresh-copy checks are recorded in issue 2;
the final changed tree requires its own offline regression and actual Windows
and Linux CI in issue 3. Commit creation alone is not a reason to repeat a
valid check. No working graph or source corpus is modified by this transfer.

The two old RAG repositories remain available. Archiving them is conditional on
the verified final cutover in issue 10.

## Cutover update, 26 October 2026

PR #11 is merged into the new `main`; hosted Windows/Linux CI and the bounded
real acceptance cycle passed. The owner confirmed deleting the former public
repository. Its history remains reachable from the private backup; the remaining
`geospoiler-rag-hybrid-backup` is now archived. Original local files and data remain
in place. See the application's final ingestion acceptance report for evidence
and the local launcher route.

## Local consolidation, 26 October 2026

At the owner's request, the active source and Git metadata were moved back to
`D:/RAG-GEOSP/RAG-GeoSpoiler`. This is the sole active checkout, on `main` with
`origin` pointing to `kovr33k/geospoiler-rag`. Runtime data, `.env` and local
unpublished files remain in place; the existing application `.venv` now has the
required dependencies and its editable installation points to this checkout.
Normal commands run directly from `GeoSpoiler-RAG-Hybrid/`; no external launcher
or second source directory is required. The original Git metadata, uncommitted
patch and retained verification evidence are backed up in the existing local
backup directory as `ingestion-migration-20261026.zip`.
