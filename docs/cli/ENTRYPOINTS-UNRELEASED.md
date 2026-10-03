# Entrypoint CLI changes — unreleased

Status: local ISSUE-060/061 implementation; not an npm release or deployment receipt.
The package version remains `0.2.1`; an already installed `0.2.1` must **not** be
assumed to contain these changes. Use the CLI and backend from the same task
branch for verification. Generic Synchronize and Access use the committed
ISSUE-058 contracts (`7c663506` and `2780dd76`). The later `6926aea3` integration
also supplies GitHub/database-source and typed Dashboard APIs. This CLI's `status`
now consumes that Dashboard contract; server compatibility retirement and actual
installed-client cutover remain coordinated by ISSUE-058.

## Commands

```bash
# Human control-plane authentication and active project are required.
puppyone import providers
puppyone import create https://example.com/page --provider url --folder /notes --idempotency-key request-1
puppyone import info <job-id>
puppyone import cancel <job-id>

puppyone synchronize providers
puppyone synchronize add gmail https://mail.google.com/mail/u/0/#inbox --folder /mail --mode manual
puppyone synchronize add url https://example.com/page --folder /pages --mode scheduled --schedule '0 9 * * *' --timezone UTC
puppyone synchronize ls
puppyone synchronize refresh <binding-id>
puppyone synchronize runs <binding-id>
puppyone synchronize run <run-id>
puppyone synchronize pause <binding-id>
puppyone synchronize resume <binding-id>
puppyone synchronize trigger <binding-id> manual

puppyone access add agent 'My agent'
puppyone access add mcp 'Context API'
puppyone access add sandbox 'My sandbox'
puppyone access providers
puppyone access ls --kind mcp
puppyone access info <surface-id>
puppyone access pause <surface-id>
puppyone access resume <surface-id>
puppyone access update <surface-id> --set description='Updated description'
puppyone access key <surface-id> --regenerate
puppyone access rm <surface-id>
```

Import configuration is a JSON object (`--config`) or `--set key=value`.
Synchronize uses structured `source.*`/`options.*` keys, for example
`--set options.max_results=10`. The external URL is not the Project target path.
A scheduled command requires explicit five-field cron; its default timezone is UTC.
OAuth account authorization remains shared Provider infrastructure, not an Access
surface. Provider admission and credentials are ultimately checked by the server.

## Current compatibility boundary

| This source CLI | Matching current backend | Intent |
| --- | --- | --- |
| `import` | `/api/v1/imports`, including additive `/providers` | one-shot ImportJob |
| `synchronize` | `/api/v1/synchronize/bindings`, `/providers`, `/runs/{run_id}` | durable binding/run |
| `access` | `/api/v1/access/surfaces`, including `/types` | Access surface; creates Agent/MCP/Sandbox |
| `status` | `/api/v1/projects/{project_id}/dashboard/resources` | typed read-only resource inventory |

Synchronize and Access require the canonical APIs from ISSUE-058 and are verified
by a loopback server with **no legacy `/integrations` or `/access` routes mounted**.
A missing canonical route returns `SERVER_UPGRADE_REQUIRED`; resource-not-found
errors remain distinct. The CLI never retries against legacy URLs. Create output contains `binding` and `execution_result`; run references use
`synchronize_binding_id`/`synchronize_run_id`, never Access IDs.

Access sends `kind`, never the retired `provider` wire selector, and includes
`X-PuppyOne-Repository-Contract: 2`. Ordinary metadata has no plaintext credentials;
explicit rotation returns `access_surface_id` and `credential`. Metadata updates
send only the requested patch; the owning service merges it without replaying a
stale/redacted inventory snapshot. Import discovery requires the additive
`/imports/providers` route from this task.
Do not remove server compatibility routes until 058 has consumer/exit evidence.
An older server without Import discovery returns an explicit API error. No
supported released-client/server matrix is asserted yet: the tested combination
is this source CLI plus the matching task-branch backend, not published npm 0.2.1.

`status` preserves the `(resource_kind, resource_id)` identity, so an Access
surface and Synchronize binding with the same ID remain separate. Human output
shows domain-qualified IDs, provider versus Access kind, explicit target and last
activity. JSON preserves the canonical `resources` envelope; it does not interpret
`access_points` or `connections`. Foreign/duplicate identities, missing targets
and credential-bearing resource metadata fail rather than becoming an empty
successful inventory. Classification conflicts and storage failures are surfaced;
a missing canonical endpoint requires a server upgrade, with no old Dashboard
request. Empty-state guidance separates Import, Synchronize and Access.

## Legacy and error behavior

- `access add <external-provider> <url>` forwards to Import by default. Explicit
  `--mode import_once` does the same; `manual`/`scheduled` forwards to Synchronize.
  Human output warns about the deprecated command. JSON identifies `resource_kind`.
- `direct`/`cli`/`git_remote`/`filesystem` creation is rejected with credential/remote
  guidance. `--gateway` and the mixed `database` alias are rejected, not silently
  translated. Database Import still requires its dedicated source/table workflow.
- Generic GitHub Synchronize is rejected: its continuous binding is a separate
  workflow; the GitHub snapshot adapter is Import-only.
- `access refresh/run/logs/trigger <surface-id>` fails before a request. Discover a
  real binding with `synchronize ls`; never reuse a surface ID as a binding ID.
- HTTP failures exit nonzero. Business/transport failures do not trigger mutation
  replay or legacy fallback; the existing authentication layer may retry once
  after refreshing an expired session on 401. An Import
  idempotency key returns the recorded job, including a recorded failure; inspect
  that job before deciding to submit a new attempt. Cancellation does not promise
  to undo a version write already committed.
- `--permission` and Agent `--scope`/`--folder` creation fail before a request:
  the generic Access create fields cannot safely express those grants. Configure
  them through the owning Access/Agent workflow, rather than accepting an ignored
  flag and accidentally creating a broader surface.
- `access ls --provider <kind>` is a deprecated CLI alias for `--kind`; both send
  only `kind`. Supplying both flags or a source-provider filter is rejected.
- `--model`/`--system-prompt` and Agent create config `model`/`llm_model`/
  `system_prompt` are explicitly rejected: the current creation service does not
  consume them. `--type` is Agent-only; Sandbox runtime uses `--set runtime=...`.
  Source-only options and ambiguous `--scope` plus `--folder` are not ignored.
- Plaintext credentials are not recovered by `access key`. Explicit
  `--regenerate` rotates and reveals the newly issued credential once.

## Reproducible local gates

```bash
cd cli && npm ci --ignore-scripts && npm run test:unit
cd ../backend
uv run --frozen --offline pytest -q tests/platform/test_entrypoint_cli_http.py tests/platform/test_dashboard_cli_http.py
uv run --frozen --offline pytest -q tests/platform/test_entrypoint_queue_cutover.py
```

The HTTP gate executes the actual Node CLI against real FastAPI routers,
application services and authorization policy on loopback. Storage/queue/provider
IO and entitlement facts are substituted. Separate Redis/ARQ tests use disposable
Redis. Neither gate is production DDL, credentials, queue-drain or deployment proof.

Worker configuration also changes: `upload_worker`, `import_worker`, and
`synchronize_worker`. Upload owns its existing `ETL_*` runtime policy and keeps
the `etl` queue; reusable file processing has no queue/task lifecycle settings.
Stop old producers and drain ready/deferred/retry/in-progress
work before cutover; never merely rename a live service or delete Redis keys.
`python -m src.infra.queue_cutover --queues <actual-old-queue-names>` is read-only.
The scanner also blocks on serialized job payloads outside the named queues;
it does not remove orphaned jobs. A clean snapshot cannot prove producers stopped. Rollback must stop new producers
and drain new work before restarting old workers; serialized dispatch names are
not interchangeable and no permanent legacy aliases are registered.
