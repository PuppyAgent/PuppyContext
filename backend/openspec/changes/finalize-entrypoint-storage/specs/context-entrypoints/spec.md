## ADDED Requirements

### Requirement: Final authoritative entrypoint storage
The application SHALL use synchronize_bindings/synchronize_runs, synchronize_github_bindings/synchronize_github_logs, access_tools.access_surface_id and independent import_database_sources. Old table/view/column and resource transport aliases SHALL be retired after verified cutover, not retained as permanent fallbacks. Each SQL object SHALL have one migration owner.

#### Scenario: Existing rows upgrade
- **WHEN** reviewed legacy records pass the migration gates
- **THEN** identifiers, encrypted credentials, tenant/target ownership, watermarks, run history and unrelated uploads remain intact under final names

#### Scenario: Import and Synchronize coexist
- **WHEN** a Project contains a reusable one-time Database Import source and a durable binding
- **THEN** each repository reads its own store and canonical inventories succeed without hiding records or interpreting the Import source as a binding

### Requirement: Evidence-based row classification
Every legacy mixed-store row SHALL have an explicit reviewed disposition and matching fingerprint of the row and its runs. The migration SHALL NOT infer ownership from provider, manual mode, path, name or equal IDs. Dual-use records SHALL have an explicit source/binding reference. Read-only retention SHALL be explicit and SHALL never enable execution.

#### Scenario: Missing or stale decision
- **WHEN** a decision is absent, ambiguous, cross-Project or based on a changed record/run
- **THEN** the data transaction fails with no copy, deletion or completion receipt

#### Scenario: Lost-history prevention
- **WHEN** an Import-only disposition would discard a run or durable binding fact
- **THEN** migration refuses it until the operator supplies a valid disposition preserving that history

### Requirement: Phased guarded migration and recovery
Expand/data and Contract SHALL be separate releases. Contract SHALL check immutable receipts and actual postconditions under locks, while deployment evidence SHALL independently establish producer/client/configuration exit. Application writes SHALL be frozen during cutover. The migration SHALL preserve tightened privileges and SHALL have fresh-install, populated-upgrade, retry, dirty-data and recovery tests against real PostgreSQL.

#### Scenario: Active writer or direct Contract bypass
- **WHEN** old application writes arrive after freeze or a populated database attempts Contract without verified data
- **THEN** the operation fails closed without partial mutation

#### Scenario: Final-schema recovery
- **WHEN** Contract has removed the old schema and transport
- **THEN** recovery uses a matching restore point and binaries or a compatible roll-forward build, retaining accepted post-cutover data and recording the actual environment
