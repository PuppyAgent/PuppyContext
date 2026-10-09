## MODIFIED Requirements

### Requirement: Protected environment promotion
The system MUST execute each protected branch merge through one shared
environment release program. Premerge CI MUST rehearse the populated upgrade
from the last accepted target version in disposable Docker. It MUST NOT
require the target production database to have already applied the candidate.
The hosted executor MUST serialize ordered schema/data phases, measured
quiescence when required, verified backup, database verification, exact source
deployment and bounded authenticated acceptance.

#### Scenario: One merge releases either environment
- **WHEN** a tested source is merged to qubits or main
- **THEN** the corresponding protected workflow invokes the same upgrade engine
  used in rehearsal and publishes acceptance only after application checks pass

#### Scenario: Contract requires a data transformation
- **WHEN** a pending Contract depends on an immutable data artifact
- **THEN** the engine completes and verifies that artifact before applying the
  Contract, without a separate manual data job between two merges

#### Scenario: Release failure and retry
- **WHEN** a migration or acceptance fails
- **THEN** the release remains unaccepted and a retry reuses verified receipts;
  incompatible application deployment and stale-source rollback are blocked

#### Scenario: Public pull request
- **WHEN** untrusted candidate code runs its upgrade rehearsal
- **THEN** only owned disposable infrastructure and synthetic credentials are
  available, and private release runners cannot execute that candidate

#### Scenario: Independent deployment would race migration
- **WHEN** a hosted service still has an independent GitHub deployment trigger
- **THEN** release admission rejects the configuration before database changes
