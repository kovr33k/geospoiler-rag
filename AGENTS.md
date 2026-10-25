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
