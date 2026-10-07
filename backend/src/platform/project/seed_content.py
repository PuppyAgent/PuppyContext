"""
Default seed content for new projects.

Creates a Getting Started.md at root + a Guides/ folder with
About Puppyone.md, Connecting Data.md, and Agent Access.md.

All writes go through Write Engine (ProductOperationAdapter).

Used by both CLI `puppyone init` and web onboarding.
"""

GETTING_STARTED_MD = """\
# Getting Started

Your context space is ready. Here's how to start using it.

## 1. Connect your first data source

Bring in data from Gmail, GitHub, Notion, local folders, or any URL.

**Web:** Click "+ New Access" in the sidebar
**CLI:** `puppyone access add <provider>`

Examples:

    puppyone access add gmail
    puppyone access add github https://github.com/org/repo
    puppyone access add folder ~/my-notes
    puppyone access add url https://example.com

## 2. Give an AI agent access

Create an MCP endpoint so Claude, Cursor, or any MCP-compatible agent can read and write your context space.

**Web:** Go to Access → Add → MCP Endpoint
**CLI:** `puppyone access add mcp "my-endpoint"`

## 3. Explore your data

**Web:** Browse files in the data explorer
**CLI:**

    puppyone fs ls
    puppyone fs tree
    puppyone access ls
    puppyone status

## Learn more

See the **Guides** folder for detailed information:
- **About Puppyone** — what is a context space
- **Connecting Data** — all supported data sources
- **Agent Access** — MCP, sandbox, and permissions
"""

ABOUT_PUPPYONE_MD = """\
# About Puppyone

**The cloud file system built for AI Agents.**

Puppyone unifies your scattered data from Notion, GitHub, Gmail, Airtable, \
local files, and more into a single context space designed for multi-agent \
collaboration. Connect any agent to this one space, and it instantly accesses \
all your context.

## What Problems Does It Solve?

### Data is scattered; agents can't see the full picture
Your data might be in 30 different places: product specs in Notion, code in \
GitHub, customer info in Airtable, pricing in Google Sheets, plus piles of \
PDFs locally. Every time you want an agent to use this data, you have to \
connect it all over again.

### Different agents need different permissions
A support agent should read product catalogs but not change prices. A dev \
agent needs to edit specs. A sales agent can view quotes but not delete \
customer records. You need fine-grained, centralized access control — not \
just "all or nothing."

### Every agent connects differently; maintenance is expensive
Cursor needs MCP, backend scripts use REST APIs, real-time scenarios \
require SSE, and complex tasks need agents running code in a sandbox. You \
need unified logging and monitoring for every access point.

## Two Core Pillars

### Connect
Puppyone's answer is a **cloud folder**. Whether your data comes from \
Notion, GitHub, Airtable, or local PDFs, once connected to Puppyone, \
they all become nodes in this folder.

- Notion pages → Markdown
- GitHub repos → Code directories
- Airtable bases → Structured documents
- Gmail → Summarized threads

For your agent, the world is now just **one single folder**, not 30 \
different SaaS silos.

### Collaborate
Puppyone provides a complete infrastructure for human-agent collaboration:

- **Version history** — every change is tracked with full audit trail
- **Access control** — fine-grained permissions for users and agents
- **MCP protocol** — any MCP-compatible agent can read/write natively
- **Sandbox execution** — agents can run code in isolated environments
- **Real-time sync** — changes propagate instantly across all consumers
"""

CONNECTING_DATA_MD = """\
# Connecting Data

Puppyone connects to your real work apps and data sources. Everything \
you connect becomes part of your context space as regular files and \
folders that both you and your agents can read.

## Supported Sources

### Cloud Services (OAuth)
These require a one-time authorization through your browser:
- **Gmail** — email threads, summarized by date
- **Google Calendar** — upcoming and past events
- **Google Drive** — documents, spreadsheets, files
- **Google Docs** — individual documents
- **Google Sheets** — spreadsheet data
- **Notion** — pages and databases
- **GitHub** — repositories, issues, code
- **Linear** — issues and projects
- **Airtable** — bases and tables

### Local Sources
- **Folder sync** — mount a local directory and keep it in sync
- **File upload** — drag-and-drop or CLI upload for PDFs, CSVs, and more

### Web & API Sources
- **URL** — pull content from any public webpage
- **Custom scripts** — write Python or Node.js scripts that fetch data \
from any API, database, or service

### Databases
- **PostgreSQL**, **MySQL** — connect directly and sync query results

## How Sync Works

Each connected source creates one or more nodes in your context space. \
Puppyone periodically fetches the latest data, detects changes, and \
updates the nodes. You can also trigger a manual sync at any time.

All sync operations are logged with timestamps, status, and error \
details for full visibility.
"""

AGENT_ACCESS_MD = """\
# Agent Access

Puppyone provides multiple ways for AI agents to access your context space.

## MCP Protocol

MCP (Model Context Protocol) is the primary way agents interact with \
Puppyone. Create an MCP endpoint, and any MCP-compatible agent — \
Claude, Cursor, Windsurf, and others — can read and write your \
context space natively.

Each MCP endpoint gets its own URL and access credentials. You control \
exactly which parts of the context space each endpoint can access.

## Sandbox Execution

For agents that need to run code, Puppyone provides isolated sandbox \
environments. Agents can execute Python, Node.js, or shell commands \
in a secure container with access to your context data.

Sandboxes are ephemeral — they spin up, execute, and shut down \
automatically. All execution is logged.

## Access Control

Puppyone gives you fine-grained control over what each agent can do:

- **Read-only** — agent can browse and read files
- **Read-write** — agent can also create and modify files
- **Scoped access** — limit an agent to specific folders or nodes

Permissions are managed per access point, so each agent or integration \
gets exactly the access it needs.

## Monitoring

Every agent interaction is logged:
- Which agent accessed which files
- What queries were made
- When it happened
- Whether it succeeded or failed

Use the dashboard or CLI (`puppyone status`) to see a unified view \
of all agent activity.
"""


async def seed_default_content(
    project_id: str,
    created_by: str,
    writer=None,
) -> dict:
    """
    Populate a newly created project with default seed content.

    All content writes go through VersionWriteCommandService.
    """
    from src.platform.project.write_lease import build_leased_worker_write_commands

    commands = build_leased_worker_write_commands(
        project_id=project_id, user_id=created_by, operation_key=f"seed:{project_id}"
    )

    files: dict[str, bytes] = {
        "Getting Started.md": GETTING_STARTED_MD.encode("utf-8"),
        "Guides/About Puppyone.md": ABOUT_PUPPYONE_MD.encode("utf-8"),
        "Guides/Connecting Data.md": CONNECTING_DATA_MD.encode("utf-8"),
        "Guides/Agent Access.md": AGENT_ACCESS_MD.encode("utf-8"),
    }

    await commands.bulk_write(
        project_id,
        files,
        actor=created_by,
        message="seed: project default content",
    )

    return {
        "getting_started": "Getting Started.md",
        "guides_folder": "Guides",
        "about": "Guides/About Puppyone.md",
        "connecting": "Guides/Connecting Data.md",
        "agent_access": "Guides/Agent Access.md",
    }
