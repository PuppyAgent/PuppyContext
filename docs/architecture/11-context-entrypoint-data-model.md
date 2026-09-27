# Context Entry Point Data Model

This is the target data model for the four ways context enters, changes, or is
exposed from a PuppyOne workspace:

- Upload: local bytes enter once.
- Import: an external snapshot enters once.
- Synchronize: a durable external service, system, or local sync relationship is
  created.
- Access: a scoped workspace surface is exposed to a person, tool, or runtime.

This model separates product entry points from lower-level write labels. A
Version Engine `source_channel` records where one committed mutation came from;
it is not the product entry-point model.

The additive target migration is:

```text
supabase/migrations/20260602010000_context_entrypoint_target_tables.sql
```

It does not drop legacy tables. Runtime code should migrate onto these tables
in stages while old rows remain readable.

## Final Tables

| Product concept | Table | Purpose |
| --- | --- | --- |
| Upload | `upload_jobs` | One local upload task and its lifecycle |
| Upload | `upload_items` | Per-file state for an upload task |
| Import | `import_jobs` | Existing canonical one-shot external import task |
| Synchronize | `connections` | Durable integration relationship and sync configuration |
| Synchronize | `sync_runs` | One execution of an integration |
| Access | `access_surfaces` | Project-root or Scope-targeted workspace entry points |
| Activity | `context_activity_items` | Read-only aggregation of upload, import, and sync history |

## Upload

`upload_jobs` replaces the overloaded use of the legacy `uploads` table for
local file and folder ingestion.

Required properties:

- `project_id`, `created_by`, and `target_path` identify the destination.
- `source_kind` is `browser`, `desktop`, or `cli`.
- `mode` is `raw`, `ocr_parse`, or `structured`.
- `status`, `phase`, `progress`, `message`, and `error_message` are owned by
  the upload pipeline.
- `result_commit_id` records the Version Engine write that finalized the job.

`upload_items` holds per-file upload and processing state. This keeps upload
policy decisions, skipped files, object-storage staging, and ETL failures out of
the higher-level job row.

Upload jobs have no provider, no OAuth binding, no schedule, and no durable
external relationship.

File upload and folder upload both belong here. A folder upload is a one-shot
copy of selected local files. Local folder synchronize belongs to Synchronize
because it keeps a durable relationship with a filesystem client.

## Import

`import_jobs` remains the canonical table for one-shot external snapshots.

The target migration adds:

- `source_kind`: repository, URL, website, template, document, or other.
- `source_ref`: structured provider-specific source identity.
- `idempotency_key`: optional key for preventing duplicate user-triggered
  imports.

Import jobs own task lifecycle. Providers only supply capabilities such as
parse, fetch, list, verify, or push. A provider must not create job rows or mark
task status directly.

Examples include GitHub repository snapshots, URL or website snapshots, one-shot
document/page imports, templates, and future external snapshot providers. None
of these should create a durable sync binding unless the user chose Synchronize.

## Synchronize

`connections` is the current table for durable integration relationships. The
product category is Synchronize; the table name remains `connections` as an
implementation and migration detail.

An integration stores:

- Provider and external resource identity.
- Optional OAuth or credential reference.
- Direction: inbound, outbound, or bidirectional.
- `target_path`: the project-root destination path for Synchronize writes.
- Trigger type and trigger config.
- Cursor, watermark, remote hash, external version, and last sync result.
- Lifecycle status: active, paused, syncing, error, or disabled.

A durable integration is configuration plus durable state. It is not a run and
it is not an import.

Synchronize rows are not path-permission scopes. A connection may keep a
`scope_id` only for rollout compatibility or root association; that field must
not define where Google/GitHub/Gmail/Search Console data is written. The write
destination is `target_path`, and execution reaches the same project-root
Version Engine write boundary used by frontend edits.

`sync_runs` is the final table for integration executions. Each row records:

- The integration connection record and project.
- Trigger source: manual, scheduled, webhook, realtime, initial, or push.
- Optional user who triggered the run.
- Direction, status, phase, progress, and worker job id.
- Result path, Version Engine commit id, and changed-file summary.

The legacy `connector_runs` table is migration input for historical run
backfill. Runtime durable source synchronization uses `connections` and
`sync_runs`.

Connector is a second-level implementation concept under a service. Synchronize
connectors may represent GitHub, Google Drive, Gmail, databases, local
filesystem sync, and future providers. They supply capabilities; the
Synchronize service owns lifecycle, sync runs, and final write semantics.

## Access

`access_surfaces` is the final table for workspace entry points.

Access surfaces are repository-target-bound and permissioned. `scope_id =
NULL` means Project root; a non-NULL value references one real non-empty-path
`repository_scopes` row. The canonical product
families are:

- `git_remote`
- `cli` / AP-FS
- `sandbox`

Access is about how a person, tool, or runtime can operate on workspace context.
It is not an external-source import mechanism.

Access connectors are second-level adapters under the Access service. Product
families should stay Git, CLI/AP-FS, and Sandbox even when implementation names
are more specific.

Agent is not a top-level entry point and should not be modeled as one of the
four product concepts. If the product exposes an Agent, it is a runtime or
feature operating through an Access surface, typically Sandbox. Implementation
or legacy rows may still carry names such as `agent`, `mcp`, or `filesystem`
during migration, but those names are not product taxonomy.

The ISSUE-039 Contract migration maps legacy root associations to NULL and
renames the remaining non-root geometry table to `repository_scopes`. Runtime
code reads `access_surfaces` plus the explicit target union; synthetic root
rows and legacy connector identities are not part of the final runtime model.

Built-in access surfaces that should be unique per target are constrained with
NULL-safe uniqueness. Product docs should present the
families as Git, CLI/AP-FS, and Sandbox even if the migration keeps compatibility
rows for older implementation names.

## Activity View

`context_activity_items` is a read-only aggregation view for the frontend. It
normalizes upload jobs, import jobs, and sync runs into a shared activity shape:

```text
id
kind: upload | import | sync_run
project_id
created_by
label
status
phase
progress
message
error_message
result_path
result_commit_id
created_at
completed_at
```

The view is an activity surface only. It must not become a write model or force
Upload, Import, and SyncRun into one lifecycle.

## Migration Rules

1. Do not create new `import_once` sync bindings.
2. One-shot external sources create `import_jobs`.
3. Local files and folders create `upload_jobs` and `upload_items`.
4. Durable external source relationships create `connections`.
5. Every integration execution creates a `sync_runs` row.
6. Access surfaces create `access_surfaces`, not import jobs.
7. Long-running upload, import, and sync work must run through worker queues.
8. All final content writes must go through the Version Engine.


## Phase-one implementation boundary

ISSUE-048 organizes backend source under `platform/upload`, `platform/imports`,
`platform/synchronize`, `platform/access`, and shared `provider`.
`platform/synchronize/github` owns durable GitHub binding/pull/push; one-time
GitHub snapshots use Import and `provider/github`. Git access to PuppyOne remains
an Access transport through the existing Version Engine.

Public `/integrations`, `/connectors`, `/access`, `/db-connector`, `/oauth` and
`/ingest` contracts remain stable. Physical table/column names are unchanged.
The GitHub job still uses the imports queue and the old serialized job name;
its dispatcher delegates to `execute_github_sync_pull`. This preserves pending
and retry jobs during mixed-version rollout. Database cleanup is ISSUE-049.
The canonical implementation map and compatibility ledger are maintained in
`puppy-issues:document/puppyone/architecture/entrypoints/phase-one-implementation.md`.
