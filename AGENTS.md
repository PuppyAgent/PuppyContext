# PuppyOne (ContextBase)

> **AI assistants working on this codebase**: the canonical Version Engine
> architecture is in
> [`docs/architecture/01-version-engine.md`](docs/architecture/01-version-engine.md).
> PuppyOne is Git-native at the version layer: stock `git` talks to a
> credential-free `/git/{project_id}.git` or
> `/git/{project_id}/scopes/{scope_id}.git` locator and supplies its separate
> Git credential through HTTP auth, while Web/API/`puppyone fs`
> writes converge through the Product Operation Adapter. Do not introduce the
> removed legacy wire protocol, construct the bounded legacy
> `/git/ap/<access_key>.git` URL in new code, introduce an external version
> package, or restore old source naming. The normative Git contract is
> [`docs/architecture/05-git-remote-accesspoint.md`](docs/architecture/05-git-remote-accesspoint.md).
>
> The canonical database release architecture is
> [`docs/architecture/13-database-release-governance.md`](docs/architecture/13-database-release-governance.md).
> Schema changes use `supabase/migrations`; non-transactional/application-code
> data changes use immutable `supabase/data_migrations` artifacts through the
> portable runner. Never hide an external script between schema migrations.

## Overview

PuppyOne is a **cloud file system built for AI Agents**, centered around two core pillars: **Connect** and **Collaborate**.

It aggregates information scattered across various sources into a unified Context Space, while providing a complete infrastructure for multi-party collaboration between humans and agents — authentication, access control, version history, audit logging, and backup/rollback. Through the file system, bash, and the MCP protocol, any agent can read and write this ContextBase just like a local file system.

### Connect

- **Multi-source data connectors** — OAuth connectors for 15+ platforms including Notion, GitHub, Gmail, Google Drive, Linear, Airtable, and more; also supports URL scraping, database connections, and custom scripts
- **Bidirectional local folder sync** — Real-time sync between local directories and the cloud Context Space via Git Remote and Puppyone CLI entry points
- **MCP protocol exposure** — Generates standard MCP interfaces for each agent or endpoint; any MCP-compatible client (Claude Desktop, Cursor, etc.) can connect directly
- **Code sandbox** — Securely execute code in isolated Docker/E2B containers; agents can invoke sandbox endpoints remotely

### Collaborate

- **Authentication & access control** — JWT for human users + Access Key for machine authentication; agent-level node access permissions
- **Version history & rollback** — File-level version management, arbitrary version diff comparison, one-click rollback; folder-level snapshots
- **Audit logging** — Records all operations (who did what to which node, and when), fully traceable
- **Collaborative editing** — Checkout/commit workflow, locking mechanism, conflict detection and resolution
- **Structured data management** — Cloud file system (folders/JSON/Markdown/files), JSON Pointer table operations

#### Authorization boundary

- Human access is resolved only by `backend/src/platform/authorization/` as
  `Organization context -> ProjectGrant -> optional child restriction`.
- `project_members` is the sole explicit Human Project role source. Organization
  membership supplies tenant context; an org-visible Project supplies only the
  Viewer baseline. Never use a Runtime key, Git remote, scope, or Agent
  visibility to create Human Project access.
- Machine entry points resolve a `RuntimeGrant` bounded by an explicit
  Project-root or Scope target, its resolved view, mode, policy, and credential
  status. A RuntimeGrant cannot enter Team, Billing,
  Project settings, members, sharing, or credential-management control planes.
- The canonical PuppyOne Git remote is the only local-to-Cloud locator. Desktop
  parses its Project-root or Scope target locally, then the current JWT must
  authorize that exact Project target before Cloud UI is shown. A Git URL is a
  locator, never authority. The Cloud does not register devices, folders, or
  checkouts, and legacy secret-bearing remotes never identify Cloud UI context.

### Platform

- **Agent management** — Create agents, bind tools, control access scope, SSE streaming chat
- **Full CLI coverage** — Every operation available via command line, enabling AI coding tools like Claude Code to drive the platform directly
- **Unified access management** — All access surface types (Git remote/CLI/agent/MCP/sandbox) are served through a single `/api/v1/access` entry point. `access_surfaces` targets Project root with `scope_id = NULL` or one real `repository_scopes` row; external source relationships live in `connections`.

## Active Development Directories

- **`backend/`** — Python (FastAPI) backend service
- **`frontend/`** — Next.js frontend application
- **`cli/`** — Node.js command-line tool (Commander.js)
- **`sandbox/`** — Docker sandbox environment (JSON editing / code execution)

## Deprecated Directories (do not modify)

- `PuppyEngine`, `PuppyFlow`, `PuppyStorage`, `tools`

---

## Backend

- **Language**: Python 3.12+
- **Framework**: FastAPI + Uvicorn (ASGI)
- **Package manager**: uv (`pyproject.toml`)
- **Database**: Supabase (PostgreSQL)
- **Storage**: AWS S3 / LocalStack
- **LLM gateway**: LiteLLM
- **Task queue**: ARQ (Redis)
- **Logging**: Loguru

### Directory Structure

```
backend/
├── src/
│   ├── main.py                # App entrypoint & lifespan
│   ├── config.py              # Global config (Pydantic Settings)
│   │
│   ├── version_engine/        # Git-native Version Engine (core write funnel)
│   │   ├── adapters/
│   │   │   ├── git/           #   Git smart-HTTP protocol boundary
│   │   │   └── operations/    #   ProductOperationAdapter for Web/API/CLI
│   │   ├── application/       #   transaction engine, merge policy, Git objects
│   │   ├── domain/            #   write/conflict intents
│   │   ├── routers/           #   content, history, conflict, AP-FS, websocket
│   │   ├── server/            #   repo manager, Supabase/S3 adapters, auth
│   │   └── services/          #   tree reader/splice, hooks, outbox, GC
│   │
│   ├── content/               # Content node tree (folder/JSON/MD/file)
│   │   └── table/             #     Structured data tables (JSON Pointer)
│   ├── tool/                  # Tool registration & search index
│   │
│   ├── provider/              # External-source adapters, OAuth, database capabilities
│   │
│   ├── platform/              # Platform services
│   │   ├── auth/              #   JWT auth (Supabase Auth)
│   │   ├── organization/      #   Org management & member invitations
│   │   ├── project/           #   Project CRUD & members & dashboard
│   │   ├── profile/           #   User profile & onboarding status
│   │   ├── workspace/         #   Workspace management
│   │   └── analytics/         #   Usage analytics
│   │
│   ├── infra/                 # Infrastructure services
│   │   ├── supabase/          #   Supabase client & repository facade
│   │   ├── s3/                #   S3 storage service
│   │   ├── llm/               #   LLM service (generation + embedding)
│   │   ├── search/            #   Vector search (Turbopuffer + RRF)
│   │   ├── chunking/          #   Text chunking
│   │   ├── security/          #   Security module (AES-256-GCM)
│   │   ├── scheduler/         #   Scheduled tasks (APScheduler)
│   │   ├── sandbox/           #   Sandbox runtime (Docker/E2B execution engine)
│   │   ├── turbopuffer/       #   Turbopuffer vector DB client
│   │   └── mcp_health.py      #   Remote MCP transport health probe only
│   ├── ingest/                # File ingestion ETL (MineRU + LLM)
│   ├── context_publish/       # Public JSON publishing (short links)
│   ├── internal/              # Internal API (X-Internal-Secret)
│   └── utils/                 # Utilities (logging/middleware)
├── mcp_service/               # Standalone MCP Server service (FastMCP)
├── sql/                       # Database DDL & migrations
├── tests/                     # Tests
├── scripts/                   # Scripts
└── docs/                      # Feature documentation
```

### Development Conventions

- **Layered architecture**: `Router → Service → Repository (Supabase)` three-tier separation
- **Dependency injection**: Use FastAPI `Depends` to inject Service and Repository
- **Fully async**: All I/O operations use `async/await`
- **Pydantic models**: All request/response defined with Pydantic schemas
- **Naming conventions**: Files `snake_case.py`, classes `PascalCase`, functions/variables `snake_case`
- **DB table naming**: New tables use **plural snake_case** (e.g. `projects`, `access_surfaces`, `version_transactions`). Deferred physical legacy names may appear only through `backend/src/version_engine/server/db_names.py`.
- **Route prefix**: Business APIs under `/api/v1`, internal APIs under `/internal`
- **Module structure**: Each module typically contains `router.py`, `service.py`, `repository.py`, `schemas.py`

### Database Tables

All tables use plural snake_case names. The "unified access" architecture serves agents, MCP endpoints, sandbox endpoints, and Git-remote/CLI credentials through `access_surfaces`, differentiated by `kind` and targeted by `(project_id, nullable scope_id)`. NULL means the Project-owned root; a non-NULL value references a true non-empty-path row in `repository_scopes`. Every machine secret lives hash-only in `access_surface_credentials` and is revealed only on issuance. Provider config must never contain credentials. External source relationships live in `connections`, and executions in `sync_runs`.

| Table | Repository | Description |
|-------|-----------|-------------|
| `projects` | `supabase/projects/repository.py` | Projects |
| `project_members` | `platform/authorization/repository.py` | Sole explicit Human Project membership/role fact |
| `organizations` | `organization/repository.py` | Organizations |
| `org_members` | `organization/repository.py` | Organization membership |
| `org_invitations` | `organization/repository.py` | Organization invitations |
| `profiles` | `profile/repository.py` | User profiles |
| `access_surfaces` | `platform/access/router.py`, `platform/access/surface_repository.py` | Unified access surfaces keyed by `kind`, targeting Project root or one Scope through nullable `scope_id` |
| `repository_scopes` | `repo/scope_repository.py`, `repo/scope_service.py` | Non-root subtree geometry (`path`, `exclude`, `max_mode`); never a repository or credential owner |
| `connections` / `sync_runs` | `platform/synchronize/repository.py`, `platform/synchronize/run_repository.py` | External source relationships and their executions |
| `access_permissions` | `platform/access/adapters/agent/config/repository.py` | Access surface ↔ content node permissions |
| `access_tools` | `platform/access/adapters/agent/config/repository.py`, `tool/service.py` | Access surface ↔ tool bindings |
| `content_nodes` | _(dropped — replaced by Version Engine Git trees in object storage)_ | Legacy content tree |
| `tools` | `supabase/tools/repository.py` | Registered tools |
| `access_surface_credentials` | `repo/access_credentials.py` | Hash-only runtime credentials, including independently revocable user Git credentials; never stores a device, folder, or checkout identity |
| `chunks` | `chunking/repository.py` | Text chunks for search |
| `uploads` | `upload/file/tasks/repository.py` | File upload/ingest tasks |
| `etl_rules` | `upload/file/rules/repository_supabase.py` | ETL transformation rules |
| `context_publishes` | `supabase/context_publish/repository.py` | Public JSON short links |
| `oauth_connections` | `provider/oauth/repository.py` | OAuth integrations |
| `chat_sessions` | `agent/chat/repository.py` | Agent chat sessions |
| `chat_messages` | `agent/chat/repository.py` | Agent chat messages |
| `agent_execution_logs` | `agent/config/repository.py`, `scheduler/jobs/agent_job.py` | Scheduled agent execution logs |
| `file_versions` | _(deprecated — no longer used in code)_ | Legacy file version history |
| `folder_snapshots` | _(deprecated — no longer used in code)_ | Legacy folder snapshots |
| deferred version tables | `version_engine/server/db_names.py` | Physical compatibility names for commit/scope/outbox/object-location storage |
| `audit_logs` | `version_engine/server/audit_repository.py` | Audit trail |
| `search_index_tasks` | `project/dashboard_router.py` | Search indexing tasks |
| `ingest_tasks` | `project/dashboard_router.py` | Ingestion tasks |
| `agent_logs` | `analytics/service.py` | Agent usage analytics |
| `access_logs` | `analytics/service.py`, `analytics/router.py` | API access analytics |

### API Routes

| Route Prefix | Module | Description |
|-------------|--------|-------------|
| `/api/v1/organizations` | organization | Org CRUD & members & invitations |
| `/api/v1/projects` | project | Project CRUD & members & dashboard |
| `/api/v1/content/{project_id}` | version_engine/routers/content_router | Versioned content tree, write, history, diff, rollback |
| `/api/v1/tables` | table | Data tables & JSON Pointer operations |
| `/api/v1/tools` | tool | Tool registration & search index |
| `/api/v1/agents` | platform/access/adapters/agent | Agent SSE streaming chat |
| `/api/v1/agent-config` | platform/access/adapters/agent/config | Agent CRUD & access permissions |
| `/api/v1/mcp` | platform/access/adapters/agent/mcp | MCP v3 tool binding & proxy |
| `/api/v1/mcp-endpoints` | platform/access/adapters/mcp_endpoint | MCP endpoint CRUD & API key |
| `/api/v1/sandbox-endpoints` | platform/access/adapters/sandbox_endpoint | Sandbox endpoint CRUD & exec |
| `/api/v1/access` | platform/access | Unified access management (all types) |
| `/api/v1/integrations` | platform/synchronize | Data source sync |
| `/api/v1/filesystem` | connectors/filesystem | Filesystem access lifecycle |
| `/api/v1/ingest` | upload | File/URL ingestion ETL |
| `/api/v1/ap-fs` | version_engine/routers/access_point_fs | Puppyone CLI scoped filesystem API |
| `/git/{project_id}.git`, `/git/{project_id}/scopes/{scope_id}.git` | version_engine/entrypoints/git/router | Canonical Git smart-HTTP clone/fetch/push; credential is HTTP auth, not URL data |
| `/git/ap/{access_key}.git` | version_engine/entrypoints/git/router | Bounded, instrumented legacy compatibility only; never construct for new clients |
| `/api/v1/workspace` | workspace | Workspace management |
| `/api/v1/db-connector` | db_connector | External database access |
| `/api/v1/publishes` | context_publish | Public JSON short links |
| `/api/v1/oauth` | provider/oauth | OAuth authorization (9+ platforms) |
| `/api/v1/auth` | auth | Authentication (login/refresh) |
| `/api/v1/analytics` | analytics | Usage statistics |
| `/api/v1/profile` | profile | User profile & onboarding |
| `/api/v1/s3` | s3 | File upload/download/presigned URLs |
| `/internal` | internal | Internal service API |
| `/p/{key}` | context_publish | Public JSON access (no auth required) |
| `/health` | main | Health check |

### Common Commands

```bash
# Install dependencies
uv sync

# Start dev server
uv run uvicorn src.main:app --host 0.0.0.0 --port 9090 --reload --log-level info --no-access-log

# Run tests
uv run pytest
uv run pytest -m "not e2e"      # Exclude e2e tests

# Start domain workers (coordinate queue drain before changing deployments)
uv run arq src.platform.upload.worker.WorkerSettings
uv run arq src.platform.imports.worker.WorkerSettings
uv run arq src.platform.synchronize.worker.WorkerSettings
```

### Deployment

Railway multi-service deployment (shared codebase, differentiated by `SERVICE_ROLE`):

- **api** (default): Main API service
- **upload_worker**: Upload completion / file processing (ARQ)
- **import_worker**: One-time ImportJob execution (ARQ)
- **synchronize_worker**: Synchronize runs and dedicated GitHub pulls (ARQ)
- **mcp_server**: MCP protocol service (FastMCP)

---

## Frontend

Workspace navigation and lifetime ownership are documented in
[`docs/frontend/2026-09-20-workspace-refactor-design.md`](docs/frontend/2026-09-20-workspace-refactor-design.md).
Route entries compose domain features; features must not import app route implementations.
Workspace navigation uses `features/workspace/navigation` so links and command
actions share leave protection. Next owns project/view/path identity; do not
reintroduce a page-local history router. Files DOM lifetime belongs to its
layout, editor drafts/writes to editor sessions, and pane geometry to the layout
store. Preserve the existing desktop appearance during responsive changes.

- **Framework**: Next.js 15 (App Router)
- **Language**: TypeScript
- **UI**: React 18 + Tailwind CSS
- **Auth**: Supabase Auth
- **State management**: Zustand + React Context
- **Data fetching**: SWR

### Directory Structure

```
frontend/
├── app/                          # Next.js App Router pages
│   ├── (main)/                   # Route group (shared AppSidebar layout)
│   │   ├── projects/             # Projects module
│   │   │   └── [projectId]/      # Project detail pages
│   │   │       ├── data/         # Data explorer
│   │   │       ├── access/       # Access management
│   │   │       ├── toolkit/      # Agent toolkit
│   │   │       ├── monitor/      # Monitoring
│   │   │       └── settings/     # Project settings
│   │   ├── settings/             # Global settings
│   │   ├── tools-and-server/     # Tools & MCP server management
│   │   ├── home/                 # Home / dashboard
│   │   ├── billing/              # Billing
│   │   └── team/                 # Team management
│   ├── api/                      # API routes (agent, sandbox)
│   ├── auth/                     # Auth callbacks
│   ├── login/                    # Login page
│   ├── onboarding/               # Onboarding flow
│   └── oauth/                    # OAuth callbacks (multi-platform)
├── components/                    # React components
│   ├── agent/                    # Agent components
│   ├── chat/                     # Chat interface
│   ├── dashboard/                # Dashboard components
│   ├── editors/                  # Editors (JSON/Markdown/Code)
│   │   ├── code/                 # Monaco / CodeMirror
│   │   ├── markdown/             # Milkdown Markdown editor
│   │   ├── table/                # Tabular JSON editor
│   │   ├── tree/                 # Tree JSON editor
│   │   └── vanilla/              # Vanilla JSON editor
│   ├── sidebar/                  # Sidebar
│   ├── views/                    # Shared view components
│   ├── onboarding/               # Onboarding components
│   └── RightAuxiliaryPanel/      # Right auxiliary panel
├── lib/                          # Utilities & API clients
│   ├── hooks/                    # Custom React hooks
│   ├── apiClient.ts              # Base API client
│   ├── chatApi.ts                # Chat API
│   ├── contentTreeApi.ts         # Content tree API
│   ├── mcpApi.ts                 # MCP API
│   ├── mcpEndpointsApi.ts        # MCP endpoints API
│   ├── sandboxEndpointsApi.ts    # Sandbox endpoints API
│   ├── projectsApi.ts            # Projects API
│   ├── organizationsApi.ts       # Organizations API
│   ├── oauthApi.ts               # OAuth API
│   ├── ingestApi.ts              # Ingestion API
│   ├── dbConnectorApi.ts         # Database connector API
│   └── profileApi.ts             # User profile API
├── contexts/                     # React Context
│   ├── AgentContext.tsx           # Agent state management
│   └── WorkspaceContext.tsx       # Workspace state management
├── middleware.ts                  # Next.js middleware (auth & routing)
└── next.config.ts                # Next.js config
```

### Common Commands

```bash
# Install dependencies
npm install

# Start dev server
npm run dev

# Production build
npm run build
```

### Environment Variables

```
NEXT_PUBLIC_SUPABASE_URL        # Supabase URL
NEXT_PUBLIC_SUPABASE_ANON_KEY   # Supabase anon key
NEXT_PUBLIC_API_URL             # Backend API URL (default http://localhost:9090)
NEXT_PUBLIC_DEV_MODE            # Dev mode flag
```

---

## CLI

- **Language**: JavaScript (ESM)
- **Framework**: Commander.js
- **Install**: `npm install -g puppyone`

### Directory Structure

```
cli/
├── bin/puppyone.js             # Entrypoint & command registration
├── src/
│   ├── commands/               # Command implementations
│   │   ├── auth.js             # Auth (login/logout/whoami/targets)
│   │   ├── org.js              # Organization management
│   │   ├── project.js          # Project management
│   │   ├── access.js           # Unified Access Point management (all access types)
│   │   ├── ap/                 # Access Point profile command domain
│   │   │   ├── index.js        # ap command registration
│   │   │   └── profiles.js     # login/use/list/current/logout/clear
│   │   ├── fs/                 # Access Point scoped filesystem command domain
│   │   │   ├── index.js        # fs command registration
│   │   │   ├── commands/       # ls/tree/find/cat/head/tail/stat/write/mkdir/touch/cp/mv/rm/upload/download
│   │   │   └── lib/            # shared FS context/http/path/render/read/transfer helpers
│   │   ├── ap.js               # Compatibility re-export only
│   │   ├── chat.js             # Agent chat (SSE streaming)
│   │   ├── config-cmd.js       # CLI configuration
│   │   ├── global.js           # Global commands (status/ps)
│   │   └── _daemon.js          # Filesystem sync daemon (internal, legacy)
│   ├── api.js                  # HTTP client
│   ├── config.js               # Config file read/write
│   ├── daemon.js               # Background daemon management
│   ├── registry.js             # Local access registry
│   ├── output.js               # Output formatting (human/JSON)
│   ├── helpers.js              # Shared utilities
│   └── state.js                # Sync state management
```

### Key Commands

Source-tree commands below require the matching backend; they are not a claim
that npm `0.2.1` has been republished. See `docs/cli/ENTRYPOINTS-UNRELEASED.md`.
Never send an external source to Access creation, or substitute an Access ID for
a Synchronize binding ID. Do not silently turn snapshots into persistent jobs.

```bash
puppyone auth login                    # Sign in
puppyone project use "My Project"      # Set active project
puppyone import create <url> --provider notion --folder /notes  # One-time snapshot
puppyone synchronize add gmail <url> --folder /mail --mode manual  # Durable binding
puppyone synchronize providers        # Server-admitted persistent sources
puppyone access add agent "Bot"        # Create an AI agent
puppyone access add mcp "Data API"      # Create MCP endpoint
puppyone access ls                     # List Access surfaces
puppyone status                        # Project dashboard
puppyone chat                          # Chat with an agent
puppyone fs semantics                  # Unix compatibility notes + resource limits for agents
puppyone fs rmdir empty-dir            # Remove an empty scoped directory through AP-FS
```

See `docs/architecture/03-cli.md` for full reference.

---

## Sandbox

Lightweight Docker sandbox environment for securely executing CLI commands (e.g. `jq` for JSON editing) in isolated containers.

```
sandbox/
├── README.md          # Usage docs
├── Dockerfile         # Alpine + jq/bash/coreutils
└── test-data.json     # Sample test data
```

Both the frontend (`app/api/sandbox/route.ts`) and backend (`src/infra/sandbox/`) integrate with sandboxes; the backend also supports E2B cloud sandboxes.

---

## Other Directories

| Directory | Description |
|-----------|-------------|
| `docs/` | Project-level documentation |
| `assert/` | Static assets |
| `scripts/` | Utility scripts |
| `todo/` | Todo items |
| `.github/` | GitHub Actions & CI |


## Four entrypoint ownership rules (ISSUE-048)

- `platform/upload`: local uploads; `platform/imports`: external snapshots.
- `platform/synchronize`: durable source bindings and runs, including `github/`.
- `platform/access`: AccessSurface (`kind`, `surface_id`), Git/CLI/Agent/MCP/Sandbox access.
- Shared `provider/` supplies adapters to Import and Synchronize; it never imports
  these entrypoint lifecycles or publishes Version Engine writes.
- Old HTTP names are wire contracts. Do not restore old Python business modules.
- Do not rename physical tables or serialized job names as part of a source move.
  Database/internal compatibility cleanup is separately tracked by ISSUE-049.
- Run `uv run pytest -q -m 'not e2e and not integration and not network'` from
  `backend/`; this includes dependency, OpenAPI and ARQ compatibility gates.
