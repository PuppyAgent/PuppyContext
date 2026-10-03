## 1. Complete owned contracts
- [x] 1.1 Canonical GitHub API/DTO/webhook, Project/OAuth authorization on canonical and transitional routes, shared/Web clients and no-fallback negative tests.
- [x] 1.2 Canonical Database Import sources API/DTO, management action alignment, shared/Web clients and cross-Project/secret/target tests; hand off physical source mapping to 049.
- [x] 1.3 Typed Dashboard/Activity aggregation and synchronize permission wire values; migrate every affected Desktop/Web consumer.
- [ ] 1.4 Audit Automation cross-resource steps, ID collisions, lifecycle/reload/retry, Scope-only and foreign/absent target cases; resolve findings rather than renaming alone.

## 2. Joint verification and retirement
- [ ] 2.1 Review and integrate ready 053/049/054/060/061 deliveries; preserve their independent WIP and ownership.
- [ ] 2.2 Verify real isolated persistence, non-empty upgrade/fresh install/retry, authorization and actual client/runtime request paths; retain per-criterion evidence.
- [ ] 2.3 Collect exact supported versions, old producer/process/client/webhook and delayed/retry queue exit facts. Obtain authorization before remote/environment operations.
- [ ] 2.4 Retire legacy HTTP/DTO/aliases and compatibility snapshots only when the corresponding exit facts are established; execute post-contract recovery checks.

## 3. Close original issues honestly
- [x] 3.1 Run complete regression/types/lint/build/architecture/OpenAPI checks, integrate passing code locally and reverify.
- [x] 3.2 Update canonical documentation and the original 058 A1–A10 / 059 A1–A6 audit tables without reducing their criteria.
- [ ] 3.3 Close only after every original criterion has evidence; report any authorization-dependent blocker precisely, never label fixtures as deployed acceptance.

## Local verification receipt — 2026-10-03

GitHub/Database contracts are in `06e13ece`; `18e440a7` incorporates local
qubits `a5b9b8f1` (053 and 060/061), not the final 049 physical migration.
The following checks cover the aggregate/identity/context increment:

- Backend offline suite: **2703 passed, 27 skipped, 72 deselected**.
- Actual Desktop/shared SDK/CLI localhost HTTP plus Redis queue checks:
  **27 passed**, including the eight-case Access-only / Binding-only /
  distinct-ID / equal-ID coexistence matrix. Its independent persistence and
  auth facts are isolated doubles, not a deployed PostgreSQL or Provider.
- Web: **216 passed**, TypeScript and Next build passed.
- Desktop Cloud/Automation: **376 passed**, source/test types, boundary check,
  changed-file ESLint and build passed. No release package was available.
- OpenAPI deltas retain the historical baseline; SynchronizeBinding now
  requires an explicit string path, and an absent path returns repair guidance.
- New Dashboard/Activity consumers have no legacy response fallback; the
  owner of 060 must separately migrate CLI `dashboardAction` from `/dashboard`.

Tasks 1.4 and 2.x remain open: the isolated matrix is not complete product
cross-resource orchestration/window acceptance, physical migration/classification,
installed-consumer exit, deployment or recovery evidence. No push, deployment,
production query or migration has been authorized or performed.

Post-integration: Cloud `8e86e89e` and Desktop `041b897d` were fast-forwarded
into both local qubits worktrees. Repeating the full backend, Desktop and Web
suites there produced **2703 / 376 / 216 passed**; the joint HTTP/Redis gate
again produced **27 passed** with the qubits Desktop source. Types and the
Automation boundary passed again. Canonical documentation and original audits
were updated without committing the shared issue repository's staged work:
**56 audited issues**, **400 Markdown / 329 explicit Status / 298 strictly
governed documents**; 25 existing cross-repository link warnings remain.
ISSUE-059 A5 is now PASS for its original isolated-regression requirement;
A1/A2/A6 and ISSUE-058 overall remain open. Worktrees are retained for that work.
