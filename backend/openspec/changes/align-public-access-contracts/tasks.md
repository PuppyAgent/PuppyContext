## 1. Contract and backend
- [x] 1.1 Freeze exact routes, fields, target/error/authorization semantics in ISSUE-058 for 060 consumption.
- [x] 1.2 Implement canonical Access surface HTTP/schema boundary over existing operations and register authorization/router ordering.
- [x] 1.3 Verify credentials, resource identity, target immutability and legacy non-Access admission; preserve historical OpenAPI fixture with an exact additive delta.

## 2. Consumers
- [x] 2.1 Migrate Desktop Access resource DTOs/requests and consumers without fallback.
- [x] 2.2 Migrate Web/cloud-core Access leaf client and remove obsolete ConnectorRun/source lifecycle assumptions.
- [x] 2.3 Verify transport contracts, actual client HTTP, relevant regression suites, types, boundaries and builds.

## 3. Delivery
- [x] 3.1 Reconcile latest local qubits, retest, and integrate passing commits only.
- [x] 3.2 Update canonical docs/audits and communicate CLI/schema/runtime handoff.
- [ ] 3.3 Retire old HTTP after actual consumers/deployment evidence (not completed by this local source increment).

## Local verification — 2026-10-03

- Backend: `uv run pytest -q -m 'not e2e and not integration and not network'`: 2607 passed, 27 skipped, 54 deselected. Historical OpenAPI fixture remains immutable; the additive Access delta fingerprints 11 paths and 16 schemas. Metadata triggers reject `import_once` before mutation; ordinary reads/writes cannot expose or inject credentials.
- Actual Node-loaded Web/shared and Desktop resource clients over localhost HTTP: `PUPPYONE_DESKTOP_SOURCE=<checkout> uv run pytest -q tests/platform/test_access_clients_http.py tests/platform/test_synchronize_clients_http.py`: 4 passed. Identity, persistence and issuance/queue facts are isolated doubles, not live JWT/DB/Provider/Redis acceptance.
- Desktop Cloud/Automation unit/component/architecture suites: 61 files, 372 passed; source/test typechecks, changed-file ESLint, Automation boundaries and build passed.
- Web: 37 files, 202 passed; typecheck and Next build passed. Resource-kind badges prevent colliding Access/Synchronize IDs from selecting a binding. CLI issuance rejects stale user/session/target responses and masked keys.
- Ruff 0.16.10 checks all changed Python files; new boundary/test files also pass formatting. OpenSpec 1.14.0 strict validation passes.
- Desktop projection joins MCP adapter detail only by persisted Access identity/Project, not path. Scope/URL-only placeholders are not healthy or counted as persisted surfaces; ordinary metadata never becomes a runnable key URL.
- Executable pairing: Cloud `2780dd76`, Desktop `adc7053a`, fast-forwarded to both local `qubits` branches after confirming their bases had not diverged. Post-integration: Backend focused 93 (including the four actual-client HTTP cases), Desktop 372, Web 202 passed.
- Canonical Access/entrypoint and Desktop Access docs plus ISSUE-058/059 audits updated in the shared documentation repository; 56 issue audits and 398 Markdown documents (295 strictly governed) validate. Its existing staged changes remain untouched/uncommitted; 25 pre-existing cross-repository source-link warnings remain. Contract handoff is recorded in ISSUE-058 §5.11 for CLI/schema/runtime owners; their branches were not merged or edited by this increment.
- Builds use explicit API configuration only. Chunk warnings remain; Desktop `release/` is absent, so package checks were skipped. No installer, real Electron-window, deployed supported-version window, historical-row migration, production query, push or deployment evidence is claimed. Specialized protocol adapters and remaining GitHub/Database Import contracts are separate work; legacy HTTP retirement stays open.
