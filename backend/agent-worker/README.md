# Cloud Agent worker

This artifact replaces the old Python Agent tool loop. Existing `access_surfaces`
Agent IDs, model/prompt/view configuration, tool bindings and scheduler triggers
remain the configuration source. There is one durable run path for chat and
scheduled execution; `POST /agents` and the old runtime DTO are removed.

## Run locally

Build from the repository root:

```sh
docker build -t puppyone-cloud-agent:pi-0.85.1-node-22.22.3 backend/agent-worker
```

Apply the two `20261006060000` / `20261006060100` migrations through the existing
database release workflow. The API and supervisor share Supabase, S3, managed
inference and runtime billing configuration. In `backend/`, start:

```sh
uv run python -m src.platform.access.adapters.agent.runtime.worker
```

`SERVICE_ROLE=agent_worker` selects that command on Railway. The repository's
service manifest includes this role; a service must still be provisioned and its
Wait for CI database gate verified at deployment. This change does not provision
or deploy it. `CLOUD_AGENT_CONCURRENCY` defaults to 4; additional supervisor
processes coordinate through database leases.

The configured Agent model, or `CLOUD_AGENT_DEFAULT_MODEL` for a first built-in
Agent, must be supported by the existing managed inference gateway.
`MANAGED_AI_ENABLED` must be enabled. No provider key enters the Pi sandbox.
Docker requires a Docker-enabled worker host. Railway's hosted worker should use
`SANDBOX_TYPE=e2b` and a separately built, immutable
`CLOUD_AGENT_E2B_TEMPLATE` containing this artifact, `/workspace`, user `node`,
Node 22.22.3 and both `.mjs` entrypoints. The E2B transport disables Internet
access and binds resources to execution/project metadata. E2B template creation
and hosted behavior have **not** been verified by the local Docker tests.

The image pins Node's base digest and Pi 0.85.1. Pi ships an npm shrinkwrap that
prevents root overrides from replacing two nested dependencies; the build runs
`patch-dependencies.mjs` to install checksum-verified undici 8.10.2 and
brace-expansion 5.0.12 into those exact locations. It does not patch Pi source.
Production image configuration should use the built registry digest. Docker
resolves and records its immutable image ID before allocating each execution.

## Client contract

All routes are under `/api/v1`, authenticated with the existing user JWT:

| Route | Behavior |
| --- | --- |
| `POST /agents/runs` | `202` after atomic receipt/run/user-message persistence |
| `GET /agents/requests/{project_id}/{request_id}` | Recover a lost submission receipt |
| `GET /agents/runs/{run_id}` | Snapshot, publication outcome and tool/approval state |
| `GET /agents/sessions/{session_id}/runs` | Newest-first runs, `limit` 1–100, optional `before` timestamp |
| `GET /agents/runs/{run_id}/events` | SSE replay, `after` or `Last-Event-ID`; no execution ownership |
| `POST /agents/runs/{run_id}/stop` | Durable idempotent stop command |
| `POST /agents/runs/{run_id}/approvals/{call_id}` | `{decision_id: UUID, allow: boolean}` |

Submit `{project_id, agent_id?, scope_id?, session_id?, request_id: UUID, prompt}`.
The client generates one request UUID per intended prompt and reuses it for
transport retries. Reusing it with different input returns 409. A session admits
one active run. Scope and Agent IDs must agree. Without an Agent ID, admission
reuses a visible configured Agent for the exact target; creating a missing
built-in Agent requires `agent.manage`. Viewing a panel creates no resources.

Sessions use mode `cloud_pi`. Legacy display messages are not Pi context and
cannot be resumed through the new protocol. Desktop/Web adoption is tracked by
ISSUE-112/113; existing clients must switch before this breaking API is deployed.

States are `queued`, `running`, `waiting_approval`, `publishing`, then
`succeeded`, `stopped`, `failed`, `conflict` or `outcome_unknown`. `succeeded`
requires canonical `committed` or `no_changes`. A no-change run can succeed
without a repository commit. State snapshots include a diagnostic `code`;
`resource_retained=true` means cleanup is waiting for durable recovery storage.
Public responses exclude checkpoint keys and internal tool manifests.

SSE IDs are per-run increasing integers. The last 512 events are retained and
read in batches of 64. An expired/invalid history window emits `reset` with the
snapshot and current sequence, then closes. Resume from that sequence. A client
must deduplicate by sequence, including after reopening the panel. On a
`text_reset` event, replace the run text with `payload.text`: an interrupted model
stream may contain an unconfirmed prefix that must not be duplicated on takeover. Mutation
events tell clients to refresh the snapshot for the full tool input/result.

File write/edit/bash require explicit per-call approval, including scheduled
runs; saved schedule configuration is retained but does not silently grant new
automatic approval. Approvals never widen the saved Project/Scope view.

## Recovery and release

Runs, execution generations, tool receipts and bounded events live in PostgreSQL.
Checksummed gzip objects store full Pi session entries (including compaction and
inactive branches), the captured repository base and bounded workspace files.
An old execution cannot write after takeover. Completed receipts can restore
missing tool results without re-execution; an `executing` receipt is unknown and
is never replayed. The trusted capture helper freezes tool processes before
copying the actual sandbox filesystem. Recovery objects must be durable before
provider deletion. Storage failure retains the provider and transfers cleanup
to the durable run owner. Terminal states remain immutable on cleanup retries.

Unpublished changes remain in recovery objects. This backend does not automatically
publish an unknown tool outcome, resolve conflicts or turn an old display-only
chat into a resumable Pi session. A new session is required when the previous
run requires resolution; operator recovery uses the retained base/files/receipts.

Publication reuses the canonical Version Engine, original base and SQL fencing.
Agent submissions opt into its existing `manual_review` policy for unsafe
overlaps; safe deterministic merges still use the same engine.
The native publication adapter has real PostgreSQL/S3 coverage with synthetic
native enrollment/readiness; the current Project readiness service still owns
its existing first-root-Git-push gate. Native readiness rollout belongs to
ISSUE-062 and is not claimed by that component test.

Deploy migrations before the API/supervisor and client switch. Stop old Agent
producers and drain existing Python runs before replacing them. Do not run
legacy and Pi producers for the same scheduled trigger. Roll back application
code only after stopping new submissions, stopping/draining runs, and settling
cleanup/billing; retain additive run tables and checkpoint objects. Do not drop
them after accepting production runs. Changes to historical migrations are
unnecessary. Runtime records block parent deletion while work/recovery is active;
terminal records cascade with intentional session/Agent/Project deletion.

## Verification

From `backend/`:

```sh
uv run pytest tests/agent/runtime -q
uv run pytest tests/agent --ignore=tests/agent/runtime tests/security tests/scheduler tests/platform/billing -q
```

The runtime suite owns local PostgreSQL 17 (with the repository's auth stub),
PostgREST v14.13, MinIO and real pinned Pi Docker containers. It uses deterministic
OpenAI protocol responses, not paid external model calls. It requires the locally
built worker image and the same local MinIO fixture image used by repository
hosting tests (`puppyone-entrypoint-minio-build:local`, `/go/bin/minio`). It never
uses an existing remote database. This is real local storage/process evidence,
not Supabase-hosted/E2B/provider-billing deployment acceptance.
