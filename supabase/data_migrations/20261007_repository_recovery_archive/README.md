# Repository recovery archive and final cutover

This artifact completes the native inventory migration. It preserves private and
unclassified source bytes, verifies their destination, and only then removes the
old current S3 keys. It does not grant access to private recovery content.

## Release order

1. Stop and drain API writes, background/queued producers, initialization,
   project deletion and GC. Wait for outstanding physical S3 calls to settle.
   Keep clients in maintenance until the final application is ready.
2. Apply `20261007010000` through `20261007080000`. The Expand updates
   deletion inventories for the native-only consumer release, so old deletion
   workers must remain stopped throughout this coordinated upgrade.
3. From the reviewed release checkout, run the read-only plans for
   `20261007_native_repository_inventory/run.py` and this `run.py`. Supply the
   matching DB/S3 environment through protected variables in each manifest.
4. Set `NATIVE_MIGRATION_WRITERS_DRAINED=yes` only after the drain. Execute the
   existing portable `puppyone-db run` runner for
   `20261007_native_repository_inventory`, followed by
   `20261007_repository_recovery_archive`. The runner owns checksums, advisory
   locks, dependency checks, verification and durable receipts. See
   [inventory runbook](../20261007_native_repository_inventory/README.md) for
   the exact inventory invocation. For the archive, from `backend/` run:

   ```sh
   NATIVE_MIGRATION_WRITERS_DRAINED=yes uv run puppyone-db run 20261007_repository_recovery_archive
   uv run puppyone-db verify 20261007_repository_recovery_archive
   uv run puppyone-db verify-external-state 20261007_repository_recovery_archive
   ```

   The standalone script defaults to read-only planning.
5. Review the verified receipts and per-project manifest hashes. Promote
   `contract.pending.sql` unchanged into **a separate schema release**. It must
   never run in the same automatic schema pass as Expand. Contract refuses any
   non-native/not-ready project, missing archive, incompatible receipt, live
   lease or mismatched duplicate field. There is no `CASCADE` cleanup.
6. Start this native-only application/worker build, verify cold Git and Product
   reads/writes, then resume traffic and native derived workers/GC. Do not restart
   an old backend binary. Returning to the old engine requires an explicit
   paired DB/object recovery and reconciliation of subsequent native commits.

No hosted database, S3 bucket, release pointer or deployment is changed by local
CI or application startup. Maintenance and hosted execution remain release work.

## Preserved data

- Public content and full-project history have already become canonical Git
  objects and refs in the first artifact. Existing standard Git OIDs and ancestry
  survive; snapshot-only history receives deterministic labelled import commits.
- Commit/scope/ref/conflict/outbox/shadow records are copied into an immutable
  source manifest. Typed referenced graphs receive canonical Git OID mappings.
  Private mappings are GC retention roots, **never advertised/fetchable refs**.
- Every current key under the exact former project prefix is copied into
  `version/<project>/recovery-archive/sha256/<first2>/<full-sha256>`.
  Private manifest bytes under `shadow-snapshots/<project>/` use the same verified
  archive flow; their previews remain private and do not become Git refs.
  Former 16-character IDs under `version/<project>/objects/<2>/<14>` are also
  archived and removed. Normal 40/64-character Git object keys are not swept.
- Each byte copy is verified by full SHA-256 and exact length. ETag conditions
  guard the source copy; an unexpected key, changed source inventory, changed DB
  facts, wrong Project locator, missing object or corrupt destination aborts.
- The artifact preserves unknown/orphan source bytes as opaque recovery data;
  it does not guess their semantics or expose an application fallback reader.
- The exact former factory Project prompt is preserved in the source manifest
  and replaced with standard Git instructions. Custom prompts are unchanged.
  New Projects receive the native prompt through the schema default.
- Old-prefixed object-location rows are preserved in the manifest, then removed.
  Canonical packs not belonging to the retired namespace retain their ordinary
  storage lifecycle. This is not a general bucket compaction operation.
- Object version history in versioned buckets is retained: deletion removes the
  current key, not historical versions. Review bucket retention separately.
- Private canonical Git graphs count toward physical capacity; forensic archive
  copies are retention overhead. Logical storage billing remains content-based.

The final SQL removes duplicate old fields/views/functions, rewrites remaining
canonical function dependencies, renames indexes/constraints/sequences/policies
without resetting IDs, removes old root publishers, and revokes service writes to
historical engine tables. Historical rows remain readable as archived metadata;
they are not a second version authority. Published migrations and immutable
artifact identifiers/checksums retain their original spelling for replay/audit.

## Failure and retry

Copy/verify completes before cleanup. A durable manifest checkpoint precedes the
first delete. On retry, **all** archive destinations and surviving source bytes
are checked before deleting anything else. Partial cleanup can resume without
re-reading an already removed source graph. Source/GC fences remain until final
capacity registration and archive completion succeed. Never clear a fence just
to silence a migration error.

Limits: the converter uses 64 MiB/object, 256 MiB/graph, 100,000 objects and 200
levels. Opaque copies are streamed in 1 MiB chunks, limited to 5 GiB/object and
1,000 listing pages per prefix. Unsupported or oversized input fails explicitly;
prepare a reviewed forward artifact instead of truncating or editing a released
artifact.

## Local acceptance

```sh
backend/.venv/bin/python scripts/testing/run_native_inventory_migration.py
```

The command owns two fresh Docker PostgreSQL/S3 stacks. `inventory` exercises
populated upgrades, private archive retention, interrupted deletion, corrupt
archive refusal, retries, producer idempotency/CAS, historical restore,
initialization visibility (SHA-1/SHA-256), and native index claims. `contract`
commits the final SQL in a separate fresh stack and then creates/writes/reads a
new native repository. The failed-input fixtures in the first stack are retained
until stack teardown; they are not deleted to make Contract pass.

Evidence is under `backend/.native-migration-test-results/{inventory,contract}/`.
These are synthetic format fixtures, not a migration of the hosted projects.
New Scope/MCP scoped filesystem/Sandbox scoped endpoints are deliberately not
implemented here. Old credentials remain restricted; they never become root
credentials. Native full-Project Git, Product APIs and bound workers are the
supported runtime path.
