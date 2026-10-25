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
