<!--
Thanks for the PR! Please fill in the sections below so reviewers can move fast.
See CONTRIBUTING.md for the full workflow:
https://github.com/puppyone-ai/puppyone-cloud/blob/main/CONTRIBUTING.md
-->

## Summary

<!-- One or two sentences: what does this PR change and why? -->

## Target branch

<!--
Default target should be `qubits` (staging). Only two PR types should target
`main`:
- release PRs from `qubits`
- same-repo urgent hotfix PRs from `hotfix/*`

All other PRs targeting `main` are blocked by the "Main Release Gate" CI job.
-->

- [ ] Base branch is **`qubits`** (or this is a `qubits` → `main` release PR / same-repo `hotfix/*` → `main` hotfix PR)

## Type of change

- [ ] feat — new feature
- [ ] fix — bug fix
- [ ] perf — performance improvement
- [ ] refactor — code change that is neither a fix nor a feature
- [ ] docs — documentation only
- [ ] chore — tooling, build, CI, deps
- [ ] hotfix — urgent production fix targeted at `main`

## Test plan

<!--
How did you verify this change? Examples:
- Ran `uv run pytest -m "unit"` locally — all green
- Manually tested the new endpoint with `curl ...`
- Verified UI in `npm run dev` at http://localhost:3000/foo
-->

## Database release phase

<!--
Complete this section for schema/data, release-pointer, migration-runner/workflow,
or application changes requiring a new database contract. Otherwise choose No
database change and remove the remaining database fields. See
docs/architecture/13-database-release-governance.md#migration-merge-admission.
Choose the single phase this PR activates; preparing contract.pending.sql is
not permission to activate Contract against a populated environment.
-->

- [ ] No database change
- [ ] Expand — additive schema and old/new-compatible application behavior
- [ ] Data — immutable `supabase/data_migrations/<id>` artifact
- [ ] Cutover — application now uses only the new fact
- [ ] Contract — cleanup after this target environment's prerequisite receipts and cutover evidence

If this changes the database, provide:

- Data migration ID / required Contract marker:
- Candidate/base SHA, target environment and last successful release evidence:
- Migration history differences and dependencies (exact missing versions/checksums, or none):
- Affected tables and estimated rows:
- Compatibility with the currently deployed API/workers; dependent releases:
- Expected runtime and lock behavior:
- Clean-install and populated-upgrade evidence; exact history-gap fixture if applicable:
- Verification and safe retry behavior:
- For cutovers: stopped writers/queues, current restore point and restart/config plan (or not applicable):
- Forward-fix / break-glass plan:
- Qubits evidence:

<!-- A passing dry run / --include-all does not prove safe dependency order. -->
- [ ] Reviewed the full diff against the refreshed target, including inherited migrations
- [ ] Shared SQL/artifacts remain immutable; no history stamping or guard bypass
- [ ] Database validation passed; applicable upgrade failures resolved or unrelated failures explicitly scoped
- [ ] This phase is ready for automatic deployment; final Contract is inactive until its prerequisites are complete

Release state being claimed: implemented / tested / merged / database ready / deployed / accepted.
Provide evidence for that state; a merged PR is not a completed hosted release.

## Linked issues

<!-- Use `Fixes #123` to auto-close, or `Refs #123` to reference. -->

## Checklist

- [ ] I have read [CONTRIBUTING.md](../CONTRIBUTING.md)
- [ ] My code follows the existing style (frontend: ESLint via `npm run lint`; backend: ruff via `uv run ruff format`)
- [ ] I have not committed any secrets, `.env` files, or credentials
- [ ] I have updated docs / comments where behaviour changed
- [ ] I have added or updated tests when adding logic that should be covered
