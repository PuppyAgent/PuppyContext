# Database Release Governance

Puppyone releases an environment by merging one tested source revision to its
protected branch. `qubits` selects staging; `main` selects production. The
release coordinator owns database upgrade, application deployment and
acceptance as one serialized operation.

## Migration merge admission

Before merge, CI starts the last accepted target version in disposable Docker,
creates representative users and repository data, and runs the candidate
upgrade program against the same volumes. The candidate must preserve login,
file contents and permissions, then pass authenticated reads and writes.
Empty-database installation remains a separate required test.

Production does not need candidate schema or data receipts before merge.
Those receipts are outputs of the release after merge. A normal promotion
from Qubits still requires a successful staging release for the exact promoted
head. Branch protection requires `Release upgrade rehearsal`, database
validation, backend/frontend checks and security checks. Production promotion
also requires `Main Release Gate`. Hosted configuration is verified separately
from repository YAML; editing a workflow does not enable branch protection.

The rehearsal resolves the last accepted `puppyone-release` deployment for the
target environment. A reviewed bootstrap SHA is used only while adopting an
environment with no coordinator receipt. Older accepted deployment statuses
remain valid baseline evidence after GitHub marks them inactive. A failed
deployment is never a successful upgrade baseline.

## Release execution

```text
merge to qubits / main
  -> candidate CI, including populated Docker rehearsal
  -> protected environment + database-wide release lock
  -> source, target and deployment-owner verification; candidate build
  -> determine pending schema/data phases from real database receipts
  -> when a data/Contract cutover is pending:
       stop ingress and periodic producers
       drain delayed, retry and in-flight work while consumers remain alive
       stop consumers; verify process exit and write-lease expiry
       create, restore-test and retain a private database backup
  -> compatible schema -> data run + verify -> dependent Contract
  -> final schema history, product contracts, Data API containment, public drift
  -> deploy the exact source SHA to all declared application services
  -> authenticated file write/read + real Agent reply on an owned test project
  -> durable accepted receipt and GitHub deployment success
```

Ordinary additive releases keep the application online. Data migrations and
Contract SQL use a maintenance window. Consumers must keep running until their
queues drain; the Synchronize worker is a consumer, while its periodic
producer belongs to the API. A task waiting for user approval is not silently
approved or discarded by release automation. Drain has a bounded timeout and
fails without applying the schema if work cannot finish.

GitHub concurrency queues releases separately for each environment. A
PostgreSQL session advisory lock also serializes the physical database, covering
build, migration, deployment and acceptance. The executor checks the owning
connection between operations. Immutable data artifacts retain their own
transaction or advisory locks and restart-safe checkpoints.

Application build/start never performs schema mutation. Railway's Automatic
Deployments setting is disabled for coordinator-owned services; their GitHub
repository connection stays attached. The coordinator checks the provider's
actual autodeploy status and repository source before migration. The
coordinator explicitly deploys its immutable commit, checks every service's
reported commit and health, and never substitutes the latest branch head.
`serviceInstanceDeployV2` returns the deployment ID that subsequent polling verifies.
`Wait for CI` alone cannot own a drain/data/Contract sequence.

## Module and artifact ownership

```text
supabase/
  migrations/                 official Supabase schema history
  data_migrations/<id>/       immutable manifest, execution, verifier, fixtures
  archive/before_b1/          immutable historical migration sources
  baselines/b1/               baseline source inventory and checksums
  releases/
    upgrade-plan.json        ordered schema boundaries and data artifact IDs
    control.sql              private coordinator journal bootstrap
    deployed-bases.json      initial accepted-version adoption pins
backend/src/infra/data_migrations/
  catalog.py, runner.py       existing artifact validation/execution/receipts
  release/
    plan.py                  dependency, retirement and checksum validation
    engine.py                environment release state machine
    target.py, schema.py      shared database operations and Supabase CLI
    journal.py               private release progress and source ordering
    docker.py, hosted.py      actual environment adapters
    railway.py               provider deployment and process-exit boundary
    backup.py, acceptance.py  restore proof and bounded application acceptance
```

Schema SQL uses the official `supabase_migrations.schema_migrations` history.
Data transformations use `public.migration_log` receipts bound to immutable
artifact checksums. `puppyone_release.runs` records source SHA, plan checksum,
phase and private evidence; it is not exposed by PostgREST or granted to
application roles. It is not a replacement schema-history table.

The B1 baseline and pre-B1 archive remain unchanged. Baseline adoption checks
the covered history and public schema fingerprint before replacing tracking
rows. Unknown or incomplete historical histories are rejected. `--include-all`
only selects files; it does not establish data prerequisites.

Each schema phase supplies the official CLI with the already-applied files and
only that phase's pending files. Later pending Contract files are withheld
until their data dependencies verify. SQL `requires-data-migration` markers
must resolve to preceding artifacts with the declared checksum. Retired
verifiers are skipped only after their declared Contract boundary is applied;
removed historical tables must not be recreated to run an obsolete verifier.

New schema-only changes append a migration. New application/S3 data work adds
one immutable data artifact with its own verifier and representative fixtures,
then places its ID between the appropriate schema boundaries in the plan.
Changing an executed artifact's bytes or fabricating completion receipts is
forbidden. `contract.pending.sql` stays outside active schema history until a
complete ordered upgrade passes rehearsal; the live database need not already
have run it before the source can merge.

## Data scope and recovery

A completed data receipt eliminates execution of that transformation on
subsequent ordinary releases. Release acceptance uses one owned test project;
it does not audit all customer repositories or scan the bucket. A one-time
legacy conversion may legitimately visit every row/object within that
migration's declared scope. That cost belongs to the immutable migration,
never to routine deployment or application startup.

Historical business classifications are explicit reviewed inputs with source
fingerprints. The executor checks them and applies the existing artifact;
it never guesses whether a legacy source is an Import, Synchronize binding or
both. Such input must be prepared for the historical cutover before release.
New releases after that Contract do not require those classifications again.

Breaking upgrades stop writers before taking their recovery point. The backup
includes Cloud public data, Auth identities and migration/release histories;
it is restore-tested in a disposable PostgreSQL container before mutation.
Hosted backups use a separate private, versioned object bucket and are read
back to verify their digest. The application object bucket retains object
versions, including versions deleted by a data transformation. A logical
database restore alone is not a complete object-store rollback.

The Cloud release owns `public`, its migration history and its private release
journal. PuppyPay owns its private financial schema and release history.
Neither Cloud installation nor public CI needs PuppyPay tables or credentials.
Public-schema drift checks must not adopt or delete another service's schema.

## Failure, retry and evidence

Every phase completes before the next begins. Failed data verification blocks
the dependent Contract. Failed schema verification blocks deployment. Failed
application acceptance leaves the release unaccepted. Migration receipts and
release checkpoints survive runner exit, so retry checks actual state and
resumes pending work; it does not rerun a completed data copy.

An accepted source is an idempotent no-op. A source older than the environment's
recorded release, or a divergent sibling, is rejected. A descendant may repair
a failed release, preserving the failed attempt's evidence. Changing the plan
for an existing source SHA is rejected.

Automation never restarts an old binary against a partially upgraded schema.
A breaking release may therefore remain in maintenance after failure. Retry
the same revision or ship a forward fix through the same coordinator. A data
restore is an explicit incident operation using the verified database backup
and corresponding object versions. Read-only diagnosis never writes a success
receipt or bypasses the environment lock.

The accepted receipt means database verification, exact source deployment and
application checks passed. A green build, a migration receipt or a healthy
container alone is insufficient. Logs and backup contents stay private;
public workflow output contains source, phase outcome and aggregate test
results, without customer IDs, object paths, SQL errors or credentials.

## Hosted activation contract

Both environments need the same infrastructure contract before their first
coordinated release:

- A dedicated ephemeral release runner group, limited to the protected
  reusable release workflow on `qubits` and `main`. Public/fork PR jobs cannot
  select the group. Its name is `PUPPYONE_RELEASE_RUNNER_GROUP` in repository
  variables; environment-specific labels select staging or production.
- GitHub environment branch policies and required checks that enforce the
  source boundary without a routine manual approval between release phases.
- A private configuration file at `/etc/puppyone/releases/<environment>.json`,
  mode 0600, with target-bound database access, Railway project token and full
  API/frontend/Agent/Upload/Import/Synchronize/MCP service inventory, Redis
  queue names, versioned backup storage and a dedicated acceptance account.
- Python 3.12, locked backend dependencies, Node/Docker build support,
  PostgreSQL 17 tools and pinned Supabase CLI on the runner. Backup and raw
  diagnostics reside in a private retained location; ephemeral-runner teardown
  does not delete the remote recovery point.
- Disabled independent Railway and Supabase schema deployment bindings. The
  coordinator is the only normal release writer and application deployer.

Missing activation configuration must fail explicitly; it must not silently
fall back to independent application deployment. Repository implementation and
actual environment activation are separately verified deliverables.

`pull_request_target` admission uses the target branch's trusted workflow, not
the proposed replacement. A gate change must first reach that protected target
through its existing owner-controlled admission procedure before it can govern
subsequent promotions. This one-time gate handover never requires applying the
candidate database migrations before merge.

## Verification contract

Tests cover an empty installation; the deployed old version with real fixture
data; repeated execution; interruption after a committed data phase; Contract
and acceptance failure; stale-source rejection; database-level concurrent
release exclusion; connection loss; private journal permissions; exact
deployment SHA; and consumer drain ordering. Integration fixtures own their
database and object volumes and never accept production credentials.

Docker acceptance checks old file contents and sessions after upgrade, then
performs new authenticated writes and checks Data API containment. The hosted
bounded check also verifies a real Agent reads the stored file and replies.
Runtime suites separately cover sandbox reads/writes, publication, concurrency
and database-request budgets. Those performance contracts remain part of the
required Agent CI, not a repeated whole-customer-data release audit.

## Upstream alignment

Schema history follows the [Supabase CLI](https://supabase.com/docs/reference/cli/supabase-db-push).
Release queuing follows [GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
Private runners follow [GitHub runner security](https://docs.github.com/en/actions/reference/security/secure-use).
Provider deployment uses the [Railway service API](https://docs.railway.com/integrations/api/manage-services).
