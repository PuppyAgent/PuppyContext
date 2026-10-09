## ADDED Requirements
### Requirement: Operation-owned persistence
The runtime SHALL persist each model/tool boundary through a named, fenced database
transaction, sharing its checkpoint and state updates without a separate renewal.

#### Scenario: Tool completion response is lost
- **WHEN** an identical completed tool command is replayed
- **THEN** its durable receipt is reused without a second event or side effect
- **AND** a different result for that identity is rejected

#### Scenario: Authority changes before tool admission
- **WHEN** a Run is stopped, expired, revoked or superseded
- **THEN** the start command does not admit further tool execution

#### Scenario: Worker artifact uses a different operation protocol
- **WHEN** the worker ready frame has a missing or incompatible operation version
- **THEN** the runtime fails explicitly before model or tool admission

### Requirement: Bounded short-run database traffic
Short real-provider fixtures SHALL measure actual database HTTP attempts including
retries, retain correctness checks, and stay within their documented budgets.
Timed heartbeats SHALL be reported as additional actual requests and bounded by
elapsed time; they SHALL NOT conceal repeated boundary renewals.

#### Scenario: File question
- **WHEN** a cold three-file workspace answers using find and read
- **THEN** the measured executor uses at most 20 database attempts plus actual timed heartbeats

#### Scenario: Batch mutation
- **WHEN** one approved tool creates 1 or 60 small files in an empty repository
- **THEN** the measured executor uses at most 35 database attempts plus actual timed heartbeats and one Git push
- **AND** object correctness and fresh clone contents are verified
