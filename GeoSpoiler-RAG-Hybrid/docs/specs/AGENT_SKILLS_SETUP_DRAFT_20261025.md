# Проект настройки agent skills для RAG

Дата: 25 октября 2026 года.

Статус: проект подтверждён и применён 25 октября 2026 года. Пользователь выбрал новый приватный GitHub-трекер `kovr33k/geospoiler-rag`, затем вариант «AGENTS.md — проект настроек подходит». Корневой `AGENTS.md` и два файла настроек записаны. Репозиторий создан для задач, код не переносился. Ниже сохранён согласованный текст для истории настройки.

Выбранный корневой файл инструкций — `AGENTS.md`. `CLAUDE.md` не создавался. Существующие пользовательские правила optional workflows сохранены.

Отдельный triage-skill не установлен, поэтому полный словарь пяти ролей и файл triage-labels не создаются. Необходимая для `to-spec` метка `ready-for-agent` заведена в новом RAG-трекере и описана в его правилах.

Одноконтекстная структура выбирается по существующей архитектуре. Пустые словарь и ADR заранее не создаются.

## Корневой файл инструкций: AGENTS.md или CLAUDE.md

~~~~markdown
# GeoSpoiler RAG

## Scope and agreed behavior

Work only on GeoSpoiler RAG and its project repositories. Preserve other projects and shared account settings.

Keep the current Luna model, `luna` profile and `xhigh` reasoning effort. Preserve the current query context policy and selected source content.

For ingestion and repository migration work, read [the specification](GeoSpoiler-RAG-Hybrid/docs/specs/RAG_INGESTION_AND_GITHUB_SPEC_20261025.md) and [the decision record](GeoSpoiler-RAG-Hybrid/docs/specs/INGESTION_SPEED_DECISIONS_20261025.md). Preserve unrelated local edits.

## Optional development workflows

When starting a substantial new feature, large refactor, or other work that would benefit from isolation, proactively offer `$using-git-worktrees`. Do not invoke it silently. After the user agrees to this offer or explicitly invokes the skill, read its installed SKILL.md and execute the original workflow fully, subject to applicable host and repository instructions. That agreement authorizes the workflow's setup actions; honor the agreed isolation preference without asking for the same consent again.

When work on a feature/development branch is complete, review has passed, and verification has succeeded, proactively offer `$finishing-a-development-branch`. Do not invoke it silently. After the user agrees to this offer or explicitly invokes the skill, read its installed SKILL.md and execute the original workflow. Integration choices such as merge, PR, keeping the branch, or discarding work remain the user's decision; skill invocation alone does not choose an option.

Offer each skill once per relevant stage, and do not repeat a declined offer unless circumstances materially change. A recommendation alone does not pause an existing implementation request or authorize another skill or agent to invoke either workflow.

## Optional Ponytail review

Proactively offer `$ponytail-review` only when there is a concrete reason to suspect unnecessary complexity: a substantial implementation or refactor with complexity signals; new dependencies, wrappers, factories, interfaces or abstractions; a diff or number of changed files unexpectedly large for the task; duplication or several similar execution paths. State the specific reason in the offer.

Usually offer after the implementation works and relevant checks pass, before ordinary code review and final CI. A later offer is appropriate if code review reveals a concrete complexity concern. Do not offer for minor edits, documentation, formatting or obvious local bug fixes. Offer once for the same concern; repeat only when circumstances materially change. An offer does not pause authorized implementation work.

Run `$ponytail-review` only after the user explicitly agrees to the offer or explicitly requests the skill. An offer alone does not authorize invocation by this agent, another agent or another skill. Keep implicit invocation disabled. After any accepted simplifications, rerun the checks affected by those changes.

## Agent skills

### Issue tracker

Issues and specs use GitHub Issues in `kovr33k/geospoiler-rag`. Before reading, creating or updating a ticket, read [issue tracker conventions](docs/agents/issue-tracker.md). Always select that repository explicitly.

### Domain docs

Single-context layout. Before exploring domain behavior or writing a spec, read [domain documentation rules](docs/agents/domain.md).
~~~~

## docs/agents/issue-tracker.md

~~~~markdown
# Issue tracker: GitHub

Issues and specs for GeoSpoiler RAG live in [kovr33k/geospoiler-rag](https://github.com/kovr33k/geospoiler-rag/issues), a private repository.

Use the `gh` CLI. Always supply `--repo kovr33k/geospoiler-rag`; the current checkout's older remotes are not the tracker authority. With GitHub API operations, use the exact `repos/kovr33k/geospoiler-rag` endpoint.

## Conventions

- Create an issue with `gh issue create --repo kovr33k/geospoiler-rag --title "..." --body-file <utf8-file>`. Preserve actual newlines in multiline bodies.
- Read an issue with `gh issue view <number> --repo kovr33k/geospoiler-rag --comments`. Inspect its body, labels and comments.
- List issues with `gh issue list --repo kovr33k/geospoiler-rag --state open --json number,title,body,labels`, adding relevant label filters.
- Apply labels with `gh issue edit <number> --repo kovr33k/geospoiler-rag --add-label <label>`.
- Publishing a spec means creating a GitHub issue, not merely saving a local Markdown copy.
- Fetching a ticket means reading the GitHub issue and its comments.
- Keep a published spec open while its implementation remains incomplete.

## Specification label

`ready-for-agent` means that an agreed specification is ready for implementation. The `to-spec` workflow applies this exact label after its required scope and test-boundary checks.

This setup does not define the other triage roles or install the optional `triage` skill.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Project boundary

This tracker is for GeoSpoiler RAG only. Its repository, labels and issues are project-local. Preserve other projects and shared account, billing and organization settings.
~~~~

## docs/agents/domain.md

~~~~markdown
# Domain Docs

## Layout

GeoSpoiler RAG is a single-context project. Its domain glossary, when present, is `CONTEXT.md` at the Git root. Architectural decision records, when present, live in `docs/adr/`.

## Before exploring

- Read `CONTEXT.md` when it exists, and use its established vocabulary.
- Read decision records relevant to the area being changed.
- If these files do not exist, proceed without creating placeholders or treating their absence as a blocker. Create domain records lazily through the domain-modeling workflow when terms or decisions are actually resolved.
- For the existing architecture and data contracts, consult `GeoSpoiler-RAG-Hybrid/ARCHITECTURE.md` and `GeoSpoiler-RAG-Hybrid/DATA_CONTRACTS.md`.
- For this ingestion work, consult the specification and decision record linked from the root instruction file.

## Writing

Use existing terms such as normalized material, Enriched v2 card, source provenance, YouTube generation, local index and explicit LightRAG load. Keep glossary definitions separate from implementation plans.

If a proposed change conflicts with an architectural decision, identify the conflict before changing that behavior.

Domain documentation and tracker setup apply only to GeoSpoiler RAG.
~~~~

## Граница выполнения

Настройка создаёт только эти файлы для текущего RAG, а затем позволяет опубликовать подготовленную спецификацию в выбранном RAG-трекере. Перенос кода, изменение remotes, архивация старых репозиториев, CI и реализация конвейера выполняются отдельно по спецификации. Luna и `xhigh` сохраняются.

