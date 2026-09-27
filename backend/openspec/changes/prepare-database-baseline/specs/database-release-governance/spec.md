## ADDED Requirements

### Requirement: Reproducible baseline candidate
The system MUST generate baseline candidates from an isolated replay of
checksum-pinned public migrations and MUST preserve application schema owners,
ACLs, RLS, platform-table triggers and required fresh-install reference data.

#### Scenario: Candidate verification
- **WHEN** a candidate is prepared or verified
- **THEN** a fresh baseline installation matches the historical replay
- **AND** database contract tests and a populated old-version upgrade pass
- **AND** no production data, payment schema or secrets enter the candidate

### Requirement: Explicit adoption boundary
The system MUST NOT automatically activate a candidate, delete shared migration
history or reconcile hosted history during baseline preparation.

#### Scenario: Candidate exists in the repository
- **WHEN** the normal Qubits or main database workflow runs
- **THEN** it still uses the existing supabase/migrations chain
- **AND** the candidate is not another migration input

#### Scenario: Old migration or candidate bytes change
- **WHEN** a pinned source migration or candidate artifact is edited
- **THEN** candidate verification fails before database replay
