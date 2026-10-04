# ISSUE-106 verification

Worktree: `puppyone-cloud-readability-106`; implementation: `991e82ef`.
Behavior baseline committed before product edits: `0833f374`.

| Command (from `frontend/`, unless noted) | Result | Evidence |
| --- | --- | --- |
| `npm run test:unit -- --no-cache tests/workspace/task-refresh.test.tsx` before refactoring | exit 0; 2 baseline tests | `baseline-targeted.log` |
| `npm run test:unit -- --no-cache` before refactoring | exit 0; 224 tests / 43 files | `baseline-unit.log` |
| `npm run lint -- --no-cache` before refactoring | exit 0; 75 existing warnings | `baseline-lint.log` |
| `npm run test:unit -- --no-cache tests/workspace/task-refresh.test.tsx` | exit 0; 18 tests | `targeted.log` |
| `npm run test:unit -- --no-cache` | exit 0; 240 tests / 43 files | `unit.log` |
| `npm run lint -- --no-cache` | exit 0; 74 existing warnings, no errors | `lint.log` |
| `node node_modules/typescript/bin/tsc --noEmit --incremental false` | exit 0; no diagnostics (also rerun after restoring generated config edits) | `typecheck.log` |
| Production build below | exit 0 | `build.log` |
| Six-event audit over all 612 frontend JS/TS source files returned by `rg --files frontend` | zero matches for every retired event | `final-event-audit.json`; original occurrences in `baseline-events.txt` |
| `git diff --check` | exit 0 | verified before implementation commit |

Production build used local placeholder configuration, without starting or changing any service:

```sh
API_INTERNAL_URL=http://127.0.0.1:9090 \
NEXT_PUBLIC_API_URL=http://127.0.0.1:9090 \
NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321 \
NEXT_PUBLIC_SUPABASE_ANON_KEY=local-build-placeholder \
PUPPYONE_NEXT_DIST_DIR=.next-issue-106 \
PUPPYONE_LOCAL_BUILD=1 npm run build
```

The first build compiled successfully but failed page-data collection because this isolated worktree lacked the backend URL configuration (`build-missing-env.log`). The configured rerun passed. No unresolved failures. Existing jsdom navigation stderr in login tests, image/hooks lint warnings and stale browser-data notices remain; none were suppressed. No deployed/browser-authenticated E2E verification is claimed.

Shared `frontend/node_modules` was used without package installation, with Vitest and lint caches disabled. No dependency or lint configuration edits. Next's generated `tsconfig.json` and `next-env.d.ts` edits were restored to their pre-build content; the isolated generated build directory was removed after verification.

Coverage includes task/list-key invalidation; widget and null-cell status; progress without file reads or collapsed-widget expansion; placeholder replacement; immediate and three-second polling with existing eligibility, terminal stop and backend progress/error freshness; unload protection and stale sweeping; polled/inline completion; root/expanded/table/project refresh; retained snapshots and retry; known-directory targeting; unmounted query refresh; real upload callbacks after initiator unmount; account/project/table/provider isolation; late responses; Import completion and unchanged Import polling.
