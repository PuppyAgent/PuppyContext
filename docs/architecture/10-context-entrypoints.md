# Context Entry Points

This document defines the four product concepts PuppyOne uses when context
enters, changes, or is exposed from a workspace:

- Upload
- Import
- Synchronize
- Access

These words should not be used interchangeably. They describe different user
intents, different backend models, and different execution guarantees.

This taxonomy is product-level. A lower-level write label such as
`source_channel` must not replace it. `source_channel` is a Version Engine audit
and conflict-policy tag for an individual write; the entry point is the user
intent that created the work.

## Summary

| Concept | User intent | Durable relationship? | Typical backend owner |
| --- | --- | --- | --- |
| Upload | Add local files or folders into a workspace | No | Upload / ETL pipeline |
| Import | Copy a snapshot from an external source | No | ImportJob |
| Synchronize | Bind an external service or system so it can be synced later | Yes | Synchronize service |
| Access | Expose a workspace scope to a person, tool, or runtime | Yes | access surfaces |

All four concepts may write to the Version Engine, but none of them owns
version semantics. Every write must enter through a Version Engine write
intent or ProductOperationAdapter command.

## Upload

Upload means the user is adding local bytes from their machine into the
workspace.

Examples:

- Upload files from the browser.
- Upload a local folder.
- Drop files into the Data view.
- Upload a PDF and optionally run OCR / ETL.

Upload is not a connector. It is not a durable relationship with the user's
machine. It is a one-shot transfer from a local source.

Folder upload is still Upload. It copies the selected folder contents once. It
must not be confused with local-folder synchronize, which is an Synchronize flow
because it creates an ongoing relationship with a local filesystem client.

Upload is allowed to have its own pipeline because it has concerns that other
flows do not:

- Browser file and folder selection.
- Direct-to-S3 or multipart transfer.
- Upload policy, ignored folders, blocked files, duplicate paths, and limits.
- OCR, structured extraction, and post-processing.
- Finalization from staged object storage into the Version Engine.

Product placement:

- Primary entry: Add content -> Upload files/folder.
- Empty workspace entry: Upload folder can initialize the workspace.
- Existing workspace entry: Upload into the current folder or selected target.

Backend shape:

```text
UploadJob / UploadItem / ETLTask
  -> staged object storage
  -> optional OCR / ETL
  -> Version Engine write
```

## Import

Import means the user is copying a snapshot from an external source into the
workspace without creating an ongoing relationship.

Examples:

- Import a GitHub repository URL as a one-time snapshot.
- Import a website or URL crawl.
- Import a Notion page once.
- Import starter/template content.
- Future one-shot providers that copy an external snapshot into the workspace.

Import is not the same thing as initialization. Initialization is just the
first successful content write in an empty workspace. An empty workspace can be
initialized by upload, import, integration sync, or start blank.

Import should use provider capabilities when useful, but the provider must not
own task status. The import job owns lifecycle.

Backend shape:

```text
ImportJob
  -> resolve provider capability
  -> provider.fetch(...)
  -> Version Engine write
  -> terminal status: completed | failed | cancelled
```

Import has no pause/resume sync controls, no schedule, no webhook, and no
long-lived external binding. It can expose progress and result history as task
history, but it should not appear as an Access surface.

Product placement:

- Primary entry: Add content -> Import from ...
- Empty workspace entry: Import from GitHub / website / URL / template.
- Activity entry: show active and recent import jobs.

## Synchronize

Synchronize means the user is creating a durable relationship between the
workspace and a third-party service, external system, or local filesystem sync
client. Synchronize is the action that happens through that relationship;
Synchronize is the product-level service category.

Examples:

- Integrate a GitHub repository branch.
- Integrate Google Drive and sync selected files.
- Integrate Gmail and refresh messages manually or on a schedule.
- Integrate a database and save query results.
- Integrate a local folder through the desktop / filesystem synchronize path.

An integration is not itself a run. It is configuration plus state:

- Provider and external resource identity.
- OAuth or credential reference.
- Direction: inbound, outbound, or bidirectional.
- Trigger: manual, scheduled, webhook, or realtime.
- Cursor, watermark, remote hash, or last synced commit.
- Lifecycle status: active, paused, error.

Each execution is a SyncRun.

Synchronize is an action on an integration. It is not an Access surface and it is
not an Agent category. Agent-powered product features may trigger work, but they
must still enter this model as either Access-mediated workspace operations or as
ordinary product workflows; they should not become a fifth entry point.

Backend shape:

```text
Synchronize
  -> connection record
  -> SyncRun(triggered_by=manual | scheduled | webhook | realtime)
  -> provider.fetch(...) or provider.push(...)
  -> Version Engine write or external write
  -> update watermark / cursor / run history
```

Synchronize may run an initial sync after creation, but that initial sync is still
a SyncRun. It should not be represented as an ImportJob unless the user chose a
one-shot import instead of a durable integration.

Product placement:

- Primary entry: Synchronize.
- Per-integration actions: Sync now, Pause, Resume, Disconnect, View runs.
- Status surfaces: Needs Action, failed runs, last synced time.

## Access

Access means the workspace exposes a scoped way for a person, tool, or runtime
to read or write context. Access is about how something operates on existing
workspace context. It is not about fetching external source data.

Canonical Access surface families:

- Git remote URL for a scope.
- CLI / AP-FS access for scoped filesystem commands.
- Sandbox access for an isolated runtime mounted on a scoped workspace view.

Agent is not a top-level entry point and should not be used as the name of an
Access family. If the product exposes an Agent, the Agent is a product/runtime
capability that operates through an Access surface, commonly Sandbox. It may be
shown in UI as an Agent feature, but architecture should classify the context
operation by the underlying Access surface and write source.

Access surfaces are backed by `access_surfaces` and scoped by the project access
model. Legacy connector rows and the pre-ISSUE-039 `repo_scopes` table are migration input and
compatibility history; runtime Access should be described as an entry point into
workspace context, not an import mechanism.

Backend shape:

```text
access_surfaces
  -> access key / identity / permission boundary
  -> Git, CLI/AP-FS, or Sandbox surface
  -> Version Engine read/write path
```

Access can be paused, scoped, permissioned, and audited. It should not create
ImportJob rows. It should not be shown for one-shot imports.

Product placement:

- Primary entry: Access workspace.
- Per-scope settings: Git remote, CLI/AP-FS, Sandbox.
- Access health and permissions live near scope/project settings.

## Source Channels

`source_channel` is a second-level write tag, not a product entry point. It is
attached when a final write reaches the Version Engine so history, audit,
notifications, and conflict policy can tell where that specific mutation came
from.

Target write channels should be precise enough to preserve intent:

- `upload` for upload finalization writes.
- `import` for ImportJob writes.
- `sync` or provider-specific sync channels for SyncRun writes.
- `access_git` for Git remote Access writes.
- `access_cli` for CLI/AP-FS Access writes.
- `access_sandbox` for Sandbox Access writes.
- `web` or `papi` for ordinary product UI/API writes that are not one of the
  four entry-point jobs.

These tags are allowed to be more granular than the four product concepts, but
they must not create new product concepts. For example, `access_sandbox` can
represent a sandbox write caused by an Agent feature; the product taxonomy is
still Access, not Agent.

## Provider Capabilities

Providers and connectors are implementation capabilities, not product flows.
A connector is a second-level adapter owned by a service. It can fetch data,
push data, expose a scoped filesystem protocol, or run a workspace runtime, but
the owning service decides whether the user is uploading, importing,
integrating, or accessing.

Examples:

- Import service may use a GitHub, URL, Notion, or template connector once.
- Synchronize service may use GitHub, Google Drive, Gmail, database, or local
  filesystem sync connectors over time.
- Access service may use Git, CLI/AP-FS, Sandbox, or AI/runtime connectors to
  expose a scoped workspace surface.

Connector code must not own job lifecycle, product navigation, or final version
semantics. It should provide capabilities to the service that invoked it.

A provider may support one or more capabilities:

- list resources
- fetch content
- push content
- verify credentials
- create webhook
- parse source URLs

The provider layer must not decide whether a user is uploading, importing,
integrating, or accessing. The service orchestrator decides that.

Good boundary:

```text
GithubProvider.fetch_repo_archive(...)
GithubProvider.fetch_branch_tree(...)
GithubProvider.push_branch_commit(...)
```

Bad boundary:

```text
GithubProvider.create_import_job(...)
GithubProvider.mark_sync_failed(...)
GithubProvider.publish_version_commit(...)
```

## GitHub Example

GitHub has multiple product meanings. They must stay separate.

| User action | Product concept | Backend model |
| --- | --- | --- |
| Paste `https://github.com/org/repo` and copy files once | Import | ImportJob(provider=github) |
| Bind this project to `org/repo` branch `main` | Synchronize | GitHub connection record + SyncRuns |
| Push and pull through PuppyOne Git remote | Access | repo_scope Git access surface |
| Upload a local cloned repo folder | Upload | Upload pipeline; `.git/` is skipped |

There should not be a third product path where `connectors[github]` creates an
`import_once` sync binding. That combines Import and Synchronize and makes the
UI, run history, and deployment model ambiguous.

## Target Execution Model

Long-running work should run outside the HTTP request path.

Worker split:

```text
api
upload_worker   -> upload finalize, OCR, ETL
import_worker   -> ImportJob execution
sync_worker     -> SyncRun execution
scheduler       -> schedules work, enqueues SyncRuns, does not fetch content
```

ARQ can be the shared execution technology, but the queues should be separate:

```text
uploads
imports
syncs
```

This prevents a slow OCR backlog from blocking GitHub imports, and prevents a
large import from starving scheduled syncs.

## Activity And History

The frontend may show upload, import, and sync activity in one Activity UI, but
the underlying models should remain separate.

Unified activity item:

```text
ActivityItem
  id
  kind: upload | import | sync_run
  label
  status
  progress
  message
  created_at
  completed_at
  result_path
```

The activity UI is an aggregation layer. It must not force Upload, Import, and
SyncRun into one database table or one lifecycle model.

## Naming Rules

Use these names consistently:

- Use "Upload" only for local files/folders.
- Use "Import" only for one-shot external snapshots.
- Use "Synchronize" for durable external services, systems, or local sync
  relationships.
- Use "Sync" for executions or actions on an integration.
- Use "Access" for workspace entry points and permissioned surfaces.
- Use "connector" only as a second-level implementation adapter under a
  service.
- Avoid using "Connect" as the architecture name. It may remain a UI verb where
  appropriate, but the product category is Synchronize.
- Avoid showing one-shot imports inside Access.
- Avoid creating `import_once` sync bindings for new flows.

## Migration Direction

Current implementation should route runtime code through the target tables. The
migration rule is:

1. Backfill existing legacy rows into target tables.
2. Stop creating new `import_once` sync bindings.
3. Route one-shot external sources through ImportJob.
4. Route durable external relationships through Synchronize.
5. Route GitHub branch binding through the GitHub integration / connection
   model, not generic `connectors[github]`.
6. Route webhook-triggered GitHub sync through sync_worker as a SyncRun, not
   an in-process background task.
7. Hide one-shot import records from Access surfaces.
8. Keep all writes going through the Version Engine.

The target product taxonomy is:

```text
Add content
  Upload files/folder
  Import from external source

Integrations
  Create durable external relationship
  Sync now / scheduled sync / webhook sync

Access workspace
  Git remote / CLI/AP-FS / Sandbox
```


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
