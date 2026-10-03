## 1. Contract and backend
- [x] 1.1 Record method/path, DTO, ID, error and authorization contracts in ISSUE-058 section 5.10.
- [x] 1.2 Add typed canonical HTTP boundary over the same Synchronize operations.
- [x] 1.3 Register Project authorization and verify OpenAPI plus lifecycle/negative HTTP cases.

## 2. Consumers
- [x] 2.1 Migrate Desktop generic resource client, types and tests without legacy fallback.
- [x] 2.2 Add shared cloud-core Synchronize client and migrate Web consumers.
- [x] 2.3 Verify request contracts, type checks, relevant regressions and builds.

## 3. Delivery
- [x] 3.1 Review against latest local qubits, run checks and integrate verified commits.
- [x] 3.2 Update canonical documentation and issue audits with exact evidence.
- [x] 3.3 Record remaining Access/GitHub/database-source HTTP work and 049/060/061 dependencies.
- [ ] 3.4 Retire legacy HTTP only after coordinated consumers and deployment evidence (not completed by this source increment).

## Verification receipt (2026-10-03)

- Tested source pairing: Cloud `570c6917`, Desktop `db5debf8`, integrated into both local qubits branches after incorporating concurrent qubits changes.
- Backend non-e2e/non-integration/non-network suite: 2582 passed, 27 skipped, 52 deselected. Actual TypeScript Desktop/Web SDK -> local HTTP tests: 2 passed, using isolated identity/persistence/queue facts, not real Provider/database/Electron-window evidence.
- Desktop Cloud/Automation: 361 passed; Web: 175 passed. Both builds/type checks passed; Desktop boundary/changed-file ESLint passed. Installer checks skipped because release artifacts are absent. Python Ruff is unavailable.
- Post-merge: Desktop 361, Web 175, Backend focused 94 (including both actual-client HTTP tests) passed.
- OpenSpec 1.14.0 strict validation passed. Canonical docs/audits in puppy-issues updated and validated: 56 audited issues; 391 Markdown files, 320 with Status, 287 governed; 25 existing cross-repository-link warnings. Shared documentation edits remain uncommitted to avoid including other agents' staged work.
- No push, deployment, production query or schema migration. ISSUE-058/059 remain OPEN. Canonical backend must precede migrated clients; no supported production version window or final legacy retirement is claimed.
