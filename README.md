<div align="center">
  <img src="assets/puppyone.svg" alt="Puppyone Logo" width="72" height="72" />
  
  <h1>Puppyone</h1>
  
  <p><b>Git-native Context Drive for AI agents.</b></p>
  <p>Puppyone provides context hosting for AI agents, with Git version control and file-level scoped permissions for every agent.</p>

  <p>
    <a href="https://www.puppyone.ai"><img src="https://img.shields.io/badge/Website-puppyone.ai-39BC66?style=flat-square" alt="Website" /></a>
    <a href="https://www.puppyone.ai/doc"><img src="https://img.shields.io/badge/Docs-Read-D7F3FF?style=flat-square&logo=readthedocs&logoColor=black" alt="Documentation" /></a>
    <a href="https://discord.gg/zwJ9Y3Uvpd"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord" /></a>
    <a href="https://x.com/puppyone_ai"><img src="https://img.shields.io/badge/X-(Twitter)-000000?style=flat-square&logo=x&logoColor=white" alt="X" /></a>
  </p>
</div>

---

## What is Puppyone?

Puppyone provides context hosting for AI agents, with Git version control and file-level scoped permissions for every agent.

It supports CLI, MCP, Git, bash/SSH, and web app interactions through Access Points for different AI agents.

<p align="center">
  <img src="assets/context-drive-access-diagram.png" alt="Puppyone Context Drive exposed through CLI, MCP, Git, and SSH access methods" width="600" />
</p>

<p align="center"><em>One shared Context Drive, multiple scoped Access Points for different agents and tools.</em></p>


---

## Get Started

### 1. Install / Deploy

#### Option A: Cloud (Hosted)

The fastest way — no infrastructure to manage.

Create an account at [puppyone.ai](https://www.puppyone.ai).

#### Option B: Self-Hosted (Docker)

Run the full stack locally with Docker. The only prerequisite is [Docker](https://www.docker.com/).

```bash
git clone https://github.com/puppyone-ai/puppyone-cloud.git
cd puppyone-cloud/docker
cp .env.example .env
docker compose up --build -d --wait
```

This starts PostgreSQL 17, Auth, API gateway, Redis, MinIO, backend, and frontend. A separate migration task applies the committed B1 baseline and all subsequent files in `supabase/migrations`, using Supabase's migration history. The API waits for migration and storage initialization to succeed. No PuppyPay checkout or paid API key is required for core file operations.

The supplied configuration is for local use. Existing PostgreSQL 15 data volumes require an explicit database upgrade; do not replace their image or delete their volumes to make startup succeed. See [installation and upgrade validation](docs/architecture/14-self-hosted-installation.md).

The Docker defaults already separate browser-facing URLs (`localhost`) from container-internal service URLs (`api`, `kong`), so the same setup works for both client-side and Next.js server-side requests.

The backend container also mounts the host Docker socket and a dedicated sandbox temp directory, so agent bash and sandbox endpoints work out of the box in the local Compose stack without changing the backend's global temp directory behavior.

| Service | URL |
|---------|-----|
| Frontend | `http://localhost:3000` |
| Backend API | `http://localhost:9090` |
| Supabase API | `http://localhost:8000` |
| MinIO Console | `http://localhost:9001` |

> **Security note:** The local Docker stack enables Docker-backed sandboxes by sharing the host Docker daemon with the backend container. This is convenient for local self-hosting, but for remote or multi-tenant deployments you should prefer `SANDBOX_TYPE=e2b` with an `E2B_API_KEY`.

The first startup builds both application images and may take several minutes. Once the command succeeds, open `http://localhost:3000`. If startup fails, inspect `docker compose ps -a` and `docker compose logs migrate storage-init api`.

Optional: to enable agent chat in the self-hosted stack, add your `ANTHROPIC_API_KEY` to `docker/.env` and restart:

```bash
cd docker
docker compose up -d
```

Optional: OAuth connectors such as GitHub, Gmail, and Google Drive require provider credentials in the backend environment. See `backend/.env.example` for the current variables.

### 2. First Run (for both cloud and self-host)

The product flow is the same for Cloud and self-hosted once the stack is running. Start by opening the web app and signing in:

- Cloud: [puppyone.ai](https://www.puppyone.ai)
- Self-Hosted: `http://localhost:3000`

If you want to manage your workspace from the CLI, install it and sign in:

```bash
npm install -g puppyone
puppyone auth login          # first run asks: Cloud / Local / Custom URL
```

For self-hosted, choose `Local` at the prompt or pass `-u http://localhost:9090`.

The CLI stores sessions per target, so you can switch between Cloud and self-hosted without re-entering credentials (`puppyone auth targets switch <url>`).

**1. Create your first Context Drive**

```bash
puppyone project create "My Project"
puppyone project use "My Project"
```

This creates a project and sets it as active. You can also create your first project directly from the web app.

**2. Add your first content**

Start with something that works immediately in both Cloud and self-hosted:

| Source | Action |
|--------|--------|
| Webpage | `puppyone access add url https://example.com --scope /refs` |
| Files or folders | Upload directly from the web app |

Use target folders to organize uploaded or synced content into any path in your Context Drive.

### 3. Optional: Enable More Features

- **Agent chat** — In self-hosted deployments, add `ANTHROPIC_API_KEY` to `docker/.env` and restart the stack.
- **OAuth connectors** — In self-hosted deployments, configure provider credentials in the backend environment before using GitHub, Gmail, Google Drive, and other OAuth-based sources.
- **Distribute via MCP** — Create an MCP endpoint when you want agents in Cursor, Claude Desktop, or other MCP clients to read your Context Drive:

```bash
puppyone access add mcp "My Context"
# → outputs MCP endpoint URL and API key
```

See the [documentation](https://www.puppyone.ai/doc) for supported sources and advanced setup details.

---

## Features

### Connected

Connect context from SaaS tools, databases, and the web into agent-friendly files.

Puppyone provides built-in source connectors for GitHub, Gmail, Google Drive, Google Docs, Google Sheets, Google Calendar, Google Search Console, web pages, local filesystem access, and Supabase databases.

All data is transformed into agent-friendly formats (Markdown, JSON, raw files) and stored in your **Context Drive** — a cloud file system that any agent can browse like a local directory.

<img src="assets/connect-demo.gif" alt="Connect data sources" width="100%" />

### Collaborative

Agent-level auth, versioning, audit, and collaboration — built for agents, not humans.

- **File Level Security (FLS)** — Per-agent file permissions enforced at the filesystem layer. If an agent doesn't have access, the file physically doesn't exist in its environment. Think Row Level Security (RLS), but for files.
- **Version history & rollback** — File-level versioning with diff comparison and one-click rollback. Folder-level snapshots for bulk recovery.
- **Audit logs** — Every read and write operation is recorded: who did what, to which file, and when.
- **Checkout / commit workflow** — Locking, conflict detection, and resolution for concurrent agent edits.

<img src="assets/auth-demo.gif" alt="File Level Security" width="100%" />

### Accessible

One Context Drive, many ways in. Your agents access it however they work best:

- **MCP (Model Context Protocol)** — Auto-generated MCP endpoints for each agent. Connect Cursor, Claude Desktop, Windsurf, Cline, or any MCP-compatible client in seconds.
- **Sandbox** — Isolated Docker/E2B containers with only the authorized files mounted. Agents execute code securely without seeing anything they shouldn't.
- **REST API** — Full programmatic access. Read, write, query, and manage everything.
- **CLI** — Every operation available via `puppyone` command line, so AI coding tools like Claude Code can drive the platform directly.
- **Local folder sync** — Real-time bidirectional sync between local directories and the cloud Context Drive via the OpenClaw protocol.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | [Python 3.12+](https://www.python.org/) / [FastAPI](https://fastapi.tiangolo.com/) |
| Frontend | [Next.js 15](https://nextjs.org/) / React 18 / TypeScript / Tailwind CSS |
| CLI | Node.js / [Commander.js](https://github.com/tj/commander.js) |
| Database | [Supabase](https://supabase.com/) (PostgreSQL) |
| Auth | Supabase Auth (JWT + Access Key) |
| Storage | AWS S3 / MinIO / LocalStack |
| Task Queue | [ARQ](https://github.com/samuelcolvin/arq) (Redis) |
| Sandbox | Docker / [E2B](https://e2b.dev/) |

---

## Contributing

We welcome issues, feature requests, and pull requests.

- For small fixes, open a PR directly.
- For larger changes, [file an issue](https://github.com/puppyone-ai/puppyone-cloud/issues/new/choose) first to discuss the design.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

---

## License

Puppyone is open source under the [Apache License 2.0](LICENSE).

You can use, modify, and distribute Puppyone freely — for personal projects,
internal company tools, self-hosted deployments (single- or multi-tenant),
commercial products, and managed services — subject to the standard Apache 2.0
terms (preserve copyright and `NOTICE`, document significant modifications, and
note that no trademark rights are granted).

See [`LICENSE`](LICENSE) for the full license text and [`NOTICE`](NOTICE) for
attribution of bundled third-party components.

> **Trademarks.** "Puppyone", "PuppyOne", and the Puppyone logo are trademarks
> of PuppyOne authors and are **not** licensed under Apache 2.0. If you fork or
> redistribute, please use a different name and logo for your distribution.

> **Hosted service.** [puppyone.ai](https://www.puppyone.ai) is the official
> managed offering operated by the PuppyOne team. The hosted service is governed
> by its own Terms of Service.
