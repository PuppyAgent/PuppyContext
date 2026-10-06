# Qubits native Git development acceptance — 2026-10-06

Passed on the local `qubits` branch. No remote push, hosted deployment, hosted
load test or user-data migration was performed. The owned test services were
stopped after acceptance. Native Scope remains outside this release profile.

## Candidate and commits

- `664dd1e6`: direct S3-object Git transport implementation and original receipts.
- `ddf4a378`: merge into local `qubits`, preserving the other integrated work.
- `5b237fb5854efdb275d563d87cd6a28596ecac9b`: collaboration tests; clean checkout
  at the start of the final native acceptance run.
- `c3cd01056b4cc8d261a55f2a18cbbbdb61ed661c`: explicit contract delta for the
  independently merged Cloud Agent runtime, plus a documentation command fix.

The final native run and the final offline backend run use the last two commits,
respectively. Their application source, dependency manifests, schema migrations,
runner and new collaboration tests have identical Git object IDs, recorded in
[summary.json](summary.json). Only tests/documentation changed between them.

The full regression initially found a missing contract delta for the existing
Agent runtime replacement: remove `/api/v1/agents` and two old request schemas;
add seven durable-run paths and two new request schemas. The fix records exactly
that reviewed design, preserving the original snapshots and all unrelated
contract comparisons. The application interfaces were not changed by the fix.

## Results

| Run | Result | Limits of the result |
| --- | --- | --- |
| Final native Git acceptance | **371 passed**, zero failed/skipped/xfail | 176 authenticated application, 150 PG/S3, 45 component cases; 1,492 other cases deselected |
| Database contracts | **329 assertions passed**, 9 SQL files | Owned local Supabase, no skip/TODO |
| Final offline backend regression | **3,726 passed**, zero failed | 904 skipped, 34 xfailed, 110 deselected; these are not passed acceptance |
| Contract/entrypoint/Agent targeted recheck | **32 passed** | 34 integration/network cases deselected |
| Ruff and whitespace | Passed | New collaboration tests and updated contract test |

The native runner exited 0. It ran from `2026-10-06T10:53:07Z` to
`2026-10-06T11:18:44Z`; pytest took 1,469.85 seconds. Linux used Python 3.12.11
and stock Git 2.50.1 with 4 GiB RAM / 4 CPU / 2 GiB scratch / 1024 PID limits.
Container memory peaked at 982,605,824 bytes (about 937 MiB), with 58 observed
tasks and no OOM/PID exhaustion. This includes test clients, not just the API.

The offline backend run took 542.49 seconds. Most skips require separate live
PG/S3/Auth/application environments; the native run above supplies the selected
native acceptance. Historical profile xfails remain explicit. Counts from
different runs overlap and must not be summed.

## Added coverage and load baseline

[Coverage and runnable commands](../../../backend/tests/repository_hosting/COLLABORATION.md)
describe the 13 new cases: two independent clients/credentials competing on an
atomic branch+tag push, disjoint edits and same-file conflicts, merge/rebase and
repush, Git/Product overlap, receipt replay, in-flight revocation, saturated
upload disconnect cleanup, byte-preserving names and bounded fetch load.

One API process, two Git slots, 1 MiB random blob, 16 successful cold fetch RPCs
per level; every pack passed stock Git verification:

| Concurrent clients | Completed | Fetch p50 | Fetch p95 | Verified workflows/s | Busy retries |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 16 | 0.261 s | 0.338 s | 3.138 | 0 |
| 2 | 16 | 0.282 s | 0.407 s | 5.703 | 0 |
| 4 | 16 | 0.279 s | 2.603 s | 5.535 | 4 |
| 8 | 16 | 0.411 s | 4.125 s | 3.814 | 12 |

Fetch percentiles include bounded Retry-After backoff and exclude client pack
verification; workflow throughput includes verification. Only the exact
`Git workers are busy` 503 is retried. All other errors fail. API-process lifetime
RSS high-water reached 241,932 KiB (about 236 MiB) during this load profile.

These small local samples provide a baseline and saturation behavior, not a
hosted latency SLA or 1000-Agent capacity proof. Other local regression work was
running concurrently; compare performance only under recorded, comparable
conditions. No service admission limits were raised to make the test pass.

## Durable evidence

- [Runner, exact candidate and schema hashes](run.json)
- [All 371 JUnit cases](junit.xml)
- [Container resource receipt](container-resources.json)
- [Load samples](bare-collaboration-load.json)
- [Backend summary and runtime identity](summary.json)
- [Artifact SHA-256 checksums](checksums.json)

Original local artifacts remain at `/private/tmp/puppyone-qubits-native-final-20261006/`.
The offline regression JUnit is `/private/tmp/puppyone-qubits-backend-final-20261006.xml`;
its checksum is in the summary. Private application logs and credentials are not
copied into this directory. The previous 358-case implementation receipt remains
unchanged in the original OpenSpec change.
