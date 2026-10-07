## ADDED Requirements
### Requirement: Formal existing-data migration
The system SHALL migrate supported legacy DB/S3 data using an immutable,
resumable operator artifact without deleting source data or widening access.

#### Scenario: Interruption and retry
- **WHEN** the migration stops after uploading an object
- **THEN** retry verifies durable bytes and resumes without duplicate authority

#### Scenario: Missing object
- **WHEN** a required object cannot be found or verified
- **THEN** the project remains fenced and no native HEAD is published

### Requirement: Local acceptance isolation
CI SHALL build disposable Docker dependencies without hosted credentials.

#### Scenario: Local validation
- **WHEN** the migration acceptance command runs
- **THEN** real SQL and S3 transformations occur only in its owned local stack
