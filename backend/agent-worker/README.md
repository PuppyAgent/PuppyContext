# Cloud Agent worker

This artifact replaces the old Python Agent tool loop. Existing `access_surfaces`
Agent IDs, model/prompt/view configuration, tool bindings and scheduler triggers
remain the configuration source. There is one durable run path for chat and
scheduled execution; `POST /agents` and the old runtime DTO are removed.

## Run locally

Build from the repository root:

```sh
docker build -t puppyone-cloud-agent:data-access-v1 backend/agent-worker
```

Apply the `20261006060000`, `20261006060100` and `20261008010000` migrations through the existing
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
Node 22.22.3, Git, ripgrep, fd-find (`fd` on PATH) and all three runtime `.mjs` files. The E2B transport disables Internet
access and binds resources to execution/project metadata. E2B template creation
and hosted behavior have **not** been verified by the local Docker tests.

The image pins Node's base digest and Pi 0.85.1. Pi ships an npm shrinkwrap that
prevents root overrides from replacing two nested dependencies; the build runs
`patch-dependencies.mjs` to install checksum-verified undici 8.10.2 and
brace-expansion 5.0.12 into those exact locations. It does not patch Pi source.
Production image configuration should use the built registry digest. Docker
resolves and records its immutable image ID before allocating each execution.

## Knowledge repository workspace

For a native full Project, the backend captures the selected cloud branch and
complete reachable Git history under the existing read pin. A standard Git bundle
travels over the provider control channel; the sandbox runs `git clone` on it.
This is an offline Git transport, not a backend checkout or a file-only export.
No E2B template start command or cloud write credential is needed for each run.
The template contains the tools; the supervisor starts each actual writing task.

After a successful turn, the provider's trusted capture command freezes Agent
processes, stages changes and creates a Git commit. The backend publishes that
original commit graph through the existing fenced native ref transaction, with
the same semantics as a push to the captured branch. This implementation does
not execute an unrestricted HTTP `git push` from an Agent tool. Unchanged turns
create no commit. Concurrent edits reject publication and retain the local commit.

Full native Project views are supported. Restricted native Agent views reject
before receiving broader history; native Scope projections remain a separate
capability. The selected branch must have a UTF-8 name; detached HEAD rejects.
Checkpoints are bounded to 32 MiB of packed Git data, 64 MiB of working files and
10,000 files. Current workspace files must be regular files (no symlinks or
submodules); their executable modes are retained. No limit truncates history:
an oversized repository fails preparation explicitly.

## Client contract

All routes are under `/api/v1`, authenticated with the existing user JWT:

| Route | Behavior |
| --- | --- |
| `POST /agents/runs` | `202` after atomic receipt/run/user-message persistence |
| `GET /agents/requests/{project_id}/{request_id}` | Recover a lost submission receipt |
| `GET /agents/runs/{run_id}` | Snapshot, publication outcome and tool/approval state |
| `GET /agents/sessions/{session_id}/runs` | Newest-first runs, `limit` 1–100, optional `before` timestamp |
| `GET /agents/sessions?project_id=…&agent_id=…` | Actor-owned Cloud Pi chat history, `limit` 1–200; one consistent authorization/list query |
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
inactive branches), the captured repository base, original Git objects/refs,
staged index and bounded working files. Git metadata is never published as a
knowledge file. A completed model turn is recorded separately from the final
frozen/committed checkpoint so a crash between those stages can be recovered.
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
Native Git publication conditionally updates its fixed ref and preserves original
commit IDs; it never regenerates commits or force-pushes over another author.
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

The data-access migration adds revision guards and batch receipts. Before
activation, drain **all** pre-migration runs and settle retained resources;
their policies do not contain a revision token. Preserve completed runs and
saved Agent configuration. Do not manufacture tokens on old in-flight work.
Use the protected database release lane, then deploy the matching API, worker
artifact and Desktop client. The current native-only source also requires the
separate native inventory/data release; a legacy hosted Project is not upgraded
by this Agent schema migration. Never point this build at a legacy repository
and infer that applying only Agent SQL makes its Git history ready.

An unknown required RPC produces sanitized, retryable
`503 database_schema_outdated`. This is a deployment mismatch, not a reason to
recreate the Agent or silently bypass admission.

`cloud_agent_performance` logs worker total attempts, maximum in-flight requests,
operation counts, stage durations, elapsed time and first text. The ASGI
`cloud_agent_api_performance` measurement includes the entire SSE lifetime.
Neither logs database query parameters, credentials or prompt/response content.

## Verification

From `backend/`:

```sh
uv run pytest tests/agent/runtime -q
AGENT_DESKTOP_REPO="$DESKTOP_CHECKOUT" AGENT_DESKTOP_REPORT_DIR="$EVIDENCE_DIR" \
  uv run pytest tests/agent/runtime/test_desktop_integration.py -q
node --test agent-worker/git-workspace.test.mjs
uv run pytest tests/agent --ignore=tests/agent/runtime tests/security tests/scheduler tests/platform/billing -q
```

The runtime suite owns local PostgreSQL 17 (with the repository's auth stub),
PostgREST v14.13, MinIO and real pinned Pi Docker containers. It uses deterministic
OpenAI protocol responses, not paid external model calls. It requires the locally
built worker image and the same local MinIO fixture image used by repository
hosting tests (`puppyone-entrypoint-minio-build:local`, `/go/bin/minio`). It never
uses an existing remote database. This is real local storage/process evidence,
not Supabase-hosted/E2B/provider-billing deployment acceptance.

The regression suite checks these persistence guarantees:

| Scenario | Required outcome |
| --- | --- |
| Three consecutive turns on SHA-1 and SHA-256 repositories | Each writing turn starts from the prior cloud commit; original parents survive; an unchanged turn creates no commit. |
| Rename/delete, Unicode paths, binary attachments, executable modes and ignored scratch files | Published bytes and modes match Git; deletions persist; ignored files remain in recovery without entering the cloud tree. |
| Two Agents publish from the same base | Exactly one succeeds; the other reports a conflict and retains its own commit and files. |
| Cloud HEAD changes or the captured branch is deleted | Publication cannot switch targets or recreate the deleted branch. |
| Truncated/corrupt pack, mismatched tip/branch or rewritten ancestry | Cloud refs stay unchanged and the original recovery checkpoint remains available. |
| Stop, timeout or permission revocation at publication | The final database fence rejects the write even after the model finished. |
| Crash after model completion or final commit checkpoint | Takeover completes without another model call; a durable commit keeps its original ID. |
| Object storage fails after sandbox commit | The sandbox survives until that exact commit and files are durably captured; cleanup cannot erase the only copy. |
| Repeated sandbox destruction/recreation | Merge parents, annotated tags, independent histories, index-only objects and dirty files survive. |
| Recovery object is damaged or used by another Project/run | Checksum and owner binding reject restoration; retries cannot overwrite an earlier checkpoint. |

The scripted model fixture checks the latest tool result before reporting
completion, so a shell error cannot pass merely because an earlier turn succeeded.
These are deterministic persistence tests; they do not evaluate model quality.

### Git workspace verification (2026-10-07)

Runtime implementation `5161a778` was verified with the locally built
`git-workspace-v1` artifact and the expanded regression suite:

- Real Git helper suite: 8 passed on both the host and the pinned Linux worker
  image (Node 22.22.3), including repeated recovery, merge/tag identity, binary
  files, modes, hook suppression and corrupt-pack rejection.
- Full Agent runtime suite: 74 passed, using real Pi/Docker, owned PostgreSQL/
  PostgREST and MinIO. Both SHA-1 and SHA-256 complete three sequential turns;
  fault tests cover concurrent publication, changed refs, invalid packs,
  authorization changes, crash windows and storage outages.
- This follow-up adds 32 cases: 27 runtime cases and 5 Git helper cases. It
  changes tests and verification documentation without changing the runtime artifact.
- Backend non-integration regression command: 3,726 passed, 904 skipped,
  34 expected failures and 150 deselected.
- Ruff, whitespace checks and strict OpenSpec validation passed.
- Canonical documentation was updated in `puppy-issues/document/puppyone/agent-runtime/`.
  Its global validator still reports the two pre-existing lifecycle metadata
  errors in the unrelated Rust-native-frontend README, with no new errors.

No hosted API, E2B template or paid model was created or deployed by this validation.
