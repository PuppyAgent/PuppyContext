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
- [ ] Complete durable publication/GC restart, remote-I/O quiescence recovery,
  long-upload lease renewal and bounded-resource/performance acceptance.
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
- [ ] Review the diff and integrate into local qubits only after applicable gates pass.
- [ ] Obtain separate environment authorization and security prerequisite evidence before rollout.
- [ ] Retire old authority only after consumer/task exit evidence; update canonical documentation.

Known target gaps and absent environment evidence block full issue completion.
No task here waives the original M01–M20 or 07/08 requirements.
