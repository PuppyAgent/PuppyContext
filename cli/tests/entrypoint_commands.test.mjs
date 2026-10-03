import assert from "node:assert/strict";
import { Command } from "commander";
import { registerAccess } from "../src/commands/access.js";
import { registerEntryPoints, sourceConfig } from "../src/commands/entrypoints.js";

class Exit extends Error {}
async function run(args, status = 200, response = {}) {
  const calls = [], logs = [], requests = [];
  const saved = { fetch: globalThis.fetch, log: console.log, error: console.error, exit: process.exit };
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ path: new URL(url).pathname, method: options.method,
      body: options.body ? JSON.parse(options.body) : null });
    requests.push({ headers: options.headers, query: Object.fromEntries(new URL(url).searchParams) });
    const data = response.data ?? (new URL(url).pathname.endsWith("/bindings") && options.method === "POST"
      ? { binding: { id: "created-1" }, execution_result: null }
      : new URL(url).pathname.endsWith("/types") ? ["agent", "mcp", "sandbox"].map(kind => ({ kind }))
      : { id: "created-1" });
    return new Response(JSON.stringify(status >= 200 && status < 300 ? { code: 0, data }
      : { detail: response.detail ?? (status === 404 ? "Not Found" : "Queue unavailable") }),
      { status, headers: { "Content-Type": "application/json" } });
  };
  console.log = console.error = value => logs.push(String(value));
  process.exit = code => { throw new Exit(String(code)); };
  let exit = 0;
  try {
    const program = new Command().exitOverride().enablePositionalOptions()
      .option("--json").option("-u, --api-url <url>").option("-k, --api-key <key>").option("-p, --project <id>");
    registerAccess(program);
    registerEntryPoints(program);
    await program.parseAsync(["node", "puppyone", "--json", "--api-url", "http://unit.test",
      "--api-key", "test-token", "--project", "project-1", ...args]);
  } catch (error) {
    if (!(error instanceof Exit)) throw error;
    exit = Number(error.message);
  } finally {
    globalThis.fetch = saved.fetch;
    console.log = saved.log;
    console.error = saved.error;
    process.exit = saved.exit;
  }
  return { calls, requests, exit, json: JSON.parse(logs.at(-1)) };
}

for (const args of [
  ["import", "create", "https://example.com/page", "--provider", "url"],
  ["access", "add", "url", "https://example.com/page"],
  ["access", "add", "url", "https://example.com/page", "--mode", "import_once"],
]) {
  const result = await run([...args, "--folder", "/notes", "--idempotency-key", "attempt-1"]);
  assert.equal(result.exit, 0);
  assert.equal(result.calls.length, 1);
  assert.deepEqual(result.calls[0], { path: "/api/v1/imports", method: "POST", body: {
    project_id: "project-1", provider: "url", source_url: "https://example.com/page",
    target_path: "/notes", idempotency_key: "attempt-1", config: {},
  } });
  assert.equal(result.json.resource_kind, "import_job");
}
for (const args of [["synchronize", "add"], ["access", "add"]]) {
  const result = await run([...args, "gmail", "https://mail.google.com/mail/u/0/#inbox",
    "--folder", "/mail", "--mode", "manual", "--set", "options.max_results=3"]);
  assert.equal(result.exit, 0);
  assert.equal(result.calls[0].path, "/api/v1/synchronize/bindings");
  assert.deepEqual(result.calls[0].body.config, {
    source: { resource_url: "https://mail.google.com/mail/u/0/#inbox" }, options: { max_results: 3 },
  });
  assert.equal(result.json.resource_kind, "synchronize_binding");
  assert.equal(result.json.binding.id, "created-1");
}
const scheduled = await run(["synchronize", "add", "url", "https://example.com", "--folder", "/pages",
  "--mode", "scheduled", "--schedule", "0 9 * * *"]);
assert.deepEqual(scheduled.calls[0].body.trigger, { type: "scheduled", schedule: "0 9 * * *", timezone: "UTC" });
for (const kind of ["mcp", "agent", "sandbox"]) {
  const result = await run(["access", "add", kind, "My surface", ...(kind === "agent" ? [] : ["--scope", "/notes"])]);
  assert.equal(result.calls[0].path, "/api/v1/access/surfaces");
  assert.equal(result.calls[0].body.kind, kind);
  assert.equal("provider" in result.calls[0].body, false);
  assert.equal(result.requests[0].headers["X-PuppyOne-Repository-Contract"], "2");
  assert.equal(result.calls[0].body.path, kind === "agent" ? null : "/notes");
  assert.equal(result.json.resource_kind, "access_surface");
}
for (const args of [
  ["access", "add", "direct"],
  ["access", "add", "mcp", "--permission", "read"],
  ["access", "add", "agent", "--scope", "/notes"],
  ["access", "add", "agent", "--model", "ignored"],
  ["access", "add", "agent", "--system-prompt", "ignored"],
  ["access", "add", "agent", "--set", "llm_model=ignored"],
  ["access", "add", "sandbox", "--type", "ignored"],
  ["access", "add", "mcp", "--scope", "/one", "--folder", "/two"],
  ["access", "add", "mcp", "--idempotency-key", "import-only"],
  ["access", "ls", "--kind", "mcp", "--provider", "sandbox"],
  ["access", "ls", "--kind", "github"],
  ["access", "ls", "--kind", ""],
  ["access", "key", "surface-1"],
  ["access", "add", "database", "postgresql://unit.test/db"],
  ["access", "add", "url", "https://example.com", "--gateway", "retired"],
  ["access", "refresh", "surface-1"], ["access", "run", "surface-1"],
  ["access", "logs", "surface-1"], ["access", "trigger", "surface-1", "manual"],
  ["synchronize", "add", "url", "https://example.com", "--mode", "import_once"],
  ["synchronize", "add", "github", "https://github.com/a/b", "--folder", "/repo"],
  ["synchronize", "add", "url", "https://example.com"],
  ["synchronize", "add", "url", "https://example.com", "--folder", "/pages", "--mode", "scheduled"],
  ["access", "add", "url", "https://example.com", "--schedule", "0 9 * * *"],
  ["import", "create", "https://example.com", "--config", "[]"],
]) {
  const result = await run(args);
  assert.equal(result.exit, 1, args.join(" "));
  assert.equal(result.calls.length, 0, "invalid intent must fail before network side effects");
  assert.equal(result.json.success, false);
}
for (const [args, path, method] of [
  [["import", "cancel", "job-1"], "/imports/job-1", "DELETE"],
  [["import", "info", "job-1"], "/imports/job-1", "GET"],
  [["import", "providers"], "/imports/providers", "GET"],
  [["synchronize", "providers"], "/synchronize/providers", "GET"],
  [["synchronize", "runs", "binding-1"], "/synchronize/bindings/binding-1/runs", "GET"],
  [["synchronize", "pause", "binding-1"], "/synchronize/bindings/binding-1/pause", "POST"],
  [["synchronize", "run", "run-1"], "/synchronize/runs/run-1", "GET"],
  [["access", "ls"], "/access/surfaces", "GET"],
  [["access", "providers"], "/access/surfaces/types", "GET"],
  [["access", "schema", "mcp"], "/access/surfaces/types", "GET"],
  [["access", "info", "surface-1"], "/access/surfaces/surface-1", "GET"],
  [["access", "pause", "surface-1"], "/access/surfaces/surface-1", "PATCH"],
  [["access", "resume", "surface-1"], "/access/surfaces/surface-1", "PATCH"],
  [["access", "rm", "surface-1"], "/access/surfaces/surface-1", "DELETE"],
  [["access", "key", "surface-1", "--regenerate"], "/access/surfaces/surface-1/regenerate-key", "POST"],
]) {
  const result = await run(args);
  assert.equal(result.calls[0].path, `/api/v1${path}`);
  assert.equal(result.calls[0].method, method);
}
for (const flag of ["--kind", "--provider"]) {
  const result = await run(["access", "ls", flag, "mcp", "--status", "active"]);
  assert.deepEqual(result.requests[0].query, { project_id: "project-1", kind: "mcp", status: "active" });
}
const update = await run(["access", "update", "surface-1", "--set", "description=updated"]);
assert.equal(update.calls.length, 1, "send only the patch; never write back a stale/redacted GET snapshot");
assert.deepEqual(update.calls[0], { path: "/api/v1/access/surfaces/surface-1", method: "PATCH",
  body: { config: { description: "updated" } } });
const rotated = await run(["access", "key", "surface-1", "--regenerate"], 200, {
  data: { access_surface_id: "surface-1", credential: "issued-once" },
});
assert.deepEqual(rotated.json.credential, { access_surface_id: "surface-1", credential: "issued-once" });
const absent = await run(["access", "info", "missing"], 404, { detail: "Access connection not found" });
assert.notEqual(absent.json.error.code, "SERVER_UPGRADE_REQUIRED", "resource 404 is not a missing API");
const shadowed = await run(["access", "ls"], 404, { detail: "Access connection not found" });
assert.equal(shadowed.json.error.code, "SERVER_UPGRADE_REQUIRED");
const missingProject = await run(["access", "ls"], 404, { detail: "Project not found" });
assert.notEqual(missingProject.json.error.code, "SERVER_UPGRADE_REQUIRED");
for (const status of [403, 404, 405, 409, 426, 503]) {
  const failed = await run(["access", "add", "sandbox", "My surface"], status);
  assert.equal(failed.exit, 1);
  assert.equal(failed.calls.length, 1, "Access mutations must never fall back or replay");
  assert.equal(failed.calls[0].path, "/api/v1/access/surfaces");
  if ([404, 405].includes(status)) assert.equal(failed.json.error.code, "SERVER_UPGRADE_REQUIRED");
}
const unavailable = await run(["import", "create", "https://example.com"], 503);
assert.equal(unavailable.exit, 1);
assert.equal(unavailable.calls.length, 1, "never automatically replay a failed mutation");
const oldServer = await run(["synchronize", "add", "url", "https://example.com", "--folder", "/notes"], 404);
assert.equal(oldServer.exit, 1);
assert.equal(oldServer.json.error.code, "SERVER_UPGRADE_REQUIRED");
assert.equal(oldServer.calls.length, 1, "no legacy route/Access fallback after canonical API failure");
assert.throws(() => sourceConfig({ set: ["__proto__.polluted=true"] }));
assert.equal({}.polluted, undefined);
console.log("entrypoint command request/error contracts: ok");
