# Reproducible installation and upgrade validation

## One source of truth

The checked-out release owns its database definition:

```text
supabase/migrations/B1 + subsequent migrations
              + required initial data in those migrations
              + explicit data artifacts for applicable upgrades
```

There is no separately maintained Docker product schema, current-schema dump,
or automatically regenerated baseline. B1 already includes its reviewed initial
reference rows. `seed.sql` is optional demo/test data, not an installation or
production upgrade prerequisite. Supabase platform roles/Auth are prerequisites
provided by the Supabase PostgreSQL image and Auth service, not PuppyOne's B1.

Every main push rebuilds an isolated database from the committed B1 and later
migrations. It also checks archived-history equivalence, populated upgrades and
the actual Docker installation. This means **rebuild a database from B1**, not
**rewrite B1**. A future B2 is an independently reviewed compaction with history
admission and archive verification; merge events never modify migration history.

## Startup ownership

```text
Supabase PostgreSQL / Auth / REST / gateway + Redis + MinIO
                         |
                 native schema migration task
                         |
                explicit data run + verify
                         |
                  storage initialization
                         |
                 API dependency readiness
                         |
                       frontend
```

`scripts/self_hosted_migrate.py` holds a session advisory lock, performs the
same B1 admission as hosted schema deployment, and invokes pinned Supabase CLI
`db push`. It does not implement a second SQL runner or populate migration
history itself. Missing/incomplete pre-B1 history and drift fail closed. A
failed migration prevents new application containers from starting.

The storage initializer can admit an empty database and empty bucket only,
after two real inventory observations. It never deletes objects, overwrites
existing completion state, or pretends an old data task succeeded. Non-empty
legacy storage needs the explicit inventory operator described in
[database release governance](13-database-release-governance.md).

The explicit `supabase/releases/standalone-data-migration.json` selects the
public data task for this release. After schema admission, the migration task
invokes the same portable runner as hosted CI, then verifies current rows even
when a completion receipt already exists. Failure prevents application startup.
The current task backfills entrypoint identifiers and search tasks; it requires
no hosted credentials or private PuppyPay dependency. The installer image takes
its minimal runner dependencies and distribution hashes from `backend/uv.lock`.
An operator-only release is rejected rather than silently skipped. Historical
data artifacts are not replayed unconditionally, and final Contract cleanup is
still a separate release after old processes and queued jobs have drained.

## Supported paths and boundaries

Web Auth uses the public Supabase URL for cookie and PKCE identity on both the
browser and server. `features/auth/supabase/server-client.ts` routes server I/O
through `SUPABASE_INTERNAL_URL` without changing that identity; middleware and
both callback handlers share this adapter. Compose also sets the explicit public
frontend origin and Auth issuer/audience, matching backend token verification.

User initialization and Profile assemble Project services through
`build_project_service()`. HTTP routes use request-scoped FastAPI dependencies.
Imperative callers must not invoke a dependency factory with unresolved
`Depends` defaults or cache those placeholders in a shared service.

- Fresh local install: from `docker/`, copy `.env.example` to `.env`, then run
  `docker compose up --build -d --wait`.
- Existing native-format B1 installation: back up database and object storage; stop application
  writers, rebuild at the new release and run the same installer. SQL history
  prevents already-applied changes from being rerun. Never use `down -v` here.
- Legacy entrypoint or repository storage: complete the explicit phased data
  cutover before starting the final application. B1 history alone does not prove
  that application data has the current representation. Missing freeze or data
  receipts must stop startup while preserving the original data.
- Pre-B1: follow the phased public archive upgrade before B1 admission. The
  installer deliberately does not guess missing historical data transformations.
- PostgreSQL 15: use a reviewed dump/restore or supported major-upgrade process
  into a separate PostgreSQL 17 installation. Preserve the original database,
  authentication configuration and object storage. A changed image tag is not
  a major-version upgrade. The tests below do not claim a PG15 physical-volume
  upgrade is automatic.

The default Compose environment is local development with authentication enabled;
it is not a ready-made Internet-facing production configuration. Hosted release
workflows and credentials are separate. Optional MCP/AI/provider features do
not gate core readiness when unconfigured. `/live` means the process lives;
`/ready` checks authenticated database access, bucket access and configured
Redis, plus MCP when configured. No health check performs DDL.

## Three executable checks

| Check | Entry point | Assertions |
| --- | --- | --- |
| Baseline rebuild | `scripts/database_baseline.py verify --candidate supabase/baselines/b1` | Fresh versus archived schema, Auth triggers, ACL/RLS, reference data, history adoption, drift rejection, future increment |
| Populated database upgrade | `Validate Database Changes` | Synthetic old users/orgs/projects survive; real data runners, receipt continuity, retry, transactional failure and concurrency |
| Entire installation | `scripts/test_self_hosted_install.py` | Real image builds; confirmation email through local SMTP; fresh login in an empty browser; tenant isolation; file read/write/raw bytes/delete; refresh/logout; service recreation with preserved volumes; broken migration blocks startup and leaves no receipt/DDL |

Run installation validation from the repo root after installing `e2e/`'s locked
dependencies and Chromium:

```bash
npm ci --prefix e2e
cd e2e
npx playwright install chromium
cd ..
python3 scripts/test_self_hosted_install.py --variant default --artifacts /tmp/puppyone-install-default
python3 scripts/test_self_hosted_install.py --variant custom --artifacts /tmp/puppyone-install-custom
python3 scripts/test_self_hosted_install.py --variant upgrade --artifacts /tmp/puppyone-install-upgrade
python3 scripts/test_self_hosted_install.py --variant legacy --artifacts /tmp/puppyone-install-legacy
```

Run variants sequentially on one host (they use the same isolated ports). Each
run creates a random Compose project and deletes only that project's test
volumes. Never point the test at an existing installation. The custom variant
rotates JWT/API/DB/storage credentials and changes the bucket. The tests use no
production credentials, Pay checkout, existing login session or LLM API.
The upgrade variant first builds the pinned native-format Qubits revision
`a5dc0ea5`, creates real accounts and files, then upgrades the same volumes to
the candidate release. It verifies session cookies, refresh tokens, file bytes,
permissions and confinement of internal tables. The legacy variant starts from
`c28e38a3` with synthetic old-format data and verifies that missing explicit
cutover prerequisites block the new API and frontend without deleting data or
recording the failed migration as applied. Tests never manufacture hosted
cutover evidence or restore retired storage interfaces.

PRs publish `Installation validation result`; all main pushes and version tags
run the installation matrix again. Main also runs database rebuild/upgrade and
B1 equivalence. Failure logs/results are retained without environment/session
files. Required branch checks and release automation must require successful
database and installation checks for the exact candidate revision; editing a
workflow alone does not change GitHub branch protection or deploy production.
