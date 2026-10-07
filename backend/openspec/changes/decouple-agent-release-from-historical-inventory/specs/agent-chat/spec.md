## ADDED Requirements
### Requirement: Bounded release acceptance
Normal Cloud Agent CI/CD MUST NOT require a full scan or successful migration of all historical hosted projects. An operator rollout SHALL identify its selected projects and certify only that scope.

#### Scenario: Unrelated historical corruption
- **WHEN** the selected project's complete source graph is readable and a sibling has unreadable historical objects
- **THEN** current-tree usage reconciliation and selected project activation may succeed without accessing that sibling history
- **AND** the sibling's stored facts remain unchanged and are not certified native-ready

#### Scenario: Concurrent current-tree changes
- **WHEN** an organization project's current snapshot changes during usage capture
- **THEN** activation and the usage baseline roll back together
- **AND** the selected source remains recoverable for retry
