# Native Git collaboration and load acceptance

This suite tests new full-Project native repositories. It does not activate
existing repositories, implement Scope, deploy services, or send load to Qubits.
The checked-out `qubits` code runs in the existing owned Linux/Supabase/S3 runner.

## Existing coverage

| Contract | Existing executable evidence |
| --- | --- |
| 78 composed Git workflows, both object formats, actual Project/credential creation | `integration/test_bare_repository_application.py` |
| v0/v1/v2, shallow/deepen/unshallow, filtered clone/lazy fetch, incremental fetch | `unit/test_direct_object_transport.py`, application workflow catalog |
| Real SQL CAS, atomic multi-ref publication, lost database ACK and rediscovery | `integration/test_native_s3_transactions.py`, `test_ref_authority.py` |
| Native Product retries, process death, object durability, quotas and billing | `integration/test_native_s3_product_*.py`, `test_native_s3_*` |
| Cancellation joins storage before releasing read pins; cache disabled; no server Git/temp repo | `unit/test_direct_object_transport.py` |
| Public API names/schemas, retired aliases, four entrypoint ownership and queues | `tests/platform/test_entrypoint_public_contract.py`, `test_entrypoint_architecture.py`, `test_entrypoint_retired_source.py`, `test_entrypoint_queue_cutover.py` |

Stock Git workspace/oracle tests alone do not establish Cloud support. The
formal application and PG/S3 layers remain separate, explicit acceptance layers.

## Added real application scenarios

`integration/test_bare_collaboration.py` adds 13 cases:

- Two distinct credentials and clients hold overlapping HTTP uploads from one
  base OID. Exactly one atomic branch+tag push wins; the rejected tag is absent.
  The losing client retains its work, fetches the winner, merges, and pushes.
  Disjoint files and an actual same-file conflict are both exercised, in SHA-1
  and SHA-256. Cold reads after an API restart verify both parents, file bytes,
  tags, stock `fsck`, and logical billing without charging rejected writes.
- Git upload overlaps a Product HTTP write. Git's stale CAS is rejected, the
  Product request replays its original receipt, a different stale request fails,
  and the Git client fetches/rebases/pushes with both changes retained (2 cases).
- Two incomplete uploads exhaust both Git slots; another request receives the
  precise busy response and Retry-After while `/live` remains responsive.
  Disconnects release leases, spools and slots, leave the ref unchanged, and
  permit a subsequent successful push (2 cases). The fixture waits for actual
  request spools before probing, since a Project lease precedes Git admission.
- UTF-8 branch/tag names and UTF-8, newline and non-UTF-8 file paths survive
  atomic push, protocol-v2 cold fetch and stock `fsck` byte-for-byte (2 cases).
- Revoking a second credential while its body is in flight prevents publication;
  the original credential still reads the old ref and can later publish (2 cases).
- A load baseline runs 16 cold fetch RPCs at each of 1/2/4/8 client concurrency
  levels. Every successful pack is consumed by stock `index-pack --strict`, then
  ref/content/`fsck` checked. Only the exact busy 503 response is retried, using
  Retry-After and a bounded deadline; other 503s or errors fail immediately.

CAS rejects stale ref updates. Client merge/rebase is a subsequent operation;
these tests do not claim that receive-pack automatically merges conflicting pushes.
Overlapping HTTP uploads prove concurrent requests, while the existing SQL tests
independently exercise publication lock/CAS races.

## Run

From the repository root:

```sh
# Focused collaboration/load run; isolated local services only.
backend/.venv/bin/python scripts/testing/run_repository_hosting.py \
  --live --s3 --docker --target --output /private/tmp/native-collaboration \
  -k bare_collaboration --disable-warnings -x

# Release candidate: existing native protocol/PG/S3/application coverage plus additions.
backend/.venv/bin/python scripts/testing/run_repository_hosting.py \
  --live --s3 --docker --target --output /private/tmp/native-git-candidate \
  -k 'native_s3 or bare_repository_application or docker_native_git_application or bare_resource_limits or direct_object_transport or native_transport_protocol or native_execution or bare_collaboration' \
  --disable-warnings -x
```

Also run the repository-required offline backend suite for API names, contracts,
entrypoint boundaries and the rest of the merged application:

```sh
cd backend
uv run pytest -q -m 'not e2e and not integration and not network'
```

## Reading performance evidence

`bare-collaboration-load.json` records successful operations, busy retries,
fetch p50/p95 (including backoff), verified-workflow throughput, and the API
process's lifetime RSS high-water mark. JUnit also includes this JSON. Runner
evidence records the exact commit, dirty state, software and container budgets.

The baseline uses one API process with two Git slots and a 1 MiB random blob.
The 4/8-client samples intentionally exceed admission. Fetch latency measures
the authenticated HTTP RPC, not Git negotiation or client object verification;
workflow throughput includes client verification. RSS is a process lifetime
high-water mark, not per-request allocation. Results characterize this local
environment; they are not a hosted p95 SLA, a 1000-Agent result, or an established
performance regression threshold. A repeatable candidate baseline is needed
before choosing a relative regression budget. Deterministic correctness,
bounded retries and resource cleanup remain hard assertions.

Hosted capacity acceptance should reuse these scenarios against an explicitly
owned test repository, scale read/write clients and repository histories, and
record the actual replica count, object-store latency, request limits and retry
rate. No credential-bearing command or real repository is embedded in this suite.
