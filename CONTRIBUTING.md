# Contributing

Thanks for considering contributing to PuppyOne!

## Developer Setup

- Fork and clone the repo
- Copy `.env.example` to `.env` in `backend/` and `frontend/` and fill in your credentials
- See [Getting Started](docs/getting-started.md) for full setup instructions

## Code Style

- **Frontend**: TypeScript/React — follow existing formatting; run `npm run lint` in `frontend/`
- **Backend**: Python 3.12+, use `ruff` formatting (configured in `pyproject.toml`); run `uv run ruff format`

## Configuration Files

- **Single source of truth**: Use `pyproject.toml` for all Python tool configurations (pytest, ruff, mypy, etc.)
- **Do NOT create** `pytest.ini`, `setup.cfg`, or other tool-specific config files
- **Rationale**: Prevents configuration drift (PEP 518 standard)

## Branches and Environments

We use a three-branch promotion model:

| Branch   | Environment | Who pushes here          | How code arrives                         |
|----------|-------------|--------------------------|------------------------------------------|
| `main`   | production  | nobody directly          | merge from `qubits` or same-repo `hotfix/*` |
| `qubits` | staging     | nobody directly          | merge from `newmu` or external forks     |
| `newmu`  | dev         | core team (PRs welcome)  | direct push or PR from feature branch    |

`main` and `qubits` are protected: no direct pushes (not even by admins). Every
change reaches them through a Pull Request that satisfies branch protection
rules (PR + required reviews + required status checks).

## Branch Naming

- `feature/<short-slug>` or `feat/<short-slug>`
- `fix/<short-slug>`
- `perf/<short-slug>`
- `docs/<short-slug>`
- `chore/<short-slug>`
- `hotfix/<short-slug>` (urgent fix targeting production)
- `db/expand-<short-slug>`, `db/data-<short-slug>`,
  `db/cutover-<short-slug>`, or `db/contract-<short-slug>`
- `temp/<short-slug>` (may be auto-cleaned once merged and idle >14 days)
- `revert-<sha-or-slug>`

## Pull Request Flow

### External contributors (from a fork)

1. Fork the repo on GitHub.
2. Create a feature branch **from `qubits`** (not `main`):
   ```bash
   git remote add upstream https://github.com/puppyone-ai/puppyone-cloud.git
   git fetch upstream
   git checkout -b feat/my-change upstream/qubits
   ```
3. Commit, push to your fork, then open a Pull Request:
   - **Base repository**: `puppyone-ai/puppyone-cloud`
   - **Base branch**: `qubits` (not `main` — `main` is reserved for releases and hotfixes)
   - **Compare**: `your-fork:feat/my-change`
4. CI does not run automatically on first-time external PRs. A maintainer will
   click "Approve and run workflows" after a quick safety review.
5. After review and CI passes, a maintainer merges into `qubits`. The change
   reaches production when the next `qubits` → `main` release PR is merged.

### Internal contributors (core team)

1. **Default (features, refactors, non-urgent fixes)**: branch from `newmu`,
   open PR into `qubits`. After validation in staging, open the next release
   PR `qubits` → `main`.
2. **Release to production**: open PR `qubits` → `main`.
3. **Hotfix (production-only urgent fix)**: create a same-repo branch from
   `main` named `hotfix/<short-slug>`, then open PR `hotfix/<short-slug>` →
   `main`. After release, immediately back-merge `main` → `qubits` → `newmu`
   to keep all branches in sync.

`main` PRs are guarded by **Main Release Gate**:

- `qubits` → `main` is allowed as the normal release path.
- same-repo `hotfix/*` → `main` is allowed for urgent production fixes.
- all other sources targeting `main` are blocked.
- if the PR author is not `realGuantum`, `realGuantum` must approve the PR.

> Why back-merge after every hotfix: `main`, `qubits`, and `newmu` are
> long-lived branches. A production-only fix must be propagated back into the
> staging and dev branches immediately, otherwise the next regular
> `qubits` → `main` release may re-open the same file conflicts.

## CI Checks

| Check                | Trigger                                  | Release policy / role               |
|----------------------|------------------------------------------|-------------------------------------|
| **Frontend Build**   | All PRs; selected `main` pushes          | `main`, `qubits`                    |
| **Run Gitleaks**     | All PRs, push to `main`, weekly schedule | `main`, `qubits`                    |
| **Main Release Gate** | PRs targeting `main`                    | `main` (release/hotfix source + owner gate) |
| **Database validation result** | All PRs; heavy jobs only for database paths | `main`, `qubits` |
| **Deploy Database to Qubits** | Every push to `qubits` | exact-SHA prerequisite for application deployment |
| **E2E Visual Tests** | Manual (`workflow_dispatch`)            | (manual release/debug check)        |
| **Supabase Preview** | All PRs                                  | (advisory, not blocking)            |
| **Branch housekeeping** | Weekly schedule                       | n/a (cleanup job)                   |

The table states release policy, not proof of installed GitHub settings.
On 2026-10-08, Qubits required only Frontend Build and Gitleaks; the database
check existed but was not required by branch protection. See the
[enforcement snapshot](docs/architecture/13-database-release-governance.md#required-checks-and-actual-enforcement).
Reviewers must resolve applicable database/upgrade failures before merging;
a mergeable PR alone does not demonstrate that a release is ready.

Branch housekeeping may delete merged remote branches idle >14 days. Protected
branches (`main`, `qubits`, `newmu`) are never deleted.

## Testing

Test layers:

| Layer | Description |
|-------|-------------|
| `unit` | Pure functions/classes, no external deps |
| `integration` | In-process integration (Supabase/Redis) |
| `contract` | FastAPI route contract tests |
| `e2e` | Full stack with Docker Compose |

Local commands (from `backend/`):

```bash
uv run pytest -v -m "unit"
uv run pytest -v -m "integration"
uv run pytest -v -m "not e2e"
```

## Commit & PR Guidelines

- Use concise commit messages with conventional prefixes: `feat`, `fix`, `chore`, `perf`, `docs`
- **Default PR target is `qubits`** (staging). Only release PRs (`qubits` → `main`) and same-repo production hotfix PRs (`hotfix/*` → `main`) should target `main`.
- Include a clear description and test plan in PRs (the PR template will prompt you).
- Link related issues with `Fixes #123` or `Refs #123`.
- Do not include unrelated changes in the same PR — keep PRs focused so review and rollback stay easy.

### Database changes

Read [Database Release Governance](docs/architecture/13-database-release-governance.md)
before changing a shared database.

- `supabase/migrations` contains official Supabase schema history and bounded,
  transactional SQL only.
- Complex/long/Python/secret-dependent backfills use immutable
  `supabase/data_migrations/<id>` artifacts.
- A PR may contain a complete ordered Expand/Data/Contract release. Each phase
  has explicit prerequisites in the shared upgrade plan.
- Refresh the target before merge and review all inherited changes, not just
  the last feature commit. Fill in the PR template's history/dependency and
  populated-upgrade evidence; do not add unrelated historical data audits.
- Prefer new draft migrations after the current target history. Already-shared
  older migrations require an explicit catch-up plan and matching upgrade
  fixture; `--include-all` alone is not proof of safe ordering.
- Keep unready cleanup in `contract.pending.sql`. Before merge, prove the full
  populated upgrade in Docker. After merge, the coordinator establishes the
  real cutover prerequisites before Contract execution and app deployment;
  production does not need to have applied the candidate before merge.
- Never edit a shared/applied migration. Add a forward artifact.
- Never place “run this Python script next” instructions in schema SQL.
- Never write to a shared remote database from SQL Editor or a laptop except
  through the documented incident break-glass process.
- Report implemented/tested, merged, database-ready, deployed and accepted
  separately, with the environment and exact commit for each claim.

Database commit examples:

```text
feat(db-expand): add project membership facts
chore(data-migration): backfill project memberships
refactor(db-cutover): read canonical project memberships
refactor(db-contract): remove legacy permission table
fix(db): add forward repair for membership constraint
```

## Security

- Never commit real secrets or `.env` files
- If a secret leaks, rotate immediately and follow the cleanup steps in [SECURITY.md](SECURITY.md)

## License

Puppyone is licensed under the [Apache License 2.0](LICENSE).

By submitting a contribution (pull request, patch, or otherwise), you agree
that your contribution is licensed to the project and its users under the
Apache License 2.0, including the patent grant described in Section 3 of the
license. You also confirm that you have the right to make the contribution
under those terms (for example, that it is your own work, or that your
employer has authorized it).

No separate Contributor License Agreement (CLA) is required at this time —
Section 5 of the Apache 2.0 license governs all inbound contributions.
