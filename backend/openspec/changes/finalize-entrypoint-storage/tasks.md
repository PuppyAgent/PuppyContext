## 1. Sole 049 migration
- [x] 1.1 Confirm ownership, immutable history, final mapping and 061 delivery; retain existing worktrees.
- [x] 1.2 Add Expand schema, redacted decision inventory/import, freeze, immutable data artifact and negative tests.
- [x] 1.3 Verify old/new writes, exact encrypted data/history, drift refusal, receipt and retry on real PostgreSQL.
- [ ] 1.4 After Release A rehearsal, deliver separately gated D01–D06 Contract, final repositories/direct SQL/view consumers and no runtime storage aliases.
- [ ] 1.5 Verify empty installation, populated upgrade, full installation and post-contract recovery; preserve 053 ACL/RLS.

## 2. Final resource cutover
- [ ] 2.1 Retire legacy HTTP/DTO routes after the matching consumer/runtime exit gate, update exact current-contract tests without rewriting historical snapshots.
- [ ] 2.2 Run actual CLI/SDK/Desktop and real persistence/Provider/worker checks on the migrated stack, including mixed Import and binding inventories and legacy read-only disposition.
- [ ] 2.3 Assemble target-environment version, producer/process/queue/webhook, restore and consumer evidence; obtain explicit authorization before any hosted operation.

## 3. Acceptance
- [ ] 3.1 Integrate and reverify source and executable release artifacts; update original audits and canonical docs.
- [ ] 3.2 Close 049/059/058 only on their unchanged original evidence requirements; do not replace environment facts with local fixtures or claim an unauthorized release.

## Release A source checkpoint

- Only additive schema is active. The two Contract scripts remain pending; final application repositories/HTTP retirement are not claimed complete.
- `python scripts/test_final_entrypoint_storage.py`: owned PostgreSQL 17, fresh/populated schema and actual portable runner, explicit source/binding/dual/historical-only decisions, stale/lossy/colliding rejection, atomic receipts/retry, pending Contract, role/column-grant/composite-FK checks, canonical Activity, exact opaque config/history/timestamps, and post-Contract pg_dump/restore retaining new accepted writes. No Source is fabricated for a reviewed historical binding-only record.
- The native test supplies Auth stubs and omits three Supabase-only extensions; this is not full Supabase/GoTrue/Provider/worker/installer or hosted acceptance.
- Operator boundary tests: 25 passed. Backend offline regression: 2741 passed, 27 skipped, 76 deselected; latest operator checks and native rehearsal repeated after retention hardening.
- Ruff, strict OpenSpec and portable artifact lint passed (10 artifacts). Artifact checksum: `4556f98f8ecd96cb60cadad0a5bc4815fff8b4abb6f18f30afed48e7ac324482`.
- Full-stack Release A rehearsal, final source cutover and target-environment evidence remain outstanding. No remote query/DDL, push or deployment was performed.
