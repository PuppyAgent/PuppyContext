## ADDED Requirements

### Requirement: Atomic byte-preserving ref publication
The native repository profile SHALL compare expected ref identity, not tree identity, and atomically publish direct refs and HEAD with transaction results, reflog, audit and outbox. Ref names SHALL retain their original bytes. SHA-1 and SHA-256 identities SHALL NOT be mixed within a repository.

#### Scenario: Same tree does not imply the same head
- **WHEN** two transactions expect the same old commit and propose different commits sharing a tree
- **THEN** at most one transaction commits and the other's old-OID comparison fails

#### Scenario: One stale ref in an atomic batch
- **WHEN** any expected ref, including an expected absent ref, differs from the locked repository state
- **THEN** no ref in the batch changes and a durable rejected result records the observed states

#### Scenario: Unborn and detached HEAD
- **WHEN** a transaction compares and changes HEAD between a branch symref and a direct commit
- **THEN** its expected symbolic target or direct OID is compared without dereferencing, and a symref may target an unborn branch

#### Scenario: Bytes and namespace conflicts
- **WHEN** valid ref names contain non-UTF8 bytes
- **THEN** database storage and result encoding preserve the bytes without Unicode replacement
- **AND** malformed names, duplicate updates, and file/directory ref conflicts are rejected

### Requirement: Durable result identity
Publication SHALL bind each operation key to the repository, admitted actor and exact request. A committed or rejected result SHALL be queryable and replayable without repeating publication effects.

#### Scenario: Response lost after commit
- **WHEN** a caller retries the same operation after losing its response
- **THEN** it receives the original result and no duplicate reflog, audit or outbox records are created
- **AND** reuse of the key with a different request is rejected

### Requirement: Fail-closed publication prerequisite
A new direct target SHALL require a repository-, object-format-, generation- and GC-epoch-bound unexpired durable closure receipt. Only the trusted backend SHALL invoke publication; application roles SHALL NOT directly mutate canonical refs or enable repository authority.

#### Scenario: Unverified or invalidated target
- **WHEN** a target lacks a matching receipt, has the wrong kind, or its receipt has expired or been fenced
- **THEN** no refs or publication events change

#### Scenario: Direct Data API access
- **WHEN** anon or authenticated attempts to read or mutate authority tables or execute publication
- **THEN** database privileges deny access, independently of RLS bypass by backend roles

### Requirement: Expand is not cutover
Schema expansion SHALL NOT switch existing repositories or rewrite user data. Native publication SHALL remain disconnected from product/transport entrypoints until storage/GC, policy, lifecycle, consumers and migration gates pass. Legacy publication SHALL be fenced for any repository explicitly switched to native authority by a future reviewed migration.

#### Scenario: Existing writer after expansion
- **WHEN** a legacy repository has no native-authority metadata
- **THEN** its existing RPCs, roots, history and credentials retain their contracts

#### Scenario: Stale legacy worker after a cutover fence
- **WHEN** a legacy root, scope-head, commit-history or named-ref writer addresses an explicitly native repository
- **THEN** the database rejects the write rather than acknowledging a second authority
