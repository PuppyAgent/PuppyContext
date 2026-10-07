# Native repository inventory migration

This is an **operator-local** data artifact, not a schema migration side effect.
Stage the reviewed release, stop/drain old writers and GC, then deploy the schema
to Qubits during the coordinated maintenance window.
Keep the native-only application and workers stopped until the inventory,
recovery archive and final Contract steps have succeeded.
Nothing in the schema migration, public CI job, or ordinary application startup
runs this artifact against hosted data. Staging/production release pointers are
deliberately unchanged by this implementation.

## Local acceptance

From the repository root, with Docker, Supabase CLI and the locked backend
environment installed:

```sh
backend/.venv/bin/python scripts/testing/run_native_inventory_migration.py
```

The command creates a uniquely owned local Docker Supabase database and S3
service, applies the real active schema migrations, runs existing SQL contracts,
then runs the immutable migration on synthetic legacy tenants. Application
credentials, hosted DB URLs and developer dotenv files are not inherited. The
Linux test container has read-only source and no host credentials or Docker
socket. Only its exact owned stack is removed afterwards. An unavailable Docker
service fails acceptance; there is no mock or native-PG fallback.

Evidence: `backend/.native-migration-test-results/inventory/{run.json,junit.xml,mut-schema.json}`.
The separate `Native inventory migration (local Docker)` workflow runs the same
command with no hosted secrets. It does not claim deployed-environment or full
product HTTP acceptance. The application runner's existing stricter profile is
unchanged; this is an explicitly separate data-migration profile.

Fixtures cover observed storage *formats*, not a downloaded copy of the 148
hosted test projects. The four unresolved old roots in the earlier metadata
inventory still require a real source lookup before hosted execution.

## Supported conversion and retained facts

- Standard SHA-1 Git loose objects in `version/` or `mut/`, indexed bundle byte
  ranges, and old numbered or immutable chunk manifests. Validate decompression,
  hash, object type, structural edges, same-project location and full closure.
- Canonical Git OIDs, commit parents and object bytes are preserved. Canonical
  loose placements are verified before old location entries for those OIDs are
  removed. Original bundles/chunks and unrelated index entries remain intact.
- Explicit 16-hex legacy raw blob/JSON-tree identifiers are treated as opaque
  old lookup keys. Their source bytes receive a full SHA-256 mapping receipt;
  the script does not pretend their historical short-hash algorithm is known.
  Lists `[T|B, id]` and historical dictionary entry shapes are supported.
- Snapshot-only history becomes deterministic commits labelled **Imported legacy
  snapshot**. Their import sequence follows the original history row order;
  these are not claimed to be original Git commits. The original history rows
  retain authors, timestamps, messages and IDs; the imported commit message also
  embeds those original metadata fields. Migration refs keep historical
  full-project snapshots reachable, including those outside the current HEAD.
- Root branches/tags and tag peel metadata migrate. Existing Scope geometry,
  credentials and memberships are untouched; Scope access never becomes root
  access. Non-root named refs currently fail with an explicit archival-mapping
  requirement. New Scope support is not supplied by this migration.
- Missing roots/descendants, corrupt Git, ambiguous full-project HEAD, foreign
  locations, absent current billing policy and oversized graphs stop migration.
  Nothing silently substitutes an empty tree. An explicitly stored empty-tree
  root gets durable empty-tree bytes and a labelled snapshot commit.
- Current bounds: 64 MiB/object, 256 MiB unique object bodies, 100,000 objects,
  9,999 explicit refs, 200 graph depth, 100,000 logical-tree path visits. These
  limits fail closed; they do not truncate data. Large/deep repositories need a
  separately reviewed forward artifact after inspection.

New native refs become the write authority. The old roots, history, Scope state,
compatibility fields and source objects are retained. GC is blocked in both the
application and SQL while `retain_source_objects` is true, including after
activation, to protect private snapshots/conflict inputs not imported as public
Git refs. Lifting retention or dropping old metadata is a later reviewed cleanup.

## Hosted execution after deployment

1. Stage the reviewed checkout, stop/drain old writers and GC, and deploy the
   schema to Qubits in the maintenance window. Verify
   the revision and supported consumers. This artifact alone does not remove
   old product consumers or implement new Scope support; follow the final
   recovery archive runbook before starting the native-only consumer build.
2. Supply that environment's **matching DB URL, S3 endpoint and bucket** through
   protected environment variables listed in `manifest.yml`. Use a direct or
   session-pooler PostgreSQL connection with the owner migration role. No
   credentials belong in shell arguments, checked-in files, or logs.
3. Run the artifact **without `--apply`** for a read-only source plan:

   ```sh
   cd backend
   uv run python -I ../supabase/data_migrations/20261007_native_repository_inventory/run.py
   ```

   `puppyone-db plan` separately checks artifact prerequisites and receipts;
   it does not inspect S3. Resolve blocked source/policy/format cases first.
4. Confirm writers, delayed/retry jobs, project creation/deletion and GC remain
   stopped and drained. Existing binary processes and physical S3 calls
   must be quiescent. SQL fencing alone does not cancel an already issued S3
   request. This is a maintenance-window migration, not zero-downtime CDC.
5. Execute the portable runner from the exact reviewed checkout:

   ```sh
   NATIVE_MIGRATION_WRITERS_DRAINED=yes uv run puppyone-db run 20261007_native_repository_inventory
   uv run puppyone-db verify 20261007_native_repository_inventory
   uv run puppyone-db verify-external-state 20261007_native_repository_inventory
   ```

   The runner owns the global advisory lock/checksum/receipt. Do not bypass it
   with a direct `run.py --apply` invocation. Each project is frozen in SQL;
   immutable objects are copied and read back; progress flushes in batches of
   200. All projects must prepare before organization activation starts. An
   organization cutover atomically publishes refs, capacity, billing baseline,
   file-policy enrollment, audit and native authority. Existing native usage is
   included in the checked full-organization reconciliation. No policy/limits
   are fabricated to make a project pass.
6. Select the artifact in the environment release pointer when scheduling its
   verification gate:

   ```json
   {
     "migration_id": "20261007_native_repository_inventory",
     "repair_migration_id": "",
     "execution_mode": "operator_local"
   }
   ```

   Existing protected CI runs only `verify.sql` against Supabase and records
   the immutable artifact checksum. It does not perform S3 work. Production uses
   the same artifact only after Qubits deployment and migration acceptance.
7. Continue with the linked recovery archive and gated Contract while writers
   remain stopped. Only after that phase, start the native-only build and verify
   cold native clone/read/write before restoring supported traffic.

## Recovery

An interruption leaves a project frozen/prepared, with its old data intact.
Rerun the **same artifact**: destination bytes and source fingerprints are
checked again, completed object identities are reused, and activation is
idempotent. A failed project prevents any new organization activation in that
run. If a later organization's activation fails, already activated organizations
remain native and the next run resumes the remainder. No successful global
receipt is written until the postcondition passes for the whole inventory.

Investigate missing data or changed fingerprints; never force the state to
`activated`, rewrite released artifact checksums, clear a fence just to resume
old writers, or treat receipt metadata as a backup. After native writes, flipping
the authority flag back would lose those writes. Source deletion and reverse
migration require their own reviewed procedure.

## MUT inventory

The current Python backend owns `src/version_engine`; `mutai` / `mut_engine`
are not runtime dependencies. Applying all current SQL still creates:

- `projects.mut_root_hash` and `synchronize_github_logs.mut_commit_id` aliases;
- six `mut_*` compatibility views over `version_*` tables;
- five old `*_mut_*` compatibility RPC names, with some old publisher bodies
  still referencing aliases/views;
- old storage recognition in the operator migration/archive inventory only.

This migration reads old storage only at the migration boundary. Native durable
reads use canonical `version/` Git objects and PostgreSQL native refs. Removing
MUT SQL names is a later Contract migration after all consumers and retained data
have been checked; this Expand/data artifact does not drop them.

## Completing runtime retirement

This first artifact preserves migration sources. Complete the separately gated
[recovery archive and Contract](../20261007_repository_recovery_archive/README.md)
before deploying the native-only consumer release. That phase also archives
private recovery bytes and removes old current S3 keys. Its committed final
schema is exercised in a second fresh Docker stack at
`backend/.native-migration-test-results/contract/`.
