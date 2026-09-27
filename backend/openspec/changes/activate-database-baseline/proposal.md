# Change: Activate B1 in the repository and archive its migration sources

## Why

The verified B1 candidate still sits outside Supabase's executable migration
directory. The user explicitly approved moving the 102 covered SQL files into
`supabase/archive/before_b1` and keeping only B1 plus later migrations active.

## What Changes

- Preserve old SQL byte-for-byte in a checksum-pinned, public archive.
- Install one timestamped B1 SQL in `supabase/migrations`.
- Verify an existing database's complete history and schema before atomically
  archiving its history records and adopting B1; never replay baseline DDL there.
- Update policy, historical upgrades, data-job prerequisites and CI fixtures.
- Document the separate hosted rollout; no hosted database is modified by this PR.

## Authorization

The user approved this directory design and implementation on 2026-09-26:
“可以就这么搞好吧”. This includes code and ephemeral verification, not an
unreviewed production reset or removal of customer data.

## Impact

- Capability: database-release-governance.
- Migration consumers, baseline verification, deployment admission, documentation.
- No PuppyPay schema or billing behavior changes.

## Approved extension, 2026-09-27

The user explicitly requested that all pre-B1 data migrations be archived with
the SQL sources, and asked for the surrounding CI/CD gaps to be addressed.
Archive all eight artifacts unchanged under `archive/before_b1/data_migrations`
and the SQL under its sibling `migrations`. Resolve immutable IDs through one
catalog, preserve existing receipts and explicit release selections, and retain
the B1 compatibility rules. Archiving never means completed or unexecutable.
Harden whole-release serialization, data-only promotion checks, operator read-only
verification, and the concrete workflow input/security issues found in review.
