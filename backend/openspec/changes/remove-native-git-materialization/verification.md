# Direct object transport verification

This change targets native full-Project repositories in the
`codex/issue-062-native-git` worktree. It does not deploy a service, migrate
existing data or close the future native Scope acceptance gates.

## Candidate identity

The final runtime, dependency lock and protocol-test SHA-256 hashes were frozen
before the final Linux run. All hashes still matched after it completed.
[Candidate manifest](evidence/candidate-sha256.json) records paths relative to
`backend/`. The checkout base was `9e2e47e0115499df0358072f6f0d4b211ff2758f`
with these uncommitted changes; the base commit alone is not the tested candidate.
Earlier development runs are not presented as frozen-candidate acceptance.

## Checks completed during implementation

- `ruff check` passed for every new/replaced transport module, stream ownership,
  native endpoint and new/updated protocol tests. `git diff --check` passed.
- `npx --offline @fission-ai/openspec validate remove-native-git-materialization --strict`
  passed.
- Version Engine and repository unit regression: **1749 passed, 1 skipped**
  (a Linux-only process-limit check on macOS). Receipt:
  `/private/tmp/puppyone-object-transport-components-20261006.xml`.
- Development Linux PG/S3/application run: **344 passed, 0 failed, 0 skipped**,
  plus **329 pgTAP assertions**. This ran while later focused protocol edge
  fixes were being completed; the frozen run below supersedes its candidate
  status. Receipt: `/private/tmp/puppyone-object-transport-native-20261006/`.
- Stock HTTP tests cover SHA-1/SHA-256 crossed with v0/v1/v2, shallow/deepen/
  unshallow, shallow-since/exclude, partial clone with lazy raw-OID fetch,
  normal clone and push. Pack tests cover full/thin/OFS/REF deltas, forward
  dependencies, checksum/expansion bounds and protected Git metadata paths.
- Eight parallel cold fetches complete with body caching disabled and server
  subprocess/temporary-file creation forbidden. This is a transport component
  proof, not eight-way production admission or a 1000-client capacity result.
- Incremental and `blob:none` reads do not fetch unrelated old blob bodies.
  Explicit lazy wants override common-object omission. Unpublished object IDs
  cannot read storage. Cancellation joins in-flight reads before releasing pins.

These counts are separate runs and are not added together.

## Final Linux acceptance

**Passed:** 358 tests, zero failures/skips/expected failures, plus 329 pgTAP
assertions across nine SQL files. The runner exited zero. The 1492 deselected
tests belong to other profiles/suites and are not counted as passed.

| Acceptance layer | Passed |
| --- | ---: |
| Authenticated application, including 78 Git workflows in each object format | 163 |
| Real S3 and PostgreSQL integration | 150 |
| Transport/protocol/resource components | 45 |

The run started at `2026-10-06T09:13:07Z` and finished at
`2026-10-06T09:36:25Z`. Pytest took 1341.42 seconds. Linux used Python 3.12.11
and Git 2.50.1 under a 4 GiB memory / 2 GiB scratch / 4 CPU / 1024 PID budget.
Container memory peaked at 863,600,640 bytes; the observed task peak was 48.
No OOM or PID-limit exhaustion occurred. These are whole-test-container metrics,
including test clients, not per-request memory or a 1000-client load benchmark.

Durable receipts:
- [Runner result](evidence/acceptance-run.json)
- [All 358 JUnit cases](evidence/junit.xml)
- [Resource receipt](evidence/container-resources.json)
- [Receipt checksums](evidence/receipt-sha256.json)

The original artifacts remain under
`/private/tmp/puppyone-object-transport-final-20261006/`. The owned local service
stack and application container were stopped after the run.

```sh
backend/.venv/bin/python scripts/testing/run_repository_hosting.py \
  --live --s3 --docker --target \
  --output /private/tmp/puppyone-object-transport-final-20261006 \
  -k 'native_s3 or bare_repository_application or docker_native_git_application or bare_resource_limits or direct_object_transport or native_transport_protocol or native_execution' \
  --disable-warnings -x
```

The runner owns disposable local PG/Auth/PostgREST/S3/Redis services and its
Linux application container. Real credentials, formal Project creation and
billing ingress are exercised. Its resource receipt distinguishes client/oracle
Git processes from the application and independently checks OOM/PID exhaustion.

## Remaining limits

Two operations per process remain the admission bound. Fetch does not need a
repository/output spool; incoming push data retains fixed byte/object/delta
budgets. Output packs currently omit delta compression, so bandwidth can exceed
stock Git. Durable publication still performs conservative full-closure reads.
This acceptance makes no deployment or 1000-concurrent-client throughput claim.
