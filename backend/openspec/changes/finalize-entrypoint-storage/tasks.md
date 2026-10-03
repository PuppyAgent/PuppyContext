## 1. Sole 049 migration
- [x] 1.1 Confirm ownership, immutable history, final mapping and 061 delivery; retain existing worktrees.
- [x] 1.2 Add Expand schema, redacted decision inventory/import, freeze, immutable data artifact and negative tests.
- [x] 1.3 Verify old/new writes, exact encrypted data/history, drift refusal, receipt and retry on real PostgreSQL.
- [ ] 1.4 After Release A rehearsal, deliver separately gated D01–D06 Contract, final repositories/direct SQL/view consumers and no runtime storage aliases.
- [x] 1.5 Verify empty installation, populated upgrade, full installation and post-contract recovery; preserve 053 ACL/RLS.

## 2. Final resource cutover
- [ ] 2.1 Retire legacy HTTP/DTO routes after the matching consumer/runtime exit gate, update exact current-contract tests without rewriting historical snapshots.
- [x] 2.2 Run actual CLI/SDK/Desktop and real persistence/Provider/worker checks on the migrated stack, including mixed Import and binding inventories and legacy read-only disposition.
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
- At this historical checkpoint, full-stack Release A rehearsal and final source cutover remained outstanding. The follow-up below supersedes those local gaps, not target-environment evidence.

## Release B local rehearsal checkpoint — 2026-10-04

- Release A `1d20cf18` passed the reused 061 full-stack harness (14 checks). Its dependency symlink makes Git porcelain dirty; tracked source matches the commit. Do not call that receipt a wholly clean worktree.
- Release B source promotes both pending Contracts byte-for-byte, updates all release pointers to the immutable final artifact, and switches repositories, direct readers, Activity and GitHub fields to final storage. The SourceConnection alias and run-repository re-exports are removed. Published canonical contracts remain unchanged; an exact delta removes 43 old paths without rewriting historical snapshots.
- Draft final fresh install/replay, real Auth/API/CLI/PostgreSQL/Redis/MinIO/workers and restart passed 14 checks (`/tmp/issue049-release-b-fresh-draft-20261004-0038/receipt.json`). This ran the working-tree Release B, not the older SHA alone.
- `scripts/test_entrypoint_storage_upgrade_runtime.py` passed 9 checks (`/tmp/issue049-populated-upgrade-ninth-20261004-0330/receipt.json`): actual A client writes; explicit Import/dual/history fixtures; installer refusal before review; frozen portable migration and guarded Contract/replay; exact encrypted envelope/history; canonical inventory/authorization/GitHub history; real shared/Desktop client-library requests; real Database Provider preview/save and worker retry; post-cutover public control-plane restore retaining new writes, followed by resumed source-only delete and readiness/drain.
- The Database Provider uses its unchanged URL policy: one fixture hostname is routed by an owned loopback HTTP proxy to real local PostgREST. No DB response, encryption, repository, authorization or worker is replaced. Historical GitHub records are fixtures, not OAuth/webhook-delivery acceptance.
- Recovery retains actual Supabase Auth/platform namespaces/extensions and object storage, restores public application objects/data/ACLs, and restarts real services. It is **not** Auth/platform/object-store disaster recovery. Temporary future-object default grants are cleared offline before restore and restored from the archive; otherwise existing Supabase defaults can silently reopen anon/authenticated access. The unchanged final verifier blocks restart on ACL/RLS mismatch. No grant failure is ignored.
- Actual clients exposed a pause race: worker start/success/error could overwrite a concurrent pause. Conditional runtime UPDATE predicates now preserve paused/disabled state while retaining already accepted commit/error facts; five deterministic regressions cover this.
- Backend: **2757 passed, 27 skipped, 76 deselected**. Joint real-client/local Redis/production Electron-window gate: **31 passed**; the window's backend facts remain isolated substitutes, separate from the real migrated-stack client-library gate. Desktop test driver `af79fc7e` uses the canonical persisted parent and is integrated locally. CLI units, native PostgreSQL rehearsal, exact OpenAPI, portable artifact lint and strict OpenSpec pass. Full-file legacy Ruff style debt is not represented as clean; changed Python passes F/E9, and new Python files pass their full rules.
- Remaining: finish the unreachable legacy route/private DTO cleanup audit, commit/integrate and reverify the final candidate, refresh canonical docs/original audits with durable redacted receipts, and obtain scoped target-environment authorization/evidence. No remote query/DDL, push or deployment was performed.
- TASK-PLAN's later 061 scope update is accepted: its legacy-version/old-queue migration requirement was withdrawn and 061 is archived. Do not reopen or reassign it; 049/058/059 retain their own resource/database/environment requirements.

Reproduce populated local acceptance with installed dependencies and no `.env` inputs:

```bash
backend/.venv/bin/python scripts/test_entrypoint_storage_upgrade_runtime.py \
  --release-a-source /absolute/path/to/1d20cf18-checkout \
  --desktop-source /absolute/path/to/desktop \
  --supabase-bin /absolute/path/to/supabase-2.107.0 \
  --artifacts /tmp/new-private-upgrade-artifacts
```

Private artifacts contain generated credentials/backups. Publish reviewed receipts only, never whole artifact directories.
