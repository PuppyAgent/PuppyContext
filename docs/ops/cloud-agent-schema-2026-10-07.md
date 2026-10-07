# Cloud Agent schema release — 2026-10-07

Desktop's `/agents/runs` request reached the local API but failed with PGRST205
because the staging database did not expose `agent_runs`. The successful
repository-discovery release `d95f0bc4` did not include the Agent control plane.

This release adds the unchanged `20261006060000` and `20261006060100` migrations
from the tested Agent implementation `3378ef15`, plus their unchanged
`20261004020000` publication-admission dependency. It does not enroll repositories,
rewrite knowledge data, run the pending native inventory/archive conversion, or
perform the native-only application cutover.

The existing protected staging workflow owns application and migration-history
recording. The hosted read-only contract now checks the four Agent tables, RLS,
backend-only RPC privileges, publication triggers, and actor/lease dependencies.
The separate local/CI transaction test exercises duplicate submission, claiming,
actual publication admission, stale execution rejection and durable reply storage.

After schema verification, the API and Agent supervisor need matching runtime
configuration, an available managed model and the pinned sandbox artifact.
Schema success alone is not evidence of a completed chat. Verify a real durable
reply through Desktop before reporting the Agent as usable.
