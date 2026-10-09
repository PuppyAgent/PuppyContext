# Change: Bound Agent and Git database operations

## Why
A short read Run makes 50 database HTTP attempts and a one-tool write Run makes
77. Runtime persistence is fragmented and Git adapters reopen contexts.

## What Changes
- Replace hot-path generic patches with named atomic execution/model/tool commands.
- Reuse operation-owned Git read snapshots, publication admission and batch storage.
- Preserve durable recovery, approval, revocation, fencing, CAS and idempotency.
- Enforce measured short-fixture budgets: read <=20, write <=35 HTTP attempts.

## Impact
Agent runtime, worker protocol, Version Engine and additive SQL RPC migration.
The user approved this design and implementation in the current conversation.
Hosted merge/deployment and E2B artifact upload remain outside this delivery.
