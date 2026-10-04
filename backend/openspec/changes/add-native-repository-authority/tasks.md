## 1. Baseline and objects (M01/M03/M18, partial)

- [x] Create isolated Cloud/Desktop worktrees from the integration baselines.
- [x] Import the existing uncommitted hosting harness without modifying its source worktree.
- [x] Reproduce non-UTF8 tree, missing tag traversal and gitlink closure failures before fixes.
- [x] Verify typed graph fixes with native Git, transport/GC regressions and the backend suite.
- [x] Harden evidence/strict-mode handling; run isolated PostgreSQL validation.
  Supplementary native PG: 5 passed, 1 known head-CAS failure. Supabase startup
  timed out and its owned containers were stopped; real Supabase/S3 gate remains open.
- [x] Diagnose Docker-level startup stalls with owned containers; add bounded,
  secret-free startup diagnostics, child cleanup and explicit live SQL evidence
  gates. A minimum Alpine process also cannot start; do not restart shared Docker
  or relabel native PG as Supabase acceptance.
- [x] Restore local Docker execution with user-authorized, process-scoped clean-env
  startup; retain existing containers/volumes/images and global proxy settings.
- [x] Run actual Supabase migrations, all 9 pgTAP files / 329 tests, and 44 real
  GoTrue/PostgREST boundary cases. Fix new-definer search paths in a forward
  migration; run SQL before Python tenants with one org to exercise GC smoke.
- [x] Correct the hosting prefix-ref oracle to use stock bare Git, retaining its
  one-winner/ref-state assertions. A fresh 100-attempt probe passed 100/100 for
  bare defaults; worktree defaults had 19 double failures (reflog D/F race).
- [ ] Track the upstream worktree reflog race in the client/version matrix;
  correcting the hosting oracle profile does not fix stock Git itself.
- [x] Add local-only Linux Docker execution of Python, stock Git and production
  adapters against the owned Supabase/Auth/PostgREST/S3 stack. Reject inherited
  dotenv/cloud credentials, remote daemons and missing container evidence.
  Preserve the declared S3 origin for SigV4. Initial selection: 69 passed plus
  329 pgTAP; no full-target, main-service/worker or multi-instance completion claim.
- [x] Run a frozen-source Linux full target: 964 passed / 43 failed, plus 329
  pgTAP. Reproduce the nine additional failures as tmpfs noexec (eight rejection
  hooks) and stock Git 2.39 empty SHA-256 clone identity (also fails stock bare).
  Enable executable test tmpfs with startup probe and pin verified Git 2.50.1;
  preserve every assertion and keep old-client/version boundaries explicit.
- [x] Add real src.main + owned Redis startup and authenticated legacy-profile
  Git/API read/write, stale-base rejection, foreign-user denial, cold process
  restart and credential revocation. Correct writable log/cache configuration.
  Selected Linux Docker regression: 13 passed plus 329 pgTAP; no auth doubles.
- [x] Preserve the second frozen full Docker failure (4e9887e4: 855 passed,
  155 failed, late fork/resource errors despite all application/Auth/SQL/S3 cases
  passing). Add init child reaping and mandatory cgroup PID/memory evidence plus
  peak process/thread diagnostics; do not increase budgets to hide exhaustion.
- [x] Run clean `e6271b9e` Linux Docker full target: 985 passed / the exact original
  34 failures, plus 329 pgTAP. All native/SQL/Auth/S3/main-application cases pass.
  PID/memory exhaustion counters stay zero; observed PID peak 40, memory peak
  707457024 bytes. Controlled stock Git `maintenance --auto --detach` reproduces
  40 orphan zombies without init versus zero with init, at the same budget.
  This repairs the test environment, not the remaining canonical capabilities.
- [ ] Complete the updated frozen full Docker target, native canonical application
  admission and worker/resource/crash-recovery acceptance. Optional external AI,
  Billing, ETL and Scheduler are disabled in this core application test profile.
- [ ] Complete real-object service and full multi-instance acceptance gates.
- [ ] Freeze complete G01–G66/API/scenario contracts and repo/ref/base integration contract.
- [ ] Complete byte/object-format/raw-header semantics including SHA-256 end to end.

### Git command conformance (M01/M18, partial)

- [x] Add 78 native-bare-versus-production-HTTP recipes for history generation,
  conflict abort/continue/skip, refs/rewrites, protocols, shallow/filter clients,
  queries, client export and maintenance. Compare cold-cache mirror refs/HEAD and
  every reachable object's OID/type/raw bytes; retain target failures.
- [x] Add 8 Project/Scope stock-client failure/recovery/concurrency tests. Injected
  publication faults, disk objects and memory control plane are not real S3/PG
  outages, process restarts or multi-instance proof.
- [x] Require native oracle/recipe success in setup and actual execution of
  declared commands; record G IDs, profile and execution boundaries in JUnit.
- [x] Document remaining G01–G66 coverage in the test CONFORMANCE.md, including
  unimplemented APIs, real storage, migration and client-version gates.
- [x] Absorb current local Cloud/ Desktop Access contracts before transport work.
- [x] Reproduce and repair revert-range, ancestor-tag receive closure and gitlink
  cold-read defects with original recipes; preserve legacy Scope restrictions.
- [x] Require stock receive acceptance even for existing objects; seed named refs
  in advertisement/quarantine and fail closed on control-plane snapshot failures.
  Add 22 Project/Scope cases. Five earlier workflows pass, including two client
  preflight mixed-batch cases; this does not implement server multi-ref transactions.
- [ ] Resolve the failing recipes without weakening byte/graph/ref comparisons;
  test count growth is not product delivery or complete Git acceptance.

### Legacy product-operation recovery (M14 compatibility, partial)

- [x] Reproduce original C04/F12 failures without changing catalog assertions.
- [x] Bind rename recovery to the engine's first successful snapshot, including
  directory sources, rather than a separately read live blob.
- [x] Distinguish operation proposals by tree/base/actor/channel/policy, retaining
  the established Git pending identity and already persisted rows.
- [x] Reproduce unflushed retry proposals on cache-independent reads; flush
  Project/Scope proposals before pending persistence and propagate ledger errors.
- [x] Add 19 component regressions; original C04/F12 plus these regressions pass.
  Native-PG strict selection: 438 passed / 14 failed / 44 real-Supabase cases
  deselected, no skip/XFAIL. Offline backend: 2591 passed / 27 skipped /
  52 deselected. Neither selection is full actual-service acceptance.
- [ ] Resolve A04/B11/C01 catalog-versus-current-policy discrepancies without
  weakening acceptance or silently changing the existing LWW compatibility profile.

### Product object/tree interoperability (M03/M14/M16, partial)

- [x] Add an explicit repository object format to ObjectStore and tree/splice
  primitives, keeping SHA-1 defaults and rejecting cross-format identities.
- [x] Reproduce and fix mode loss in product tree spines, including untouched
  executable/symlink/gitlink siblings, move/copy, mode-only copy replacement,
  and blob replacement of an external gitlink. Preserve byte names and no-op saves.
- [x] Exclude external gitlinks from local object closure and logical-byte/file
  limit measurements, including gitlinks whose OID coincides with a local blob.
- [x] Exercise staged product splices followed by pinned publication and cold
  native Git readback in both formats using actual owned S3/PostgREST. Product
  primitives invoke no Git process or transport materialization. This is NOT
  canonical ProductOperationAdapter/API admission or atomic billing acceptance.
- [x] Implement pinned revision/base snapshots for already admitted native
  readers; exercise declared formats, unborn/detached HEAD, typed tags, cold
  reads, unreachable proposals, budgets, GC exclusion and same-OID HEAD switches.
  Share the pin lifecycle with NativeGitRepository and use its base guards in
  product-primitive interoperability. Reject empty-repository format mismatch.
  Selected actual-service regression: 119 passed (97 S3, including all 78 native
  workflows; 22 component), plus 329 pgTAP. Canonical product/API/Scope/current-
  credential integration and complete resource acceptance remain open.
- [ ] Wire repository-format selection, consistent ref/base snapshots and
  admitted publication into all product/automatic consumers; do not activate
  native authority on the strength of these primitive tests.

## 2. Unified authority (M02/M04–M09/M11–M16)

- [x] Add dormant Expand schema and SQL primitive without editing B1: byte refs,
  old-OID/HEAD CAS, atomic batches, immutable results, reflog/audit/outbox, role
  restrictions and legacy-publication fences. No runtime activation/receipt issuer.
- [x] Test the primitive with native PG17, SHA-1/SHA-256 Git oracles, real SQL roles,
  concurrent create/replay, late publication rollback, populated expansion and
  injected DDL failure/retry. Auth and object-closure receipts are fixture stubs.
- [x] Verify dormant expansion/ACLs through actual owned Supabase/PostgREST;
  preserve old SQL files and assert new-definer hardening rollback/data/ACL safety.
- [x] Repair legacy source-head CAS with Project-first locking and expected
  identity for root/Scope, including absent rows; retain old RPCs and ACLs.
- [x] Preserve metadata-only Git commits and require versioned checked RPCs,
  including metered publication; fail closed on old schemas, with no fallback.
- [x] Release receive cache leases before publication via private immutable
  snapshots; fix stale Scope aliases exposed by actual concurrent publication.
- [x] Verify populated SQL repair/rollback/retry and production adapter through
  real SDK/PostgREST with backend/client roles; native authority stays dormant.
- [ ] Complete target-environment security/deployment gates; local fixture success
  does not authorize activation or prove real-user upgrade compatibility.
- [x] Add physical closure verification, backend-only pin/receipt issuance and
  GC sweep/read fencing; test actual owned S3-compatible Storage + PostgREST,
  hot-cache masking, missing objects, lost acknowledgements and populated DDL rollback.
- [x] Add a stock-Git native adapter with refs-only advertisement, private physical
  readback, typed roots/peels, protocol negotiation and SQL atomic/non-atomic publication.
  The ASGI test fixture supplies an explicit grant; the canonical router does NOT
  select this adapter yet. This does not repair the old route's remaining targets.
- [x] Exercise SHA-1/SHA-256 HTTP push/cold clone, rewrite/delete/atomic success,
  and SHA-256 physical GC quarantine; preserve reader availability during fencing.
- [x] Verify clean core commit `11ec37d5` against actual Supabase + S3:
  756 passed / 34 failed, with 329 pgTAP tests; all 150 added cases pass,
  including 101 S3-layer cases and all 78 native HTTP workflow comparisons.
  The sole removed earlier failure (`pull-merge`) is a deterministic recipe
  correction, NOT a product repair: both clients now receive the same merge
  message instead of embedding different remote URLs. Offline: 2703 passed /
  27 skipped / 76 deselected. Later dependency absorption and edits are outside
  this clean receipt. The 34 remaining original targets still block acceptance.
- [x] Reproduce chunk URI rejection and late-part PUT corruption after ACK using
  actual owned S3 in both formats. Use immutable part/manifest keys without
  changing manifest wire version 1; preserve legacy reads and reject mutable
  placements as native proof. Validate all chunk locations/ranges before read
  or deletion; test orphan GC and fail-closed corrupt/foreign manifests.
  Selected mixed-layer run: 41 passed with all 329 pgTAP tests, including actual
  S3 corrupt/foreign orphan rejection with zero DELETEs and a retained GC fence.
  Small configured
  chunks do not establish large-object or independent-process acceptance.
- [x] Verify clean chunk commit `58edaba2`: actual Supabase/S3 target 823 passed /
  34 failed, 329 pgTAP tests; all 28 added cases pass, no new failure names.
  S3 layer: 109 passed. Offline: 2766 passed / 27 skipped / 76 deselected.
  The earlier clean `b2703d2c` receipt is 795 passed / the same 34 failures.
  Subsequent dependency merge `7703647b` has 2772 offline passes; it is outside
  the chunk commit's full live receipt.
- [x] Reproduce replacement and late-index ACK loss in both formats against
  actual S3/PG. Read back each new physical placement before index mutation;
  validate incoming identity; fence native location writes by current pin/epoch.
  Reject stale/foreign/absent GC contexts before physical deletion and check
  token again for index removal. Retry sealed pins without new preparation.
- [x] Add forward Expand-only index RPCs/direct-DML fencing, SQL role/format/
  actor/state/reparenting tests, populated rollback/unchanged retry, missing-RPC
  no-downgrade checks and real Auth/PostgREST client denial. Selected mixed-layer
  validation: 91 passed plus all 329 pgTAP tests. This uses controlled continuations,
  not independent-process restart or production authorization evidence.
- [x] Verify clean storage fence commit `07efb294`: actual Supabase/S3 target
  885 passed / the same 34 failures, with 329 pgTAP tests. All 62 added cases pass;
  layers: native 18, component 545/34, PG 143, actual Auth/REST 58, S3 121.
  Offline: 2773 passed / 27 skipped / 76 deselected. Database policy covers all
  five task migrations after commit (its CLI compares committed HEAD, not staging).
  Corrupt foreign bundle metadata also yields zero DELETEs and a retained fence.
- [ ] Complete independent-process/multi-instance storage fault and location-cache
  recovery, long-upload renewal, paired restore and applicable resource gates;
  neither the chunk nor location-fence selections close these requirements.
- [ ] Complete durable publication/GC restart, remote-I/O quiescence recovery,
  long-upload lease renewal and bounded-resource/performance acceptance.
- [x] Add backend-only current-actor/Project-lease SQL admission and a guarded
  control adapter. Revalidate platform roles and runtime restrictions, serialize
  revocation, retain current-reader result replay, and reject missing capability.
  Verify actual S3 old-ACK readability after final credential/lease denial,
  actual Auth/REST client denial and populated expansion rollback/retry.
- [x] Reproduce and fix credential/lease expiry after repository lock waits;
  verify rollback of sequence/refs/result/reflog/outbox, and allow a freshly
  read-only grant to replay while rejecting new writes and changed requests.
  Selected actual-service regression: 54 passed plus 329 pgTAP; no skips/XFAIL.
  This is not a full clean-revision target receipt or canonical authentication.
- [x] Connect technical capacity to native publication/collection: explicit
  Org/Project inventory, atomic unique-object reservations/ledger, pre-PUT
  admission, verified-closure sealing, old-issuer fences and non-expiring
  unsettled-I/O claims. Preserve separate customer logical billing and Project
  cleanup identity. Test queued expiry, cross-Project races, populated expansion
  rollback/retry, real Data API denial, cold old-ACK reads and post-DELETE SQL
  recovery. Earlier selection: 98 passed plus 329 pgTAP; later same-pin I/O
  settlement review reproduced two failures and required per-invocation claims.
- [x] Fix per-invocation settlement: neither pin transition nor another retry
  clears outstanding I/O. Retain cancellation/timeout claims; reconcile only
  verified metadata without creating fake I/O. Same-pin red 2 -> green 6 selected.
- [x] Require isolated single-attempt native PUT/DELETE; no implicit multipart,
  ambiguous HEAD absence or accepted missing/retried mutation receipt. Actual S3
  lost-ACK tests cover both formats. Bind clients to endpoint/credential source
  without mutating shared retry/proxy configuration. Broad dirty selection 295
  passed +329 pgTAP; final client-rebinding/I/O selection 18 passed +329 pgTAP.
  Supplementary component/GC checks 75 passed. Keep native canonical dormant.
- [ ] Complete canonical logical billing/entitlement settlement, ref policy,
  lifecycle retirement and independent-process/remote-I/O recovery before treating
  this as complete M11 acceptance. No repository is automatically enrolled.
- [ ] Complete canonical admission/ref-policy/quota/consumer integration; do
  not activate this wrapper based on stored/admitted fixture credentials.
- [ ] Integrate the SQL primitive into the admitted RefTransactionService, including
  ref policy, lifecycle leases, non-atomic orchestration and result-query consumers.
  The service/physical publication path now exists, but production admission,
  quota/lifecycle orchestration, canonical route selection, result-query consumers
  and legacy Scope/product integration still need work.
- [ ] Integrate full Git transport and advertised protocol capabilities.
- [ ] Integrate product/automatic writers, reads, Scope adapters and lifecycle.
- [ ] Complete maintenance, export/restore, derived events and usage verification.

## 3. Consumers and migration (M05/M10/M17–M20)

- [ ] Correct Desktop branch/upstream behavior with legacy profile compatibility.
- [ ] Fix Workspace snapshot/base and failure recovery; retain unfinished work.
- [ ] Implement inventory, online backfill/change replay, fencing and transparent cutover.
- [ ] Run complete real PG/S3, concurrency/fault, old-client and restore gates.
- [ ] Review the diff; integration into local qubits now requires renewed user
  authorization even after applicable gates pass. Current authorization is local
  isolated Docker implementation/acceptance only: no qubits merge/push, CI/CD,
  deployment, real-repository activation or user-data migration.
- [ ] Obtain separate environment authorization and security prerequisite evidence before rollout.
- [ ] Retire old authority only after consumer/task exit evidence; update canonical documentation.

Known target gaps and absent environment evidence block full issue completion.
No task here waives the original M01–M20 or 07/08 requirements.
