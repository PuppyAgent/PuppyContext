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
- [ ] Resolve the observed stock-Git prefix/reflog create race without weakening
  the original oracle; 6/100 standalone attempts had two failed writers.
- [ ] Complete real-object service and full multi-instance acceptance gates.
- [ ] Freeze complete G01–G66/API/scenario contracts and repo/ref/base integration contract.
- [ ] Complete byte/object-format/raw-header semantics including SHA-256 end to end.

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
- [ ] Complete target-environment security/deployment gates; local fixture success
  does not authorize activation or prove real-user upgrade compatibility.
- [ ] Implement durable object receipts/pins and GC-publication coordination.
- [ ] Integrate the SQL primitive into the admitted RefTransactionService, including
  ref policy, lifecycle leases, non-atomic orchestration and result-query consumers.
  Existing root-first same-tree CAS and transport targets still fail.
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
