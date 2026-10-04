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

### Requirement: Immutable physical chunk placement
A physical chunk or manifest key SHALL identify its exact bytes, independently of the Git object's compressor or chunk partition. Native durability proof SHALL validate Project/object namespace binding and physical integrity; compatibility-only mutable placements SHALL NOT establish such proof. Garbage collection SHALL validate all manifest-owned keys before deletion and SHALL fail closed on corruption or unavailable storage.

#### Scenario: Late parts from another producer
- **WHEN** a producer using different compression or chunk boundaries completes old part PUTs after another publication is acknowledged
- **THEN** the acknowledged closure remains byte-exact and readable
- **AND** its manifests cannot refer to overwritten parts with different bytes

#### Scenario: Existing manifest readers
- **WHEN** an existing reader follows the version-1 manifest's explicit chunk keys
- **THEN** it can read new immutable placements without a new manifest wire version
- **AND** new readers retain existing ordinal-layout compatibility without treating that layout as native durability proof

#### Scenario: Foreign or malformed orphan manifest
- **WHEN** an orphan manifest contains foreign keys, invalid coverage, or cannot be read reliably
- **THEN** no deletion is issued from that manifest and native collection retains its safety fence

### Requirement: Native object-location mutation provenance
Replacing a canonical location SHALL require physical verification of the replacement before index mutation. Native registration SHALL revalidate an uploading publication pin's actor, Project, format, generation, GC epoch and current expiry under the repository lock. Native index removal and new physical deletion SHALL require the current GC token. Direct backend DML SHALL NOT bypass native coordination; missing RPC capabilities SHALL fail closed. Database-owner repair authority and legacy/shadow table ACLs SHALL remain explicit and unchanged.

#### Scenario: Rejected replacement cannot corrupt prior ACK
- **WHEN** a new bundle containing an already published object is missing or corrupt
- **THEN** rejection preserves the earlier object's canonical location and cold readability, not just unchanged refs

#### Scenario: Late index completion after collection
- **WHEN** an old producer resumes after its pin expired and a subsequent GC epoch collected its bundle
- **THEN** registration is rejected even if its async context survived
- **AND** a newer acknowledged closure remains byte-exact and readable

#### Scenario: Sealed retry and stale collector
- **WHEN** publication retries a sealed pin without a ref result
- **THEN** it reuses the sealed proof instead of performing new location writes
- **WHEN** a collector's context survives completion of its GC token
- **THEN** it cannot initiate a new native physical deletion

### Requirement: Coherent native content revisions
Admitted native readers SHALL capture refs, generation, sequence and declared format in one pinned snapshot. Physical object identity and typed graph edges SHALL govern lazy content traversal. Object existence or a rejected receipt SHALL NOT authorize a read. Missing refs SHALL NOT become empty content except for an unborn HEAD or explicit absent-base construction. A product edit through HEAD SHALL guard both its captured symbolic selector and the resolved ref OID.

#### Scenario: Same commit but changed default branch
- **WHEN** a product operation captures HEAD pointing to main and another writer repoints HEAD to topic at the same commit
- **THEN** the original default-branch operation is rejected rather than silently writing the formerly selected branch

#### Scenario: Declared format and readable graph
- **WHEN** an admitted reader selects a commit or typed tag in a SHA-256 repository
- **THEN** its tree identity is read from verified bytes under the captured format, without transport materialization
- **AND** an unrelated stored or rejected-proposal object remains unreadable

### Requirement: Current actor and lifecycle admission
Admitted native publication SHALL revalidate current platform role or full-Project runtime credential facts and a matching live Project write lease in the publication transaction. Cached grants and copied lease contexts SHALL NOT replace these checks. Revocation SHALL serialize with publication, and validity SHALL be rechecked after queued locks. Backend admission RPCs SHALL NOT authenticate caller-supplied actor identities or grant Scope credentials full-repository access.

#### Scenario: Revocation wins a race
- **WHEN** membership, credential or Surface revocation commits before publication acquires the corresponding fact locks
- **THEN** publication fails without changing refs, result, reflog or outbox

#### Scenario: Validity expires while queued
- **WHEN** a credential or lease expires after preflight while publication waits for a repository lock
- **THEN** final admission rejects the mutation and all provisional SQL publication effects roll back

#### Scenario: Recovering an acknowledged result
- **WHEN** the original actor retains current read access but no longer has the original write lease or write mode
- **THEN** the exact original result can be replayed without uploading or publishing again
- **AND** changed request content still fails the original digest check

### Requirement: Atomic retained-object capacity
Native canonical writers SHALL reserve technical object-body bytes and object counts before physical uploads, deduplicating logical object identity within each Project and enforcing both repository and Organization ceilings atomically. The complete verified closure, including named refs and retained objects, SHALL be reconciled before publication. Missing required policy, inventory or checked RPC capability SHALL fail closed. Technical capacity SHALL NOT redefine the existing `storage.logical_bytes` billing metric; canonical publication still requires its applicable billing/entitlement settlement.

#### Scenario: Concurrent sibling repositories
- **WHEN** two repositories in one Organization concurrently request allocations whose sum exceeds the shared ceiling
- **THEN** at most the fitting allocation succeeds and the rejected batch leaves counters and ledger unchanged
- **AND** identical OIDs stored in different Project namespaces do not provide cross-tenant or cross-repository quota credit

#### Scenario: Compressed blob on a non-default ref
- **WHEN** a highly compressed blob would fit by encoded bytes but exceed the decoded-body ceiling on a tag or other named ref
- **THEN** admission rejects before physical PUT and preserves every previously acknowledged root

#### Scenario: Retry and uncertain physical I/O
- **WHEN** the same object is allocated again or the original result is replayed
- **THEN** no duplicate capacity charge is added
- **WHEN** an uploading producer expires or dies without proving physical-I/O quiescence
- **THEN** its in-flight claim does not expire and collection cannot refund that capacity
- **AND** even a deduplicated reupload retains its own unsettled-I/O claim
- **AND** retries sharing one operation/pin cannot settle another storage invocation; pin seal/release does not erase outstanding claims

#### Scenario: Unknown SDK mutation outcome
- **WHEN** a physical PUT or DELETE times out after it might have executed
- **THEN** the native mutation boundary SHALL NOT automatically retry and reinterpret eventual success as quiescence
- **AND** missing or nonzero retry evidence retains the invocation/collection fence until explicit worker and physical-I/O quiescence is established

#### Scenario: Old issuer bypass
- **WHEN** an enrolled repository is published through an older receipt issuer or a receipt without matching capacity attestation
- **THEN** SQL rolls back the new publication rather than treating the missing capability as unmetered access

### Requirement: Preserve logical billing during native publication
Native canonical publication SHALL preserve the existing `storage.logical_bytes` contract for the selected current tree, including per-path multiplicity and excluding external gitlinks. Retained-object admission is separate. Ref publication, logical usage settlement, audit and outbox SHALL be atomic and idempotent, using the current acknowledged entitlement projection and its revision. Missing initialized usage or checked settlement capabilities SHALL fail closed. No invoice semantics are changed by repository migration.

#### Scenario: Shared subtrees and named refs
- **WHEN** two paths share the same subtree or blob
- **THEN** logical current-tree bytes count each path even though physical objects deduplicate
- **WHEN** a named-ref update leaves the selected current tree unchanged
- **THEN** it adds no logical current-tree charge but still requires repository-wide retained-object admission

#### Scenario: Concurrent current-tree writes and entitlement changes
- **WHEN** sibling Projects concurrently change their current trees or an entitlement changes during publication
- **THEN** settlement serializes with the existing Organization usage/entitlement boundary and cannot spend a stale quota projection
- **AND** rejection rolls back refs, usage, audit and outbox together

#### Scenario: Original-result recovery
- **WHEN** a currently authorized reader recovers the exact original ref result
- **THEN** logical usage is not charged again and recovery does not depend on the old write lease or entitlement revision

#### Scenario: Legacy full reconciliation after enrollment
- **WHEN** the legacy root-only reconciler attempts to replace usage for an Organization containing a billing-enrolled repository
- **THEN** its counter and event changes are rejected atomically until a checked native-aware reconciliation path exists
- **AND** the previously settled usage and acknowledged refs remain unchanged

#### Scenario: Complete mixed-authority reconciliation
- **WHEN** the backend reconciles an Organization containing legacy and native repositories, including more Projects than an ordinary UI page
- **THEN** it captures the complete Project/authority/generation/default-HEAD/root inventory, pages measurements without truncation, and verifies current-tree object identities and path-multiplicity sizes
- **AND** before replacing usage it locks the Organization and Projects and rejects changed, incomplete, expired or overflowed measurements atomically
- **AND** a lost acknowledgement replays the original result without rereading storage or changing the counter again
- **AND** cancellation and bounded cleanup discard only transient measurement metadata, never acknowledged results or unsettled storage I/O claims

### Requirement: Expand is not cutover
Schema expansion SHALL NOT switch existing repositories or rewrite user data. Native publication SHALL remain disconnected from product/transport entrypoints until storage/GC, policy, lifecycle, consumers and migration gates pass. Legacy publication SHALL be fenced for any repository explicitly switched to native authority by a future reviewed migration.

#### Scenario: Existing writer after expansion
- **WHEN** a legacy repository has no native-authority metadata
- **THEN** its existing RPCs, roots, history and credentials retain their contracts

#### Scenario: Stale legacy worker after a cutover fence
- **WHEN** a legacy root, scope-head, commit-history or named-ref writer addresses an explicitly native repository
- **THEN** the database rejects the write rather than acknowledging a second authority
