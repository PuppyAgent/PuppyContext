# Context Resources

PuppyOne has four top-level resource services for bringing context into a
workspace or exposing context from it:

- Upload
- Import
- Synchronize
- Access

Everything else is a child resource, adapter, execution, or write tag. In
particular:

- A connector is a second-level adapter under a service.
- Synchronize is an action/execution under Synchronize.
- An access surface is a child resource under Access.
- `source_channel` is a Version Engine write tag, not a product resource.

## Resource Map

```text
Context Resources
├── Upload service
│   ├── UploadJob
│   ├── UploadItem
│   ├── ETLTask
│   ├── files / folders / OCR / structured extraction
│   └── Version Engine write: source_channel=upload
│
├── Import service
│   ├── ImportJob
│   ├── import connector
│   │   ├── GitHub repository snapshot
│   │   ├── URL / website snapshot
│   │   ├── Notion / document / template snapshot
│   │   └── future one-shot providers
│   └── Version Engine write: source_channel=import
│
├── Synchronize service
│   ├── connection record
│   ├── SyncRun
│   ├── target_path: project-root write destination
│   ├── integration connector
│   │   ├── GitHub branch / repository binding
│   │   ├── Google Drive / Gmail / Docs / Sheets
│   │   ├── database / SaaS provider
│   │   └── local filesystem sync client
│   ├── synchronize action
│   │   ├── manual
│   │   ├── scheduled
│   │   ├── webhook
│   │   └── realtime
│   └── Version Engine write: source_channel=sync
│
└── Access service
    ├── access surface
    ├── access connector
    │   ├── Git remote
    │   ├── CLI / AP-FS
    │   ├── Sandbox
    │   └── Agent runtime connector
    ├── scope / identity / permission boundary
    └── Version Engine write:
        ├── source_channel=access_git
        ├── source_channel=access_cli
        └── source_channel=access_sandbox
```

## Upload

Upload is a one-shot transfer of local bytes into the workspace.

Examples:

- Upload one or more files from the browser.
- Upload a local folder once.
- Drop files into the Data view.
- Upload a PDF/image/document and optionally run OCR or ETL.

Upload owns transfer and processing state:

```text
UploadJob
  -> UploadItem
  -> optional ETLTask
  -> staged object storage
  -> Version Engine write
```

Upload has no provider binding, no OAuth binding, no schedule, and no durable
relationship with the local machine. Folder upload is still Upload. Local-folder
synchronize belongs to Synchronize because it keeps a durable relationship with
a sync client.

## Import

Import is a one-shot external snapshot copied into the workspace.

Examples:

- Import a GitHub repository URL once.
- Import a website or URL crawl once.
- Import a Notion page or document once.
- Import starter or template content.

Import owns task lifecycle:

```text
ImportJob
  -> import connector.fetch(...)
  -> Version Engine write
  -> completed | failed | cancelled
```

The import connector supplies provider-specific fetch capability. It must not
own job status, retries, product navigation, or version semantics. Import has no
pause/resume sync controls, no webhook, and no long-lived external binding.

## Synchronize

Synchronize is a durable relationship between the workspace and an external
service, external system, or local filesystem sync client.

Examples:

- Bind a GitHub repository branch and keep it synchronized.
- Integrate Google Drive and sync selected files.
- Integrate Gmail and refresh messages manually or on a schedule.
- Integrate a database and save query results.
- Integrate a local folder through the filesystem sync client.

Synchronize owns configuration and durable sync state:

```text
Synchronize service
  -> connection record (provider, external resource, target_path)
  -> SyncRun
  -> integration connector.fetch(...) / connector.push(...)
  -> Version Engine root write or external write
  -> update cursor / watermark / remote hash / run history
```

Synchronize is not a top-level product resource. It is an action on an
integration. Each execution is a `SyncRun`, triggered manually, by schedule, by
webhook, by realtime events, or by a push path.

The current implementation table is named `connections`. That is an
implementation and migration detail; the product category is Synchronize.

`connections.target_path` is the project-root destination path for fetched data.
It is not an Access scope. During rollout a connection row may still carry a
root `scope_id` or a historical scope fallback, but Synchronize write routing
must use `target_path` plus the Version Engine root write boundary.

## Access

Access exposes a scoped way for a person, tool, or runtime to operate on
workspace context. Access is about operating on existing workspace data, not
fetching external source data.

Canonical Access families:

- Git remote
- CLI / AP-FS
- Sandbox

Access owns permissioned workspace surfaces:

```text
Access service
  -> access surface
  -> scope / identity / permission boundary
  -> Git, CLI/AP-FS, or Sandbox connector
  -> Version Engine read/write path
```

An access surface resolves to:

- `project_id`
- `scope_path`
- `exclude` rules
- `mode` (`r` or `rw`)
- optional identity binding
- channel pause state

External examples:

```text
Git Remote root locator:
  https://<host>/git/{project_id}.git

Git Remote scoped locator:
  https://<host>/git/{project_id}/scopes/{scope_id}.git

Git credential:
  separate HTTP Basic password; never part of the locator

CLI / AP-FS:
  /api/v1/ap-fs/*
  X-Access-Key: <access_key>

Sandbox:
  scoped runtime session mounted on a workspace view
```

The URL, credential, and legacy migration rules are owned by
[Git Remote Locator, Credential, And Access Point Contract](05-git-remote-accesspoint.md).

All Access families submit reads and writes through the same Version Engine
boundary. They may behave externally like separate scoped workspaces, but
internally they share the project object store and project history.

Agent is not a top-level resource. Agent features may operate through Sandbox or
another Access connector. The architecture classifies the context operation as
Access and the write tag as the underlying Access channel, typically
`access_sandbox`.

## Connector Boundary

Connector is the reusable adapter layer below a service. It answers provider or
runtime-specific questions:

- How do I fetch data from this provider?
- How do I push data back?
- How do I expose a scoped filesystem protocol?
- How do I mount a scoped runtime?

The service answers product questions:

- Is this one-shot or durable?
- Which lifecycle table owns status?
- Is this Upload, Import, Synchronize, or Access?
- Which actor, permission boundary, and write tag should reach the Version
  Engine?

Good boundary:

```text
ImportJob -> GitHub connector -> fetched files -> Version Engine
Synchronize -> GitHub connector -> SyncRun -> Version Engine
Access -> Git connector -> scoped push -> Version Engine
```

Bad boundary:

```text
GitHub connector creates ImportJob
GitHub connector decides this is Synchronize
Sandbox connector owns product history
Connector publishes Version Engine commits directly
```

## Version Engine Boundary

All four services converge at the Version Engine. The Version Engine owns:

- path normalization at the write boundary
- CAS, merge, conflict, and commit publication
- history and audit rows
- outbox / notification behavior
- final project tree state

The services own user intent and lifecycle. Connectors own provider/runtime
capability. `source_channel` only labels the final write.


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
