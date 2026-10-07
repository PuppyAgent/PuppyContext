# Frontend readability gates

Run from `frontend/`:

```sh
npm run lint
npm run lint:dependencies
npm run test:readability-gates
npm run lint:baselines -- <base-commit>
```

Both scans cover the entire `frontend/` and root `packages/`, including tests
and tooling. Generated builds, dependencies, coverage and generated Next types
are excluded. ESLint retains `next/core-web-vitals` unchanged and adds complexity
20, depth 4, parameters 5, no nested ternaries, 800 file lines and 300 function
lines (both line rules skip comments and blank lines). It uses the native
`eslint-suppressions.json` format. No product code was changed to seed the gates.

Next 15's in-process lint pass does not apply CLI bulk suppressions, so its build
lint pass is disabled; the dedicated `frontend-readability.yml` workflow runs
the complete ESLint CLI gate before unit tests and build. Type checking remains
enabled in `next build`. The existing backend and frontend workflows are untouched.

Dependency-cruiser includes TypeScript type-only imports, re-exports and dynamic
imports. `tsconfig.dependencies.json` inherits the application configuration and
maps the remaining source-only root package aliases, which have no package
manifests. Normal `@/` aliases and installed `@puppyone/cloud-core` exports resolve
to source. Missing local imports fail rather than disappearing from the graph.
Shared UI and package sources may depend on one another, but never on frontend
application code.

After fixing violations, remove only unused baseline entries:

```sh
npm run lint:prune
npm run dependencies:prune
```

Both regular checks fail for stale entries, so fixes must shrink the baseline in
the same change. Neither prune command adds entries. Do not regenerate baselines
with `--suppress-all` or the dependency-cruiser baseline reporter for new code.
ESLint's native unit of suppression is a file/rule count: replacing a violation
with another in the same file while keeping that count cannot be distinguished.
Moving a suppressed violation to a new file is rejected; fix it during the move.

CI compares each file/rule count and each dependency entry against the PR base
SHA or push-before SHA, rejecting new keys, count increases and duplicate
dependency entries. The initial adoption has no baseline on its base branch;
only that case uses the captured initial snapshots under
`artifacts/tests/issue-105/`. Once merged, the actual Git base is always the
ratchet, including reductions already merged. Invalid Git references fail.
Initial snapshots are historical evidence, not files to update when pruning.

Initial baseline at `923e3911`:

| ESLint rule | Violations |
| --- | ---: |
| no-nested-ternary | 313 |
| complexity | 76 |
| max-lines-per-function | 30 |
| max-lines | 13 |
| max-params | 8 |
| max-depth | 5 |
| react-hooks/rules-of-hooks (existing rule) | 1 |
| **Total (146 files, 231 file/rule entries)** | **446** |

Dependency baseline: **59 entries**: 40 `components-no-features`, 17
`lib-no-application`, 2 `no-circular`. There are no feature-to-route,
package-to-application or unresolved-local violations. These are resolved file
edges/cycles, not the issue's directory-level cycle estimate or import-statement
counts.

The existing hook error is `frontend/contexts/VersionWebSocketContext.tsx:61`
(`_useProjectId` calls `useContext`). It is preserved and individually baselined.
The previous default lint scope omitted `contexts/`, `features/`, shared UI and
root packages: 75 warnings/0 errors on that scope; 103 warnings/1 error when
running the same old rules over the full frontend. Flat config gives identical
diagnostics on both original inputs. Root packages add 2 existing warnings, for
105 unsuppressed warnings overall. Warning severity has not changed.

See the migration snapshots and command logs in `artifacts/tests/issue-105/`.
The negative tests invoke real CLIs in temporary repositories and prove the six
limits, retained legacy rules, dependency directions, alias/type-only/dynamic
resolution, new cycles, native suppressions, pruning and Git baseline ratchets.

References: [ESLint native bulk suppressions](https://eslint.org/docs/latest/use/suppressions),
[dependency-cruiser known violations](https://github.com/sverweij/dependency-cruiser/blob/v17.4.3/doc/cli.md#--ignore-known-ignore-known-violations).
