## ADDED Requirements

### Requirement: Bounded Agent data operations
The runtime SHALL access persistence through named operations with measured database round-trip budgets.
#### Scenario: renewal
- **WHEN** an active execution renews its lease
- **THEN** one database request checks fence, cancellation, deadline and authorization/configuration revision
- **AND** no full admission or configuration service is invoked.
#### Scenario: context race
- **WHEN** permissions or Agent configuration change after context loading and before submission or publication
- **THEN** the atomic command rejects the obsolete context without publishing an unauthorized operation.

### Requirement: Batched durable response
The runtime SHALL preserve response content and event order while persisting bounded text batches.
#### Scenario: fragmented reply
- **WHEN** Pi emits seventy text fragments
- **THEN** the client receives the exact complete text through durable events or snapshot
- **AND** writes scale with bounded batches rather than fragment count.
#### Scenario: failure at a barrier
- **WHEN** persistence fails before checkpoint or terminal acknowledgement
- **THEN** the runtime does not confirm unpersisted progress and recovery preserves established fencing and side-effect semantics.

### Requirement: Measured reply acceptance
The delivery SHALL test complete replies against real PostgreSQL, PostgREST and an isolated Pi worker and record database attempts, concurrency and stage timing.
#### Scenario: performance regression
- **WHEN** a new call site adds unbounded queries or exceeds a named operation budget
- **THEN** automated architecture or real transport budget tests fail.
