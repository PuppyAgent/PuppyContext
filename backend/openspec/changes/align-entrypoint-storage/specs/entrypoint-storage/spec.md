## ADDED Requirements
### Requirement: Canonical entrypoint storage
The backend SHALL persist Access bindings by access_surface_id, GitHub bindings
in github_sync_bindings with log.binding_id, and search jobs in search_index_tasks.
Public HTTP and JSON contracts SHALL remain unchanged.

#### Scenario: Existing records survive upgrade
- **WHEN** the data runner upgrades a populated B1 database
- **THEN** IDs, ownership, OAuth references, timestamps, payloads and states survive,
  and non-search uploads remain unchanged.

### Requirement: Safe release boundary
Expand and Contract SHALL be separate releases. Cleanup SHALL require an exact
verified artifact receipt on populated databases and fresh consistency checks.

#### Scenario: Data drift after backfill
- **WHEN** the target differs from the source after a receipt was recorded
- **THEN** Contract aborts atomically without deleting any source data.

#### Scenario: Old worker writes during cutover
- **WHEN** an old worker updates a search task after backfill
- **THEN** the dedicated task record changes in the same transaction.

### Requirement: Backend-only access
RLS and grants SHALL prevent anonymous and authenticated direct database access
to backend-only entrypoint tables, including via temporary views.

#### Scenario: Anonymous request through the legacy GitHub view
- **WHEN** an anonymous client reads or writes github_integrations
- **THEN** PostgreSQL denies access.
