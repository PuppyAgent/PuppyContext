# Decisions

Use the existing Expand -> operator-local Data -> verified release workflow.
Never change released artifacts or release pointers as part of a local test.
The operator must stop/drain writers and GC before applying; SQL fences provide
defense against late legacy publication, lease admission, and project deletion.
An interrupted job stays fenced until resumed. No blind rollback after native
writes. Source metadata and S3 objects are retained.

Progress is project-local. Activation groups prepared projects by organization
so the existing absolute logical-storage baseline can be reconciled atomically
with native authority; unrelated already-native usage is preserved. Missing
objects, invalid hashes, ambiguous HEADs, excessive graphs and missing billing
policy stop activation. No invented entitlements or empty-tree substitution.

Canonical SHA-1 objects retain their OIDs. Explicit 16-hex MUT raw objects use
opaque legacy lookup IDs, recorded full source SHA-256, and supported JSON shapes; malformed canonical
Git bytes never fall back to raw data. Snapshot-only legacy history is imported
as explicitly labelled snapshot commits, not presented as original Git commits.
Original history rows remain intact. Full-project historical roots are retained
by migration refs; Scope views are not promoted into full-project branches.

The artifact is self-contained to satisfy the immutable runner contract. Local
integration tests exercise the artifact against production schema, real S3,
and cold native reads; no hosted credentials or developer dotenv are inherited.
