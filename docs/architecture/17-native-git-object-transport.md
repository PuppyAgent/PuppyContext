# Native Git transport over canonical objects

This is the native full-Project transport introduced after the initial ISSUE-062
bare-hosting acceptance. Native Scope projections remain a separate requirement.
Locators and credentials follow [the Git access contract](05-git-remote-accesspoint.md).

## Authority and request flow

S3 contains canonical Git commit/tree/blob/tag bytes. PostgreSQL owns refs,
symbolic/detached HEAD, object format, generation and object-location indexes.
An object may be loose in S3 or a byte range inside an immutable storage bundle.
StorageBackend resolves that physical detail; transport uses object IDs and
typed graph edges. Git wire packs and S3 storage bundles are different formats.

```text
Standard Git client
  -> canonical HTTP route -> current RuntimeGrant -> bounded admission
     -> discovery: PG refs and verified tag peel metadata
     -> fetch: pinned published graph -> StorageBackend -> S3
          -> incremental pack encoding -> HTTP stream -> client
     -> push: bounded incoming pack/delta quarantine
          -> typed new-object closure -> RefTransactionService
          -> durable S3 writes + closure proof -> atomic PG ref CAS
          -> Git report-status from the canonical transaction result
```

The previous adapter reconstructed the published closure in a temporary bare
repository for stock upload-pack/receive-pack, multiplying disk use by active
requests. Native transport now creates no bare repository, writes no existing
repository graph to disk, runs no Git subprocess and buffers no complete output
pack. A small request LRU can be disabled or evicted without affecting correctness.

## Components

| Component | Responsibility |
| --- | --- |
| `adapters/git/native_repository.py` | Admitted orchestration and canonical publication handoff |
| `adapters/git/native_wire.py` | Packet limits, byte-preserving discovery and receive commands |
| `adapters/git/native_fetch.py` | v0/v1/v2 rounds, common objects, shallow boundaries and filters |
| `adapters/git/object_reader.py` | Published reachability, pinned durable reads, bounded body cache |
| `adapters/git/object_pack.py` | Bounded incoming pack/delta codec and incremental output encoder |
| `adapters/git/execution.py` | Admission and streaming ownership through cancellation/completion |
| `read/repository_snapshot.py` | Authoritative snapshot and read pin |
| `write_engine/ref_transaction.py` | Closure proof, current policies, publication and ref CAS |

Dulwich is pinned as an object-validation/delta codec inside transport. It owns
neither refs nor storage and does not replace the Version Engine. The adapter
does not instantiate a Dulwich Repo or MemoryObjectStore.

## Concurrency and safety

Client object IDs must be reachable from captured refs or retained published
history. Mere S3 existence or a rejected publication receipt grants no access.
Raw-OID lazy fetch and external thin-pack bases obey the same rule. Structural
reachability searches do not download unrelated blobs.

Read pins remain held through streamed responses and push publication. A
cancelled response joins outstanding storage work before releasing its pin and
worker slot. Incoming checksum, sizes, count, delta bounds, object structure and
typed closure are checked before publication. Pack parsing cannot update refs.
Atomic pushes use one canonical transaction. Unknown SQL acknowledgments recover
using the original request identity and never invent a negative ref report.

Workers share immutable S3 objects without a writable repository directory or
filesystem ref locks. PostgreSQL CAS still resolves competing ref changes. S3
throughput, CPU budgets, ref contention and admission remain real constraints.

## Resource contract and tradeoffs

- Per-process admission remains two Git operations; this does not claim 1000
  simultaneous transfers per process.
- Fetch repository/output scratch is zero. HTTP may spool a negotiation request
  capped at 16 MiB.
- Push input is capped at 128 MiB or a tighter deployment limit. Its incoming
  object/delta spool is capped at 256 MiB and contains no downloaded old graph.
- Limits remain 64 MiB per object, 256 MiB graph bodies, 100,000 objects, 10,000
  refs and 300 seconds. The object-body cache is capped at 8 MiB per request.
- Output packs compress individual objects without delta optimization. Fetch
  omits known common trees/blobs, but bandwidth can exceed stock Git's packs.
  A future bounded delta encoder requires no local repository.
- Partial-clone filters support `blob:none`, `blob:limit`, `tree` and combinations;
  unsupported selectors are rejected explicitly.
- Publication retains conservative full-closure verification, which can reread
  S3 history. Reusing durable closure proofs is a separate optimization.

The protocol follows Git's [pack protocol](https://git-scm.com/docs/gitprotocol-pack),
[protocol v2](https://git-scm.com/docs/protocol-v2) and
[pack format](https://git-scm.com/docs/pack-format).

## Verification

Component tests forbid temporary files and Git subprocesses during cold fetch,
compare both object formats against stock Git, exercise eight concurrent readers,
prove incremental/filtered reads omit unnecessary blobs, and cover cancellation,
unpublished-object denial and malformed packs. HTTP tests cross three protocol
versions with both formats. Real PG/S3 and authenticated application acceptance
are separate layers; component substitutes are not deployment evidence.

Receipts and remaining limits are recorded in
`backend/openspec/changes/remove-native-git-materialization/verification.md`.
