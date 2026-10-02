# Local Web Preview and Resource Regression

> Status: Operational
> Canonical owner: Puppyone Cloud Engineering
> Code source: `puppyone-cloud`
> Last verified: 2026-10-03 against `puppyone-cloud@43d3aa92`

These changes are locally verified task-branch work, not a published release.
Desktop's service admission/ownership contract has one canonical owner:
`puppy-issues@88c2de66:document/puppyone-desktop/operations/local-cloud-development.md`.
This document owns the Web build, health interface and regression harness.

## Explicit production preview

Run `npm run build:local-login` from `frontend/` after configuring that checkout's
**local** public Supabase/API/Web environment. Public environment values are
baked into the build: rebuild after changing source, dependencies or those
values. Desktop's runtime environment cannot rewrite the browser bundle.

The build has one CPU and a 2048 MiB V8 old-space budget. It writes
`.next-desktop-login`, separate from live Web HMR's `.next`, then prepares
`standalone/public` and `standalone/.next-desktop-login/static` using the same
packaging contract as `Dockerfile.local`. Desktop runs the generated standalone
`server.js`, not `next start` with `output: standalone`. It never builds this
artifact implicitly. Do not treat a partial/failed build as a usable preview.

## Health interface

`GET /api/health` is a non-cached liveness response:

```json
{"schemaVersion":1,"service":"puppyone-cloud-web","status":"ok","mode":"production","version":"0.0.0"}
```

`mode` is `development` for `next dev`. `HEAD` is body-free. The route sends
`Cache-Control: no-store`, sets no cookies, and imports no auth, database, storage
or page-rendering dependencies. It is not a dependency readiness or login
certification endpoint. Periodic monitoring must not render `/login`.

## Synthetic regression

Use a clean, isolated frontend test checkout with no real `.env` files. Build
its fixture without creating a local environment file:

```bash
NEXT_PUBLIC_SUPABASE_URL=http://127.0.0.1:9 \
NEXT_PUBLIC_SUPABASE_ANON_KEY=synthetic-memory-test-key \
NEXT_PUBLIC_API_URL=http://127.0.0.1:9 \
NEXT_PUBLIC_APP_URL=http://127.0.0.1:3000 \
npm run build:local-login

npm run test:server-memory -- --scenario=health
npm run test:server-memory -- --scenario=login
```

The default harness mode is `preview`. It starts a bounded custom Next server
using the same production build/runtime on an ephemeral loopback port. The
Desktop integration suite separately verifies the actual standalone executable,
static assets, service reuse, teardown and released ports. Neither test completes
an actual user authentication round-trip or connects to a live backend/database.

The harness warms routes before collecting post-GC samples. Defaults are 30
samples, five requests per sample, 1000 ms intervals, a 1536 MiB old-space
budget, 2048 MiB RSS ceiling and a 180-second deadline. CI uses smaller
768/1024 MiB heap/RSS ceilings and 300 requests per scenario. A median retained
heap growth over 32 MiB fails independently of the absolute memory budget.
Reports preserve samples, CPU counters, trends, Node/Next versions and limits.

A disposable HMR source sandbox prevents Next's generated type/config writes
from changing the source checkout. The worker has a synthetic home/environment,
does not inherit user SDK credentials, and refuses real environment files.
Workers are stopped and scratch code removed on success, failure or timeout;
`report.json` and a bounded `server.log` remain under the reported temporary
artifact directory. Explicit `--cpu-profile=true` / `--snapshot=true` options
create local diagnostics; heap snapshots themselves need additional memory.
The workflow uploads only the synthetic JSON/log receipts, not heap/profile files.

`.github/workflows/frontend-resource-regression.yml` defines bounded production
health/login regression on frontend PRs and `qubits`/`main` changes. Its YAML and
local commands were checked; no remote workflow run or branch-protection change
is claimed.

## Known development-runtime gap

With Next 15.5.9, 200 synthetic development login requests retained about
130 MiB after GC. A temporary 15.5.27 maintenance comparison retained about
129.8 MiB and was rolled back; dependency versions are unchanged. A production
comparison with 300 health/login requests grew the median baseline by only
0.146/0.692 MiB, with about 243/249 MiB peak RSS in the measured window.

`--mode=dev --scenario=login --samples=20 --interval-ms=250 --requests-per-sample=10`
reproduces the development gap and intentionally fails the growth gate. The
application delivery avoids this runtime for background login; it does **not**
claim that the upstream retention bug is repaired. Do not patch `node_modules`,
raise budgets, periodically clear build caches, or schedule restarts and call
that the root-cause fix. Long-duration soak and a verified upstream repair remain
follow-up work; finite passing windows cannot exclude slower leaks.

Sources: `puppyone-cloud@43d3aa92:frontend/scripts/build-local-login.mjs`,
`puppyone-cloud@43d3aa92:frontend/app/api/health/route.ts`,
`puppyone-cloud@43d3aa92:frontend/scripts/check-server-memory.mjs`, and
`puppyone-cloud@43d3aa92:.github/workflows/frontend-resource-regression.yml`.
