# New full-Project bare hosting (2026-10-06)

Transport follow-up: `remove-native-git-materialization` replaces the disposable
stock-Git adapter described by this historical acceptance receipt. Native Git
now reads S3 objects directly and streams packs without a server-side bare repo.
See `docs/architecture/17-native-git-object-transport.md` and that change's own
verification receipt; the frozen candidate and results below remain historical.

The user authorized implementation and local isolated acceptance of the first
bare-hosting phase. New native Scope projections remain a future requirement;
legacy Scope migration, old projection-OID imports and old-client continuation
are cancelled requirements. Existing Projects stay on their existing authority.
This document does not authorize production activation or a local qubits merge.

## Creation and management

`POST /api/v1/projects/` accepts an explicit additive repository profile:

```json
{"name":"Example","org_id":"organization-id","repository":{"profile":"native","object_format":"sha256","default_branch":"trunk"}}
```

The existing idempotency key, membership, project quota and creator-Admin
coordinator remain authoritative. The new RPC atomically publishes the empty
native aggregate and marks the creation operation ready. It requires a valid
billing entitlement projection and an initialized logical-usage baseline (zero
may be established only for an empty organization). It creates no legacy root,
commit or scope sentinel. Omitting `repository` retains legacy creation.

The existing credential issuance API supplies a Project-root credential and the
canonical `/git/{project_id}.git` remote. SHA-1/SHA-256, non-main and unborn HEAD
are explicit repository properties. Native Scope configuration is rejected by
the persistence boundary; it is not a partially enabled resource.

`GET /api/v1/content/{project_id}/head` reads admitted native refs and returns
`head`, `head_commit_id`, format, generation and ref sequence. Unborn HEAD has
an empty `head_commit_id`. Legacy responses retain their existing shape.
`PUT` at the same path requires Project Manage and submits through the shared
RefTransactionService, with `request_key`, `generation`, exact expected HEAD
state and `target_branch`. PostgreSQL rechecks current Admin membership inside
the transaction. A mismatched expected state is 409; identical replay returns
the original result. Reusing a key with a different request body returns 409.
Uncertain ACK recovery replays through the same service to validate the original
SQL request digest; a result lookup alone is not sufficient. The existing
operation-status API can recover that key.

## Publication and lifetime

```text
Real identity / Project-root grant
    -> bounded HTTP spool and owned worker
    -> disposable stock Git validation
    -> RefTransactionService
         publication pin + current admission + logical/file/physical policy
         durable S3 objects -> verified closure -> atomic PG ref transaction
    -> stock Git report only from the canonical result
```

A publication attempt has one stable server-generated request key. If SQL ACK
is lost, Git resolves that exact attempt through current-reader authorization.
If its result remains unknown, HTTP 503 asks the client to rediscover refs; the
server never invents a negative Git ref report for a possibly committed write.
No opaque request key or database query is required for normal Git recovery.

The private materializer retains its read pin through publication. Already
verified immutable objects can be reused while GC is excluded. New objects
still pass durable upload, closure verification and final policy/CAS admission.
Progress during preparation renews publication pins. Cancellation joins the
worker before releasing its input or Project write lease. A timed-out/unknown
remote I/O retains its durable uncertainty record; a cancelled thread is not
treated as proof that S3 has stopped.

## Deletion and accounting

The existing Project deletion job owns drain, purge and quiet verification.
Drain blocks on active writes, native GC and unsettled storage I/O. A known
completed I/O can settle its own identity after the Project enters deleting;
it cannot admit another mutation. A per-Project logical counter follows the
same acknowledged ref event transaction as organization usage. Drain settles
that counter exactly once; unknown pre-existing baselines fail closed.
Physical capacity survives drain and failed/uncertain purge. It is refunded
only when the existing worker completes its verified deletion job. A second
completion cannot refund it again. New private SQL helpers have no client-role
or service-role execute grant.

## Bounded first-release profile

`/git/{project_id}.git/health` publishes the supported profile and its limits.
These are technical ceilings. Receive requests additionally retain the existing
deployment and enforced plan batch limits, taking the smallest applicable cap;
upload negotiation also respects a tighter deployment setting.

| Resource | Limit |
|---|---:|
| Incoming receive pack | 128 MiB |
| Upload negotiation | 16 MiB |
| Retained object bodies per new repository | 256 MiB / 100,000 objects |
| Single object body | 64 MiB, additionally restricted by the billing file limit |
| Refs (including HEAD) | 10,000 |
| Concurrent native Git workers per application process | 2; overflow returns 503 |
| Whole worker admission deadline | 300 seconds |
| Outgoing spool deadline | 300 seconds |
| Temporary Git directory/output budget | 768 MiB, plus bounded input spool |
| Git child address space on Linux | 768 MiB |
| Git child CPU / open files / single file | 300 seconds / 128 / 384 MiB |

Child limits are installed by a standalone exec helper, never `preexec_fn` in
a threaded server. Process groups are killed and reaped on failure/cancellation.
Output spools own their cleanup through the response lifetime. Remote I/O may
need to drain after the local deadline; its retained ledger prevents a false
quiescence claim. Multi-process deployments must size aggregate worker count
and scratch storage from these per-process limits. Acceptance uses a fixed
4 GiB memory / 2 GiB scratch / 4 CPU / 1024 PID Linux container and records
resource exhaustion independently of test results.

## Recovery and acceptance evidence

The recovery drill stops the isolated writer, captures a PostgreSQL dump and
the Project's complete S3 key/body manifest, and records the acknowledged refs.
It then acknowledges a later write. It restores the dump into an independent
owned database (excluding provider-managed GraphQL and its extension ACLs) and
the objects into an independent owned bucket, cold-clones
through the production Git adapter and checks refs, raw object bytes and fsck.
The post-snapshot ACK is deliberately absent: this proves an explicit recovery
point, not zero RPO. The restored data-plane reader uses SQL transport instead
of PostgREST; it is not a managed Supabase/Auth disaster-recovery claim.

The actual application matrix uses formal billing ingress, Project creation,
credential issuance and canonical HTTP for every workflow. Supplementary tests
cover committed/lost ACK, native CAS/concurrency, process death, GC/pins, resource
cancellation and deletion. Historical legacy-profile failures remain recorded.
Final B1–B6 status and the frozen candidate receipt belong to ISSUE-062; Scope
S1–S5 and full ISSUE-062 completion are not inferred from this phase.

## Phase acceptance receipt

The frozen runtime and test candidate `eba763d55fd2e9600c93b3787918fbe44516945b` passed all 1469
selected integration/unit cases in the owned strict Linux PG/S3/application
run, with no failures, skips or expected failures, plus 329 pgTAP assertions.
The formal application matrix contains 78 workflows in each object format.
The same candidate's required backend regression completed with
`3701 passed, 891 skipped, 76 deselected, 34 xfailed, 54 warnings in 502.31s (0:08:22)`.
These counts are independent and are not added together. Original legacy known
gaps are retained; this receipt does not close Scope S1–S5 or the full issue.

The durable node index, raw runner/JUnit, resource, recovery-point and backend
receipts are stored under ISSUE-062 `evidence/2026-10-06-bare-*` in the issues
repository. B1–B6 and the reserved Scope boundary review passed. No qubits merge,
remote push, production deployment or user-data migration was performed.
