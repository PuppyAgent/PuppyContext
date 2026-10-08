## Implementation
- [x] Define and document workspace, recovery and publication contracts.
- [x] Separate worker control, tools and Git operations.
- [x] Implement stock Git synchronization through the shared Git service.
- [x] Separate conversation persistence and provider workspace recovery.
- [x] Wire supervisor lifecycle, fencing, reuse and publication reconciliation.
- [x] Add real sandbox, Git, concurrency, recovery and request-budget tests.
- [x] Run local regression and documentation checks; record exact limitations.

The user subsequently authorized Qubits release on 2026-10-09; the release
checklist lives in `add-session-workspaces-and-object-proofs/tasks.md`.

## Hosted provider acceptance
- [x] Build the committed E2B template and run both real E2B recovery/Git tests.

Local checks use real Docker, PostgreSQL/PostgREST, MinIO and stock Git. The
committed public Worker artifact passed real E2B acceptance on 2026-10-09:
2 tests in 115.57 seconds, including snapshot recovery after deletion and three
Git turns covering same-ID pause/resume, external writes and expiry/rebuild.
Template `dtj6qxwvr32fleetor3f`, build `3cd59bab-0680-4bdf-ba8f-9c1bdcaeec3f`.
This provider result is separate from hosted application deployment.
