# Native authority — staged implementation

This is an implementation design, not the deployed architecture or a cutover
receipt. User authorization covers local development, not target environments.

## Boundary

Add `20261003010000_expand_repository_ref_authority.sql` after ISSUE-053's
containment migration. Do not edit B1, entrypoint storage, credentials or data
release pointers. Six new tables are empty on upgrade: repository metadata,
byte-valued refs (including HEAD), closure receipts, ref transactions, reflog
entries and ref events (outbox). The service role can read these tables but
cannot enable authority or mutate refs/results directly. No runtime entrypoint
selects the new native Git adapter yet. The publication service now calls the
native RPC in explicit native fixtures (see the physical-publication section).
No production activation RPC is provided.

Legacy/shadow repositories retain their existing authority. Native fixture
repositories are created explicitly by the database owner in disposable tests.
Activation in a real environment is forbidden until the existing M01–M20,
07 migration and 08 evidence gates pass. The collector now coordinates native roots through a durable fence; all other
readers/writers, policy, metering, recovery and migration still require integration.

## Transaction primitive

`apply_version_ref_transaction(project, actor, request_key, generation,
updates, receipt_id, message)` is a service-role-only SECURITY DEFINER RPC with
fixed search_path. Authorization/ref policy must already be admitted by the
backend; actor and Project identifiers are not user-provided authority.

Each update contains `name_b64`, `expected`, and optional `new`. States are
`{"kind":"absent"}`, `{"kind":"oid","oid":"…"}`, or
`{"kind":"symbolic","target_b64":"…"}`. Omitting `new` means verify;
`new=absent` means delete. HEAD uses no-dereference CAS, may be detached, or may
point to an unborn branch. This foundation supports symbolic HEAD only; other
symbolic refs, partial/non-atomic batch orchestration and public result APIs
remain future work. Names are bytea, with canonical base64 at the JSON boundary.
Initial bounded limits are 1024 name bytes, 256 operations and an 8 KiB message.
They are explicit admission limits, not claims of unbounded Git compatibility.

Lock order is Project lifecycle row -> repository metadata -> receipt. All ref
and HEAD mutation serializes on the repository row, including creation of
previously absent refs. Generation fences migration epochs; ref_sequence counts
committed change batches. Verify-only transactions do not advance it. No network
or S3 I/O occurs under these locks. Independent Projects do not share a lock.
All expected states and the final ref namespace are checked before mutation.
A rejected CAS is a durable result, not a successful no-op. Malformed requests,
missing admission or receipts are errors before transaction acceptance.

Results are scoped by `(project_id, actor, request_key)` and bound to a
server-computed request digest. Replay precedes current receipt/lifecycle
checks so an acknowledged result survives later expiry/fencing. Query returns
null when no accepted result exists; absence is NOT proof a concurrent request
cannot still commit. The backend must reconcile/retry with the same key.
Changing a request requires a new key. Results, changed-ref reflog entries, audit
and outbox commit together. Old OIDs in reflog must become retention roots in
the future collector. There is no janitor for these facts in this tranche.

## Initial storage prerequisite (historical foundation)

A receipt describes immutable full-closure roots and types, manifest SHA-256,
repository object format, generation, GC epoch, verification and expiry times.
It was owner-issued in the initial foundation. The subsequent pin expansion
adds a backend issuer after physical verification and publication/GC coordination. Receipt
constraints and publication checks are exercised with synthetic metadata only;
these tests do not prove that objects exist in S3 or that GC is coordinated.

## Compatibility fence and security

Triggers on the existing root columns, scope-state, commit-history and named-ref
tables reject legacy publication only when explicitly native metadata exists.
They lock the Project first to order against future cutover. Existing null or
shadow metadata does not block old clients. Project cascade deletion remains
possible after the parent row is removed. No GUC bypass is provided.

New tables enable RLS, revoke default/public/client grants, and grant only
backend SELECT. Definer helpers are owner-only; only the two public RPCs gain
service_role EXECUTE. All legacy table ACLs and RPC signatures are preserved.
No migration grants broad new permissions or uses user JWT fields as SQL auth.

Actual pgTAP exposed a missing explicit `pg_temp` search-path entry. The forward
`20261003020000_harden_repository_authority_search_path.sql` sets all three
new definers to the existing ISSUE-053 contract `pg_catalog, public, pg_temp`.
It preserves the preceding migration, function identity/body, ACLs and data;
transaction rollback and unchanged-SQL retry are tested on populated fixtures.

## Product-operation recovery (legacy compatibility fixes)

Two original catalog failures are corrected before native authority activation:
rename recovery now uses the engine's first successfully evaluated snapshot
(including folder sources), not an independent live-head lookup; operation
pending IDs include proposal tree/base/actor/channel/policy because these writes
have no client commit ID. Existing Git ID derivation remains unchanged and no
persisted pending row is rewritten. This is not a new exactly-once request API.

A retry can also construct a pending tree containing concurrent, untouched paths
that were absent from the first attempt's already-flushed candidate. Both root
and Scope operation writers now flush that retry batch before recording pending
review. A flush or ledger failure propagates rather than acknowledging a proposal
that cannot be recovered. Disk/cache-independent tests exercise this ordering;
they do not implement S3 receipts/pins, GC coordination, atomic pending-ledger
persistence or admitted native publication. The catalog's three remaining policy
discrepancies stay failing; current LWW behavior and catalog assertions are not
changed just to produce a green result.

Bulk Product writes also preserve an optional caller-supplied `base_commit_id`
through HTTP schemas, command normalization, byte/reference batching and the
existing operation writer's atomic expected-head guard. Previously HTTP input
silently discarded that field and overwrote a newer acknowledged file. Empty
strings mean an absent base; they must not become an omitted precondition. Empty
batches still check a supplied base, CAS retries retain it, and one base cannot
be spread across separate Scope transactions. Omitting the precondition retains
existing legacy policy. An exact forward delta changes only `BulkWriteRequest`;
this compatibility fix does not implement native ref/HEAD/grant publication or
supply a missing starting base for automatic producers.

## Release properties and remaining gates

Phase: Expand only. Existing data rows rewritten: zero. Runtime: small DDL plus
brief trigger-install relation locks; lock_timeout 5s, statement_timeout 2min.
Migration is transactional: failure rolls back and can be retried through the
normal release runner. Forward repair requires a new migration once shared.
No destructive Contract and no S3 operation. Qubits deployment evidence: none.

Tests use native PG17/auth stubs as supplementary evidence and native Git for
semantic comparison. Actual local Supabase now applies the real migrations and
runs all nine pgTAP files (329 assertions), including the existing GC smoke probe.
Forty-four real GoTrue/PostgREST cases verify client denial, backend read-only
access, RPC commit/rejection replay, actor-bound queries, byte refs and HEAD.
No HTTP/auth mock is used there; repository metadata and receipts remain
owner-installed synthetic fixtures. SQL tests run before Python's many tenants
so global reconciliation batches are not polluted; one probe org prevents a
silent no-org GC smoke skip. The runner records SQL counts/skip diagnostics and
uses the same official Docker Hub registry as database CI.

Selected real S3 receipt/collector interleavings are now covered below. Full
admitted/ref policy, lifecycle/quota orchestration, consumers, restart/restore,
migration/performance and environment deployment gates remain open. A separately observed native Git prefix/reflog race remains unresolved;
its existing target assertion is retained. Passing these foundation tests MUST
NOT enable receive-pack capabilities or mark M02/M04, much less ISSUE-062,
complete.

## Legacy receive closure repair (2026-10-03)

A push client may omit any object reachable from advertised refs, not merely
objects in the latest tree. Actual receive POST therefore uses the complete
reachable-history cache, including stored named refs; profile-specific closure
receipts retain incremental copies. Ref advertisement remains refs-only, and
product/API writes do not acquire this Git graph-walking dependency. Cold-cache
receive cost and large-history performance remain acceptance work, not a claim
that full hydration is free.

A gitlink names an external repository commit and does not make a healthy view
corrupt when absent locally; ordinary missing blobs remain corruption. Stock
Git acceptance is required before publication: object presence alone cannot
convert a receiver rejection to success. The receive ref snapshot is strict,
so a control-plane outage cannot become an empty namespace. This does not add
atomic DB CAS to legacy named refs or activate native repository authority.

## Version identity and source-head CAS compatibility repair

`20261003030000_fix_legacy_publication_head_cas.sql` keeps the old publish RPC
identity, defaults, ACLs and unguarded tree-CAS contract. It locks the Project
before the source-head row (including absence), and honors explicit expected
heads for both root and Scope. Root-first Git submissions now provide that
expected identity and publish new commits even when their tree is unchanged.
Ordinary non-Git content no-ops retain their previous behavior.

Two backend-only `_checked` wrappers make schema-before-code fail closed,
including the storage-usage path. The production history adapter selects these
when the expected source head is present (empty means expected absence). It
never falls back to the older RPC: that RPC accepted the argument while ignoring
it for root writes before the repair. No new definer privileges, native refs
activation, object receipts or data backfill are introduced.

Receive requests take a request-owned immutable object snapshot under the cache
lease, then release that lease before Git admission and database publication.
Only required reachable objects are retained, using hard links with copy fallback;
pruning cannot invalidate the active receive. The unchanged publication-barrier
race can now reach SQL concurrently instead of deadlocking behind the cache lock.
This trades per-request snapshot work for correct lock lifetime; large-history
performance remains an explicit gate.

Releasing that lock exposed a second stale-write defect: a Scope's old visible
alias remained acceptable after another writer advanced its canonical head. An
alias is now admitted only while the canonical Scope head is absent. This keeps
root-derived/excluded/legacy projections compatible without turning a stale Git
CAS retry into automatic merge and acknowledgement. Both first-attempt and
retry rejection are covered.

The SQL tests use explicit expected heads (unchanged acceptance assertions),
concurrent absent/existing rows, populated upgrade, late-DDL rollback/retry and
original RPC/ACL preservation. Actual Supabase tests exercise the production
history adapter through the real SDK/PostgREST and deny client JWT invocation.
Objects in the original HTTP conformance fixture remain disk-backed. The new
native-profile fixture below is separate and does not relabel that evidence.

## Physical publication and native transport implementation

`20261003040000_expand_publication_pins_and_gc_fence.sql` adds backend-only
publication/read pins, immutable verified root/peel metadata, GC runs and snapshot
RPCs. It widens the existing GC quarantine OID constraint for SHA-256 without
changing old RPC identities or SHA-1 rows. Expansion is transactional, bounded
and tested against populated data with a late failure and unchanged-SQL retry.
No repository is enrolled, activated, backfilled or rewritten by the migration.

`RefTransactionService` binds actors to admitted Project/full-repository grants,
validates byte refs/direct types, acquires a pin before uploads, physically verifies
typed closure, seals a receipt and calls the atomic SQL primitive. Recovery uses
the SQL request digest, even after expiry/generation changes; no legacy fallback
or cache-only durability assertion exists. Scope grants cannot use this full-repo
interface. Lifecycle leases, ref policy and atomic billing remain caller-level
integration work; a supplied test grant is not end-user authorization evidence.

Physical reads bypass the memory cache and stale location cache. Traversal verifies
framing, hash, ordered parents, trees and nested tags, excluding external gitlinks.
The manifest hashes exact object identity/type/size/body facts and supplies verified
peel metadata. SHA-256 codecs retain SHA-1 defaults for existing callers.

GC discovers authority from PG. Shadow/unavailable coordination fails closed.
Native collection protects refs, reflog states, receipts and intrinsic empty trees,
and uses physical reads rather than process cache. Publication admission and sweep
admission serialize. An exclusive sweep fence never expires automatically: an
uncertain DELETE may still finish later. Recovery must prove BOTH worker and remote
storage/index I/O quiescence; killing a worker alone is insufficient. Native S3
and index deletion errors propagate instead of silently releasing this fence.

Dry-run GC uses a read pin and does not advance the GC epoch. Readers may continue
while writes are fenced, including during an uncertain orphan deletion: no new
publication roots can enter the active sweep epoch, so current refs remain covered
by its mark snapshot. Read pins protect copying against subsequent sweeps. A
request-owned complete Git repository needs no remote pin after copying finishes.

`NativeGitRepository` uses stock receive/upload-pack and preserves exact incoming
Git objects. Advertisement is refs-only, with verified peels; protocol version is
forwarded through a sanitized environment. Official per-command report-status,
not target-object presence or arbitrary stdout text, governs admission. Atomic
pushes use one SQL transaction; ordinary batches use per-command transactions.
Disposable receive refs use stock reftable on default macOS filesystems and files
on Linux; the canonical namespace is always PG. Local evidence covers macOS Git
2.50.1; the Linux/version matrix is not implied by that result.

Tests invoke this production adapter through a small owned ASGI fixture backed by
actual Supabase Storage's S3-compatible endpoint and real PostgREST/PG. They compare
the same 78 recipes with native bare Git, plus fault/GC/transaction cases. The fixture
supplies an explicit grant, not canonical credential/consumer admission. Original
route failures are retained. The pull-merge recipe now supplies an identical merge
message before both executions: Git otherwise embeds different remote URLs in the
message, legitimately changing OIDs. Exact ref/HEAD/raw-object assertions remain.

`--live --s3` starts the owned object service; a required S3 layer with no passing
S3 tests cannot pass the runner. Bucket/endpoint guards reject external resources.
This is Supabase's actual S3-compatible service, not MinIO or AWS production proof.
The complete admission/resource, long-upload renewal, multi-process restart,
consumer, migration and paired recovery gates still block activation. In particular,
full-history hydration and existing byte-returning backend APIs are not a proved
bounded-memory/large-repository transport design. No product Save imports this
transport materializer, and no existing repository's authority is switched.

## Immutable chunk placements (storage prerequisite repair)

A Git OID does not identify compressed bytes or chunk boundaries. The old chunk
keys reused an OID/ordinal and mutable manifest key. With actual owned S3, after
allowing the chunk URI through the canonical-namespace check, a late producer's
partial PUTs reproduced loss of a previously acknowledged object's readability
in both formats (`chunk size mismatch`). This is a physical-placement defect,
not a Git OID/ref normalization issue.

New chunk keys contain the SHA-256 of their exact bytes, within the Project and
object namespace. Manifest keys contain their exact JSON digest. The manifest
wire version remains **1**, with an additional placement marker: existing readers
already follow explicit keys and ignore additional fields. The actual S3 reader
source from `9847a64e` read both new SHA-1/SHA-256 layouts against an in-memory
storage double; this is old-source component evidence, not hosted deployment.
Existing ordinal manifests remain compatibility-readable but cannot establish
native durability. Migration must copy them to immutable placements before proof;
this change performs no data backfill or authority activation.

Native proof recognizes the chunk URI and validates canonical manifest/part
locations, manifest digest, part digests, identity, exact size and contiguous
coverage. GC validates the entire deletion set before issuing a DELETE. Only an
explicitly absent manifest permits listing its owned object prefix; malformed,
foreign or unavailable manifests fail closed. Parts are object-scoped, so deleting
an orphan cannot remove a shared chunk belonging to another Git OID.

Small configured chunks exercise actual S3 late-PUT isolation and orphan GC in
both formats. They do not prove large-object memory/performance, process restart,
upload/index epoch fencing or safe canonical-location replacement. Those gates,
canonical admission and all consumer/migration work remain open.

## Object-location replacement and late index fencing

Two additional actual-S3 regressions distinguish ref atomicity from storage
safety. A rejected new bundle could overwrite the location of an earlier ACK,
leaving its unchanged ref unreadable even though the original physical bundle
still existed. Separately, a partial index writer could resume after pin expiry,
GC and a newer successful publication, installing locations in the deleted old
bundle. Both defects reproduced in SHA-1/SHA-256; neither was fixed by a final
closure check alone.

S3 writes now validate incoming loose-object identity before I/O and read back
each newly uploaded physical bundle/part/manifest before mutating any location.
This reads each physical upload once, not once per member. Existing cold readers
still use range reads; the component test retains that separate assertion.
These byte-returning APIs and additional I/O still require resource/performance
acceptance, not an inference of bounded large-repository behavior.

`20261004010000_expand_object_location_publication_fence.sql` adds backend-only
registration/removal/deletion-admission RPCs and a direct-DML fence. It does not
modify any prior migration, table ACL, existing data or repository authority.
Registration locks Project -> repository -> pin and validates current actor,
Project, format, generation, GC epoch, uploading state and wall-clock expiry.
Batches are limited to 200 validated canonical locations. A copied context or
queued request cannot install a location after its pin becomes invalid. No
network I/O occurs under the SQL locks. Missing RPC capability never falls back
to a direct upsert.

The invoker-identity trigger rejects direct service-role native index mutations,
including reparenting either side. Its explicit database-owner exception allows
only the existing trusted owner/repair authority and the narrow validated definer
RPCs. It is not a user-configurable GUC bypass. Legacy/shadow DML and existing
ACLs remain unchanged. Direct service-role Project DELETE remains denied;
location rows retain their existing explicit-cleanup contract (no new FK or
implicit data deletion).

Native collection checks backend Project binding and propagates its GC token.
Physical DELETE is admitted before storage I/O against the current token, and
index removal rechecks it under the repository lock. Missing/finished/foreign
contexts cannot initiate new native deletion. This does not make an already
issued remote DELETE cancellable: the non-expiring fence and recovery's worker
AND remote-I/O quiescence prerequisite still apply. Shadow collection remains
fail-closed. An uncertain upload may leave a bounded pin; later location writes
must still pass SQL even if an old async context survives expiry.

A sealed pin cannot accept more location writes. A retry after sealing but before
its ref result reuses the sealed proof instead of uploading again. Fixture-only
preloads now explicitly open/release publication pins without issuing receipts
or exposing a readable ref root. The native router and real-user admission remain
disconnected, and lifecycle/quota/consumer/migration gates are not waived.

## Pinned native content/base snapshots

`read/repository_snapshot.py` captures declared format, generation, ref sequence,
byte refs and a read pin from one PG transaction. It validates Project/pin
binding, HEAD presence and ref types, and exposes immutable decoded ref states.
Lazy object reads bypass staging/cache and verify exact Git identity and typed
edges. A stored object becomes readable only as a captured ref root or a verified
edge; rejected receipts are not an alternative read authority. The decoded-byte
budget is bounded, but the underlying byte-returning S3 API, metadata size,
long-I/O renewal and total process resource gates still require further work.

A content revision distinguishes missing refs, unborn HEAD, detached HEAD and
peeled tags/trees. Product-base construction can explicitly permit an absent
ref; ordinary missing-ref reads do not invent empty content. An edit through
symbolic HEAD emits both a verify-only HEAD guard and the selected branch's old
OID CAS. Merely switching default branches at the same OID therefore invalidates
the earlier default-branch operation instead of silently writing the old branch.

The native Git adapter now shares this pin lifecycle and closes the pin after
its private copy, before running transport I/O. Discovery stays metadata-only;
a missing/mismatched declared format is rejected even for an empty repository.
The product-primitive interoperability path uses the same snapshot/base guards
without importing transport materialization. These are consumers of an already
admitted internal grant, not replacements for current-credential authorization,
policy, quota, canonical ProductOperationAdapter/API/Scope integration or cutover.

Selected actual-service regression: 119 passed (97 S3-layer, including all 78
native workflow recipes; 22 component), plus 329 pgTAP tests. This selection is
not a clean full-revision receipt and does not remove the legacy route's 34 gaps.

## Current actor and Project write-lease admission

`20261004020000_expand_repository_write_admission.sql` adds backend-only
preflight/final-publication wrappers. Organization -> Project locks fence tenant
and lifecycle changes. Membership -> Surface -> credential locks serialize with
revocation and deletion cascades; existing platform SQL resolvers, not a second
role matrix, decide effective access. Scope credentials remain ineligible for
full-repository writes. A matching live Project lease is required for a new
mutation. These functions trust backend-supplied admitted identity; an actor ID
or stored credential row is not bearer authentication.

Credential/lease wall-clock expiry can occur while waiting for later repository
or receipt locks, despite holding the credential/lease rows. Regression tests
reproduced successful publication after such expiry. Final checks therefore run
after the underlying ref primitive, within the same SQL transaction; failure
rolls back refs, result, sequence, reflog, audit and outbox together. Preflight
also rechecks after acquiring repository locks. Original-result replay requires
current read access, preserves digest checks and does not require an old lease.
The service accepts a newly resolved read-only grant for this recovery path;
only a genuinely new request proceeds to the write-action check.

`AdmittedRefAuthorityRepository` uses these RPCs at pin preflight and final
publication. Its lease provider supplies the caller's active ProjectWriteLease;
copied/released context is not authority, and missing capabilities do not fall
back to raw publication. Tests use observed independent SQL-session lock waits,
populated DDL rollback/retry, actual Auth/PostgREST client denial, and actual
S3 publication followed by credential/lease invalidation and cold old-ACK reads.
Stored/admitted fixture credentials do not prove end-user Git HTTP authentication.
Selected actual-service regression: 54 passed plus 329 pgTAP, with no skipped or
XFAIL cases. This is not a complete clean-revision acceptance receipt.

The raw ref repository remains a backend internal primitive. This wrapper does
not enable canonical routing, enforce ref policy or repository-wide quota,
implement consumer integration, or close long-I/O/process-recovery gates. Those
are required before native authority can be activated for existing repositories.

## Explicit technical capacity and uncertain-I/O accounting

The forward capacity expansion adds no policies or inventory to existing
repositories. An operator must establish the complete baseline before setting
Org/Project inventory initialized. The metric is unique local Git object **body**
bytes and object count per Project, aggregated across its Organization; it is not
encoded S3 bytes, tree-path logical billing, or cross-Project deduplication.
All commit/tree/blob/tag objects and retained allocations count. External gitlink
edges do not invent local objects or storage charges.

Org -> Project serialization and policy-row locks make whole reservation batches
atomic. Pin actor/format/generation/epoch/state and wall-clock expiry are checked
at execution, including after queued locks. Exact retry adds no duplicate charge
or ledger entry. A copied context cannot opt out of an enrolled policy. Required
missing policy/RPC capabilities fail closed; low-level dormant profiles without
policy remain explicitly distinct from canonical admission.

Native S3 admission runs before loose, bundle, part or manifest PUT. The admitted
ref service reconciles the full physically verified closure before guarded seal;
SQL receipt/transaction triggers fence older unmetered issuers for enrolled
repositories. Original result replay bypasses neither digest nor current-read
checks, but needs no new allocation, current write lease or active capacity policy.
The existing `storage.logical_bytes` billing contract is not repurposed or silently
settled by this subsystem; canonical billing/entitlement integration is still open.

Each storage invocation has an independent I/O identity, even for deduplicated
reuploads or retries sharing the SAME operation/pin. Two red regressions exposed
that automatically clearing claims on pin seal/release let a retry settle another
producer. Neither transition nor expiry/deletion now removes claims. Storage
settles only its invocation after all physical/index calls succeed; cancellation
and unknown outcomes retain claims. Closure-only reconciliation makes no I/O claim,
and guarded seal rejects any outstanding invocation on that pin. Explicit recovery
must establish that specific invocation's worker and remote-I/O quiescence.
Collection protects unsettled claims and refunds only after physical/index cleanup
under the current non-expiring sweep token. Allocation inventory also survives
Project metadata deletion, retaining cleanup identity and charges until retirement.

A post-DELETE SQL failure leaves the allocation as recovery inventory. The native
collector uses bounded, token-bound pages and fresh canonical absence checks,
not a listing miss or cache, to recover missing placements/reservations. Such
entries cannot override incomplete live-root proof or unsettled-I/O protection.
Tests establish controlled same-process recovery with explicitly known quiescence;
they do not establish independent-process or unknown remote-I/O quiescence.
The generic boto3 client retains its existing adaptive retry configuration.
Native mutation contexts instead require an isolated single-attempt view, bound
to the source client's endpoint, credentials and transport configuration. It uses
`total_max_attempts=1`, forbids implicit multipart uploads, and treats missing or
nonzero SDK retry evidence as uncertain. Native DELETE is idempotent and does not
reinterpret an ambiguous HEAD failure as absence. Neither shared clients nor
process proxy configuration are mutated. Client replacement/recovery is tested;
old clients stay alive until explicit service shutdown, not a configuration swap.
Controlled real S3 PUT/DELETE lost-ACK tests observe exactly one physical request,
retained invocation/GC fences, explicit known-quiescence recovery and old-ACK reads.
This does not make arbitrary external timeout/process-death recovery automatic.

This connects technical capacity to the existing native service and collector,
not to canonical Git/API/product routing. Ref policy, public operation identity,
consumer migration, logical billing, lifecycle retirement and large-resource
acceptance remain required before activation.

### Checked logical settlement (M15, partial; no activation)

An additional empty Expand enrolls no repositories and initializes no usage.
The optional `RepositoryBilling` connection requires admitted control and retained
capacity. It checks the existing `storage.logical_bytes` counter and acknowledged
PuppyPay projection, then binds measured old/new default OIDs to the actual refs
inside the publication transaction. Tree sizes preserve path multiplicity while
memoizing shared subtrees; blobs are not re-read or paths exponentially expanded
when the complete physical manifest already exists. Commit history/gitlinks do
not become current-tree charges. A named-ref-only change has zero logical delta.

The SQL wrapper takes the existing Organization/metric advisory locks before
Project locks. Current entitlement revision and wall-clock expiry, actor and
lease are checked after billing waits as well as ref waits. Usage, ref result,
reflog, audit and outbox roll back together. Events use ref-transaction identity,
not commit OID, so later rewinds/HEAD changes can revisit a commit correctly.
A private transaction-bound attestation fences older publishers after explicit
billing enrollment; exact original-result replay still needs only current read
access and the matching original request digest, not a new billing projection.
No caller-provided quota limit or uninitialized-usage fallback is accepted.

A red regression demonstrated that the legacy full reconciler could reset a
native incremental counter to its stale legacy-root total. A new event fence
rolls back that old reconciliation for enrolled Organizations. This deliberately
fails closed for the old caller. Checked full reconciliation is described below;
lifecycle settlement, complete file/named-ref policy and canonical authenticated
routing remain separate gates.
Actual S3/PG tests use synthetic entitlement rows and explicit admitted grants;
they are not evidence of the external billing service or native public HTTP.

### Complete logical reconciliation

The legacy Project-list facade defaults to 100 rows and cannot be an accounting
inventory. A forward Expand captures all Organization Projects in SQL under the
existing Organization/metric advisory locks, with no object I/O. The service
reads stable inventory pages of 200, measures canonical native trees under read
pins (not commit history), and preserves legacy namespace/chunk compatibility
for legacy or shadow authority. Current-tree DAG memoization preserves repeated
paths without exponential expansion. Object/decoded-byte budgets include the
native selector commit; incomplete inventories and more than one million
Projects fail closed, never truncate.

Final reconciliation takes the same locks, compares both directions of the
captured inventory (including HEAD symref, object format and generation), checks
all measurements and current entitlement, then delegates to the existing usage
counter/event function. Private same-transaction authorization admits this call
through the older-reconciler fence. Waiting past capture or entitlement expiry
rolls everything back. Completed receipts survive measurement-row cleanup and
replay without another charge or renewed entitlement. Cancellation cannot undo
an already committed/unknown-ACK result. At most one unfinished inventory per
Organization is admitted; expired/cancelled metadata is cleaned in bounded
batches, not silently replaced by another capture. This cleanup grants no GC or
storage-invocation quiescence authority.

The application storage-reconciliation scheduler selects the checked service;
RPC or measurement failures never fall back to the old root-only path. This is
internal service authority, not an end-user authorization shortcut.

### Checked file policy and continued admission (partial; no activation)

An empty forward Expand explicitly enrolls file policy only alongside retained
capacity and logical billing. The checked publisher binds the current
`upload.max_single_file_bytes` projection/revision, incoming physical manifest
and actual before/after selected HEAD to the same ref/usage transaction. Private
transaction-bound authorization fences old publishers. New oversized blob
allocations fail before PUT, including blobs reachable only through named refs
or history. Previously published large blobs can remain in retained history or
be tagged after a plan downgrade; allocations and rejected receipts alone do
not confer grandfathered publication rights. Current-tree moves preserve per-OID
path counts, whereas additional oversized copies are rejected. Typed DAG
counting does not expand paths or count external gitlinks.

Publication pins capture the checked actor and Project lease atomically.
Every new storage invocation, including deduplicated uploads, rechecks current
actor, lease and entitlement after waits. An uploading pin cannot borrow a
retry's new lease. A verified pin can recover with a current lease, but remains
sealed against further PUTs; billing re-verifies that pin's incoming roots,
never inserts unpublished roots into a read snapshot. Exact committed-result
replay still needs only current read permission and the original digest.

Admitted read pins and metadata-only advertisement/`ls-refs` recheck current
credentials after repository lock waits. Metadata discovery still does no
object I/O and does not create read pins. Backend reconciliation uses the
scheduler's distinct backend-only control, not a fabricated user credential or
an exception to the end-user reader's checks. These capabilities do not select
native authority in the canonical router or finish Scope, consumer, recovery,
retirement and migration gates.

### Initialization is not repair

The legacy initializer previously treated an unsuccessful object-existence probe
as permission to replace an acknowledged root with the intrinsic empty tree.
The regression reproduces that loss even without a concurrent writer. The new
forward Expand adds a service-only checked initializer; the engine has no direct
setter or missing-RPC fallback. Existing valid roots return unchanged without any
object probe. Missing physical bytes remain corruption/unavailability, not unborn
metadata. Missing roots alongside accepted commits, Scope state or refs reject.

First initialization locks Organization, Project and existing repository metadata,
requires legacy SHA-1 authority and initializing/ready lifecycle, and verifies a
current Project lease before and after the update. A publication winning the lock
race is returned unchanged. Native authority requires its own lifecycle operation,
never a legacy empty-tree repair. The DDL initializes no Project and performs no
storage access. Populated rollback/retry, real SDK/Auth ACLs and the legacy main
application profile are tested separately from native activation.

### Canonical native Git source routing (synthetic enrollment only)

The canonical Git router now asks the manager for fresh PG authority after the
existing credential/locator resolution. Explicit native rows select a fresh
Project-bound S3 backend and the mandatory admitted/capacity/billing/file-policy
service. Missing metadata RPCs or malformed metadata reject, never downgrade.
Absent metadata and shadow rows retain legacy routing. The legacy repository
cache also rechecks authority and cannot masquerade as a native read source.
This is a source implementation, not enrollment or permission to activate any
existing repository before the outstanding lifecycle/consumer/migration gates.

Native HTTP preserves Git-Protocol, request spooling, fetch audit attribution,
Project leases and read-only/foreign/revoked credential boundaries. Health is a
pinned physical graph diagnostic; it does not repair missing objects or describe
unavailability as an empty repository. Native caches are disposable, so checked
rebuild validates the graph rather than installing another persistent authority.
Unmapped legacy locators and Scope grants cannot become full-repository native
access. Product/Scope/automatic-writer and compatibility mapping work is still
unfinished; these paths fail closed rather than reading a stale legacy root.

Real application-process tests create JWT-authorized Projects and Git credentials,
then explicitly install synthetic empty native authority and policy/entitlements
as the owned test database owner. No fabricated durability receipt or runtime
grant is used. Both formats exercise non-main HEAD, atomic branch/typed-tag push,
file-policy rejection before oversized allocation, cold restart/protocol-v2 fetch,
exact refs/bytes, read-only/anonymous/foreign rejection and credential revocation.
This proves canonical authenticated Git, not external PuppyPay, native product
Save/API, existing-data cutover or complete activation readiness.

### Ongoing pin authority and maintenance inventory

Canonical admission must remain current during renewal, not only pin creation.
`renew_admitted_version_object_pin` rechecks current actor before and after the
primitive's repository/pin lock waits. Publication pins additionally require the
stored admitted Project lease; read pins need neither write permission nor a write
lease. Expiry/revocation rolls back renewal. The admitted adapter never falls back
to primitive renewal; backend-only GC/reconciliation keep their separate primitive
capability. This adds no I/O settlement authority and does not solve long single-I/O
heartbeat/resource bounds by itself.

The frozen a8dd06c7 run exposed 18 native GC consumer regressions because the new
legacy-facade guard also rejected maintenance callers. The scheduled GC worker now
uses a dedicated Project-bound inventory, with no legacy current-tree/head or
publication interface. Existing legacy history, Scope heads, view mappings, pending
outbox/conflicts, named refs and shadow snapshots remain conservative retention
inputs. They are not native current content. The collector requires either an
explicit complete inventory or the old scoped-root interface; absent capability
is not an empty repository. Metadata lookup still fails closed. Physical verification,
quarantine, per-invocation uncertainty and destructive token rules are unchanged.
Two actual S3/PG worker cases prove selection, preserved refs and persisted run
records for both formats; legacy-facade reads remain rejected. Other consumers,
lifecycle, migration and complete resource/recovery gates remain unfinished.

### Selected native Product reads (M08, not Product writes or activation)

`ProductOperationAdapter.open_read` validates the Project grant before fresh
mandatory authority selection. Explicit native authority opens an admitted read
pin and a typed, format-checked `NativeTreeReader`; absent/shadow authority retains
the legacy adapter. Metadata/capability failures never select the legacy path.
The native view has no publish, repair or transport-materialization capability.

Authenticated content `ls`, `cat`, `raw`, `stat` and `tree` use this context for
both bytes and base metadata. `repository_revision` reports format, generation,
ref sequence, target ref (text when representable and base64), expected OID,
tree OID and captured HEAD guard. This describes the actual read, not write
permission. Scope-head aliases are not invented from legacy historical rows.
Native entry DTOs include Git mode and lossless base64 name/path fields; display
text is JSON-safe and is not byte identity. The five routes accept the additive
`path_bytes_b64` alternative (not alongside a nonempty text path), with the
existing path-length ceiling and no traversal/NUL/empty segments. Symlinks remain
blob bytes and gitlinks remain external. The native tree walk is iterative and
rejects its entry-budget overflow rather than returning a truncated success.
This is not complete RAM/network/deadline or long-single-I/O acceptance.

The HTTP schema change has a forward contract delta for exactly five paths and
five response schemas, checking both prior and new digests; historical entrypoint
fixtures and security schemes are unchanged. Actual src.main/JWT/PG/S3 cases now
read both formats before/after cold application restart without changing refs or
legacy roots. Enrollment/entitlements remain owner-installed synthetic facts.
Signed inline/download streaming, historical/Scope readers, native Product
writes, automatic producers, lifecycle and migration are still separate gates.

The investigation also reproduced destructive legacy read-time healing: either
a missing subtree or a missing blob caused `list_dir` to replace a valid root
with an incomplete Scope-derived tree, removing healthy siblings too. That
nonempty-root mutation is removed; component and actual S3/PG tests preserve the
acknowledged metadata and recover by restoring only physical bytes. Authority
errors in legacy Project/Scope reads now propagate as unavailable rather than
empty roots/heads or missing paths. The older empty-root Scope compatibility
path is unchanged and is not claimed as native read or repair authority.

### Native Product preparation and explicit write ingress (M09, partial)

A Product request needs a caller-stable UUID and a digest of its complete
normalized intent, including its genuine starting base, selected ref, expected
OID and captured HEAD guard. Capturing a newer head during a retry is not a
substitute. `version_product_operations` reserves a stable server creation time
and immutable candidate ref request under `(Project, actor, request UUID)`.
This is metadata only: it neither saves file bytes nor publishes a version.
The result authority remains `version_ref_transactions`, not a second journal
result or a legacy root/history setter.

The empty Expand exposes only checked read/begin/prepare RPCs to service_role. They
lock Org/Project, revalidate current actor and lease after waits, and require
active native format/generation for new work. Existing result recovery requires
current read authority and the original input digest, not a write lease or new
quota/object allocation. The prepared request hash uses the exact existing ref
transaction digest. A ref-result insertion fence atomically rejects an
unprepared or mismatched request before any ref/audit/outbox/usage ACK can escape.
An unrelated earlier ref transaction cannot be claimed retroactively. Direct
journal DML and helper execution are denied to application roles.

There is no Project cascade on this cleanup identity. A pending journal row is
not an acknowledged root, read grant, grandfathering proof or GC/I/O settlement.
The journal never expires uncertainty or releases another invocation's claim.
`NativeOperationWriter` constructs Product commits directly from one admitted
starting snapshot, with private disk-backed draft objects and the journal's
stable clock. It does not invoke Git or materialize transport. Both hash formats
and literal byte names/modes use the existing tree primitives. Product edits
select a branch or detached HEAD; tag/custom-ref management remains the native
Git/ref interface rather than silently converting a typed ref into a commit. Publication still
uses RefTransactionService and its current capacity, file, logical billing and
physical closure checks. Same-tree operations submit check-only preconditions;
changing HEAD to another branch invalidates the captured selector even if its
OID is unchanged. Prepared, still-valid verified receipts resume without another
splice/PUT; exact acknowledged responses are recovered before a new lease.

The five Product write routes accept an optional `native` envelope containing a
caller UUID, versioned normalized input (`input_version=1`), captured
`repository_revision` and lossless byte-path alternatives. Omitted HTTP fields
are normalized by the frozen v1 command contract, not future schema defaults.
Absence preserves the legacy path, not a guessed native base. Native requests
never select a legacy facade. Only already explicitly enrolled repositories can
use this path; route availability is not activation/cutover authority. The wire
change is an exact five-schema delta plus the new envelope schema; paths and
security contracts are unchanged. Copy/touch normalization is shared code, not
an advertised completed HTTP/producer integration.

Candidate metadata includes the tree/commit identity and ordered byte-path
changes; a matching changed-ref outbox event includes that immutable metadata.
It is not a second publication authority or a completed derived-event consumer.
The next recovery batch separates physical attempt pins from this immutable
logical candidate. Its private attempt inventory has no Project/pin/lease cascade;
only the currently selected attempt digest can publish. A never-started original
pin is tombstoned too, so a delayed old worker cannot create it after rotation.
Uploading pins cannot borrow another invocation's lease; live verified receipts
(including never-retired original fixed native pins) may rebind for verification/
ref publication, never for PUT. Another live lease
reports busy. Once a prior lease is unavailable, a fresh attempt can proceed
without treating expiry, worker death or retry as remote-I/O quiescence: old claims
are untouched and continue fencing GC. The same lease can also start a distinct
invocation, with a distinct pin rather than reusing its uncertain physical work.
A late old worker may recover a different attempt's committed result through
current read admission; it cannot publish with its retired digest or settle that
other invocation's claims. Original clock, input and candidate bytes remain fixed.
Selected actual PG/S3/application verification covers these transitions,
including a late old PUT after the new ACK. This does not close independent-
process/restore/resource acceptance or producer input handoff. Automatic producers, legacy/Scope mappings, rollback/conflict
submission, other consumers and compatibility migration remain unfinished.
Native activation stays fenced.

The subsequent process fault harness uses distinct OS processes, SIGKILL and real
lease expiry. An owned loopback proxy keeps one complete signed PUT alive outside
the killed process, then forwards it to actual S3 after a new process's ACK. No
endpoint, global proxy or runtime budget changes are involved. Original input is
supplied by the test supervisor and the RuntimeGrant is synthetic; this proves a
controlled process/network boundary, not production input handoff, end-user token
authentication, arbitrary multi-instance failures or paired restore. The old I/O
claim remains even after the proxy observes completion; no automatic settlement
is inferred from a successful retry.

### Contracts required before migrating remaining producers (M05/M09/M16)

The explicit native v1 envelope is not an automatic legacy adapter. In particular:

- Persist a producer's identity, complete immutable input/artifact references,
  original base and destination before delayed work. A raw object OID, stored
  `who`, cached grant or old lease is not staging/read/write authority.
- Preserve omitted base versus explicit empty base and the existing legacy
  conflict policy. A legacy reevaluation is not permission to change the base or
  immutable candidate of an outstanding explicit native v1 request. Its logical
  intent/evaluation/result binding must be specified before wiring it to refs.
- Establish old Project/Scope/credential bindings and selected-ref semantics
  before cutover. An old OID-only request cannot disambiguate a same-OID HEAD
  switch by guessing current HEAD; choose an explicit compatibility contract.
- Scope credentials must never be converted into full-repository grants. View
  reconstruction, bounded tree changes, original submitted objects/history and
  canonical publication require a checked, atomic compatibility path.
- Human producers use canonical named Project actions. Runtime/automatic
  producers must revalidate their current credential or existing binding/job
  authority, lifecycle and pause/drain state; a historical creator ID is not an
  unconditional service permission.
- Derived view/event metadata and artifact staging are not new version/result
  authorities. Missing bytes or mappings fail closed, not into empty roots.
- Lifecycle cleanup must retain format/namespace/claim provenance after deletion;
  the present retained inventory is not yet a completed deletion/settlement path.

These prerequisites apply across uploads, imports, synchronize, MCP, content
Tables, seed/templates and Workspace, not just the five explicit HTTP routes.
