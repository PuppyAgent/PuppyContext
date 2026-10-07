# Change: Bound Cloud Agent database operations

## Why
Measured short replies performed 239–377 worker database requests. Repeated full admission on heartbeat and per-token writes dominate latency and have no enforceable query boundary.

## What Changes
- Introduce named query/command ports, immutable authorization context, database revision guards and narrow renewal.
- Batch text persistence and compose authorized run views; instrument actual database HTTP attempts and concurrency.
- Retain canonical authorization, native Git publication, fencing, approval and recovery semantics.
- Add real PostgreSQL/PostgREST/Pi and client response/performance acceptance.

## Impact
- Affected specs: agent-chat (extends accepted replace-agent-runtime and add-agent-git-workspace changes).
- Affected code: Agent runtime/service, authorization facts, Supabase transport, immutable migration, tests.
- User approved this architecture and implementation in the current conversation on 2026-10-07. No additional design approval is pending.
