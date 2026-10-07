# ISSUE-105 verification evidence

Source baseline: `923e3911` (worktree and local `qubits` matched at task start).
Node `22.22.3`, npm `10.9.8`. All commands ran locally in this worktree.
`frontend/node_modules` is a local directory, clonefile-copied from canonical
dependencies before local npm installs; canonical dependencies were not changed.

| Command (from frontend unless noted) | Result | Evidence |
| --- | --- | --- |
| `npm run lint -- --no-cache --format json --output-file ../artifacts/tests/issue-105/legacy-default.json` (before migration) | exit 0; 75 warnings | legacy-default.json/log |
| `node node_modules/next/dist/bin/next lint --dir . --no-cache --format json --output-file ../artifacts/tests/issue-105/legacy-full.json` (before migration) | exit 1; 103 warnings, 1 existing hook error | legacy-full.json/log |
| `node scripts/readability/run-eslint.mjs --format json --output-file artifacts/tests/issue-105/flat-full.json` (flat config before adding six rules) | exit 1; same existing error, 105 warnings including 2 in newly scanned root packages | flat-full.json/log |
| `node artifacts/tests/issue-105/compare-migration.mjs` (repository root) | exit 0; both original scopes exactly identical | migration-comparison.json/log |
| `node scripts/readability/run-eslint.mjs --suppress-all --format json --output-file artifacts/tests/issue-105/readability-initial.json` (one-time initial capture) | exit 0; 446 existing violations suppressed | readability-initial.json; eslint-initial.json |
| `frontend/node_modules/.bin/depcruise --config frontend/.dependency-cruiser.cjs --output-type baseline frontend packages` (root; one-time capture) | exit 0; 59 existing violations | dependencies-initial.json; dependencies-before.json |
| `npm run test:readability-gates` | exit 0; 21 tests passed | gate-tests.log |
| `npm run lint:baselines -- 923e3911` | exit 0; no enlargement | baseline-ratchet.log |
| `npm run lint` | exit 0; 105 existing warnings, no unsuppressed errors | lint.log |
| `npm run lint:dependencies` | exit 0; 59 known violations, 0 new | dependencies.log |
| `npm run test:unit` | exit 0; 222 tests / 42 files passed | unit-tests.log |
| `npm run build` (environment below) | exit 0; type checking, compilation, page generation and tracing passed | build.log |
| `npm ci --ignore-scripts --dry-run` | exit 0; manifest/lock compatible | lockfile-check.log |
| `git diff --check` (root) | exit 0 | executed before commit |

Build environment: `PUPPYONE_LOCAL_BUILD=1`,
`PUPPYONE_NEXT_DIST_DIR=.next-check`,
`NEXT_PUBLIC_SUPABASE_URL=https://placeholder.supabase.co`,
`NEXT_PUBLIC_SUPABASE_ANON_KEY=placeholder-anon-key-for-build-only`,
`NEXT_PUBLIC_API_URL=http://localhost:9090`.
The build-generated changes to tracked `tsconfig.json` / `next-env.d.ts` were
removed afterward; the isolated build output is ignored.

Lint JSON snapshots retain all diagnostics (including positions, suggestions and
fixes) and relative file paths; embedded source copies were removed. The
`source-before.json` hashes identify the original frontend input. The migration
comparator selects that exact input and Next's original default directories;
the full message objects compare equal. The saved dependency scan retains the
summary and representative resolved app/package aliases, not the entire graph.

The initial baseline snapshots are also the one-time bootstrap ceilings for CI
when its base commit predates this gate. Do not regenerate them during pruning.
After adoption the previous Git commit's baselines supply the ratchet.

Non-blocking output: 105 existing lint warnings, Supabase Edge Runtime build
warnings, jsdom navigation-not-implemented messages in passing login tests.
The sole prior lint error is `react-hooks/rules-of-hooks` at
`frontend/contexts/VersionWebSocketContext.tsx:61`; it remains enabled and has one
native suppression. No product behavior, backend, remote branch or deployment
was changed.
