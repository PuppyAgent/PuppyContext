# Change: Align public Synchronize resource contracts

## Why

ISSUE-058's accepted 2026-10-02 architecture decision requires canonical resource APIs and clients. The 2026-10-03 identity fix restored binding ownership, but consumers still use `/integrations` and legacy run fields.

## What Changes

- Implement the approved generic `/synchronize` HTTP contract with explicit SynchronizeBinding/SynchronizeRun response types, request validation and the existing Project authorization policy.
- Reuse the current Synchronize operations and persistence/queue paths. Do not introduce a second state writer, copy records into Access, or perform database classification here.
- Migrate Desktop and Web/shared SDK generic Synchronize clients. Preserve Automation/Workflow product naming, not obsolete leaf resource DTOs.
- **BREAKING for migrated clients:** new create results use `binding`; execution references use `synchronize_binding_id` and `synchronize_run_id`; bindings expose `last_synchronize_commit_id`. No fallback to legacy URLs or Access IDs.
- Retain the existing legacy HTTP surface during ISSUE-058 S2/S3 only. Removing it requires coordinated CLI/GitHub/Access consumers and deployment evidence; compatibility is not final issue acceptance.

## Approval and scope

This implements the already accepted ISSUE-058 sections 5.2/5.4 and the user's instruction to continue implementation in isolated worktrees, test, then merge into local qubits. It does not authorize push, deployment, production data operations, or acceptance-scope reduction.

## Impact

- Backend: generic Synchronize HTTP boundary, authorization manifest, OpenAPI and HTTP regression tests.
- Consumers: Desktop Automation, Web source flows and cloud-core generic Synchronize client.
- No schema/repository/worker migration (ISSUE-049/061), CLI implementation (060), or Git engine work (062).
- Access, database-source and GitHub final HTTP cutovers remain separately tracked inside 058, not implicitly completed here.
