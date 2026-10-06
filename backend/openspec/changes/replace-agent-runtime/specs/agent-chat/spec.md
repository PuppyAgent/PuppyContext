## REMOVED Requirements
### Requirement: Agent SSE endpoint
**Reason**: execution must outlive HTTP subscribers.
**Migration**: clients submit durable runs and subscribe to sequenced events.

### Requirement: Table data 读写回流
**Reason**: the canonical repository revision and Version Engine own file publication.
**Migration**: materialize the authorized repository projection and publish against its original base.

### Requirement: 工具选择逻辑
**Reason**: Pi owns one tool loop for chat and scheduled execution.
**Migration**: derive allowed tools from the existing Agent configuration.

## ADDED Requirements
### Requirement: Durable Agent runs
The system SHALL persist a deduplicated run before acknowledging submission, independently of its HTTP connections.
#### Scenario: reconnect
- **WHEN** a client disconnects after submission and reconnects with a cursor
- **THEN** the original run remains queryable and returns committed events or a snapshot reset.

### Requirement: Fenced isolated execution
The system SHALL execute the pinned Pi harness in a sandbox and reject writes from stale executions.
#### Scenario: lease takeover
- **WHEN** an execution lease expires
- **THEN** a new execution reconciles durable checkpoints and receipts before continuing
- **AND** an uncertain side effect is never automatically repeated.

### Requirement: Confirmed publication
The system SHALL retain unconfirmed changes and distinguish successful, conflicting, failed and unknown publication results.
#### Scenario: lost publication response
- **WHEN** the canonical publisher's response is lost
- **THEN** recovery queries the original publication identity and does not claim success without a durable receipt.
