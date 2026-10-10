# Environment release design

The existing schema CLI, data artifact catalog and verifiers remain the
execution primitives. `release/plan.py` validates dependencies; `engine.py`
owns ordering; `target.py`, `schema.py` and `journal.py` own database state.
`docker.py` and `hosted.py` adapt actual service, backup and acceptance I/O.

The database advisory lock covers the entire release, including acceptance.
A private journal binds phases and verification to an immutable source SHA
and phase-plan checksum. Repeat runs recheck migration history and immutable
data receipts. An accepted source is a no-op; a stale source is rejected.

Additive releases keep services online. Pending data transformations and
Contract SQL require actual producer stop, queue drain, consumer exit and a
restore-tested backup. A failed migration never starts a candidate binary or
automatically rolls an old binary onto a changed schema. A forward fix or
idempotent retry resumes through the same coordinator.

Hosted runner groups accept only the protected release workflows; PRs execute
on disposable GitHub-hosted runners with synthetic credentials. Private
configuration supplies service IDs, queue names, backup location and a
dedicated acceptance account. Provider autodeploy is disabled at activation;
the coordinator deploys the checked source SHA after database verification.

Acceptance reads and writes an owned temporary project and checks a real
Agent reply. Docker tests preserve an old-version project, login session and
object data through the upgrade. Public output contains only aggregate test
results; backups and customer diagnostics remain private.
