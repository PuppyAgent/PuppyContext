# Native engine retirement acceptance — 2026-10-07

The initial local acceptance used the `qubits` worktree based on
`3378ef15bfa219a8c100f3403b2d40dbbb8af34a`. It was committed as `aeef0d2d` and
pushed to the draft [Qubits release PR #1382](https://github.com/puppyone-ai/puppyone-cloud/pull/1382)
after authorization to deploy and migrate the staging environment. Hosted
read-only preflight and PR CI have started. No Qubits DB/S3 mutation, service
stop, release-pointer change, PR merge or application deployment has occurred.
Production has not been targeted. Local acceptance does not establish hosted
release readiness; the blockers below must be resolved before cutover.

## Qubits release attempt: not ready to deploy

The deployed Qubits backend is still at `9cea98f2`; its database is the test
project `qextonmjqbhxgokmjbio` and its S3 bucket is `contextbase` in the same
project. All 148 projects are still using the old root authority. The new
repository and migration inventory tables are absent. The local Supabase CLI
identity does not list this project; protected CI owns the database connection.

Read-only conversion preflight uses the artifact's converter over GET-only
database metadata/S3 adapters, with no S3 put/delete or DB execute capability.
It found a real provider difference: hosted Supabase returns HTTP 404 with an
empty S3 error code for missing keys. The release candidate now treats only
that empty-code 404 as missing, preserving namespace fallback; explicit
AccessDenied, NoSuchBucket and server errors still fail. Neither new artifact
has been applied to a hosted database; their candidate checksums below have
been repinned before release, not rewritten behind an existing receipt.

The corrected preflight has inspected at least 140 projects and found at least
23 blocked projects (15 missing required objects, eight missing historical
root metadata). This is an interim count, not a complete recovery assessment.
There are 198 history rows without root metadata: 196 short-ID commits, one
full Git commit and one scoped Git commit. A sampled short-ID commit is also
absent from both source object namespaces. Missing data is never fabricated
or skipped. The operator must resolve source gaps before all-project activation
and Contract. The user explicitly chose to retain every project and wait for
missing objects to be restored before migration. No cleanup/deletion workaround
is authorized; hosted cutover remains on hold under that decision.

The first PR CI run exposed additional release blockers:

- Default/custom standalone installations fail project creation with
  `storage_billing_entitlement_unavailable`: the native SQL requires PuppyPay
  entitlements even when standalone billing is disabled. This needs an explicit
  storage/billing policy correction; do not fabricate PuppyPay rows or weaken
  the hosted quota guard to make installation pass.
- Populated standalone upgrade stops at
  `DATA_MIGRATION_REQUIRED:20260927_entrypoint_storage_backfill`. The installer
  currently pushes schema before completing the earlier reviewed data phase;
  the current pointer alone cannot perform the full staged upgrade. Final
  entrypoint classification/freeze and native inventory/archive phases must
  be coordinated before starting native-only consumers.
- The staged release pointer still selects `20261003_final_entrypoint_storage`;
  it does not yet orchestrate the two native artifacts. The hosted operator
  route needs its protected connection, actual writer/queue drain evidence,
  S3 configuration and receipt verification before the separate Contract.
- The deployed upload worker still uses `file_worker`; the final source uses
  `upload_worker`. Consumer role/source and Wait-for-CI settings need a complete
  verified cutover plan before any service is restarted on the candidate.

CI repairs in this candidate update a deleted test reference, the authorization
route manifest, current grant-bound MCP fakes, historical migration rehearsal
selection and Linux evidence-directory UID handling. A test that re-created the
removed bare-cache warmer was retired; direct-object protocol suites remain.
The secret scan's 81 findings were individually verified against the exact
source commit: 80 public contract/source digests and one synthetic request UUID.
Only those exact fingerprints are suppressed; no global secret rule is disabled.

Follow-up local validation: 91 focused unit cases passed; the historical
entrypoint migration rehearsal passed all fresh/populated/conflict/retry/Contract
checks; the Docker native migration rerun passed 23 inventory cases plus the
separate final Contract case. The containment rehearsal passed its populated
upgrade, REST/JWT denials, ten unsafe privilege mutations and fresh pgTAP suite
(`/private/tmp/puppyone-ci-containment-fixed.json`). PR-wide CI remains required, including the two
unresolved standalone failures above. Evidence files are
`/private/tmp/puppyone-ci-followup-unit.log`,
`/private/tmp/puppyone-ci-entrypoint-fixed.log` and
`/private/tmp/puppyone-native-migration-linuxuid.log`.

## Delivered behavior

- Native PostgreSQL refs and canonical S3 Git objects are the only runtime
  version authority. Product writes and stock Git share checked ref publication.
  The old project-root publisher, startup root repair, old namespace fallback,
  scoped bare transport and private snapshot HTTP entrypoints are removed.
- Product reads/history/restore, creation/seeding, Upload, Import, Synchronize,
  GitHub, table edits and Agent publication bind current grants to native reads
  and writes. Derived indexes use a native durable queue. Old Scope credentials
  fail closed; they cannot become Project-root credentials.
- Retried producers use durable request/input/base facts and publication CAS.
  GitHub can recover a successful native commit after losing its provider log;
  incomplete snapshots cannot silently delete existing files. Upload staging
  does not place objects into the canonical namespace before admission.
- The inventory artifact preserves native Git identity where present and
  converts snapshot-only formats into labelled Git commits. The recovery
  artifact preserves private graphs and unknown source bytes, verifies full
  hashes/lengths, then removes old current keys with resumable cleanup. This also
  covers the separate private snapshot manifest prefix and its private previews.
  The old factory prompt is archived and converted to Git instructions; user
  custom prompts are preserved verbatim. New defaults use standard Git.
- The separately gated Contract removes old aliases, duplicate fields and root
  publisher RPCs. Historical engine records remain read-only archive metadata.
  Runtime source, frontend, CLI and operational scripts have no `MUT`/`mut_`
  references. Historical SQL and operator conversion artifacts intentionally
  retain source identifiers needed to replay upgrades and locate old data.
  The obsolete standalone namespace-copy script was removed in favor of the
  checked portable artifacts. The old GitHub smoke script used retired HTTP
  routes/tables and was removed; current provider retry/CAS/failure tests remain.
  No live external GitHub account acceptance is claimed here.

## Validation

| Check | Result | Evidence |
|---|---|---|
| Native/component and affected consumer unit suites | 955 passed, 1 platform skip | `/private/tmp/puppyone-native-wide-final.log` |
| Broader platform/table/provider consumer units | 953 passed, 8 skips, 2 opt-in e2e/network cases deselected | `/private/tmp/puppyone-native-consumer-units-final.log` |
| Agent runtime, owned PostgreSQL/PostgREST/MinIO/Pi | 74 cases passed across main run (73) and dispatcher recheck (1) | `/private/tmp/puppyone-native-agent-units-cutover.log`, `/private/tmp/puppyone-native-agent-dispatcher-final.log` |
| Data migration framework units | 145 passed, 11 infrastructure skips | `/private/tmp/puppyone-retirement-migration-unit.log` |
| All backend test collection | 3,703 selected, 24 migration tests deselected, no collection errors | `/private/tmp/puppyone-retirement-final-collection.log` |
| Owned Docker inventory/archive/consumer migration | 23 passed, no skips/failures | `backend/.native-migration-test-results/inventory/{run.json,junit.xml}` |
| Separate Docker stack: committed final Contract and subsequent native creation/write/read/readiness | 1 passed, no skips/failures | `backend/.native-migration-test-results/contract/{run.json,junit.xml}` |
| SQL suite and schema/privilege containment | 329 SQL assertions passed in each of the two migration stacks; smoke checks passed | `/private/tmp/puppyone-retirement-final-migration.log` and `/private/tmp/puppyone-retirement-contract-final.log` |
| Owned Docker hosting selected-case coverage | All 1,465 selected cases have passing evidence across corrected/resumed runs; no unresolved failed/skipped selected case | `backend/.native-retirement-hosting-results/combined-coverage.json` and four retained JUnit reports |
| Artifact validation | All 12 repository artifacts validated; actual 17 changed DB files passed change policy | `python -m src.infra.data_migrations lint` plus worktree change policy |
| Source/static checks | No retired name matches in current runtime/client/scripts; Ruff lint passed 146 Python files, formatting passed 144 non-artifact files; `git diff --check` passed | Final worktree scan; checksummed artifact bytes retained unchanged |
| OpenSpec | Both `add-native-inventory-migration` and `retire-project-root-engine` passed strict validation | OpenSpec CLI |

The host unit skip is the Linux child memory/file limit case; that case also
passed in Docker hosting acceptance. The migration framework's skipped cases
require their explicit integration fixture. These are not passed assertions.
An exploratory all-backend run encountered Redis/ETL infrastructure-dependent
errors; this document does not claim all backend regression tests passed.

Hosting evidence is combined coverage, **not one clean end-to-end runner exit**.
The first interrupted run had 304 passed and 9 failed cases; subsequent runs had
167 passed / 1 failed, 440 passed / 1 failed, and 704 passed / 0 failed.
Overlapping successful cases are deduplicated against the current collected
1,465-case selection; the latest recorded result for each selected case is also
verified as passed. Every stack passed 329 SQL assertions and schema/privilege
smoke checks, with no recorded container resource failures. A resumed subset
omits already-passed application cases and does not by itself satisfy the
runner's full-application coverage gate.

Corrections were the canonical deletion-prefix SQL, native-creation/billing
fixtures, idle HTTP keepalive handling in the local application harness, bounded
waiting for a streamed read pin before GC, and assertions for the removed server
repository facade/canonical namespace errors. The Agent dispatcher now shares
the supervisor fixture's explicit readiness setup; real authorization, claims,
publication and cleanup assertions remain. Native readiness itself is checked
separately through the real application and committed-Contract suites.

### Migration acceptance details

The real PostgreSQL/S3 cases cover raw short-ID snapshots, canonical loose and
indexed/chunked Git objects, history metadata, branches/tags, corrupt/missing or
cross-project source refusal, organization-atomic activation and billing,
retries, source retention, private archive verification, interrupted deletion,
corrupt archive refusal before further deletion, native GC retention, producer
idempotency/input mismatch/stale CAS, forward restore, native projection claim
and acknowledgement, and hidden initialization for SHA-1 and SHA-256.

The final Contract is tested in a separate fresh stack. Negative inventory
fixtures are never deleted merely to make the global Contract gate pass.

### Retired tests and their replacements

Tests for deleted implementations were removed along with those implementations.
They instantiated the former root publisher or projected scoped bare transport;
retaining them would require reviving the engine being retired.

| Retired implementation tests | Current verification |
|---|---|
| Root CAS writer/server facade and old initialization | Native Product journal/ref transaction suites, native S3 writes, producer cutover and initialization migration cases |
| Old receive/upload-pack and scoped projection | Direct-object transport units; stock Git HTTP/S3 workflows; full application bare repository acceptance |
| Old history/ref reconstruction and root repair | Pinned native history (including SHA-256, merge topology, cursor invalidation and mode-only changes); missing objects fail without rewriting authority |
| Outbox/private snapshot/conflict-root GC | Native projection claims/rebuild/ack; verified recovery archive and native GC retention |
| Old scope publisher/client compatibility | Explicit denied old Scope credentials; new Scope views remain a separate feature |

Shared stock-Git history/ref/network workflow cases and HTTP/API fixture helpers
were retained in `tests/repository_hosting/harness/`. This cutover does not replace
actual protocol acceptance with mocks.

The Agent supervisor matrix now exercises native SHA-1 and SHA-256 instead of
the removed legacy/native split. Its process-loss, approval, checkpoint,
publication-retry, cancellation and concurrent-write assertions remain.
The public API guard applies an explicit retirement delta for the removed
conflict/private-snapshot/scope-sync endpoints and native history DTOs. The
earlier immutable API baselines and authorization assertions remain in place.

## Reproduction and deployment boundary

From the repository root with local Docker, Supabase CLI and locked dependencies:

```sh
backend/.venv/bin/python scripts/testing/run_native_inventory_migration.py
backend/.venv/bin/python scripts/testing/run_repository_hosting.py \
  --live --s3 --docker --target \
  --output backend/.native-retirement-hosting-results -q
```

The added `native-inventory-migration-tests.yml` workflow runs the first command
without hosted credentials. The runner requires an owned local Docker daemon,
uses read-only source, and tears down only its own containers.

The immutable artifact checksums remain:

```text
20261007_native_repository_inventory
a875d3082fdb2314c35e06d11e177c7a6ec2f0783dd2e3630f05199d66d56f70
20261007_repository_recovery_archive
af6a951436f36f712bf078f818c5f629575df566bead7ab2a37047fbe669f118
```

Hosted release order is stop/drain writers and GC -> Expand SQL -> inventory
conversion/verification -> private archive/verification/source cleanup ->
verified receipts -> separate Contract release -> native consumers and cold
read/write checks. The [archive runbook](../../../../supabase/data_migrations/20261007_repository_recovery_archive/README.md)
is the execution reference. Never start this native-only build over unconverted
inventory, restart old writers after conversion, or combine Contract with the
initial automatic Expand pass.

These fixtures represent source formats, not a migration of the 148 hosted test
projects. The previously observed missing roots still require source inspection.
Unsupported graph size/depth, ambiguous state, or non-root named refs stop the
artifact and require a reviewed forward artifact. Old Scope is not carried
forward. Historical versions of deleted keys in versioned S3 buckets retain
their configured retention; this migration removes current old keys only.
