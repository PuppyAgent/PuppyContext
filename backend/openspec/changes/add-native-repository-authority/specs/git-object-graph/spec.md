## ADDED Requirements

### Requirement: Byte-preserving tree codec

The Git object layer SHALL preserve raw tree names through decode/encode and
sort entries by Git's byte ordering, including the directory slash rule.
Display text SHALL NOT become canonical object bytes. Invalid components,
truncated OIDs and duplicate entries SHALL be rejected.

#### Scenario: Non-UTF8 name
- **WHEN** a valid SHA-1 tree contains names not decodable as UTF-8
- **THEN** decoding and re-encoding retains the exact tree bytes and OID
- **AND** ordering matches stock Git rather than Unicode surrogate ordering

### Requirement: Shared typed object dependencies

Transport and GC SHALL use the same typed local object dependencies. Commit
parents SHALL retain order, annotated tags SHALL reference their declared
object type, and gitlinks SHALL NOT require an external commit in this store.
Message/signature encoding SHALL NOT prevent structural graph traversal.

#### Scenario: Nested tag targeting a tree
- **WHEN** a tag references another tag whose target is a tree
- **THEN** the full local closure is retained and can be materialized into Git
- **AND** any submodule OID remains an external reference

#### Scenario: Malformed structural edge
- **WHEN** required headers, object IDs or target types are invalid
- **THEN** graph extraction fails instead of reporting an empty closure

### Requirement: Conservative verification

GC SHALL stop destructive sweeping when object hashes, types or graph edges
cannot be verified. Only positively identified legacy raw roots may retain the
existing opaque-leaf compatibility behavior. Git fallback traversal SHALL NOT
silently omit unreadable dependencies.

#### Scenario: Corrupt tag during GC
- **WHEN** a reachable tag cannot be structurally verified
- **THEN** no candidate objects are deleted for that repository

#### Scenario: Materialization type mismatch
- **WHEN** a tag declares a tree target but the target is a blob
- **THEN** transport materialization fails rather than presenting a valid closure

### Requirement: Honest validation scope

Tests SHALL distinguish native Git, component doubles and real services.
Strict target runs SHALL fail on failures, skipped required tests, xfails or
missing results. Passing graph tests SHALL NOT advertise full native refs,
SHA-256 hosting, real-service durability or completed user migration.

#### Scenario: Unavailable database
- **WHEN** target validation does not execute its database tests
- **THEN** its evidence cannot be considered complete or successful
