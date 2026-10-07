# Cloud readability integration verification

Local-only review of ISSUE-105/101/106 in
`/Users/supersayajin/project/puppyone-cloud-frontend-readability`, branch
`codex/frontend-readability`. Input HEAD: `b81777ca`; strict baseline ceiling:
`aac9aca02ef83d0ea807e179126dc1a1675ee944`. Source under final verification:
`4050128e150426fb90fea24deab2fd911e3dabb2`.

Read repository AGENTS.md (no applicable nested instructions), all three pending
issues, their worker reports, Cloud Frontend Code Organization, Workspace
Architecture and Performance, and the current repository workspace design.
The coordinator owns canonical documentation and issue audits; none were edited.

## Findings and fixes

- Reproduced nine new dependency violations before editing (`dependencies-before.log`).
  Moved the newly introduced task model/actions/invalidation to `lib/tasks` and the
  auth-aware provider to `contexts`. ETL and Import keys now belong to
  `lib/queryKeys`. `useImportJobs` receives account identity and its lifetime guard
  from its auth-aware caller. Task data still belongs to the current SWR provider.
- Preserved the existing 18 task tests before and after relocation. Added seven
  Import lifetime tests and one progress subscription test. Four new tests first
  demonstrated callbacks writing to the newly selected project/account, stale
  in-flight data on account return, and post-unmount writes. A fifth failure
  demonstrated a late rejected request poisoning a new account lifetime's error
  state. Both failing captures are retained. Captured query commands now have
  explicit keys and lifetime guards; account teardown clears old Import cache
  data and dedupe markers. Current read errors and old-project snapshots survive
  ordinary navigation. Successful creation callbacks still work.
- Progress remains separate from list data. A 100-update test proves no extra list
  subscriber renders, widget expansion, or immediate polling restart; the existing
  three-second poll still runs on schedule. Completion invalidation remains scoped
  to account/project/organization and known upload folders, uses the active SWR
  provider, and preserves file snapshots while refreshing.
- Removed the deleted `data-ui` alias left in the gate worker's TypeScript config;
  the synthetic dependency fixture now uses a generic example package. No real
  shared package was modified. No remaining package/path references were found.
- Pruned only fixed ESLint entries. No thresholds, exclusions, dependency rules,
  suppression ceilings, or existing tests were relaxed.

## Acceptance review

| Issue / criterion | Integration result |
| --- | --- |
| 101 A1 | PASS: lookup-based icons and all 12 legacy/current glyph snapshots unchanged; 24 icon tests in the full suite. FileTypeIcon complexity 3 / 20 counted lines. |
| 101 A2 | PASS: saved pane snapshots unchanged; 15,360 combinations in characterization tests and 20,000 separately compared edge cases. Main resolver complexity 12 / 48 lines; all extracted helpers meet exact thresholds. |
| 101 A3 | PASS: package stays deleted; removed merged stale TS alias; package-reference audit, boundary script, deployment contract and build pass. |
| 101 A4 | PASS: full unit suite, lint and actual workflow-environment production build. |
| 105 A1 | PASS: reviewed retained legacy configuration and original diagnostic equality evidence; ESLint CLI 9.39.1 remains the entrypoint. |
| 105 A2 | PASS: all six thresholds unchanged, native suppressions shrink, new helpers are unsuppressed; 21 real-CLI mutation/ratchet tests pass. |
| 105 A3 | PASS: actual nine new edges removed; dependency gate has zero new violations; existing negative direction/cycle/alias tests pass. |
| 105 A4 | PASS locally: independent workflow retained; exact build env passes without local-build override. Hosted CI not run. Baseline counts below. |
| 106 A1 | PASS: shared account-scoped SWR list and provider-scoped actions; original list-key invalidation/placeholder/clear tests retained. |
| 106 A2 | PASS: scoped completion/progress, three-second polling, current/error snapshots, table/project isolation, after-unmount upload completion, account lifetime and progress performance covered. |
| 106 A3 | PASS: all six exact event names absent from 627 JS/TS sources; owning project/list invalidation after upload remains effective even after dialog closure. |
| 106 A4 | PASS: 26 focused task tests; final full suite 296 tests / 46 files, lint and production build. |

## Exact commands

From the worktree root:

```sh
node artifacts/tests/cloud-integration/verify.mjs
node artifacts/tests/issue-101/check-readability.mjs
node artifacts/tests/issue-101/compare-layout.mjs
node scripts/check-data-ui-boundaries.mjs
rg -n 'packages/data-ui|@puppyone/data-ui|data-ui/' frontend packages scripts .github
# rg exit 1 means no references; all other commands above exit 0.
git diff b81777ca --check
```

`verify.mjs` executes the following from `frontend/`, stores each full log, and
records every exit code in `final-results.json`:

```sh
npm run lint:prune
npm run dependencies:prune
npm run lint:baselines -- aac9aca02ef83d0ea807e179126dc1a1675ee944
npm run lint
npm run lint:dependencies
npm run test:unit
env -u PUPPYONE_LOCAL_BUILD -u PUPPYONE_NEXT_DIST_DIR -u API_INTERNAL_URL \
  NEXT_PUBLIC_SUPABASE_URL=https://placeholder.supabase.co \
  NEXT_PUBLIC_SUPABASE_ANON_KEY=placeholder-anon-key-for-build-only \
  NEXT_PUBLIC_API_URL=http://localhost:9090 npm run build
```

Also run from `frontend/` (each exit 0):

```sh
npm run test:readability-gates
npm run test:unit -- --no-cache tests/workspace/task-refresh.test.tsx tests/workspace/task-lifetime.test.tsx
npm run test:bff-contract
npm run test:hydration-contract
npm run test:deployment-contract
```

`event-audit.json` records an all-source scan for each of the six exact names.
The initial 18-test run is `tasks-before.log`; relocation remains 18/18 in
`tasks-relocated.log`. Intentional failing regression evidence is
`import-races-before.log` (4 failures) and `import-error-race-before.log` (1 failure).
Final task tests are `tasks-final.log`. All captured log whitespace is normalized.

## Ratcheted baselines

| Rule | aac9aca0 | Final |
| --- | ---: | ---: |
| no-nested-ternary | 313 | 291 |
| complexity | 76 | 74 |
| max-lines-per-function | 30 | 30 |
| max-lines | 13 | 13 |
| max-params | 8 | 8 |
| max-depth | 5 | 5 |
| react-hooks/rules-of-hooks (existing) | 1 | 1 |
| Total | 446 | 422 |

ESLint: 231 → 227 file/rule entries, 146 → 143 files. Dependency baseline remains
59 entries: 40 components-no-features, 17 lib-no-application, 2 no-circular;
**zero new edges**. See `baseline-counts.json` and `baseline-ratchet.log`.

## Build and delivery boundary

The workflow's three placeholder environment variables were used exactly.
`PUPPYONE_LOCAL_BUILD`, `PUPPYONE_NEXT_DIST_DIR`, and `API_INTERNAL_URL` were unset.
Source review confirms the local-build flag only limits Next build CPU workers;
there is no additional network bypass in that branch. Both the initial and final
actual-environment builds pass, including type checking and 33/33 static pages.
No workflow change or network-service mutation was needed.

All final checks pass. Existing nonfatal diagnostics remain: 103 lint warnings,
jsdom login-navigation stderr, browser-data freshness notices, and Supabase Edge
Runtime warnings. No authenticated browser or hosted-CI verification is claimed.
Backend code/services and deployed environments were untouched. Dependencies were
already a real worktree-local directory; no install or shared dependency mutation
occurred. No canonical branch edits, merges, pushes, worktree removal, remote PR,
or puppy-issues edits were performed.

Source commits:

- `98753bce` — `refactor(frontend): align task queries with dependency boundaries`
- `0420e942` — `fix(frontend): guard import queries across account and view lifetimes`
- `4050128e` — `chore(frontend): ratchet merged readability fixes and remove stale package alias`
