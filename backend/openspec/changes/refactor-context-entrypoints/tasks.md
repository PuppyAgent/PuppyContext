## 1. Schema

- [x] 1.1 Add target tables for upload jobs and upload items.
- [x] 1.2 Keep import jobs canonical and add source identity fields.
- [x] 1.3 Add target tables for durable connections and sync runs.
- [x] 1.4 Add target table for access surfaces.
- [x] 1.5 Add read-only activity aggregation view.
- [x] 1.6 Keep migration additive and leave legacy tables readable.

## 2. Backend Runtime

- [x] 2.1 Route upload creation to `upload_jobs` and `upload_items`.
- [x] 2.2 Route one-shot external imports only through `import_jobs`.
- [x] 2.3 Route durable external source setup through `connections`.
- [x] 2.4 Route every durable source execution through `sync_runs`.
- [x] 2.5 Split ARQ queues by domain. ISSUE-061 source implementation now uses `etl`/`imports`/`synchronize` and `upload_worker`/`import_worker`/`synchronize_worker`; this is not a deployed cutover receipt.
- [x] 2.6 Move GitHub webhook sync out of API in-process background tasks. Webhook enqueues `execute_synchronize_github_pull` on Synchronize; disposable Redis/ARQ dispatch and dedup gates pass.
- [x] 2.7 Stop creating new `import_once` connector or sync bindings.

## 3. Frontend

- [x] 3.1 Present Add content as Upload and Import. (Default + menu now surfaces Upload files + Import from URL + Import from a source.)
- [x] 3.2 Present durable source setup as Connect.
- [x] 3.3 Present Git remote, CLI, Agent, MCP, and Sandbox as Access. (Git remote/CLI/MCP present; Sandbox opened; Agent intentionally OFF per product decision — `AI_AGENT_ENABLED=false`.)
- [x] 3.4 Show upload, import, and sync activity through the aggregation view. (Import + Upload widgets now read `/api/v1/activity` like Sync.)
- [x] 3.5 Hide one-shot imports from Access surfaces.

## 4. Validation

- [x] 4.1 Add migration tests or Supabase reset coverage for the target schema. The real local runtime harness installs and replays the committed schema in fresh PostgreSQL through the normal migration runner; non-empty old-release upgrade remains separate.
- [x] 4.2 Add backend tests for ImportJob versus SynchronizeBinding lifecycle. Admission, service/worker rejection, model boundaries and real CLI HTTP gates cover separate ownership.
- [x] 4.3 Add worker tests for import timeout and cancellation finalization. Import job, queue-cutover and Upload lifecycle suites pass; shared infrastructure does not imply shared lifecycle policy.
- [ ] 4.4 Add frontend tests for the separated product entry points.

## 5. ISSUE-060/061 local integration (2026-10-03)

- [x] 5.1 Integrate 053 security baseline and committed 058/059 Synchronize/Access contracts without altering published migration artifacts or the historical API fixture.
- [x] 5.2 Route CLI Import/Synchronize/Access to their owning canonical APIs; reject ignored/ambiguous options and never fall back to legacy mutations.
- [x] 5.3 Verify actual CLI, Desktop and Web resource clients over loopback HTTP, real Redis/ARQ dispatch, backend regression, Web tests/types, CLI unit and wheel ownership.
- [ ] 5.4 Collect actual producer-stop, queue drain/recovery and deployed-version observations before declaring ISSUE-061 A7 passed. Local integration is not release authorization.

The CLI HTTP gate substitutes storage/credential IO and authentication identity,
not routing/application services/Project authorization. It does not certify
installed schema, live Provider credentials, production queues or deployment.
See `docs/cli/ENTRYPOINTS-UNRELEASED.md` for supported source pairing and rollback
requirements. ISSUE-049 owns remaining physical migrations; 058 owns legacy HTTP
retirement and remaining GitHub/database-source contracts.

## 6. Real local runtime follow-up (2026-10-03)

- [x] 6.1 Boot actual PostgreSQL/Auth/PostgREST/Redis/MinIO, API/MCP and three independent worker processes without dependency overrides or hosted credentials.
- [x] 6.2 Repair runtime findings: Import URL-to-neutral-source projection; MCP/Sandbox Scope `max_mode` and explicit Project-root handling. Add focused regression tests.
- [x] 6.3 Exercise actual CLI create/read/cancel/idempotency, manual Synchronize failure/retry, raw Upload completion/abort/queued finalization, scoped MCP IO and foreign-user denial; verify persisted content and commit/run identities.
- [x] 6.4 Stop local admission, verify queued work is not consumed by sibling workers, restart app/workers and PostgreSQL/Redis/MinIO, then reconcile durable results and read-only drain.

`scripts/test_entrypoint_local_runtime.py` reproduces these checks on fresh local
volumes. This does not satisfy 5.4's old-release/cloud cutover: no old serialized
dispatch was migrated, no OAuth/paid OCR was exercised and no in-flight SIGKILL
recovery was claimed. Continue local verification before any cloud migration.
