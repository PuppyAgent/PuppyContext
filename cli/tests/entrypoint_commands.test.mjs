import assert from "node:assert/strict";
import { Command } from "commander";
import { registerAccess } from "../src/commands/access.js";
import { registerEntryPoints, sourceConfig } from "../src/commands/entrypoints.js";

class Exit extends Error {}
async function run(args, status = 200) {
  const calls = [], logs = [];
  const saved = { fetch: globalThis.fetch, log: console.log, error: console.error, exit: process.exit };
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ path: new URL(url).pathname, method: options.method,
      body: options.body ? JSON.parse(options.body) : null });
    return new Response(JSON.stringify(status === 200 ? { code: 0, data: { id: "created-1" } }
      : { detail: "Queue unavailable" }), { status, headers: { "Content-Type": "application/json" } });
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
  return { calls, exit, json: JSON.parse(logs.at(-1)) };
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
  assert.equal(result.calls[0].path, "/api/v1/integrations/connections");
  assert.deepEqual(result.calls[0].body.config, {
    source: { resource_url: "https://mail.google.com/mail/u/0/#inbox" }, options: { max_results: 3 },
  });
  assert.equal(result.json.resource_kind, "synchronize_binding");
}
const scheduled = await run(["synchronize", "add", "url", "https://example.com", "--folder", "/pages",
  "--mode", "scheduled", "--schedule", "0 9 * * *"]);
assert.deepEqual(scheduled.calls[0].body.trigger, { type: "scheduled", schedule: "0 9 * * *", timezone: "UTC" });
for (const kind of ["mcp", "agent", "sandbox"]) {
  const result = await run(["access", "add", kind, "My surface", "--scope", "/notes"]);
  assert.equal(result.calls[0].path, "/api/v1/access");
  assert.equal(result.calls[0].body.path, "/notes");
  assert.equal(result.json.resource_kind, "access_surface");
}
for (const args of [
  ["access", "add", "direct"],
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
  [["synchronize", "providers"], "/integrations/connectors", "GET"],
  [["synchronize", "runs", "binding-1"], "/integrations/connections/binding-1/runs", "GET"],
  [["synchronize", "pause", "binding-1"], "/integrations/connections/binding-1/pause", "POST"],
]) {
  const result = await run(args);
  assert.equal(result.calls[0].path, `/api/v1${path}`);
  assert.equal(result.calls[0].method, method);
}
const unavailable = await run(["import", "create", "https://example.com"], 503);
assert.equal(unavailable.exit, 1);
assert.equal(unavailable.calls.length, 1, "never automatically replay a failed mutation");
assert.throws(() => sourceConfig({ set: ["__proto__.polluted=true"] }));
assert.equal({}.polluted, undefined);
console.log("entrypoint command request/error contracts: ok");
