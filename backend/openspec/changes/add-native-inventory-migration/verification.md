# Local migration acceptance — 2026-10-07

Command: `backend/.venv/bin/python scripts/testing/run_native_inventory_migration.py`

- Exit 0, 16 passed, zero skipped/xfail. Other hosting suites were intentionally
  deselected; this is the dedicated whole-inventory migration profile.
- Actual owned Docker Supabase PostgreSQL and S3, source mounted read-only.
- Existing SQL contracts: 9 files / 329 assertions, all passed.
- Migration framework/policy tests: 39 passed. Runner/workflow checks: 91 passed.
- Ruff, immutable artifact policy, OpenSpec strict validation, git diff checks passed.
- Standard Git 2.50.1 cold mirror clone/fsck and first checked native write passed.
- Tested legacy loose/bundle/chunk placement, raw JSON trees/blobs, empty tree,
  branches/tags, retained Scope metadata, snapshot history/author/message,
  interruption/retry, missing/corrupt objects, foreign locations, late writes,
  organization-wide atomic usage, owner-only activation, source GC retention,
  and normal GC on an unrelated native repository.

The existing local Supabase CLI is 2.67.1; the CI workflow pins 2.107.0. The
workflow command was tested locally; GitHub Actions itself was not dispatched.
Hashes and actual run metadata are in `verification.json`; source was an
uncommitted working tree based on bb736235, not a deployed release SHA.

No hosted writes, deployment, push or full migration of the 148 cloud test
projects occurred. Fixtures reproduce observed storage formats, not actual
hosted object contents. Missing old roots and other preflight failures require
resolution before hosted execution. Consumer migration/native-only creation,
new Scope support and removal of old MUT schema aliases remain separate work.
