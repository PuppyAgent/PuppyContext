## ADDED Requirements
### Requirement: Independent conversation and workspace recovery
The runtime SHALL persist conversation progress independently from provider
workspace recovery and SHALL reuse unchanged workspace recovery points.

#### Scenario: Read-only tool cycle
- **WHEN** the Agent lists or reads files
- **THEN** no complete file collection or Git bundle generation occurs
- **AND** tool receipts retain the unchanged recovery point.

#### Scenario: Partial mutation failure
- **WHEN** a mutation fails after changing files
- **THEN** recovery retains the changed workspace or marks the outcome unconfirmed
- **AND** the tool is not blindly replayed.

### Requirement: Git publication at the run boundary
The runtime SHALL use stock Git and the shared Git service for repository data
and SHALL publish only after the model completes successfully and writers stop.

#### Scenario: Unchanged workspace
- **WHEN** a run completes without file or commit changes
- **THEN** no automatic commit or push is performed.

#### Scenario: Changed workspace
- **WHEN** a run completes with changes
- **THEN** one automatic commit and one publication operation save the candidate
- **AND** retries retain the original candidate and operation identity.

#### Scenario: Concurrent or stale execution
- **WHEN** the branch base changes or the execution fence is superseded
- **THEN** publication is rejected and unpublished work is retained.

### Requirement: Workspace module boundaries
Run orchestration SHALL depend on named ports. Git object codecs and project
object storage SHALL remain inside Version Engine. Provider SDK calls SHALL
remain in the sandbox subsystem.

#### Scenario: Add a provider or query
- **WHEN** a provider adapter or named query is added
- **THEN** it passes the same contract and real transport budget tests
- **AND** callers do not receive generic database or object-store clients.
