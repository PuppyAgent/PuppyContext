## Delivery checkpoints (user-approved cadence, 2026-10-05)

Implement coherent cross-layer batches before running connected suites. Keep
only small safety checks in the implementation loop. Run the full frozen target
at integration checkpoints, not after each local change. The original M01–M20
and G01–G66 scope is unchanged; the historical evidence below is not completion.

| Checkpoint | Original scope | Closure criterion | State |
| --- | --- | --- | --- |
| Repository contract | M01–M04, M06–M08 | Typed repository/ref/base/operation identity; complete protocol/API families; no second authority | OPEN |
| All producers and compatibility | M09, M16; M05 mapping contract | Product/automatic/scoped producers share publication, original bases and recoverable results; old bindings remain stable | OPEN |
| Complete client workflows | M10, M17 | First publication, ordinary sync, Workspace and failed/incomplete work preservation against actual Cloud | OPEN |
| Operational closure | M11–M15, M18 | Storage, GC, lifecycle, derived effects, independent-worker recovery, export/paired restore and bounded resources | OPEN |
| Migration and retirement | M05, M19–M20 | Reentrant inventory/replay/cutover/recovery, all consumers retired safely, integrated version and final evidence | OPEN |

Global contracts precede caller edits: read selection and old-entrypoint binding
are distinct; a logical operation ID is distinct from its physical attempts and
per-invocation I/O IDs; metadata preparation is not publication; result replay is
a current read. No client is forced to invent a base or silently follow a changed
HEAD. Code complete, batch verified and environment rollout are separate states.
A discovered TODO is assigned to its owning checkpoint unless it blocks the
current end-to-end path. No production activation or remote action is authorized.

Consumer dependency map (all rows must close before cutover):

| Consumer family | Shared boundary / prerequisite | Remaining acceptance |
| --- | --- | --- |
| `content_write` Product routes | Versioned normalized intent → native writer → ref transaction | Finish actual JWT/cold/replay checkpoint; preserve old omitted-base semantics via explicit old-entrypoint mapping, never guessed HEAD |
| `access_point_fs`, `internal/mcp_runtime`, content tables | Scope/view grant and stable old binding | No widening into full repository grants; base/result/audit compatibility |
| Upload jobs/handlers, imports runner/database, synchronize write port | Persist request/base/actor before delayed work; staged-content identity | Independent attempts, current admission, pause/drain, no orphan-as-published proof |
| Project seed/templates and template registry | Initializing lifecycle, explicit authority/profile | No automatic enrollment or quota initialization; interrupted initialization recovery |
| Content history/signed reads, shadow snapshots, dashboard/git view/exporter | Admitted historical/view snapshots and response-lifetime pins | Complete bounded reads; metadata outage is not absence or repair permission |
| Desktop and Workspace router/sync worker | Explicit destination/profile/HEAD plus initialization journal | Actual Cloud first publication, failure preservation, branch and UI flows |
| Integrity/hooks/derived workers, billing/GC/deletion | Native events and complete maintenance/lifecycle contracts | Current-tree vs retention distinction; settlement and paired restore |
| Inventory/backfill/cutover/retirement | Every consumer above and M18 evidence | Synthetic migration/rollback rehearsal, old-user continuity, then authorized rollout |

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
- [x] Preserve clean `1d54c3d1` full Docker result: 1086 passed, original 34
  failures plus a real-S3 workspace-stash cold-mirror timeout and its HTTP-server
  teardown error; 329 pgTAP, no PID/OOM exhaustion. Isolated same-revision probe
  4 passed is not a repair. Capture all worker stacks on future Git deadlines
  without changing the 30-second budget. Supplementary backend: 2773 passed,
  27 skipped, 76 deselected; not strict target acceptance.
- [x] Run clean frozen logical-settlement `defff62e`: 1131 passed / exactly the
  original 34 failures, plus 329 pgTAP; no skips/errors/exhaustion. Supplementary
  backend: 2773 passed / 27 skipped / 76 deselected. This is not complete target
  acceptance; current canonical/consumer/migration work remains separate.
- [x] Repeat full order at clean frozen `8defc2cb`: 1089 passed / the original
  34 failures, 329 pgTAP, no skips/errors/exhaustion. The extra timeout did not
  recur; preserve the earlier failure, do not label diagnostics a product repair.
- [x] Run clean frozen `b652330b`: 1220 passed / exactly the original 34 failures,
  plus 329 pgTAP, no skips/errors/resource exhaustion. All application/Auth/S3/PG
  layers pass; this does not close canonical routing or migration. Supplementary
  backend: 2773 passed / 27 skipped (2800 cases, zero failures/errors).
- [x] Preserve clean frozen a8dd06c7: 1246 passed / 52 failed plus 329 pgTAP;
  original 34 plus 18 native GC consumer regressions. Supplementary backend:
  2773 passed / 27 skipped / 76 deselected. No skip/error/resource exhaustion
  in the hosting run; this is failed acceptance, not a completed routing rollout.
- [x] Reproduce ongoing pin admission failure (4 failed / 1 passed); add current
  actor and publication-lease checks before/after renewal waits, without granting
  write authority to read pins or removing the backend primitive. Validate ACLs,
  actual SDK/PostgREST, queued expiry and populated Expand rollback/retry.
- [x] Give scheduled GC a maintenance-only inventory instead of a current-tree
  facade. Preserve all retention roots, old-ACK/rejection/uncertainty assertions
  and legacy native-access guard; test the actual worker in both formats.
  Connected selections: 77+329, expanded 181+329 (all 18 regressions pass), then
  final narrowed-inventory 103+329. Components/GC/system: 432 passed. These
  dirty selections are not a frozen full repair result or original-34 resolution.
- [x] Run clean frozen `9b060399`: 1289 passed / exactly the original 34 failures,
  plus 329 pgTAP, no skips/errors/resource exhaustion. All 18 native GC consumer
  regressions are repaired; supplementary backend 2773 passed / 27 skipped /
  76 deselected. This remains failed full acceptance, not activation readiness.
- [x] Run clean frozen `6fe12550`: 1343 passed / exactly the original 34
  failures, plus 329 pgTAP; no skips/errors/resource exhaustion. Preserve the
  preceding startup failure (CLI exit 1 after 27.784s, no SQL/JUnit); its cause
  remains unestablished and unchanged-budget retry is not causal repair.
  Supplementary frozen backend: 2773 passed / 27 skipped / 76 deselected.
- [ ] Diagnose the intermittent cold-mirror/server-shutdown failure in full order.
- [ ] Complete the updated frozen full Docker target, native canonical application
  admission and worker/resource/crash-recovery acceptance. Optional external AI,
  Billing, ETL and Scheduler are disabled in this core application test profile.
- [ ] Complete real-object service and full multi-instance acceptance gates.
- [ ] Freeze complete G01–G66/API/scenario contracts and repo/ref/base integration contract.
- [ ] Complete byte/object-format/raw-header semantics including SHA-256 end to end.

### Initialization preserves acknowledged roots (M07/M14 prerequisite)

- [x] Reproduce initialization replacing a nonempty acknowledged root when its
  object probe fails, and missing checked capability falling back to a setter.
  Replace this with a backend-only SQL initializer: preserve existing valid roots
  without storage probes; require genuine absence, legacy SHA-1 authority, valid
  lifecycle and a current Project lease for first initialization. Check after
  lock waits and reject corrupt accepted metadata, native authority and missing
  RPC capability. No enrollment, repair or activation in DDL.
- [x] Validate populated Expand rollback/retry, SDK/PostgREST and anon/JWT denial,
  publication/initializer race and queued expiry. Connected owned Docker: 24
  passed plus 329 pgTAP (Auth 3, PG 8, legacy application 1, S3 2, component 10).
  Original three failing component regressions are retained separately. Additional
  corrupt-root/lifecycle tests: strict native-PG 23 passed; component/billing/deep
  scenarios 412 passed. The earlier native-PG selection's three Auth skips remain
  a rejected strict receipt, not acceptance.
- [ ] Complete native initialization together with lifecycle and migration gates.

### Logical billing integration (M15, in progress)

- [x] Add verified-tree logical byte measurement without Git materialization,
  blob re-reads or exponential path expansion. Preserve path multiplicity,
  file modes/gitlink exclusion and current-tree rather than history semantics.
  Focused component/legacy billing selection: 18 passed; not SQL settlement.
- [x] Bind actual before/after default-ref selection to current-tree measurement;
  atomically settle the existing Organization usage counter with current
  entitlement revision, actor/lease checks and ref/audit/outbox publication.
  Match the existing Organization advisory-lock ordering, fence old issuers,
  preserve read-only exact replay, and fail closed on missing initialized usage.
- [x] Connect optional checked logical billing to native publication and verify
  SQL atomic quota/entitlement rejection, sibling races, queued expiry, replay,
  HEAD switches/deletion, old-issuer ACL fences and populated Expand rollback.
  Real S3 SHA-1/SHA-256 tests preserve old ACKs through quota rejection and reuse
  sealed proof during recovery. Guarded selection: 43 passed plus 329 pgTAP;
  combined billing/admission/capacity/snapshot/transaction regression: 161 passed
  plus 329 pgTAP, no skips/errors/resource exhaustion. Component suite: 337 passed.
- [x] Reproduce the old full reconciler overwriting native usage (one real PG
  regression failed), then fence its event/counter transaction for enrolled Orgs.
- [x] Implement checked mixed-authority reconciliation and select it in the
  application scheduler: complete SQL inventory, 200-row pages, current-tree
  physical measurement, full inventory CAS, current entitlement, late expiry
  rollback, cancellation/backpressure, bounded metadata cleanup and lost-ACK
  replay. Keep legacy namespace/chunk reads distinct from native proof. Selected
  owned Docker: 82 passed plus 329 pgTAP (S3 4, PG 27, Auth 24, component 26,
  legacy application 1). Preserve earlier SQL/fixture and skipped-auth receipts;
  focused strict native-PG recovery: 10 passed. No external billing claim.
- [ ] Complete lifecycle logical settlement and native canonical worker acceptance.
- [ ] Complete named-ref/file-policy admission, canonical caller integration and
  real authenticated native application acceptance. No billing activation.

### Canonical native Git routing (M06, partial)

- [x] Select fresh PG authority in canonical Git routes; require admitted control,
  retained capacity, logical billing and file policy. Preserve Git-Protocol,
  spooling, leases, fetch audit and physical health checks. Missing metadata/RPCs
  fail closed; cached legacy roots cannot serve native reads. No enrollment.
- [x] Exercise actual src.main, JWT-issued credentials and checked PG/S3 publication
  in both formats: non-main HEAD, atomic branches/typed tags, oversized rejection,
  cold restart/protocol-v2 fetch/fsck, read-only/foreign/anonymous/revoked denial.
  Synthetic owner enrollment and entitlement projection are explicit. First
  connected selection 16 passed + 329 pgTAP; guarded selection 41 passed + 329
  pgTAP (application 3, S3 2, Auth 3, PG 13, component 20), no skips/errors/gaps.
- [x] Preserve legacy fixture authority and original assertions; component/router
  regression 503 passed. Supplementary backend initially 2772 passed / 1 failed /
  27 skipped: one mixed-protocol MagicMock fixture lacked explicit legacy selection,
  so its undefined result correctly could not become a legacy fallback. Explicit
  fixture selection plus admission/selector regression: 21 passed, with unchanged
  mixed-write/recovery/latency assertions; not a replacement full-backend receipt.
- [ ] Complete native ProductOperationAdapter/API, Scope/legacy mappings and automatic
  writers; complete lifecycle/consumer/resource/recovery/migration gates before
  activating existing repositories. Original canonical target failures remain open.

### Selected native Product reads (M08, partial)

- [x] Reproduce native Product reader/route fallback failures (8 adapter and 10
  route cases); use a single admitted ref snapshot for selected content reads
  and their real base, without transport materialization or legacy aliases.
- [x] Wire authenticated `ls`, `cat`, `raw`, `stat`, `tree`; expose captured
  ref/OID/HEAD guard, byte-path alternatives and modes without following symlinks
  or fetching external gitlinks. Preserve historical HTTP contract fixtures and
  add an exact five-path/five-schema forward delta, not a wildcard exemption.
- [x] Reproduce destructive read-time root healing (2 failures) and authority
  failures disguised as absence (16 failures). Preserve nonempty acknowledged
  roots, healthy siblings and physical-only recovery in component/owned S3/PG
  tests. Do not reinterpret legacy incident rows as native root authority.
- [x] Exercise actual JWT/application/PG/S3 reads in both formats before/after
  restart; synthetic enrollment remains explicit. Guarded connected selection:
  103 passed + 329 pgTAP (component 78, PG 9, Auth 3, application 3, S3 10), no
  skips/errors/resource gaps. Components/contract/legacy regression: 534, then
  535 passed including equal text/base64 Unicode path-length behavior.
  Preserve the earlier backend 2772/1 contract-delta failure and the accidentally
  selected Desktop-window test's missing-environment failure; neither is a full
  native Product acceptance result.
- [ ] Complete signed inline/download streaming, historical/Scope readers and
  all remaining authority-aware consumers; complete long-I/O/resource bounds.
- [ ] Complete native Product writes, actual starting-base/target propagation,
  automatic producers, lifecycle, recovery and migration before activation.

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

- [x] Reproduce HTTP bulk-write discarding a supplied stale base and overwriting
  the newer ACK. Preserve optional bases through byte/reference bulk commands,
  including empty-base/no-op/CAS-retry and multi-scope rejection. Retain 25 red
  cases; expanded 31 component regressions pass. Register one exact forward
  request-schema delta; historical fixtures and omitted-base policy stay intact.
  Components/legacy/contracts: 520 passed; actual owned services: 64+329 (PG11,
  Auth8, application3, S3 4, component38), including real JWT bulk success/409,
  anonymous denial, cold restart/fetch and preservation of earlier commit bytes.
  The first 60+329 selection is rejected for missing the requested S3 test layer;
  preserve it and the initial/miscomputed contract-delta failures. Supplementary
  dirty backend: 2773/27 skipped/76 deselected. Native Product publication and
  automatic starting-base capture remain unfinished; this is not M09 acceptance.
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

### Native Product preparation and explicit ingress (M09, partial)

- [x] Reproduce absent retry-stable preparation (22 PG reds) and the additional
  four mismatched/unprepared publication reds in both object formats.
- [x] Add empty, private intent/candidate metadata with stable server time,
  original input digest, exact native ref-request digest and atomic result fence.
  Current actor/lease is rechecked after waits; matching result recovery is a
  current read without a new write lease. Add checked SDK methods without fallback.
- [x] Verify the initial journal metadata ACL/queued-expiry/upgrade and real
  Supabase API boundary (dirty selected 82 plus 329 SQL). Stored-actor proof is
  distinct from end-user authentication and S3 durability.
- [ ] Close native revision/grant/request identity through Product operations,
  staging, commit construction, replay and ALL HTTP/automatic producers.
  The five explicit routes are verified at clean cb8480c6: actual JWT/PG/S3/cold
  Git, both formats, raw names, no-op/base/quota, sealed/lost-ACK and current-read
  replay/revocation. Frozen full 1484/original34 +329 SQL; failure set unchanged,
  not whole-issue acceptance. Keep all four integration failures (stale HTTP500,
  creator downgrade, missing member org_id, the test's own extra read pin).
- [x] Separate physical attempts for explicit Product requests with the original
  input available. Checked current admission, immutable candidate/clock, old-pin
  tombstones, active result fencing and per-invocation uncertainty survive retry.
  Actual PG/S3 verifies expired/unacknowledged upload recovery and a real old PUT
  completing after the new ACK; the old worker recovers that result as a reader.
  Dirty connected115 +329 SQL includes original fixed-pin verified reuse and
  post-pin-wait lease expiry rollback; components559 and supplementary backend
  2773/27 skipped/76 deselected. These
  are selections, not a new frozen full result or process/restore acceptance.
  Preserve missing-RPC8, PG81/2 ambiguous-name and connected99/12 invalid-qualifier
  failures; the final expected_roots name fixes the SQL without relaxing guards.
- [ ] Complete independent-attempt recovery across the remaining producer paths,
  durable input handoff and independent-process/restore/resource gates. Another
  invocation's unsettled I/O never becomes eligible merely because retry passed.
- [ ] Complete other consumers, legacy/Scope mappings and migration before activation.

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
- [x] Add optional checked file admission alongside capacity/billing: new blob
  limits before PUT, prior-published grandfathering, current-tree move/copy
  multiplicity, private publisher fences and atomic rollback. Capture actor/lease
  on pins; revalidate every new I/O claim and prohibit uploading-pin lease
  borrowing. Checked readers/metadata discovery recheck current credentials.
  Preserve current-reader result replay and backend-only reconciliation authority.
  No canonical routing or native activation is implied. Selected pre-metadata
  integration: 98 passed +329 pgTAP; metadata guard final strict native-PG:
  47 passed; current component/storage-billing: 365 passed.
  Retain the sealed-retry regression (unpublished incoming roots were incorrectly
  sent through a read snapshot) and its repair: fresh verification under the
  publication pin, never wider reader authority. Subsequent broad regression and
  clean frozen acceptance remain separate receipts.
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
