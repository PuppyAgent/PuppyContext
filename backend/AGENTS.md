<!-- OPENSPEC:START -->
# OpenSpec Instructions

These instructions are for AI assistants working in this project.

Always open `@/openspec/AGENTS.md` when the request:
- Mentions planning or proposals (words like proposal, spec, change, plan)
- Introduces new capabilities, breaking changes, architecture shifts, or big performance/security work
- Sounds ambiguous and you need the authoritative spec before coding

Use `@/openspec/AGENTS.md` to learn:
- How to create and apply change proposals
- Spec format and conventions
- Project structure and guidelines

Keep this managed block so 'openspec update' can refresh the instructions.

<!-- OPENSPEC:END -->

# ContextBase Backend — AI Assistant Guide

## 项目概述

ContextBase 后端是一个基于 **FastAPI** 的 Python 服务，为 LLM Agent 提供结构化上下文管理、MCP 协议集成和智能数据处理管道。

- **语言**: Python 3.12+
- **框架**: FastAPI + Uvicorn (ASGI)
- **包管理**: uv (`pyproject.toml`)
- **数据库**: Supabase (PostgreSQL)
- **存储**: AWS S3 / LocalStack
- **向量搜索**: Turbopuffer
- **LLM 网关**: LiteLLM

## 架构（Git-Native Version Engine）

**Git objects/trees/commits are the version facts. Supabase is the control
plane for project refs, scope refs, transactions, audit, conflicts, and outbox
repair.**

- 没有 `content_nodes` 表；内容由 Version Engine Git tree/object storage 提供
- Web/API/`puppyone fs` 写入全部通过 `ProductOperationAdapter`
- Stock Git 客户端通过 `/git/...` smart-HTTP 进入 Git transport adapter
- 所有写入最终收敛到 `GitNativeTransactionEngine`
- Scope、权限、excludes、pause、冲突策略和 audit 都在 server 端执行
- 旧线协议和外部版本包不再是运行时依赖

### 授权架构（Organization → Project → Runtime）

- Human Project action 必须经过 `src/platform/authorization/` 的 canonical
  `AuthorizationService`，以具名 `ProjectAction` 求值；业务模块不得直接读取
  `project_members`、解释 raw role 或复活 `verify_project_access`。
- `project_members` 是唯一显式 Human Project role 事实。Organization membership
  只建立 tenant context；`visibility='org'` 仅给 Viewer baseline。
- Agent visibility、publish ownership、upload ownership 等 child rule 只能继续
  收紧 `ProjectGrant`，不能放大权限。
- Git/CLI/Agent/MCP/Sandbox credential 只能形成绑定到显式
  Project-root 或 Scope target 的 `RuntimeGrant`，不得替代 Human ProjectGrant 或进入成员、分享、设置、Billing
  与 credential management control plane。
- canonical PuppyOne remote 是本地到 Cloud 的唯一 locator。Desktop 在本地解析
  Project-root/Scope target；Backend 只接收结构化 target，并用当前 JWT 的
  ProjectGrant 再授权。Cloud 不登记 device、folder、checkout 或 workspace instance；
  Git URL、Scope key 与本地路径都不是 authority。新增 Project-scoped route 必须
  登记在 `src/platform/authorization/manifest.py`。

## 项目结构

```
backend/
├── src/
│   ├── main.py                # 应用入口 & 生命周期
│   ├── config.py              # 全局配置 (Pydantic Settings)
│   │
│   ├── version_engine/        # Git-native 版本引擎 (核心读写通道)
│   │   ├── adapters/
│   │   │   ├── git/           # Git smart-HTTP clone/fetch/push
│   │   │   └── operations/    # ProductOperationAdapter
│   │   ├── application/       # 事务引擎、merge/conflict、Git object/tree/commit
│   │   ├── domain/            # write/conflict intents
│   │   ├── routers/           # content/history/conflict/AP-FS/WebSocket
│   │   ├── server/            # repo manager, auth, Supabase/S3 backends
│   │   └── services/          # tree reader/splice, hooks, outbox, GC, trace
│   │
│   ├── content/
│   │   └── table/             # 结构化数据表 (JSON Pointer)
│   ├── tool/                  # 工具注册 & 搜索索引
│   │
│   ├── provider/              # 共用外部来源能力：适配器、OAuth、数据库 query
│   │
│   ├── platform/              # 平台服务
│   │   ├── upload/            # 上传业务、旧 ingest 上传 URL 的实现
│   │   ├── imports/           # 一次性导入，含数据库保存
│   │   ├── synchronize/       # 持续绑定/SyncRun；github/ 在这里
│   │   ├── access/            # AccessSurface；agent/MCP/sandbox 在 adapters/
│   │   ├── auth/              # JWT 认证
│   │   ├── organization/      # 组织管理
│   │   ├── project/           # 项目管理
│   │   ├── profile/           # 用户资料
│   │   ├── workspace/         # 工作空间
│   │   └── analytics/         # 使用统计
│   │
│   ├── infra/                 # 基础设施
│   │   ├── supabase/          # Supabase 客户端
│   │   ├── s3/                # S3 存储
│   │   ├── llm/               # LLM 服务
│   │   ├── search/            # 向量搜索
│   │   └── scheduler/         # 定时任务
│   │
│   └── utils/                 # 工具模块
├── tests/                     # 测试
└── sql/                       # 数据库 DDL & 迁移
```

## 核心模块

### MCP 四层职责与唯一数据路径

```text
MCP client
    │ streamable HTTP / session / notifications
    ▼
mcp_service/                         transport only
    │ /internal/mcp-runtime/tools|call
    ▼
src/internal/mcp_runtime.py          tool registry + dispatch
    │ one hash credential lookup + one scope/policy resolution
    ▼
access_surface_credentials ── access_surfaces ── Project + optional repository_scopes
    │                                 │
    │ custom bindings                 │ filesystem operations
    ▼                                 ▼
access_tools                   version_engine/scoped_fs
                                      │
                                      ▼
                              Version Engine Git data
```

- **传输层**：`mcp_service` 只处理协议、session、通知和 RPC 适配，不解析
  agent/endpoint 配置，不缓存凭证，不读取产品数据。
- **能力层**：`src/internal/mcp_runtime.py` 是唯一工具注册和调用入口；所有
  文件数据读写由 `version_engine/scoped_fs` 完成。绑定的 search 工具只读取
  由同一 scope 内容构建的索引，并再次验证 tool path 位于该 scope 内。
- **配置层**：agent 与 MCP endpoint 都是 `access_surfaces`；API key 只在
  `access_surface_credentials` 以 hash 保存和认证；自定义工具绑定只在
  `access_tools`。
- **管理层**：远程 MCP 的独立基础设施职责仅为 `infra/mcp_health.py` 的传输
  健康探测。legacy `mcps`、`mcp_bindings` 及其 repository 不属于运行时。

缓存失效 webhook 只负责通知已连接 session 重新拉取工具；传输层没有产品
配置或表数据缓存，因此不存在第二套鉴权缓存。

### ProductOperationAdapter（产品写入入口）

```python
class ProductOperationAdapter:
    def __init__(self, repo_manager: VersionRepoManager): ...

    async def write_file(project_id, path, content, who, *, scope="", message="", ...) -> WriteResult
    async def delete(project_id, paths, who, *, scope="", message="", ...) -> WriteResult
    async def mkdir(project_id, path, who, *, scope="", message="", ...) -> WriteResult
    async def move(project_id, source, destination, who, *, scope="", message="", ...) -> WriteResult
    async def bulk_write(project_id, files, who, *, scope="", message="", ...) -> WriteResult
    def read_file(project_id, path, *, scope="") -> bytes
```

### VersionTreeReader（读取入口）

```python
class VersionTreeReader:
    def __init__(self, repo_manager: VersionRepoManager): ...

    def list_dir(project_id, path, scope="") -> list[VersionEntry]
    def read_file(project_id, path) -> bytes
    def stat(project_id, path) -> VersionEntry | None
    def list_tree(project_id, path, max_depth) -> list[VersionEntry]
    def exists(project_id, path) -> bool
    def get_root_hash(project_id) -> str
```

### 事务入口

```python
from src.version_engine.dependencies import (
    get_product_operation_adapter,
    get_tree_reader,
    get_repo_manager,
    create_product_operation_adapter,
    create_tree_reader,
)
```

`GitNativeTransactionEngine` 是唯一发布边界：验证 scope/auth/excludes/base
state，执行 merge/conflict policy，并在一个 SQL 事务内发布 ref/history/
audit/transaction/outbox。

## API 路由

| 路由前缀 | 模块 | 说明 |
|----------|------|------|
| `/api/v1/content/{project_id}` | version_engine/routers/content_router | Content API (ls/cat/stat/tree/write/mkdir/mv/rm/history/diff) |
| `/api/v1/ap-fs` | version_engine/routers/access_point_fs | Puppyone CLI scoped filesystem API |
| `/git/{project_id}.git`, `/git/{project_id}/scopes/{scope_id}.git` | version_engine/entrypoints/git/router | Canonical Git smart-HTTP; stable locator plus separate HTTP credential |
| `/git/ap/{access_key}.git` | version_engine/entrypoints/git/router | Instrumented legacy compatibility only; no new URL construction |
| `/api/v1/tables` | content/table | 数据表 JSON Pointer 操作 |
| `/api/v1/projects` | platform/project | 项目管理 |
| `/api/v1/organizations` | platform/organization | 组织管理 |
| `/api/v1/tools` | tool | 工具注册 |
| `/api/v1/agents` | platform/access/adapters/agent | Agent SSE 聊天 |
| `/api/v1/agent-config` | platform/access/adapters/agent/config | Agent CRUD |
| `/api/v1/mcp` | platform/access/adapters/agent/mcp | MCP v3 工具绑定 |
| `/api/v1/synchronize` | platform/synchronize/public_router | SynchronizeBinding / SynchronizeRun canonical API |
| `/api/v1/integrations` | platform/synchronize/router | 058 S2/S3 有限兼容；新客户端不得 fallback |
| `/api/v1/access` | platform/access | 统一 Access 管理 |
| `/api/v1/ingest` | ingest | 文件/URL 导入 |
| `/api/v1/oauth` | oauth | OAuth 授权 |
| `/internal` | internal | 内部 API |

## 常用命令

```bash
uv sync                 # 安装依赖
uv run uvicorn src.main:app --host 0.0.0.0 --port 9090 --reload --log-level info --no-access-log
uv run pytest           # 运行测试
```

## 部署 (Railway / Nixpacks)

后端用 **Railway + Nixpacks** 构建，配置文件全部在 `backend/`：

- `backend/railway.toml` — `[build]` / `[deploy]`，定义 builder 与 startCommand
- `backend/nixpacks.toml` — `[phases.setup]` / `[phases.install]`，覆盖 Nixpacks 默认 Python 流水线，自己建 venv → pin uv → `uv export` → `pip install -r requirements.lock.txt`

### Railway 控制台必须设置的项

每个共享本仓库的 service（`api` / `upload_worker` / `import_worker` / `synchronize_worker` / `mcp_server`）都必须把
`Settings -> Service -> Source -> Root Directory` 设成 `backend`（写 `/backend` 也等价）。

否则：

- Railway 把整个 monorepo 根当成构建上下文，根本看不到 `backend/railway.toml` 和 `backend/nixpacks.toml`
- Nixpacks 会用默认 Python 流水线

### 即使 Root Directory 设对了，`[phases.*]` 也不能写在 railway.toml 里

`railway.toml` 的 schema 只认 `[build]` / `[deploy]` / `[environments.*]`。`[phases.setup]`
和 `[phases.build]` 这种 Nixpacks 自定义阶段写在 railway.toml 里会被**静默忽略**。

所有 Nixpacks 自定义阶段必须放到独立的 `backend/nixpacks.toml`。

### 必须覆盖的是 `[phases.install]`，不只是 `[phases.build]`

Nixpacks 默认 Python+uv provider 把建 venv + 装依赖塞在 **install** 阶段，
渲染出来大概是：

```
python -m venv --copies /opt/venv \
  && . /opt/venv/bin/activate \
  && pip install uv==$NIXPACKS_UV_VERSION \
  && uv sync --no-dev --frozen
```

当 provider 没注入 `NIXPACKS_UV_VERSION` 时（最近 uv 升级后会偶发），渲染结果变成
`pip install uv==`，整个 build 直接挂：

```
ERROR: Invalid requirement: 'uv==': Expected end or semicolon
```

`[phases.build]` 在 install 之后才跑，光覆盖 build 没用 —— **必须覆盖 `[phases.install]`**，
我们的 `nixpacks.toml` 就是这么写的（自己建 venv，pip 装一个 pinned 的 uv，再 uv export →
pip install requirements）。

### uv 版本要硬编码，不要用 `${UV_VERSION}` 这种变量展开

中间踩过一次：在 `[variables]` 里定义 `UV_VERSION = "0.5.11"`，cmd 里写
`pip install --upgrade pip 'uv==${UV_VERSION}'`。看起来很优雅，实际整套 build 直接挂：

- TOML 字符串里的单引号会**原样**进入 Dockerfile 的 `RUN` 行
- bash 在单引号里**不会**展开 `${UV_VERSION}`
- pip 拿到字面字符串 `uv==${UV_VERSION}`，再次报 `Invalid requirement: 'uv==${UV_VERSION}'`

跟原来的 `pip install uv==` 是同类失败。结论：直接在 cmd 里硬编码 `uv==0.5.11`，
不要叠加 TOML + bash 两层 escape 规则。要升级 uv 就改 `nixpacks.toml` 里那一行字面量。

### 不要在仓库根目录放 Python 项目文件

`pyproject.toml` / `uv.lock` / `.python-version` **只能放在 `backend/`**。

哪怕只是放一个空的 `uv.lock` 或者只写 `[tool.black]` 的 `pyproject.toml` 在根目录，
Nixpacks 也会触发 Python 检测并尝试默认安装流程，把上面的 build 弄挂。

### 多 service 用同一份代码

`startCommand` 通过 `SERVICE_ROLE` 环境变量切换：

| `SERVICE_ROLE` | 进程 |
|----------------|------|
| 未设置 / `api` | `uvicorn src.main:app` (FastAPI) |
| `upload_worker` | `arq src.platform.upload.worker.WorkerSettings` |
| `import_worker` | `arq src.platform.imports.worker.WorkerSettings` |
| `synchronize_worker` | `arq src.platform.synchronize.worker.WorkerSettings` |
| `mcp_server` | `uvicorn mcp_service.server:app` |

不需要复制代码。角色/dispatch 切换前必须停止旧 producer 并排空旧队列，包括延迟、重试和在途任务；不能只改 Variables 后滚动升级。Upload 的 `ETL_*` queue/runtime 配置由 `platform/upload/config.py` 拥有，保留已部署的变量和 `etl` 队列；Import/Synchronize 分别拥有自己的 queue/timeout。`infra/queue_config.py` 只共享 Redis 连接，`infra/file_processing` 不持有入口任务状态。

## 开发约定

- **分层架构**: Router → Service → ProductOperationAdapter/VersionTreeReader
- **依赖注入**: FastAPI Depends
- **全异步**: 所有 I/O 使用 async/await
- **路径标识**: 文件以 path（如 "docs/readme.md"）标识，不使用 UUID
- **命名**: 文件 snake_case.py，类 PascalCase，函数/变量 snake_case
- **路由前缀**: 业务 API 在 /api/v1，内部 API 在 /internal
