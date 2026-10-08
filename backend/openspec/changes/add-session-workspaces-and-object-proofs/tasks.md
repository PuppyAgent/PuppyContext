## Implementation and acceptance
- [x] Define final lifecycle and incremental publication contracts in existing architecture owners.
- [x] Implement persistent Session workspace commands and provider pause/resume.
- [x] Implement bounded six-hour retirement with fencing and retry ownership.
- [x] Implement protected immutable-object proofs and incremental validation.
- [x] Reuse verified tree aggregates for policy and billing; fence invalidation and GC.
- [x] Pass lifecycle, real storage, concurrency, fault and request-budget regression gates.
- [x] Pass complete non-network regression, source/spec and documentation checks.

Local acceptance: 187 Agent/read-budget cases passed (2 hosted E2B cases not
enabled); the final concurrency/set-query changes passed 34 component and 24
real lifecycle/storage cases. The required non-network gate passed 2,738 cases
(885 environment/profile skips); supplementary native PostgreSQL hosting
regression passed 1,001 cases. Nine stock-Git JS cases, Ruff, strict OpenSpec and
documentation validation passed. SQL acceptance includes populated upgrade and
pre-activation transaction rollback. No customer inventory was scanned.

Hosted E2B artifact upload/acceptance and shared migration/deployment are separate
release actions, not established by Docker or local SQL results.
