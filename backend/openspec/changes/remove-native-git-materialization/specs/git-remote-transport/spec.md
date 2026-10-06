## ADDED Requirements
### Requirement: Native Git transport without repository materialization
Native full-project Git SHALL serve supported clone/fetch/push operations from
canonical object storage and PG refs without a server-side bare repository or
Git subprocess. Existing objects SHALL NOT be copied to request-local disk.

#### Scenario: Cold concurrent fetches
- **WHEN** independent clients fetch with no local transport cache
- **THEN** objects are read through the admitted object store and packs streamed
- **AND** no request creates a bare repository or complete object-directory copy

#### Scenario: Durable concurrent pushes
- **WHEN** pushes upload new objects and update refs concurrently
- **THEN** only incoming data may use bounded scratch storage
- **AND** existing durability, permission, CAS and atomic batch semantics apply

#### Scenario: Cancelled transfer
- **WHEN** a transfer disconnects, times out or exceeds its budget
- **THEN** its worker, reader pin, buffers and admission slot are released safely
- **AND** uncertain remote publication retains its durable recovery state
