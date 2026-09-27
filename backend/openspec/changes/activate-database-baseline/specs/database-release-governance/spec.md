## ADDED Requirements

### Requirement: Archived baseline source history
The repository SHALL keep the executable baseline and later migrations in
`supabase/migrations`, and preserve covered sources unchanged in a separately
checksum-verified archive that normal deployment does not execute.

#### Scenario: Fresh installation
- **WHEN** an empty Supabase database applies the active migration directory
- **THEN** B1 and later migrations execute without replaying archived SQL

### Requirement: Verified existing database adoption
Baseline adoption SHALL verify complete source history and schema before
atomically preserving original history and replacing covered tracking rows.
It MUST NOT rerun baseline DDL or manufacture data-job completion receipts.

#### Scenario: Existing populated database
- **WHEN** history and schema match the approved baseline
- **THEN** history adoption preserves all business rows and subsequent db push is clean

#### Scenario: Drift or incomplete upgrade
- **WHEN** a source version is missing or schema permissions differ
- **THEN** adoption fails without changing migration history or customer data

### Requirement: Complete baseline data archive
The repository SHALL archive every pre-B1 data artifact alongside schema history,
including its manifest, entrypoint, verification, fixtures and operator documentation.
A shared catalog SHALL resolve active and archived artifacts by immutable ID and
checksum. Archiving MUST NOT create completion receipts or rerun completed work.

#### Scenario: Existing completion receipt
- **WHEN** a completed task is moved unchanged into the archive
- **THEN** its original checksum and receipt remain valid and its runner does not repeat it

#### Scenario: Tampered or duplicated archive
- **WHEN** an archived artifact is modified, loses a file, gains a file or is duplicated in the active directory
- **THEN** catalog validation fails before database operations

### Requirement: Complete database release evidence
The hosted pipeline SHALL serialize whole environment releases, preserve pending
runs, and require exact Qubits evidence for data-only as well as schema changes.
Operator verification SHALL use the immutable catalog and read-only database
verification without fabricating task receipts.

#### Scenario: Data-only promotion
- **WHEN** a main promotion changes only data artifacts or release controls
- **THEN** absence of successful Qubits evidence for that exact head rejects promotion

#### Scenario: Failed operator postcondition
- **WHEN** the archived verification SQL rejects the database state
- **THEN** the release fails and no successful attestation is published
