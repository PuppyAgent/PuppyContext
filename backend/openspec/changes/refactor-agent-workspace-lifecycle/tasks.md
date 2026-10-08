## Implementation
- [x] Define and document workspace, recovery and publication contracts.
- [x] Separate worker control, tools and Git operations.
- [x] Implement stock Git synchronization through the shared Git service.
- [x] Separate conversation persistence and provider workspace recovery.
- [x] Wire supervisor lifecycle, fencing, reuse and publication reconciliation.
- [x] Add real sandbox, Git, concurrency, recovery and request-budget tests.
- [x] Run local regression and documentation checks; record exact limitations.

Merge and hosted deployment are intentionally not part of this delivery.

## Hosted provider acceptance
- [ ] Build the committed test-only E2B template and run both real E2B recovery/Git tests.

Local checks use real Docker, PostgreSQL/PostgREST, MinIO and stock Git. E2B
upload is pending explicit authorization after automatic approval review rejected
exporting the internal worker artifact to E2B. No E2B test or hosted deployment
was executed. This item is not satisfied by Docker results.
