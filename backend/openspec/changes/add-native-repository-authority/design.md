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
calls the new RPC in this tranche. No production activation RPC is provided.

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

## Release properties and remaining gates

Phase: Expand only. Existing data rows rewritten: zero. Runtime: small DDL plus
brief trigger-install relation locks; lock_timeout 5s, statement_timeout 2min.
Migration is transactional: failure rolls back and can be retried through the
normal release runner. Forward repair requires a new migration once shared.
No destructive Contract and no S3 operation. Qubits deployment evidence: none.

Tests use native PG17/auth stubs as supplementary evidence, native Git for
semantic comparison, and role-switched SQL for ACLs. Actual Supabase/PostgREST,
real S3 receipts, collector interleavings, protected-ref policy, lifecycle lease
integration, consumers, restore/migration/performance and full acceptance remain
open. Passing these foundation tests MUST NOT enable receive-pack capabilities
or mark M02/M04, much less ISSUE-062, complete.
