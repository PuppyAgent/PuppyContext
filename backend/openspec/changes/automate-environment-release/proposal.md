# Change: One merge executes the complete environment release

## Why

Requiring production migration receipts before merging the migrations creates
a circular dependency. Separate schema/data jobs and independent application
autodeploy cannot coordinate a populated breaking upgrade.

## What Changes

- Premerge admission requires an isolated populated Docker upgrade; production
  receipts are generated after merge by the environment release coordinator.
- Use one checksum-bound phase plan and Python executor for Docker and hosted
  upgrades. Preserve immutable migration bytes and existing receipt contracts.
- Serialize build, measured drain, restore-tested backup, schema/data phases,
  deployment of the exact source, and bounded authenticated acceptance.
- Isolate hosted credentials and diagnostics on restricted release runners.

## Impact

Affected capability: database-release-governance. Affected code: release
workflows, portable migration runner, Docker installation tests and existing
release architecture. The user approved these four changes on 2026-10-09.
This implementation does not authorize merging main or mutating production.
