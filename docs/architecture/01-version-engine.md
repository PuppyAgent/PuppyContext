# PuppyOne Version Engine

The current runtime uses one native Git authority: PostgreSQL repository refs
and immutable Git objects in S3. A Project owns a repository with multiple
branches/tags and symbolic or detached HEAD. A stored Project tree column is
historical migration metadata; it does not determine current content.

This document describes the native consumer cutover. Older root-first designs
remain in Git history. Deployment still requires the ordered data migration and
separate Contract described below.

## Read and write paths

```text
Human JWT -> ProjectGrant             Machine credential -> RuntimeGrant
                  \                    /
                   current admission in PostgreSQL
                              |
           +------------------+-------------------+
           |                                      |
 Web / CLI FS / Upload / Import /           stock Git smart HTTP
 Synchronize / Agent / table editing        clone / fetch / push
           |                                      |
 ProductOperationAdapter                   NativeGitRepository
           |                                      |
 NativeOperationWriter                     parsed ref edits + pack
 (base + request key + tree splice)                 |
           +------------------+-------------------+
                              |
                  RefTransactionService
              current grant + lease + generation
              file policy + capacity + logical billing
              verified object closure + publication pin
                              |
                 atomic PostgreSQL ref transaction
                              |
       +----------------------+-------------------------+
       |                      |                         |
 repository refs          request/result             ref events
 HEAD, branches, tags     journal + audit             projection jobs
       |                                                |
       v                                                v
 pinned RepositorySnapshot                    rebuild path/text indexes
       |                                      WebSocket notification
 NativeTreeReader / NativeHistory
       |
 S3: version/<project>/objects/<oid[:2]>/<oid[2:]>
       blob / tree / commit / annotated tag
```

Git transport reads object graphs directly from S3 through a bounded admitted
snapshot. It does not materialize a complete local bare repository. Temporary
incoming packs and changed Product objects remain bounded execution resources;
they are not durable repository state. See
[native Git transport](17-native-git-object-transport.md).

## Authority and concurrency

A read captures refs, generation and object format under a renewable read pin.
It can access only objects reachable through that captured view. Private archive
roots are retained by GC but do not enlarge user read/fetch authority.

Product reads obtain repository identity, refs and pin from one admitted read
operation, without a preceding repository snapshot query. Human authorization
facts use one joined database read; list authorization uses deduplicated batches
of at most 100 projects. Policy evaluation remains in `platform/authorization`.

Verified object bytes are reusable only within the live read snapshot, bounded
by 8 MiB and 1,024 entries. Every cache hit still checks pin liveness and captured
reachability; closing the snapshot clears its cache. An object read reuses its
location lookup, including a missing result, for that attempt. Batch reads reuse
the batch lookup instead of querying missing locations again per object. A
missing loose object can still refresh placement to handle concurrent packing.
Publication verification continues to require durable storage reads.

`tests/repository_hosting/integration/test_read_request_budgets.py` measures
actual PostgreSQL/PostgREST and object-storage requests using owned fixtures.
It covers ordinary reads, repeated reads in a single pin, batch boundaries,
authorization revocation and the populated authorization migration. These
contracts run alongside the Agent runtime budgets in CI.

A Product write supplies the exact starting ref/HEAD/tree, request UUID and input
digest. Its journal records attempts/results. Replaying a completed request
returns its original result; changing its input is rejected. A concurrent ref or
HEAD change rejects the stale publication. Background producers bind their
initiating user's current grant and a durable task/step identity; they do not
publish as the Project creator by default. Table read-modify-write operations
carry the revision that supplied their input.

Git workspace preparation reads only admitted refs, format and generation; its
`GitBranchBase` CAS identity does not require the Product splice base tree.
Transport service construction can use this operation's format metadata without
another authority-selection query. Each actual read still obtains admitted
refs or a live pin, and generation changes reject the bound Git request.

A receive operation lends its live snapshot to billing and file-policy checks.
`get_version_publication_context` groups initial repository/result/policy reads;
final publication still checks fresh authority and policy revisions. New objects
and the canonical empty tree share a bounded batch. The last closure-capacity
batch and seal form one SQL transaction. Physical I/O leases and durable closure
verification remain mandatory, independently of query-count optimization.

Physical object publication requires a live admission, verified closure and
capacity reservation. SQL rechecks current membership or credential status,
lifecycle, generation, policy, lease and CAS at publication. Logical billing,
ref changes and audit are coupled to that transaction. A cache or previously
resolved Python grant cannot substitute for the final check.

### Incremental object verification

Publication validates every new object's bytes, hash, type and references. Its
graph walk stops at a retained, typed proof in `version_object_proofs`, keyed by
Project, repository generation and OID. These are durable derived facts, never
a second object authority or an authorization cache. They become reusable by
other requests only in the transaction that accepts their publication. An
unpublished proof belongs only to its issuing pin. Read/publication pins fence
the repository generation and GC epoch; physical collection and confirmed
integrity failures invalidate affected proofs and their dependents before
removing bytes. A known-invalid root is rejected at both sealing and final CAS.

`storage/publication.py` composes the incremental closure;
`infrastructure/supabase/object_proofs.py` owns bounded, request-scoped lookup
and registration commands, including negative lookup reuse. Fresh durable
readback remains mandatory for objects lacking a reusable proof. Bootstrap is
subject to object/byte budgets; exceeding them fails closed and requires repair.
Ordinary push neither walks an already verified commit history nor lists S3.

Proofs retain validated edges, sizes and tree aggregates. Logical usage counts
every path occurrence, while physical capacity counts unique objects. Changed
trees compose child aggregates; file-limit checks prune subtrees by maximum
blob size and still honor current policy and grandfathered path multiplicity.
Historical object bodies are not re-downloaded for these checks. S3 readback
coalesces nearby ranges in the same immutable container within a byte budget.

The selected ref identifies the latest commit. Old commits and their trees and
blobs remain immutable in the same object namespace; a new version updates refs,
not old object bytes. Incremental verification trusts protected, previously
verified bytes. Independent integrity audits detect silent storage damage; a
successful small push does not attest to freshly reading all old bytes.

Native initialization enrolls HEAD, capacity and billing before seeding. Hidden
initializing projects are accessible only through the current initialization
lease; ordinary Human/Runtime reads remain denied until completion. Both SHA-1
and SHA-256 are supported.

## Derived views

History reads Git commits/parents/trees, not an independently written history
row. Multi-ref history uses bounded topology traversal and signed cursors tied
to the captured ref set. If refs change between pages, clients refresh. Restore
creates a new forward commit with the chosen historical tree.

Path and text indexes are rebuilt from a pinned default-HEAD snapshot. A durable
per-project queue coalesces ref changes, including migration activation and new
repository enrollment. Claims expire/retry; a stale sequence cannot overwrite a
newer index. Indexes and notifications may lag; their failure cannot undo an
acknowledged Git write. Non-UTF-8 Git paths remain available through native Git
and byte-path Product APIs but are excluded from SQL text indexes.

GC uses native refs, publication/read pins and verified private archive mappings.
It never reconstructs current content from historical Scope state. Migration
retention blocks collection until source preservation is verified.

## Source organization

```text
backend/src/version_engine/
  entrypoints/git/             HTTP transport and credential admission
  entrypoints/http/            Product content/history/repository APIs
  adapters/git/native_*        direct-object Git protocol
  adapters/git/object_*        bounded pack and object handling
  adapters/product/            normalized commands and tree splices
  write_engine/
    native_operation_writer.py Product journal and commit construction
    ref_transaction.py         shared publication coordination
    git_object_*.py            standard Git format and graph validation
    engine.py                  native initialization facade only
  read/
    repository_snapshot.py     pinned ref/closure provenance
    native_tree_reader.py      trees and exact file bytes
    native_history.py          commit DAG, history, diff and timestamps
  infrastructure/supabase/     refs, admissions, capacity, billing and GC ports
  storage/                    canonical S3 layout and mutation fencing
  derived/native_events.py     native index/notification worker
  bootstrap/                  explicit request/worker dependency wiring
```

Scope geometry and Runtime target types remain separate from repository
identity. This release does not carry forward the old scoped engine. An old
Scope credential is rejected rather than widened to the Project root. New Scope
views require their own branch/view admission and acceptance work. Legacy scoped
MCP/Sandbox filesystem endpoints report `native_scope_not_available`.

## Data migration and release

See the [inventory artifact](../../supabase/data_migrations/20261007_native_repository_inventory/README.md)
and [recovery archive/cutover runbook](../../supabase/data_migrations/20261007_repository_recovery_archive/README.md).

```text
Stop/drain all old writers and GC -> Expand SQL
           -> verify/convert inventory -> activate native refs
           -> archive remaining private/unknown bytes -> verify -> remove old keys
           -> verified receipts -> separate gated Contract -> start native consumers
```

Existing standard Git identities are preserved. Former short-ID snapshots become
labelled imported Git commits; source identifiers and metadata retain an exact
mapping. Private recovery bytes are copied and verified before deletion and do
not become public refs. Contract removes old aliases, duplicate fields and root
publishers after those proofs. Immutable migration history retains the identifiers
needed to replay past releases; the application has no old-namespace fallback.

Local CI runs owned Docker PostgreSQL/S3 with synthetic migration fixtures and
separate final-schema acceptance. Hosted migration is an explicit later release
operation, not application startup, schema-only deployment or CI side effect.
See the [local retirement acceptance record](../../backend/openspec/changes/retire-project-root-engine/acceptance.md)
for checks, replacement test coverage and release limits.
