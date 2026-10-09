## Implementation
- [x] Add guarded atomic Agent commands and previous-schema upgrade tests.
- [x] Wire worker/supervisor to commands; retain recovery and approval barriers.
- [x] Consolidate Git publication and metadata access without stale authority.
- [x] Add real-sandbox request-budget, retry, revocation and concurrency coverage.
- [x] Update existing architecture documents and run required regression checks.

## Validation
- Required backend regression: 2,717 passed; existing marker/environment skips retained.
- Full Agent runtime suite with PostgreSQL/PostgREST, MinIO and local Docker Pi:
  148 passed, 3 skipped (external Desktop checkout and two E2B cases).
- Final worker protocol/recovery/read-write acceptance after version gate: 8 passed.
- Real Supabase/S3 repository publication selection: 117 passed; fresh SQL suite: 342 passed.
- Publication batch/architecture checks: 13 passed; stock Git Node tests: 9 passed.
- Measured fixed-model executor attempts: three-file question 20; one-tool writes
  of 1 and 60 files both 35. Actual periodic heartbeats remain separately bounded
  and included in reports. No hosted latency or real-model inference claim.
- OpenSpec strict validation, Ruff, SQL artifact lint and documentation validator pass.
- Hosted migrations/deployment and E2B acceptance are not part of this local delivery.
