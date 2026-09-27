# PuppyOne data migrations

Supabase owns schema history in `../migrations`. This directory contains
production-data transformations that cannot safely run as one bounded schema
transaction, including Python/application-secret backfills and staged online
changes.

`schema_history_baseline.json` is the one-time, immutable hash inventory of
schema files already shared through Qubits when this governance model was
adopted. It permits exact historical promotion without exempting any new file
from current policy.

## Baseline archives

All eight pre-B1 artifacts, including SQL and Python jobs, live unchanged in
`../archive/before_b1/data_migrations/`. New artifacts are added here. The shared
catalog resolves both locations by ID and rejects duplicates. Archive hashes
are pinned in `../baselines/b1/manifest.json`; existing completion receipts keep
their original IDs and checksums. Moving an artifact never marks it completed.
B1-incompatible jobs are retired on B1 databases, but remain available to older
installations that still satisfy their original schema prerequisites.

`python3 scripts/database_history.py data-path <migration_id>` (repository root)
resolves a location without connecting to a database. Release workflows use the
same resolver. `puppyone-db verify-external-state <id>` verifies operator-run
postconditions in a read-only transaction, without creating a completion receipt.

## Artifact contract

Every released migration is an immutable directory:

```text
<migration_id>/
├── manifest.yml
├── run.sql | run.py
├── verify.sql
└── README.md          # optional operator context
```

The manifest may select `sql` or `python`; it never contains an arbitrary shell
command. `verify.sql` MUST fail closed when the intended postcondition is not
true. Python jobs MUST be idempotent because their work and receipt publication
cannot share one database transaction. They MUST be self-contained single-file
jobs and MUST NOT import PuppyOne application modules; the isolated runner does
not expose repository code through `PYTHONPATH`.

## Commands

Run from `backend/` after installing the locked environment:

```bash
uv run puppyone-db lint
uv run puppyone-db list
DATA_MIGRATION_DATABASE_URL='postgresql://...' \
  uv run puppyone-db plan <migration_id>
DATA_MIGRATION_DATABASE_URL='postgresql://...' \
  uv run puppyone-db run <migration_id>
```

`DATA_MIGRATION_DATABASE_URL` should use a session-mode pooler or direct
PostgreSQL endpoint that supports advisory locks. Connection credentials are passed to libpq through
environment variables, not on the process command line.

Hosted runs bind the API URL and PostgreSQL URL to the same protected Supabase
project ref. Direct and session-pooler URLs are supported; an unprovable target
pairing is rejected before execution.

## Release sequence

1. Merge and deploy the additive Expand schema.
2. Dispatch the data migration in Qubits and verify its receipt.
3. Dispatch the same artifact in Production and verify its receipt.
4. Deploy application cutover code.
5. In a later PR, copy the reviewed contract SQL into
   `supabase/migrations/<timestamp>_contract_<name>.sql`.

The Contract MUST carry both `requires-data-migration` and the immutable
`data-migration-checksum` marker, and must compare that checksum in its receipt
guard. This prevents an unrelated or stale receipt from authorizing data loss.
If the artifact includes `contract.pending.sql`, the promoted migration must be
an exact byte-for-byte copy; repository policy rejects an edited copy.

Never edit a released artifact. Add a new forward migration instead.

## What belongs in the Supabase migration folder

Small, pure-SQL changes may remain in `supabase/migrations` when they are
bounded, transactional, and require no pause, external secret, application
runtime, or external service. Seed/demo data belongs in `supabase/seed.sql`.
Everything else belongs here.
