## 1. Baseline and objects (M01/M03/M18, partial)

- [x] Create isolated Cloud/Desktop worktrees from the integration baselines.
- [x] Import the existing uncommitted hosting harness without modifying its source worktree.
- [x] Reproduce non-UTF8 tree, missing tag traversal and gitlink closure failures before fixes.
- [x] Verify typed graph fixes with native Git, transport/GC regressions and the backend suite.
- [x] Harden evidence/strict-mode handling; run isolated PostgreSQL validation.
  Supplementary native PG: 5 passed, 1 known head-CAS failure. Supabase startup
  timed out and its owned containers were stopped; real Supabase/S3 gate remains open.
- [ ] Freeze complete G01–G66/API/scenario contracts and repo/ref/base integration contract.
- [ ] Complete byte/object-format/raw-header semantics including SHA-256 end to end.

## 2. Unified authority (M02/M04–M09/M11–M16)

- [ ] Add schema/RPC and real role/constraint/upgrade tests without editing B1.
- [ ] Implement durable object receipts/pins and GC-publication coordination.
- [ ] Implement old-OID CAS, atomic refs/HEAD, idempotent result, audit and outbox.
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
