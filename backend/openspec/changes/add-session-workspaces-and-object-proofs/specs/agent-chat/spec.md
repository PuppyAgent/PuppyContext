## ADDED Requirements

### Requirement: Session-owned paused workspaces
The runtime SHALL retain a successful Session's sandbox while idle, pause its
compute and explicitly resume that same resource for the next admitted Run.

#### Scenario: Normal next turn
- **WHEN** the next Run starts before retirement and the resource remains compatible
- **THEN** the original sandbox and Git working directory are reused
- **AND** no complete workspace restore or clone occurs.

#### Scenario: Six-hour idle retirement
- **WHEN** a clean workspace has been unoccupied for six hours
- **THEN** a bounded, fenced worker claims and deletes its exact provider resource
- **AND** new work waits for cleanup confirmation before allocating a new identity.

#### Scenario: Unknown provider result
- **WHEN** pause or deletion acknowledgement is lost
- **THEN** persistent ownership survives and the operation is reconciled or retried
- **AND** a late response cannot change or delete a replacement generation.

### Requirement: Incremental immutable-object publication
The Version Engine SHALL verify every new object and compose its closure with
valid published proofs belonging to the same repository generation under a live pin.

#### Scenario: Small change over long history
- **WHEN** a new commit changes a file and its parents have reusable proofs
- **THEN** validation reads new bytes and bounded dependency metadata
- **AND** physical and metadata work does not grow per historical object.

#### Scenario: Invalid historical dependency
- **WHEN** integrity checking or collection invalidates an object proof
- **THEN** dependent proofs cease to be reusable and publication rejects invalid roots
- **AND** ref, policy and billing transactions cannot report a partial success.
