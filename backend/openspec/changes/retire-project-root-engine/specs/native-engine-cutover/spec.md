## ADDED Requirements

### Requirement: One runtime repository authority
The application SHALL use native Git repository refs for supported reads and
writes and SHALL NOT fall back to the retired project-root engine or namespace.

#### Scenario: An unmigrated project reaches the final application
- **WHEN** no active native repository exists
- **THEN** the operation fails explicitly without reading old roots or publishing
  through the former protocol.

### Requirement: Verified data preservation before Contract
The system SHALL preserve content, history and private recovery facts before
dropping compatibility schema. Private objects MUST NOT become public Git refs.

#### Scenario: A private recovery snapshot is migrated
- **WHEN** its referenced object graph is verified and archived
- **THEN** its mapping remains durable without granting repository fetch access.

### Requirement: Local upgrade acceptance
The cutover SHALL supply local Docker tests for populated upgrades, idempotent
data conversion, corruption rejection and the gated final schema.

#### Scenario: A Contract lacks a required receipt
- **WHEN** a populated database has not completed the data artifact
- **THEN** Contract fails atomically without deleting schema or source facts.
