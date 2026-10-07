# Retire the project-root engine

## Why
The user approved a single native Git engine on 2026-10-07. Renaming the old
root-first publisher does not achieve that: current Product and worker consumers,
the database contract, and stored object graphs must converge together.

## What Changes
- **BREAKING** Remove runtime storage namespace fallback and database name aliases.
- Preserve existing content, history and private recovery facts through immutable
  operator-run data artifacts; never widen old Scope access to repository access.
- Route supported repository consumers and creation through native refs and
  checked Product publication; retire the old project-root publication path.
- Supply a gated Contract for a subsequent release, with local Docker upgrade
  and fresh-install acceptance. No hosted mutation or deployment is authorized.

## Impact
- Version Engine, Product/worker consumers, project creation, storage and schema.
- Existing released migrations and artifact checksums remain immutable history.
- Native Scope projections remain separate; old Scope semantics are not restored.
