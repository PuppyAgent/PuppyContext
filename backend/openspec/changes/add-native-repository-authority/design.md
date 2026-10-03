# Dormant authority expansion — M02/M04 foundation

This is an implementation design, not the deployed architecture or a cutover
receipt. User authorization covers local development, not target environments.

## Boundary

Add `20261003010000_expand_repository_ref_authority.sql` after ISSUE-053's
containment migration. Do not edit B1, entrypoint storage, credentials or data
release pointers. Six new tables are empty on upgrade: repository metadata,
byte-valued refs (including HEAD), closure receipts, ref transactions, reflog
entries and ref events (outbox). The service role can read these tables but
cannot enable authority or mutate refs/results directly. No runtime entrypoint
calls the native ref-transaction RPC in this tranche. No production activation RPC is provided.

Legacy/shadow repositories retain their existing authority. Native fixture
repositories are created explicitly by the database owner in disposable tests.
Activation in a real environment is forbidden until the existing M01–M20,
07 migration and 08 evidence gates pass. This includes adapting GC and readers:
the existing collector does NOT know these roots yet.

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

## Storage prerequisite, not storage proof

A receipt describes immutable full-closure roots and types, manifest SHA-256,
repository object format, generation, GC epoch, verification and expiry times.
It is owner-issued in this foundation: no service-role receipt issuer is enabled
until S3 verification and publication/GC coordination are implemented. Receipt
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

Real S3 receipts, collector interleavings, admitted/ref policy, lifecycle leases,
consumers, restore/migration/performance and environment deployment gates remain
open. A separately observed native Git prefix/reflog race remains unresolved;
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
Objects in the HTTP conformance fixture are still disk-backed, not real S3.
