# Connectors and Synchronize

This document is the single architecture source for connector boundaries and
the durable Synchronize runtime.

Connectors are second-level adapters under a product service. They can fetch
external data, expose a scoped filesystem protocol, or run a workspace runtime,
but they do not own product lifecycle, write paths, conflict policy, or version
semantics.

## Product Boundary

Product entry points are named by product resource:

- Upload
- Import
- Synchronize
- Access

Connector is an implementation capability, not a product entry point. A
connector may be used by one or more services, but it must not decide which
product flow is being executed.

Implementation classes may use service names such as `SynchronizeService`, but
the product resource and architecture boundary are named `Synchronize`.

```text
Product service
  (Upload / Import / Synchronize / Access)
        |
        v
Connector
  (data provider / filesystem / AI runtime / sandbox adapter)
        |
        v
ProductOperationAdapter or Version Engine write command
        |
        v
VersionWriteEngine
```

Agent features may run through Sandbox or other Access connectors, but Agent is
not a fifth product entry point.

## Connector Rules

Connectors produce capabilities and typed content intents for their owning
service. They do not publish refs directly and do not implement conflict
semantics.

Connector responsibilities:

- Fetch external resources.
- List selectable provider resources.
- Expose scoped workspace protocols or runtimes for Access.
- Normalize files and metadata.
- Return provider-shaped content or relative files.
- Let the owning product service write through Version Engine.

Connector non-goals:

- No connector-specific version protocol.
- No connector-specific history table.
- No connector-side last-writer-wins outside Version Engine.
- No connector-owned product lifecycle.

## Synchronize Runtime

Synchronize is the durable relationship between a project and an external
source. Synchronize sync execution uses `platform.synchronize` only:

- `SynchronizeRepository`
- `SynchronizeService`
- `SynchronizeEngine`

Do not add compatibility routes, `SyncService`, `SyncRepository`, or
`SyncEngine` back under `provider`.

Runtime flow:

```text
Synchronize
  -> connector.fetch(config, credentials)
  -> optional provider materializer
  -> SynchronizeEngine
  -> Version Engine write_bytes / bulk_write
  -> connection state update
```

The connector fetches provider-shaped data. The materializer converts that data
into a stable PuppyOne file layout. `SynchronizeEngine` mounts the result under
`connections.target_path` and writes through Version Engine. Version Engine is
the only publish authority.

`connections.scope_id` may exist physically for root ownership, but application
code must not use it to derive integration write paths. Synchronize write paths
come from `connections.target_path`.

## Materialization

PuppyOne owns how provider data becomes workspace files. SaaS providers should
not expose raw storage knobs to users.

Materialization schemas are versioned independently from the HTTP API:

```json
{
  "id": "puppyone.gmail.thread_markdown",
  "version": 1
}
```

New connections pin the latest schema in `connections.config`:

```json
{
  "materialization_schema": {
    "id": "puppyone.gmail.thread_markdown",
    "version": 1
  }
}
```

Publishing a v2 schema does not change existing connections. Existing
connections should be migrated explicitly if their pinned schema changes.

Every SaaS materialization should include:

```text
_meta/source.json
index.json
```

`_meta/source.json` records provider, schema id, schema version, connection id,
sync time, and source hash. `index.json` is the machine-readable lookup surface.

Examples:

- Gmail writes thread Markdown files plus an index.
- Google Docs writes Markdown documents plus an index.
- Google Sheets writes workbook metadata, CSV sheets, and sheet schemas.
- Google Calendar writes daily Markdown event groups plus an index.
- Google Drive writes a manifest until full file export is enabled.

Databases are not automatic SaaS document feeds. A database connection is a
live data access relationship with catalog, preview, and explicit
snapshot/export actions.

## SaaS Source Setup

OAuth-backed SaaS setup should feel like selecting a resource from an
authorized external system, not like pasting arbitrary URLs into a form. A raw
URL is only a source identity for non-OAuth Web Page imports.

Every SaaS source setup uses two stages:

1. Source authorization and resource selection.
2. Source-specific import options plus PuppyOne destination.

The first Add sync screen chooses the provider and verifies authorization. The
second screen configures the selected source. For OAuth providers, source
selection should be a provider resource picker, not a raw URL field.

OAuth provider URLs are display/backlink metadata. They are not accepted as the
authoritative source identity. If a future URL shortcut is added, it must
resolve through OAuth into the same canonical `source.resource_id` shape before
save.

## Config Contract

Connection config separates source identity from import options:

```json
{
  "source": {
    "provider": "google_sheets",
    "resource_type": "spreadsheet",
    "resource_id": "1abc...",
    "resource_name": "Pipeline Metrics",
    "resource_url": "https://docs.google.com/spreadsheets/d/1abc.../edit",
    "account_label": "user@example.com"
  },
  "options": {
    "sheet_names": [],
    "max_rows_per_sheet": 10000
  }
}
```

All new SaaS provider configs use:

```json
{
  "source": {},
  "options": {}
}
```

`source` identifies the external resource. `options` controls how PuppyOne
imports that resource.

Legacy flat keys such as `source_url`, `site_url`, `days_past`, and
`max_results` are removed from the integration contract. Existing flat-config
rows should be migrated or recreated. New API writes and connector fetch code
must use the structured shape only.

Provider config validation lives in `platform.synchronize.config_contract`.
API routers must call that contract instead of embedding config shape rules
inline.

Database columns such as `connections.external_resource_id`,
`connections.external_resource_label`, and `connections.external_url` are
derived from `config.source`. They are indexes/display mirrors, not independent
configuration inputs.

Destination path, trigger mode, schedule, status, and sync bookkeeping are not
provider config. They live on the connection row or related trigger fields.

## Auth Naming

The codebase has two related OAuth concepts:

- `oauth_type`: backend credential resolver key, used by runtime execution.
- `oauth_ui_type`: frontend OAuth popup/status key, used by setup UI.

These must stay explicit. Do not infer UI OAuth routes from credential resolver
names.

Example:

```python
ConnectorSpec(
    provider="google_sheets",
    auth=AuthRequirement.OAUTH,
    oauth_type="sheets",
    oauth_ui_type="google_sheets",
)
```

Frontend code should use `oauth_ui_type || oauth_type || provider` for
authorization status and popup flows. Backend execution should use only
`oauth_type`.

## Resource Picker

OAuth-backed providers that expose selectable resources implement:

```text
GET /api/v1/integrations/providers/{provider}/resources
```

Query params:

- `q`: optional search term
- `cursor`: optional pagination cursor
- `resource_type`: optional provider-specific filter

Response:

```json
{
  "resources": [
    {
      "id": "1abc...",
      "type": "spreadsheet",
      "name": "Pipeline Metrics",
      "url": "https://docs.google.com/spreadsheets/d/1abc.../edit",
      "subtitle": "Modified yesterday",
      "icon": "google_sheets",
      "authorized": true
    }
  ],
  "next_cursor": null
}
```

Resource pickers must use the same OAuth credential resolver path as sync
execution. If credentials are missing or expired, the picker returns an
authorization-required state rather than pretending the provider has no
resources.

Google Workspace resource pickers share the Drive files listing helper under
`provider.google_workspace.resources`.

## Provider Config Reference

Provider auth model:

| Provider | Auth model | Primary source selection |
| --- | --- | --- |
| Gmail | OAuth | Authorized mailbox/account plus filters |
| Google Calendar | OAuth | Calendar picker |
| Google Docs | OAuth | Document picker |
| Google Sheets | OAuth | Spreadsheet picker |
| Google Search Console | OAuth | Property picker |
| Google Drive | OAuth | Drive file/folder picker |
| GitHub | Optional OAuth | Repository picker when enabled |
| Web Page | No OAuth | URL is the source identity |

### Gmail

```json
{
  "source": {
    "provider": "gmail",
    "resource_type": "mailbox",
    "resource_id": "me",
    "resource_name": "Gmail",
    "account_label": "user@example.com"
  },
  "options": {
    "query": "in:inbox",
    "max_results": 50
  }
}
```

Gmail should not ask for a URL.

### Google Calendar

```json
{
  "source": {
    "provider": "google_calendar",
    "resource_type": "calendar_set",
    "resource_id": "selected",
    "resource_name": "Selected calendars",
    "account_label": "user@example.com"
  },
  "options": {
    "calendar_ids": ["primary"],
    "days_past": 30,
    "days_future": 30,
    "max_results": 100
  }
}
```

Calendar sync should require explicit calendar selection. It should not
silently fetch all calendars unless saved config explicitly selects all
calendars.

### Google Docs

```json
{
  "source": {
    "provider": "google_docs",
    "resource_type": "document",
    "resource_id": "1doc...",
    "resource_name": "Product Plan",
    "resource_url": "https://docs.google.com/document/d/1doc.../edit",
    "account_label": "user@example.com"
  },
  "options": {}
}
```

### Google Sheets

```json
{
  "source": {
    "provider": "google_sheets",
    "resource_type": "spreadsheet",
    "resource_id": "1sheet...",
    "resource_name": "Revenue Model",
    "resource_url": "https://docs.google.com/spreadsheets/d/1sheet.../edit",
    "account_label": "user@example.com"
  },
  "options": {
    "sheet_names": [],
    "max_rows_per_sheet": 10000
  }
}
```

### Google Search Console

```json
{
  "source": {
    "provider": "google_search_console",
    "resource_type": "site_property",
    "resource_id": "sc-domain:example.com",
    "resource_name": "example.com",
    "account_label": "user@example.com"
  },
  "options": {
    "date_range": "28d",
    "dimensions": ["query", "page"],
    "row_limit": 500
  }
}
```

The UI must not default to a freeform `site_url` field.

### Google Drive

```json
{
  "source": {
    "provider": "google_drive",
    "resource_type": "folder",
    "resource_id": "1folder...",
    "resource_name": "Research",
    "resource_url": "https://drive.google.com/drive/folders/1folder...",
    "account_label": "user@example.com"
  },
  "options": {
    "recursive": true,
    "max_results": 50,
    "include_mime_types": []
  }
}
```

Google Drive is hidden from the integration picker until its product behavior
is intentionally enabled.

### GitHub

GitHub is `optional_oauth`: public repositories can work without OAuth, private
repositories require authorization.

```json
{
  "source": {
    "provider": "github",
    "resource_type": "repository",
    "resource_id": "puppyone-ai/puppyone-cloud",
    "resource_name": "puppyone-ai/puppyone-cloud",
    "resource_url": "https://github.com/puppyone-ai/puppyone-cloud",
    "account_label": "puppyone-ai"
  },
  "options": {
    "ref": "main",
    "include_paths": [],
    "exclude_paths": []
  }
}
```

### Web Page

Web Page is not OAuth-backed. The URL is the source identity.

```json
{
  "source": {
    "provider": "url",
    "resource_type": "web_page",
    "resource_id": "https://example.com/docs",
    "resource_name": "https://example.com/docs",
    "resource_url": "https://example.com/docs"
  },
  "options": {
    "crawl_options": {}
  }
}
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
