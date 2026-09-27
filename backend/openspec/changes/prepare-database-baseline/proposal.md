# Change: Prepare a reproducible Cloud database baseline

## Why

Cloud currently replays 102 SQL migrations for a new database. The user approved
the versioned-baseline architecture and requested research plus a concrete
baseline candidate. Generating SQL alone is insufficient: application triggers
on Auth tables, RLS/ACLs and fresh-install reference data must also survive.

## What Changes

- Generate a flattened baseline from immutable migrations in a disposable
  Supabase PostgreSQL 17 stack, with no hosted credentials or database URL.
- Verify fresh install equivalence, pgTAP contracts and a populated legacy
  upgrade; publish a checksum-pinned candidate and evidence.
- Keep the current migration/deployment chain intact during preparation.
- Record activation dependencies: historical catch-up, exact schema prerequisites,
  immutable hash policy, hosted history reconciliation and Docker bootstrap.

## Authorization

The user approved the architecture in this conversation and explicitly requested
implementation: “先预研一下然后把这个 baseline 抽象出来…开始做吧”.
This change implements that preparation scope, not production baseline adoption.

## Impact

- Affected capability: database-release-governance.
- Affected paths: scripts/database_baseline.py, supabase/baselines, tests and CI.
- No released SQL, cloud database, runtime API, Pay schema or deployment secret
  changes are part of this preparation.
