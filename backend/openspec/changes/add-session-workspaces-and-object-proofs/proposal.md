# Change: Persist Session workspaces and compose incremental publication proofs

## Why
Normal multi-turn execution needs to preserve a sandbox's working directory and
Git history while idle. Publication needs to validate new objects without
rediscovering already verified historical objects on each push.

## What Changes
- Persist fenced Session workspace ownership, explicit pause/resume and six-hour
  idle retirement. Keep Run scheduling and publication authority unchanged.
- Use provider pause/connect and the same sandbox identity on normal subsequent
  turns; cold reconstruction is reserved for absent or retired resources.
- Compose durable, generation-bound object proofs under live retention pins.
  Preserve fresh verification for new bytes, atomic refs, file policy, billing,
  integrity invalidation and controlled collection.
- Add real provider, SQL, S3, request-budget and fault-injection acceptance.

## Approval
The user explicitly approved these architecture contracts and requested their
complete implementation on 2026-10-09. This is the implementation record for that
accepted scope. Shared deployment is separate from isolated acceptance.

## Impact
- Agent runtime, sandbox controller/provider lifecycle and Git publication.
- Two additive schema migrations; no existing migration is rewritten.
- Existing architecture owners remain authoritative; no parallel architecture.
