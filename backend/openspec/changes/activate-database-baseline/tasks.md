- [x] Archive immutable sources and activate one timestamped baseline SQL.
- [x] Update policy, historical path resolution and data-job prerequisites.
- [x] Add transactional, schema-verified history adoption and deployment admission.
- [x] Update fresh-install, historical-upgrade and adoption CI coverage.
- [x] Document paths, commands and hosted rollout limits; verify all checks.

Validation on `a884dcf3`: isolated B1 verification (run 36245551178), complete
database workflow (run 36245551188), Backend tests, Frontend Build, Gitleaks and
Supabase Preview all passed. Qubits and production history remain unchanged.

Approved extension, 2026-09-27:
- [x] Archive all eight immutable data artifacts beside the schema sources.
- [x] Use one ID resolver in runner, release selection, operator checks and tests.
- [x] Preserve artifact checksums, receipts, retired-task admission and old upgrades.
- [x] Fix audited release serialization, promotion scope and workflow input handling.
- [x] Run regression and isolated Supabase CI, document findings and remaining limits.

Archive extension verification: all 102 SQL files and all eight data artifact
directories moved byte-for-byte; artifact checksums remain unchanged. Local full
backend regression on `4b751ede`: 2464 passed, 27 skipped, 50 deselected. The
isolated B1 workflow (36255314481) and populated database upgrade job (36255314441)
passed on `c2126e61`; the latter workflow exposed legacy fixture paths, corrected
in `4b751ede`. Final combined CI reruns after those test-path corrections.
Repository policy, strict OpenSpec and increment-only secret scanning pass.
Historical secret findings and the legacy PG15 Compose installer are recorded
as outstanding audit items; no Qubits/production mutation was performed.
