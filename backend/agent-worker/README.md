# Cloud Agent worker

This artifact replaces the old Python Agent tool loop. Existing `access_surfaces`
Agent IDs, model/prompt/view configuration, tool bindings and scheduler triggers
remain the configuration source. There is one durable run path for chat and
scheduled execution; `POST /agents` and the old runtime DTO are removed.

## Run locally

Build from the repository root:

```sh
docker build -t puppyone-cloud-agent:workspace-v3 backend/agent-worker
```

Apply the complete ordered `supabase/migrations` release through the existing
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
Node 22.22.3, Git, ripgrep, fd-find (`fd` on PATH) and the runtime controller, Git relay and Pi worker modules. The E2B transport disables Internet
access and binds resources to execution/project metadata. E2B template creation
and hosted behavior have **not** been verified by the local Docker tests.

The image pins Node's base digest and Pi 0.85.1. Pi ships an npm shrinkwrap that
prevents root overrides from replacing two nested dependencies; the build runs
`patch-dependencies.mjs` to install checksum-verified undici 8.10.2 and
brace-expansion 5.0.12 into those exact locations. It does not patch Pi source.
Production image configuration should use the built registry digest. Docker
resolves and records its immutable image ID before allocating each execution.

## Knowledge repository workspace

For a native full Project, admission fixes the repository generation, selected
branch and base OID. The sandbox uses stock Git `init` + `fetch` + `checkout` for
the initial working copy. Fetch transfers full reachable history; subsequent
turns resume the same Session sandbox and fast-forward from the same
Git service. No backend checkout, file inventory or Git bundle is produced.

A root-owned controller launches Pi and tools as uid 1000. The sandbox has no
Internet access or cloud credentials. During preparation and final push only,
a loopback HTTP listener relays bounded smart-HTTP bytes over the provider's
private control channel to `version_engine/adapters/git/run_transport.py`.
That adapter uses the same `NativeGitRepository` and ref transaction service
as public `/git/{project}.git`. It owns the Git protocol; Agent only supplies
an admitted project, original base, candidate and fenced run identity.

Read tools reuse the existing provider recovery point. Detached tool writers are
terminated before confirming a mutation boundary. Mutating tools, including
failed shell commands, save a provider snapshot before acknowledging the tool
receipt. Conversation checkpoints contain bounded Pi entries and opaque recovery
references in PostgreSQL, never project files or pack data. On successful model
completion, the controller stops all model processes, stages and commits changes
once, preserves the exact candidate, then runs stock `git push --porcelain`.
Unchanged runs create no commit or push. Original operation receipts reconcile
lost replies; conflicts retain the candidate without force pushing.

Docker recovery uses a root-private named volume with immutable snapshot
directories; unchanged files share hardlinks. It survives container deletion,
not loss of the Docker data disk. E2B uses native durable snapshots and reconnects
the command stream with sequence deduplication. Recovery restarts the controller
and model under a new execution fence after faults. Normal successive turns
preserve the sandbox, `.git`, index and worktree without snapshot restoration.

Session ownership is durable in `agent_session_workspaces`, separate from the
Run execution lease. After confirmed publication/no changes, the controller
stops its processes and Git relay, then Docker pauses the persistent container
or E2B pauses the VM. New admitted work explicitly resumes that same resource.
The worker independently reclaims clean workspaces after six hours without an
active/queued Run. Claims are indexed, bounded and fenced; lost deletion replies
are retried against the original resource identity. An absent or incompatible
resource is retired before allocating a new identity. Provider timeouts are not
treated as confirmed absence. Explicit Session/Project deletion preserves an
independent cleanup record until provider deletion is confirmed.

The worker requires the Session workspace and incremental object-proof schema
before activation. Build and deploy the matching controller artifact; drain
older workers before changing the protocol/template. SQL and artifact readiness
are release prerequisites, not runtime fallbacks to the earlier lifecycle.

Full native Project views are supported. Restricted views reject before fetching
broader history. The selected branch must have a UTF-8 name; detached HEAD rejects.
Conversation payloads are limited to 8 MiB, each Git transport body to 128 MiB,
and Docker working memory/disk by its provider limits. Exceeding a limit fails
explicitly; history is never silently truncated. Recovery material is retained
while referenced by run/tool checkpoints. Resource cleanup only removes compute;
it must not erase a referenced recovery volume or snapshot.

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

### Runtime measurements

API and supervisor console logs use the shared JSON logger in containers
(`LOG_JSON_CONSOLE=1` explicitly enables it). At the end of each execution,
`cloud_agent_performance` contains `record.extra.agent_performance` with the
Run ID, actual database HTTP attempts/failures, peak in-flight count, first
text/total time and phase timings. At the end of each API response or SSE
subscription, `cloud_agent_api_performance` contains the corresponding
`record.extra.agent_api_performance` report, route template and Run ID when
present in the route. The logging bridge preserves these two metric fields;
arbitrary request extras and content are not copied.

Keep API and supervisor counts separate when collecting a hosted baseline.
Each peak in-flight value covers that individual trace, not the whole cluster;
phase durations can overlap and must not be added as a total. Dispatcher idle
polls and object-storage requests/bytes are not included in these reports.
The isolated integration test's aggregate database trace remains a separate
measurement, not a substitute for hosted observation.

Runs, execution generations, tool receipts and bounded events live in PostgreSQL.
Versioned checksummed manifests store full Pi session entries (including compaction
and inactive branches), repository base and provider recovery reference. Files,
Git history, index and untracked work remain inside the provider snapshot. A
completed model turn is recorded separately from final committed recovery so a
crash between those stages can be recovered. An old execution cannot write after takeover. Completed receipts can restore
missing tool results without re-execution; an `executing` receipt is unknown and
is never replayed. The trusted controller freezes tool processes before
saving the provider recovery point. Recovery objects must be durable before
provider deletion. Storage failure retains the provider and transfers cleanup
to the durable run owner. Terminal states remain immutable on cleanup retries.

Unpublished changes remain in recovery objects. This backend does not automatically
publish an unknown tool outcome, resolve conflicts or turn an old display-only
chat into a resumable Pi session. A new session is required when the previous
run requires resolution; operator recovery uses the retained base/provider snapshot/receipts.

Publication reuses the canonical Version Engine, original base and SQL fencing.
Native Git publication conditionally updates its fixed ref and preserves original
commit IDs; it never regenerates commits or force-pushes over another author.
Cloud Agent admission requires a Project Git surface and a valid authoritative
native HEAD, including one installed by a verified migration or product write.
It does not require an external client's first Git push. The existing external
client onboarding projection keeps its separate first-push gate. Admission still
evaluates the real Project grant and saved Agent policy; publication retains its
authority, lifecycle, billing and CAS checks. Real PostgreSQL/PostgREST/S3 tests
cover admission after a product commit without a Git push, missing HEAD/surface,
cross-account denial and the one-request context budget.

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
node --test agent-worker/git.test.mjs
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
| Three consecutive turns on SHA-1 and SHA-256 repositories | One sandbox allocation, two same-ID resumptions and three pauses; original parents survive; an unchanged turn creates no commit. |
| Six-hour boundary, queued work, provider loss and late cleanup ACK | Only eligible resources are retired; reconstruction has a new identity and preserves canonical Git history. |
| Another writer changes the cloud while paused | Wakeup incrementally fetches and fast-forwards without replacing the sandbox. |
| Small push over long retained history | Durable proofs bound verification to new objects/dependencies; no per-history object downloads or S3 LIST. |
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

### Acceptance boundaries

Local acceptance exercises real Pi, Docker, PostgreSQL, PostgREST, MinIO and
stock Git. The model response source is deterministic. E2B acceptance is an
explicit opt-in test against a template built from the same committed artifact;
Docker results alone do not establish hosted provider behavior. No test runs
an inventory or migration against existing customer projects.

To run the explicit E2B acceptance suite, set `E2B_API_KEY`,
`CLOUD_AGENT_E2B_TEMPLATE` to the test artifact, and `CLOUD_AGENT_TEST_E2B=1`,
then run `uv run pytest tests/agent/runtime/test_e2b_workspace.py -q`. This creates
and deletes test sandboxes/snapshots; the Git test uses an owned local database
and object service through the private relay, not a hosted customer project.
