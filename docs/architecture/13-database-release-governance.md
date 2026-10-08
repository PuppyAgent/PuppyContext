# Database Release Governance

PuppyOne uses Supabase's official schema history and a portable extension for
online production-data transformations. GitHub Actions is an adapter, not the
migration engine.

## Public core and private billing boundary

PuppyOne Cloud is independently installable open-source software. PuppyPay is
an optional, separately released private service. Each owns its tables and its
migrations, even when hosted installations share one Supabase database:

```text
Public Cloud repo                         Private PuppyPay repo
  Cloud SQL migrations                     Pay Alembic migrations
  Product tables / entitlement projection  Financial ledger / balances / prices
              |                                         |
              +----------- versioned HTTP API ----------+

Public CI: no Pay checkout, schema, service, or credentials
Private CI: independent Pay DB tests + pinned public-consumer contract check
```

Cloud schema/data migrations MUST NOT require a Pay table, foreign key, RPC,
HTTP call, migration revision, or completion receipt. Pay migrations MUST NOT
require Cloud tables. User/org IDs cross the API boundary as identifiers;
financial facts stay owned by Pay. Product-side entitlement projections remain
Cloud-owned tables installed by the public migration history. Disabling hosted
billing never means pretending that a missing required financial service worked.

The public database validation workflow checks a fresh installation without any
`puppypay` schema, then upgrades synthetic user/organization data from
`20260720000000` to the current migration head and checks preservation. Existing
older migration fixtures continue covering their specific historical cutovers.
Source-boundary checks reject private Python imports and direct private-schema
references; they supplement, rather than replace, executable database tests.

The official staging/production deployment workflows and owner-specific main
release policy run only in `puppyone-ai/puppyone-cloud`. Forks still run public
validation. A self-hosted operator supplies their own deployment adapter and
database credentials; they never need our protected environments or Pay repo.

PuppyPay's private CI tests fresh/previous-revision upgrades against PostgreSQL
without Cloud/Auth tables. It also checks the real public consumer selected by
an immutable commit in `contracts/puppyone-consumer.json`. The private check
round-trips entitlement publications/acknowledgements and billing facts, and
validates requests emitted by Cloud's actual managed-inference service against
Pay's request models. It uses synthetic data and an in-process HTTP transport;
it is not a live checkout/provider or whole-application E2E test.

A Cloud API change requires verifying its candidate SHA in the private workflow
before enabling the hosted feature and updating the reviewed consumer pin. This
private release check MUST NOT become a prerequisite of public schema upgrades
or fork CI. Pay's own deployment waits for its local tests, container check, and
consumer contract check. Compatible changes allow the two services to deploy
independently; breaking API removals need a versioned transition. Installing a
new schema and enabling a new hosted feature are separate release decisions.

The Docker self-host bootstrap uses the same active migrations through a
separate migration task. See [installation validation](14-self-hosted-installation.md)
for the empty-stack test and supported upgrade boundaries. No hosted environment
or GitHub branch-protection setting is changed by editing these workflow files.

## One rule, two lanes

### Active baseline and immutable archive

`supabase/migrations/20260926000000_baseline_b1.sql` is the sole executable B1.
Its 102 historical sources are preserved byte-for-byte in
`supabase/archive/before_b1/migrations/`. Later schema changes append timestamped SQL to
`migrations/`; the ordinary Supabase CLI never scans the archive. `baselines/b1`
holds the source inventory, hashes and verification evidence, not another copy
of the executable SQL. No independent current-schema snapshot is maintained.

All eight pre-B1 data artifact directories are also archived unchanged under
`supabase/archive/before_b1/data_migrations/`, including manifests, runners,
verification and fixtures. The shared catalog resolves current and archived
artifacts by immutable ID, pins their original checksums, and rejects duplicates.
Moving files never rewrites database receipts or implies work is complete.
Release selection and operator verification use this same catalog.

`scripts/database_baseline.py` compares archived replay with B1 installation in
an isolated Supabase PostgreSQL 17 stack, including ACL/RLS, ownership, functions,
Auth triggers, reference data and later migrations. It also verifies populated
upgrades and atomic migration-history adoption, with no hosted credentials.

`scripts/database_history.py check` verifies an existing database without
committing changes. `adopt` requires the complete archived history and reviewed
catalog fingerprint before atomically preserving all original history rows in
`migration_log` and replacing the covered tracking rows with B1. No customer
rows or data-job completion receipts are rewritten. Missing history or schema
/ permission drift stops before history writes. Surviving data jobs understand
B1 schema coverage; retired jobs do not run against removed tables.

The protected schema workflow performs adoption before native `db push`.
Both its dry-run plan and execution use `--include-all` so a reviewed migration
merged from a parallel branch is applied even if its timestamp precedes an
already-deployed version. This includes unapplied history only; it does not
replay applied migrations, alter receipts, or bypass SQL Contract/data guards.
The flag does **not** validate dependencies or prove that executing older SQL
after newer SQL produces the intended schema. The merge admission rules below
still apply; a successful dry run is only a plan, not an upgrade rehearsal.
Supabase's direct GitHub integration does not execute this custom admission:
an existing branch needs the protected transition before that integration can
resume. Fresh preview databases can apply B1 normally. This repository change
does not itself assert that any hosted database has adopted B1.

Older installations can materialize the full public archive with
`database_history.py stage --output <new-directory>` and use the existing
phased data/schema upgrade rules before adoption. No private Pay repository is
needed. Historical data transformations are not replaced by schema stamping.

See [`supabase/baselines/README.md`](../../supabase/baselines/README.md) for the
commands and rollout boundary. Compose now targets PG17; an existing PG15 volume
still requires a separate explicit major-version upgrade. Never automatically
swap the image on an existing data volume. Assess future compaction at stable
release milestones, not monthly.

```text
Schema lane
supabase/migrations -> supabase db push -> supabase_migrations.schema_migrations

Data lane
supabase/data_migrations + archive/before_b1/data_migrations
    -> shared ID catalog -> puppyone-db -> public.migration_log
```

Use `supabase/migrations` for DDL and small pure-SQL changes that are bounded,
transactional, and need no pause, application secret, runtime code, or external
service. Use `supabase/data_migrations` for bounded database transformations
that need batching, retry/resume, Python, an application HMAC secret, or a
release boundary between Expand and Contract.

GitHub-hosted CI is a **Supabase-only** control plane. It must never enumerate,
delete, copy, or otherwise mutate an external data plane such as S3. An
external inventory/legacy-cleanup release declares
`"execution_mode": "operator_local"` in its environment release pointer.
The versioned, reviewed operator script is run locally with that environment's
credentials and an explicit `--apply`; CI then executes only the immutable
artifact's `verify.sql` against Supabase to prove the recorded completion state.
This preserves a deployment gate without scheduling an S3 scan or deletion on
every push.

Public **validation** may provision disposable local Docker Supabase/S3 services
with synthetic fixtures and no hosted credentials. This is separate from the
hosted deployment control plane above. The native inventory migration uses
`scripts/testing/run_native_inventory_migration.py` for that local acceptance;
its operator artifact and rollout sequence are documented in
[`20261007_native_repository_inventory/README.md`](../../supabase/data_migrations/20261007_native_repository_inventory/README.md).

`supabase/seed.sql` is only bootstrap/demo/test data. It is not a production
upgrade mechanism.

## Release orchestration

### Migration merge admission

A migration timestamp orders files; it is not proof that a release is ready.
An older migration arriving from a parallel branch is not by itself a policy
violation. An unexplained history gap, missing dependency, or premature
Contract promotion MUST stop that release before a shared database write.

Before merging a database-affecting change into an automatically deployed
branch, the author and release owner MUST:

1. Refresh the target branch and review the **entire resulting diff**, including
   inherited migrations, release pointers and application/worker changes. A
   feature's small final commit does not make a large accumulated branch a
   small release. Separate independently releasable work; record unavoidable
   dependencies and their rollout order.
2. Record the candidate SHA, target/base SHA, target environment and its last
   successful application/schema/data evidence. Use the full applied-version
   set, not just the largest migration number. A partially failed release may
   have applied some schema files while leaving the old application running.
3. Classify each history difference using the table below. Obtain target
   metadata through the protected release connection; ordinary PR jobs retain
   synthetic fixtures and no shared credentials. Do not execute untrusted PR
   code with protected-environment credentials to obtain this evidence.
4. Verify the final schema and application contract through the appropriate
   isolated upgrade fixture before enabling the release. Dependency review
   includes replaced functions, grants/RLS, renames and data postconditions,
   not just whether individual SQL statements return success.

| Difference | Required treatment |
| --- | --- |
| New, unshared draft migration | Prefer a timestamp after the refreshed target history and all dependencies. Renumber only drafts not applied to a shared database, merged into a shared release branch, or pinned by an immutable artifact; update draft references and rebuild disposable tests together. |
| Older, unapplied migration already shared or pinned | Preserve its identity and bytes. List the exact missing versions, why newer versions ran first, dependencies and checksums; rehearse that same history gap in an isolated database. A reviewed catch-up may use `--include-all`. |
| Remote version missing from the checkout, duplicate version, or changed shared SQL | Reconcile the correct source/history before release. Never reset the shared database, delete its history, or stamp unapplied SQL as applied merely to make CI green. |
| SQL cannot safely run after the deployed newer versions | Prepare an explicit forward recovery/catch-up plan with an upgrade fixture. `--include-all`, renumbering shared history, and a clean-install test are not repairs. |

If another database change reaches the target while a PR is waiting, recheck
the dependency set and affected upgrade evidence. This means reviewing version
metadata and bounded fixtures, not auditing every historical project or scanning
object storage. The exact catch-up plan and its successful receipt should make
subsequent ordinary releases a verified no-op, not a recurring repair job.

### Release scope and admission evidence

An ordinary release is independently deployable with compatible schema changes.
A rename/removal, representation change or old-writer shutdown is a phased
cutover. The PR MUST identify which class it belongs to and which single phase
it activates. A merge into `qubits` starts deployment; it is not a place to park
an active migration whose prerequisites are expected to be completed later.

For a phased cutover, the release owner MUST establish these facts before
promoting the final Contract/application release:

- prerequisite Expand deployment and data artifact identity/checksum;
- required data verification receipts and actual preservation postconditions;
- the exact API, workers, schedulers and any local/shared writers involved,
  their stop/drain commands, relevant queues and the restart versions/config;
- the credential path and a recoverable backup/restore procedure available in
  that environment; prove they are usable before starting the maintenance window;
- the applicable write freeze and consumer-exit evidence, captured while the
  writers are stopped, with a current restore point and compatible recovery path.

Some artifacts require a freeze **before** data copying. Follow their explicit
ordering; the general state diagram does not waive that prerequisite. An empty
queue while producers still run, a freeze flag alone, or a failed SQL guard does
not prove consumer exit. Validation can happen earlier; records of actual
stopping, backup creation and copying can only be recorded after those actions.

PITR is one recovery mechanism, not a universal product requirement. A verified
consistent logical backup can also satisfy the recovery plan when it covers the
affected database state and has a usable restore procedure. Match the chosen
mechanism to the target environment. A stale scheduled backup, an unavailable
PITR window or an unverified archive must not be presented as current evidence.

If prerequisites are not ready, keep cleanup in `contract.pending.sql` and ship
the independently compatible phase. Do not merge the final-only application
alongside an inactive Contract that it already requires. Changes already shared
remain immutable; a release that has advanced partially needs an explicit
recovery plan, not removal of migration files or bypassed checks.

Ordinary application releases are not whole-database historical audits. Their
acceptance scope is schema compatibility, synthetic regression fixtures and a
bounded set of real project flows. Unrelated historical corruption belongs to
separate repair work and must not become an implicit CI/CD dependency.

The one-time Qubits entrypoint cutover can run its existing owner commands via
`Qubits Entrypoint Cutover`, using the protected staging connection. Its default
inspection is read-only and reports hashed identities and recovery metadata.
Explicit preparation consumes reviewed decision/process evidence from staging
secrets, validates an available PITR point after the recorded writer stop or a
protected operator attestation for an actually restored logical backup, and
calls the existing approval/freeze commands. It does not stop processes, invent
queue evidence, classify rows, execute data copying, or create receipts. The
ordinary deployment workflow resumes the portable data migration afterward.

For logical recovery, `ENTRYPOINT_CUTOVER_RESTORE_POINT` is
`operator-logical:<project-ref>:sha256:<archive-sha256>` and the protected
`ENTRYPOINT_CUTOVER_LOGICAL_BACKUP` JSON contains `format_version: 1`,
`project_ref`, `sha256`, positive `bytes`, `created_at`, `verified_at`,
`retained_at`, `restore_procedure`, `verification_record`, `verified_by` and
`restore_verified: true`. Timestamps must include timezones and order the real
writer stop, backup and successful restore verification. The operator keeps
the private archive and verification record; a checksum alone is insufficient.
This is an operator attestation, not an independent cloud-provider availability
check. The adapter neither uploads a backup nor relaxes database grants to
make a restore pass. Keep the artifact accessible for the recovery window.

Native adoption can use an explicit project selection with its own immutable
receipt. The [Qubits Agent rollout](../../supabase/data_migrations/20261008_qubits_agent_current_tree/README.md)
keeps selected full history and sibling data intact, while using the existing
current-tree reconciliation once for a correct organization usage baseline.
Global archival and destructive Contract retain their own full-inventory gates;
they are not part of this bounded rollout. Current-tree usage has independent
read/traversal budgets and retains object sizes, not every blob. It must not
inherit the selected history converter's aggregate 256 MiB limit: a large
unselected current tree still needs truthful usage accounting. Per-object
hash/type/size checks and the final snapshot fence remain mandatory. Run a
large one-time measurement close to object storage, with per-project progress;
normal deployment only verifies the selected completion state. An unselected project is not thereby
declared native-ready, and no legacy runtime fallback is introduced.

The entire staging/production release and manual data dispatch share an outer
`database-release-<environment>` concurrency group. Runs and pending releases
are not cancelled (`queue: max`); reusable steps retain their separate
`database-<environment>` lock. The outer lock prevents two releases from
interleaving their schema/data phases. A failed release must be investigated
before promoting another version; queue order is not a replacement for receipts.

Main Release Gate checks exact-head Qubits deployment evidence for schema,
data-only, release-pointer, archive, runner and database-workflow changes,
including renames out of those paths. Operator attestations use catalog checksum
validation and read-only verification with timeouts; failed checks emit no
success attestation and never manufacture runner receipts.

## Repository structure

```text
supabase/
├── migrations/                 # B1 and subsequent schema migrations
├── archive/before_b1/
│   ├── migrations/             # 102 immutable historical SQL files
│   └── data_migrations/        # all 8 immutable historical task directories
├── baselines/b1/               # hashes, coverage and verification evidence
├── data_migrations/            # new post-B1 data artifacts
│   ├── manifest.schema.json
│   ├── schema_history_baseline.json # immutable pre-governance hashes
│   └── <migration_id>/
│       ├── manifest.yml
│       ├── run.sql | run.py
│       ├── verify.sql
│       └── contract.pending.sql # optional reviewed future contract
├── tests/
└── seed.sql

backend/src/infra/data_migrations/  # portable CLI and runner
.github/workflows/                  # thin hosted adapters
```

The manifest chooses only `sql` or `python` and one file in its directory. It
does not contain an arbitrary shell command. Its checksum covers the normalized
manifest, entrypoint, and verification SQL.

SQL artifacts contain SQL only. The runner rejects transaction control, every
psql meta-command, and `COPY ... PROGRAM`; the runner owns the transaction,
connection, lock, verification, and receipt boundary.

Standalone `verify` operations run in a read-only PostgreSQL transaction with
both client and server timeouts. During an SQL data migration, entrypoint,
verification, and receipt remain one atomic transaction so a failed
postcondition rolls back the transformation.

Python child processes receive only the environment variables declared by the
manifest plus a small runtime/CA/proxy allowlist. They do not inherit the
database URL, GitHub token, or an ambient `PYTHONPATH`. They run in Python
isolated mode from the artifact directory. Python artifacts are single-file
jobs and cannot import PuppyOne's mutable `src` application package or sibling
helpers outside the checksum; third-party packages come from the repository's
locked runtime.

## Release state machine

```text
Expand -> Data -> Cutover -> Contract
```

1. Expand adds compatible schema. The running application can tolerate old and
   new rows.
2. Data copies/transforms old facts. Qubits runs first; Production runs only
   after Qubits verification.
3. Cutover makes the application read/write only the new fact and waits for old
   instances to drain.
4. Contract removes the old schema in a later PR. SQL fails closed unless the
   receipt checksum and actual row-level postcondition both pass.

Do not put Expand and Contract in the same release. A fresh install with zero
legacy rows may apply the final Contract without running irrelevant historical
data jobs.

Passing the fresh-install exception does not authorize that Contract against a
populated Qubits or Production database. Contract SQL may be authored and tested
in its artifact directory early, but promoting it into `supabase/migrations`
requires the target environment's completed prerequisite evidence.

An identity-representation cutover may intentionally use one atomic Contract
without a dual-write phase only when both representations cannot safely coexist
and all of the following stricter gates hold: a read-only immutable preflight,
exact checksum receipt, mutation freeze, database restore point, one-transaction
mapping, previous-schema upgrade fixture, dirty-data rejection proving no
receipt/no mutation, credential-ID/hash continuity, and same-SHA application
deployment. ISSUE-039 is this narrow case: keeping both a synthetic root Scope
and Project-root identity would preserve the ambiguity the change removes. This
exception does not permit ordinary feature Expand and Contract work to be
collapsed.

## Operator commands

From `backend/`:

```bash
uv run puppyone-db lint
uv run puppyone-db list
DATA_MIGRATION_DATABASE_URL='postgresql://...' \
  uv run puppyone-db plan <migration_id>
DATA_MIGRATION_DATABASE_URL='postgresql://...' \
  uv run puppyone-db run <migration_id>
DATA_MIGRATION_DATABASE_URL='postgresql://...' \
  uv run puppyone-db verify <migration_id>
```

Protected `qubits` and `main` pushes use the environment release orchestrators
to run schema deployment, CI-mode data plan/run/verify, and a final schema
check as one serialized release. `operator_local` releases instead run their
external-data script outside GitHub Actions, then the orchestrator verifies the
Supabase completion SQL and performs the same final schema check. The standalone
`Data Migration` GitHub workflow remains for protected-branch diagnosis and
idempotent recovery of CI-mode artifacts; `plan` and `verify` are read-only,
while `run` mutates database data and writes a receipt after verification.

The final drift check is explicitly scoped to PuppyOne's `public` schema.
Protected databases may colocate schemas owned by another service, such as
PuppyPay's `puppypay` schema; those schemas are neither PuppyOne migration input
nor drift. Cross-schema platform invariants that PuppyOne depends on, including
its trigger on `auth.users`, remain covered by the preceding smoke contracts.

For the project-storage inventory, run the explicit operation from `backend/`
against the intended environment only after a dry run:

```bash
uv run python ../scripts/backfill_project_storage_principals.py
uv run python ../scripts/backfill_project_storage_principals.py --apply
```

The script owns any S3 listing and any approved cleanup. It is not invoked by a
deployment workflow. Its database postcondition is the checked-in
`20260720_project_storage_inventory/verify.sql`; a failed CI verification means
the operator operation has not completed and blocks the release.

The bundled GitHub adapter targets Supabase Cloud and therefore validates a
project ref against canonical direct/session-pooler hosts. Self-hosted PuppyOne
deployments use the same CLI/manifest contract from their own CI adapter and
may omit `SUPABASE_PROJECT_ID` when those canonical hosts do not exist.

The database URL MUST be a direct or session-pooler PostgreSQL URI that supports
session advisory locks. It is stored as an environment secret and passed to
libpq through `PGDATABASE`, never rendered in command arguments.

For hosted Supabase runs, the runner also proves that `SUPABASE_URL` and the
direct/session-pooler database URI belong to `SUPABASE_PROJECT_ID`. It refuses
an unprovable pairing so a legacy API backfill cannot mutate one project and
write its receipt to another.

## Receipt semantics

PuppyOne reuses `public.migration_log`:

- no row: incomplete or failed;
- row with matching artifact checksum: completed;
- row with a different checksum: immutable-history violation.

The JSON summary contains only checksum, source SHA, runner version, legacy
flag, and verification state. Never write credentials or user data into it.
GitHub/GitLab/Jenkins keeps detailed execution logs.

## CI/CD gates

Pull requests must pass:

- manifest/schema validation;
- immutable migration-file policy;
- unique 14-digit, timestamped snake_case schema filenames;
- no hidden Python/script instruction in new schema SQL;
- marked data dependency for destructive Contract SQL;
- an artifact checksum pinned in every destructive Contract;
- fresh database rebuild and pgTAP;
- legacy fixture -> data runner -> idempotent rerun -> Contract;
- corruption fixture -> failed preflight -> no receipt and no schema mutation;
- concurrent idempotent target enable and existing-credential continuity;
- targeted backend runner and authorization-boundary tests;
- no shared-database credentials in pull-request jobs.

The pull-request workflow always publishes one stable `Database validation
result` check. Non-database PRs finish after a read-only path check; database
PRs cannot publish success until every policy, rebuild, upgrade, and lint job
succeeds. This avoids the required-check deadlock caused by workflow-level
path filters.

### Required checks and actual enforcement

Release policy requires `Database validation result` to be a required merge
check on both `qubits` and `main`, alongside their other required checks. The
GitHub branch rule must be configured separately: a workflow or this document
does not install that protection. A migration-relevant failed upgrade or runtime
check must be resolved even if GitHub labels it advisory; a known unrelated
failure needs a scoped, documented disposition, not a claim that all checks pass.

The validation matrix MUST distinguish:

- **Clean install:** proves that the full history builds a new database.
- **Supported populated upgrade:** starts from the previously deployed schema
  with synthetic representative rows and verifies preservation and new behavior.
- **Out-of-order catch-up:** when applicable, reproduces the exact missing
  versions and newer applied versions, including dependency/final-state checks.
- **Refusal and retry:** missing receipts, live writers, stale fingerprints or
  failed verification reject unsafe work without false completion receipts;
  retry after the real prerequisite is satisfied succeeds.
- **Hosted acceptance:** checks the declared project flows and telemetry against
  the deployed candidate. This is separate from every fixture above.

Per-PR validation MUST NOT require a full copy of hosted data or a whole-project
historical audit. Protected deployment inspects migration metadata and bounded
postconditions. A legitimate one-time conversion or accounting reconciliation
has an explicit scope and receipt; it is not silently rerun on every push.

**Enforcement snapshot, 2026-10-08 (`a937c363`):** Qubits branch protection was
read directly and required only `Frontend Build` and `Run Gitleaks`; `strict`
was false. `Database validation result` exists but was not a required Qubits
check. The generic target-history/dependency admission described above is not
yet an automated pre-merge check. Current SQL guards and the protected release
workflow reject missing prerequisites at deployment time, which is later than
the required release preparation. The new cutover CI adapter verifies PITR
points only; logical-backup support there is also an implementation gap. These
are open enforcement gaps, not completed work. Until corrected, release owners
must apply the documented admission rules and must not treat the narrower
GitHub merge button as release approval.

**Follow-up, 2026-10-08:** Qubits protection now requires the existing
`Database validation result` in addition to Frontend Build and Gitleaks; their
GitHub App bindings and other settings were preserved. The cutover adapter now
also validates the protected logical-backup attestation described above.
Generic automated target-history/dependency admission remains an open gap;
these changes do not themselves establish a successful hosted migration.

Schema and data jobs for an environment share the same concurrency group and
cannot cancel a running database operation. Production uses a protected GitHub
Environment. A Production data `run` first verifies the same artifact checksum
and row-level postcondition against the Staging environment; failure prevents
Production execution.

For a normal `qubits -> main` release that changes schema, Main Release Gate
requires a successful Qubits schema deployment for the exact source SHA. A new
Contract additionally requires:

- successful staging `verify <migration_id>` on the Qubits head SHA;
- successful production `verify <migration_id>` on the current main/base SHA.

For non-owner releases, the owner's approval must also target the exact current
head SHA; a commit pushed after review invalidates the gate. Database hotfix
label changes re-run the same trusted metadata-only gate.

The Qubits release orchestrator runs on every `qubits` push, including code-only
commits. It resolves the staged release pointer and automatically executes the
schema and data lanes for that exact SHA; a receipt makes already-completed
artifacts a verified no-op. The Production orchestrator mirrors this sequence
on `main`, but performs a read-only Qubits verification before any Production
data write.

### Application deployment ordering

Schema deployment is a prerequisite of application deployment for the same
source SHA. Every Railway service sourced from `qubits` (API and all workers)
must enable GitHub Autodeploy **Wait for CI**. Railway then keeps the candidate
deployment waiting until the push workflows, including `migrate-staging.yml`,
finish successfully and skips the candidate if any workflow fails. A service
must never be configured to become live from a `qubits` push while the database
workflow for that SHA is still running. See Railway's
[GitHub Autodeploy controls](https://docs.railway.com/deployments/github-autodeploys).

This ordering is an environment setting as well as a repository contract. The
release operator verifies **Wait for CI** for every service whenever a service
is created, reconnected to GitHub, or changes its source branch. The schema
attestation remains the durable evidence; a green application build alone is
not evidence that its database contract exists.

Runtime handling is defense in depth, not a substitute for ordering. A missing
required database function or schema capability returns a sanitized,
retryable `503 database_schema_outdated`; clients keep durable operation state
and retry. Raw PostgREST/SQL errors and function signatures never cross the API
boundary. Local code ahead of remote `qubits` uses a local database or accepts
this fail-closed response; developers do not push migrations from laptops to a
shared environment.

When a data artifact contains `contract.pending.sql`, repository policy
requires the promoted Contract migration to be a byte-for-byte copy. Its
receipt checksum proves which data transformation ran; exact-copy enforcement
proves that reviewers approved the destructive SQL that is being promoted.

### Release completion and failure reporting

Record these states separately, naming the environment and immutable SHA:

| State | Evidence |
| --- | --- |
| Implemented / tested | Source plus named checks, their scope and failures/skips |
| Merged | PR and actual target-branch commit |
| Database ready | Successful schema/data verification for that release and environment |
| Deployed | Required API/worker/client versions and runtime configuration actually live |
| Accepted | The bounded user flow succeeds on those versions, with required performance measurements |

Only the final applicable state supports saying the requested release is
complete. A mock model, local E2B smoke, healthy old deployment, merged PR, or
passing build cannot substitute for real hosted acceptance.

On failure, report the actual workflow/step/error, any already-applied phase,
the version still serving users, and the next concrete recovery action. Fix that
failure within the declared scope. Do not expand an ordinary release into an
unrelated historical cleanup, repeatedly rerun a deterministic prerequisite
failure, fabricate evidence, or require a fresh user approval for an action
already authorized in the session. A real credential/permission restriction
must be identified separately from the technical release defect.

## Environment secrets

Create protected `staging` and `production` GitHub Environments. Store the same
generic names in each environment, with environment-specific values:

```text
SUPABASE_ACCESS_TOKEN
SUPABASE_PROJECT_ID
SUPABASE_DB_PASSWORD
DATABASE_URL
SUPABASE_SERVICE_ROLE_KEY
ACCESS_CREDENTIAL_HASH_SECRET
```

The reusable workflows read only these Supabase/database secrets; callers do
not inherit the repository's other secrets. S3 credentials belong only in the
operator's local target-environment `.env` (or equivalent local secret store),
never in a GitHub Environment. Allow `qubits` and `main` to reference `staging`
(main performs the Production promotion check); allow only `main` to reference
`production`. Require reviewers for Production writes.

`DATABASE_URL` should prefer the Supabase session pooler on port `5432` when
direct IPv4 DNS is unavailable. Do not use transaction-pooler port `6543` for
jobs that hold session advisory locks.

## Upstream alignment

This extension preserves Supabase's documented contract: schema SQL stays in
[`supabase/migrations`](https://supabase.com/docs/guides/deployment/database-migrations),
`db reset` rebuilds from those files, and `db push` deploys unapplied versions.
Supabase also recommends CI/CD or Branching for staged deployment and warns
teams not to change remote schemas through the Dashboard once migration history
is in use. Seed files remain insertion-only bootstrap data, consistent with the
[Supabase seeding guide](https://supabase.com/docs/guides/local-development/seeding-your-database).

GitHub enforces merge checks through separately configured
[protected branch rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
Adding a workflow without requiring its result leaves a merge-admission gap.

## Commit and PR convention

Branches:

```text
db/expand-<topic>
db/data-<topic>
db/cutover-<topic>
db/contract-<topic>
hotfix/db-<topic>
```

Commits:

```text
feat(db-expand): add project membership facts
chore(data-migration): backfill project memberships
refactor(db-cutover): read canonical project memberships
refactor(db-contract): remove legacy permission table
fix(db): add forward repair for membership constraint
```

One PR owns one phase. Applied/shared schema migrations and released data
artifacts are immutable. Fix mistakes with a new forward artifact.

At governance adoption, migrations already shared through Qubits are pinned in
`supabase/data_migrations/schema_history_baseline.json`. They may be promoted
unchanged to an older Production history even when they contain pre-governance
patterns; changing their bytes or the baseline fails policy. Files outside that
baseline follow all current rules immediately.

Every database PR states its phase, affected tables, compatibility window,
estimated rows/runtime/locks, migration ID, verification, retry behavior,
forward-fix plan, destructive operations, and Qubits evidence.
It also supplies the refreshed base/target history, any exact missing-version
catch-up plan, upgrade fixture and the cutover prerequisites that make this
phase eligible to merge. Use the database section of the PR template; mark
non-applicable items explicitly instead of implying they have been verified.

## Break glass

Remote SQL Editor writes and laptop-to-shared-database pushes are forbidden in
normal operation. An incident exception requires an incident record, reviewed
SQL in Git, recoverable backup/PITR, bounded transaction/timeouts, captured
output, and a follow-up forward migration. A database hotfix PR to `main`
requires the `database-break-glass` label.

## Historical production catch-up (2026-09-23)

The deployed June production history can catch up to the already deployed July
Qubits history through `supabase/releases/20260923_production_catchup.json`.
This immutable plan pins every schema byte and data-artifact checksum and lists
all intermediate boundaries. The single July 16 legacy filename receives an
exact-checksum policy compatibility entry; new destructive SQL still requires
the normal Contract metadata. The original baseline and SQL files stay unchanged.

`historical_release.py` runs before ordinary production schema admission. It is
a no-op after the July terminal version. Earlier databases require unexpired,
project-bound `LEGACY_UPGRADE_AUTHORIZATION` containing the plan ID, a verified
restore point, and verified write-freeze evidence. Official `supabase db push`
applies unchanged prefixes; the portable runner executes each required data job.
Final continuity checks preserve users, profiles, projects, commits, existing
organization/project memberships, original credential hashes, and derived legacy
credential hashes. No external storage operation runs in this job. The existing
operator-local storage inventory and final public-schema diff still gate release.

The connection adapter validates a protected project's direct/session-pooler URI.
When `DATABASE_URL` is absent, it obtains short-lived official Supabase CLI login
credentials with `SUPABASE_ACCESS_TOKEN`; it does not reset the database password.
Connections are refreshed between phases and before the final drift check. The
password travels through libpq environment variables, not process arguments.
`SUPABASE_DB_PASSWORD` is no longer required by the hosted schema adapter. API
backfills still require the matching service-role key and the original effective
application HMAC key. These keys never appear in release evidence.
