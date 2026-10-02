# Entrypoint CLI changes — unreleased

Status: local ISSUE-060/061 implementation; not an npm release or deployment receipt.
The package version remains `0.2.1`; an already installed `0.2.1` must **not** be
assumed to contain these changes. Use the CLI and backend from the same task
branch for verification. Final public-path cutover is coordinated by ISSUE-058.

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
puppyone synchronize pause <binding-id>
puppyone synchronize resume <binding-id>
puppyone synchronize trigger <binding-id> manual

puppyone access add agent 'My agent'
puppyone access add mcp 'Context API'
puppyone access add sandbox 'My sandbox'
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
| `synchronize` | `/api/v1/integrations/connections`, `/connectors` | durable binding/run |
| `access` | `/api/v1/access` | Agent/MCP/Sandbox surface |

The latter two public paths are **temporary**, not the final ISSUE-058 contract.
The CLI cutover must land with that API's `/synchronize/bindings`, `/providers`,
run endpoints and `/access/surfaces` artifacts and be retested over real HTTP.
Do not remove server compatibility routes until 058 has consumer/exit evidence.
An older server without Import discovery returns an explicit API error; the CLI
never retries against a mixed Access endpoint or guesses another resource ID.
No supported released-client/server matrix is asserted yet.

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
- HTTP failures exit nonzero. Mutations are not automatically replayed. An Import
  idempotency key returns the recorded job, including a recorded failure; inspect
  that job before deciding to submit a new attempt. Cancellation does not promise
  to undo a version write already committed.
- Plaintext credentials are not recovered by `access key`. Explicit
  `--regenerate` rotates and reveals the newly issued credential once.

## Reproducible local gates

```bash
cd cli && npm ci --ignore-scripts && npm run test:unit
cd ../backend
uv run --frozen --offline pytest -q tests/platform/test_entrypoint_cli_http.py
uv run --frozen --offline pytest -q tests/platform/test_entrypoint_queue_cutover.py
```

The HTTP gate executes the actual Node CLI against real FastAPI routers,
application services and authorization policy on loopback. Storage/queue/provider
IO and entitlement facts are substituted. Separate Redis/ARQ tests use disposable
Redis. Neither gate is production DDL, credentials, queue-drain or deployment proof.

Worker configuration also changes: `upload_worker`, `import_worker`, and
`synchronize_worker`. Stop old producers and drain ready/deferred/retry/in-progress
work before cutover; never merely rename a live service or delete Redis keys.
`python -m src.infra.queue_cutover --queues <actual-old-queue-names>` is read-only.
A clean snapshot cannot prove producers stopped. Rollback must stop new producers
and drain new work before restarting old workers; serialized dispatch names are
not interchangeable and no permanent legacy aliases are registered.
