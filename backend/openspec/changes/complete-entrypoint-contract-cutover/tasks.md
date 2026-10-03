## 1. Complete owned contracts
- [x] 1.1 Canonical GitHub API/DTO/webhook, Project/OAuth authorization on canonical and transitional routes, shared/Web clients and no-fallback negative tests.
- [x] 1.2 Canonical Database Import sources API/DTO, management action alignment, shared/Web clients and cross-Project/secret/target tests; hand off physical source mapping to 049.
- [x] 1.3 Typed Dashboard/Activity aggregation and synchronize permission wire values; migrate every affected Desktop/Web consumer.
- [x] 1.4 Audit existing Automation/Access resource identities, ID collisions, lifecycle/reload/retry, Scope-only and foreign/absent target cases; resolve findings rather than renaming alone. Independent 059 A1 review does not require a new orchestration product; production Main/preload/App window recovery is verified below.

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
- New Dashboard/Activity consumers have no legacy response fallback. Follow-up
  by the 060/061 owner migrates CLI `dashboardAction` to `/dashboard/resources`,
  with real CLI HTTP tests for collisions, Project policy, failed reads and
  old-server upgrade behavior. This closes the CLI handoff, not tasks 2.x.

At the aggregate checkpoint, tasks 1.4 and 2.x remained open. The later
independent A1 review and real-window gate below supersede that source/window
status, not physical migration/classification, installed-consumer exit,
deployment or recovery requirements. No push, deployment,
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
A1/A2/A6 were still recorded open at that checkpoint. The current follow-up
below supersedes A1/A2; worktrees remain for A6 and ISSUE-058 joint closure.

## CLI handoff and real-window follow-up — 2026-10-03

Accepted the 060/061 owner's **fba8b0bd** CLI Dashboard delivery instead of
merging a competing implementation. Only additional selector/response-output/
credential tests and explicit 405 upgrade guidance are added. Source CLI unit
passes; actual CLI HTTP verifies the canonical typed inventory and no fallback.
059 A1 is accepted for the existing product's resource boundaries, not a new
multi-step orchestration engine.

Desktop **29830222** adds `tests/e2e/cloud/entrypoint-window.smoke.mjs`.
`tests/platform/test_entrypoint_desktop_window.py` launches production Electron
Main/preload/App against real routers/services/policy with isolated
identity/persistence/Provider/queue facts. The UI creates a binding, recovers it
in a new renderer document, sees a failed run, refreshes the same binding, then
recovers a failed inventory read after another reload. Two run IDs remain
attached to the one binding. Three reloads (including setup) have distinct
document nonces; screenshots/request trace are retained. There is no external
network attempt. This satisfies the window portion of 059 A2 without claiming
PostgreSQL, real Provider execution, deployment or installer acceptance.

Final source verification after incorporating the owner's CLI implementation:
backend **2703 passed, 27 skipped, 76 deselected**; combined real-client HTTP /
local Redis / real-window gate **31 passed** (30 HTTP/queue + 1 window);
Desktop **376**, Web **216**, CLI unit, test types, boundary, changed-file lint
and strict OpenSpec pass. Earlier 22-case parallel CLI unit / 33-case combined
gate receipts used a superseded parallel implementation; they are not the final
integrated source count. The owner's implementation is the single implementation.

Reproduce the additional window gate from backend, with Desktop Electron/Vite
installed and a graphical session:

```bash
PUPPYONE_DESKTOP_SOURCE=/absolute/path/to/desktop \
  uv run pytest -q tests/platform/test_entrypoint_desktop_window.py
```

Tasks 2.x/3.3 remain open: 049 D05/D06 source mapping/classification, real upgrade
and fresh-install checks, target-environment consumer/queue exit, final contract
retirement and release/recovery evidence. No remote operation is authorized by
these local tests.
