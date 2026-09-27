# Entrypoint storage backfill (ISSUE-049)

Release A: apply `20260927010000_expand_entrypoint_storage.sql`, run this artifact
through `puppyone-db run 20260927_entrypoint_storage_backfill`, verify, then deploy
the new backend. Both environment release pointers select the CI lane. Existing
B1/history-adoption gates and checksums remain intact.

The SQL runner owns one transaction and its receipt. Lock wait is capped at five
seconds, execution at five minutes. A lock timeout or verification failure rolls
back the whole job and earns no receipt. Retry through the runner after resolving
contention or inconsistent data. Do not edit this artifact after release. Large
installations that cannot complete within the bound need a separately reviewed
batched artifact; do not disable the timeout on a live database.

Search-task keys are `uploads.id` (the tool ID), not the legacy DTO's `id=0`.
Every job column is preserved except the fixed `type=search_index` discriminator.
Old and new task writes are mirrored transactionally during the rollback window.
The original uploads creator FK deletion behavior is preserved. Project deletion
and its storage-principal inventory include the new table.

`contract.pending.sql` is NOT an active schema migration. In a separate release,
after all old API/worker processes and old queued/delayed/retry jobs are gone and
the old-application rollback window ends, verify the artifact in both deployment
environments and copy it unchanged to a new timestamped `_contract_` migration.
Never merge Expand and that promotion as one release. The reviewed Contract
removes transition columns, view and triggers only after checking current row
identity and the immutable receipt under lock. An empty install needs no old-data
receipt. Post-Contract verification and completed-job retries remain safe.

Rollback before Contract: the Phase-1 backend still uses the compatibility names
and sees new writes. Rollback after Contract: roll forward with a new reviewed
migration/application fix, or restore the complete pre-Contract backup during a
write freeze. Do not reintroduce only an old binary or independently restore a
single mirrored table.

Local proof: `backend/.venv/bin/python scripts/test_entrypoint_migration.py` owns
its temporary Supabase stack, uses synthetic records, and accepts no hosted DB URL.
This code delivery does not attest that any shared environment has been migrated
or that its workers have drained.
