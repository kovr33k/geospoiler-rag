# Post-push review follow-up — execution record (2026-08-13)

## Scope

This record captures the completed follow-up implementation and verification after the post-push review. No production normalized/enriched data, backup contents, or credentials were modified by this documentation update.

## Completed work

### Phase A — media contract and operational hardening

- Added a media/card synchronization contract version so legacy enrichment progress cannot suppress regeneration after media metadata changes.
- Added portable relative media paths while retaining runtime compatibility for stale absolute `media_cache` paths.
- Added failed-media observability: transcription route, attempts, status, latency, and diagnostics are persisted in sidecars/artifacts.
- Added atomic transcription text+sidecar updates with malformed message IDs failing closed.
- Added unified media-backfill operator CLI with explicit `--all`, `--limit`, or `--message-id` selection, dry-run support, and machine-readable reports. Live backfill was not run.
- Replaced the re-enrichment preflight's historical hard-coded `131` corpus count with dynamic accounting invariants; the current `129`-document corpus is accepted when internally consistent. The four-source YouTube invariant remains strict.

### Phase B — review and documentation

- Added regression coverage for media contract invalidation, portable paths, malformed IDs, dynamic re-enrichment preflight, and media backfill behavior.
- Updated the follow-up review documentation and recorded residual operational gates below.

## Verification evidence

- Immutable backup: **160 files**, SHA-256 verified.
- Full test suite: **663 passed, 1 skipped**.
- Ruff: **clean**.
- Media dry-run inventory: **23 STT candidates + 95 visual candidates**.
- No live media egress/backfill was executed; that action requires separate external authorization.

## Current verdict: BLOCKED

Release/closeout remains blocked until the remaining operational gates are explicitly completed:

1. Authorize and execute the live egress/backfill canary, then review its machine report and media/card synchronization results.
2. Rotate/revoke exposed credentials and verify the runtime uses only the replacement secrets. Credential rotation is pending.
3. Re-run the immutable backup and acceptance checks after those gates, preserving the resulting manifests and reports.

The code and test changes are ready for that controlled operational pass; this record does not claim production acceptance.
