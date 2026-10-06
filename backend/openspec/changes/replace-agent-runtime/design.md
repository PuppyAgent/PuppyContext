# Hosted Agent execution

The existing Agent adapter owns admission and runtime orchestration. A separate worker process polls PostgreSQL, claims a bounded lease and creates a new execution identity. HTTP subscribers only read committed snapshots/events. Closing a subscriber does not stop a run.

Each execution runs a pinned Pi SDK and its tools in a provider sandbox with network egress disabled. A framed control channel carries model requests to the existing managed inference service; provider keys remain in the backend. A local loopback bridge translates the Pi OpenAI-compatible provider protocol. Workspace files are the exact authorized projection of one captured repository revision.

Before a tool starts, its input and checkpoint are durable. Mutating tools wait for an explicit approval bound to the run and tool call. Completed tool receipts and full Pi session entries are checkpointed, including branch/compaction entries. A takeover never replays an effect whose receipt is still executing. It reports outcome_unknown and retains the last acknowledged checkpoint and resource identity.

Checkpoint objects are immutable and checksummed. The DB reference and event are committed only after the object write succeeds. Publication uses the captured base and stable run publication identity. A final database fence rejects stale execution publication. Failure/unknown leaves recovery material retained; success is emitted only after the canonical publication result is durable.

The scheduler runs independently of the API process. Authorization and Agent activation are rechecked at admission, takeover, model/tool boundaries and publication. Run duration, model calls and event retention are bounded. Runtime billing uses the existing ledger and stable run identity.
