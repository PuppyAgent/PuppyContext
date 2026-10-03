import assert from "node:assert/strict";
import { Command } from "commander";
import { registerGlobalCommands } from "../src/commands/global.js";

const dashboard = () => ({
  project: { id: "project-1", name: "Test" }, nodes: { total: 0, folders: 0, files: 0 },
  resources: [
    { resource_kind: "synchronize", resource_id: "same", project_id: "project-1", provider: "url", path: "notes", status: "active" },
    { resource_kind: "access", resource_id: "same", project_id: "project-1", kind: "mcp", status: "paused",
      target: { kind: "scope", project_id: "project-1", scope_id: "scope-1" } },
  ], tools: [], uploads: [],
});
class Exit extends Error {}
async function run(data, { status = 200, json = true, detail = "Not Found" } = {}) {
  const saved = { fetch: globalThis.fetch, log: console.log, error: console.error, exit: process.exit };
  const calls = [], logs = [];
  let code = 0;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify(status === 200 ? { code: 0, data } : { detail }), {
      status, headers: { "Content-Type": "application/json" },
    });
  };
  console.log = console.error = value => logs.push(String(value));
  process.exit = code => { throw new Exit(String(code)); };
  try {
    const program = new Command().exitOverride().option("--json").option("--api-url <url>")
      .option("--api-key <key>").option("--project <id>");
    registerGlobalCommands(program);
    assert.match(program.commands[0].description(), /Synchronize.*Access/);
    await program.parseAsync(["node", "puppyone", ...(json ? ["--json"] : []), "--api-url", "http://unit.test",
      "--api-key", "test-token", "--project", "project-1", "status"]);
  } catch (error) {
    if (!(error instanceof Exit)) throw error;
    code = Number(error.message);
  } finally {
    globalThis.fetch = saved.fetch;
    console.log = saved.log;
    console.error = saved.error;
    process.exit = saved.exit;
  }
  assert.equal(calls.length, 1, "never fall back to an untyped dashboard or retry a failed read as empty");
  assert.equal(new URL(calls[0].url).pathname, "/api/v1/projects/project-1/dashboard/resources");
  assert.equal(calls[0].options.method, "GET");
  assert.equal(calls[0].options.headers["X-PuppyOne-Repository-Contract"], "2");
  return { code, output: logs.join("\n"), data: json ? JSON.parse(logs.at(-1)) : null };
}
const result = await run(dashboard());
assert.equal(result.code, 0);
assert.deepEqual(result.data.dashboard.resources, dashboard().resources);
const human = await run(dashboard(), { json: false });
assert.match(human.output, /synchronize:same/);
assert.match(human.output, /access:same/);
assert.match(human.output, /scope:scope-1/);
assert.match(human.output, /path:notes/);
assert.doesNotMatch(human.output, /Access Points/);
const empty = await run({ ...dashboard(), resources: [] }, { json: false });
assert.match(empty.output, /import create/);
assert.match(empty.output, /synchronize add/);
assert.match(empty.output, /access add agent\|mcp\|sandbox/);
assert.doesNotMatch(empty.output, /access add <provider>/);

for (const mutate of [
  data => { delete data.resources; data.connections = []; },
  data => { data.project.id = "foreign"; },
  data => { data.resources[0].project_id = "foreign"; },
  data => { data.resources.push(data.resources[0]); },
  data => { data.resources[0].resource_kind = "connection"; },
  data => { data.resources[0].resource_id = ""; },
  data => { data.resources[0].path = null; },
  data => { data.resources[1].target.project_id = "foreign"; },
  data => { data.resources[1].target.scope_id = ""; },
  data => { data.resources[1].target.kind = "project_root"; },
  data => { data.resources[1].credential = "secret-must-not-leak"; },
]) {
  const invalid = dashboard(); mutate(invalid);
  const failed = await run(invalid);
  assert.equal(failed.code, 1);
  assert.equal(failed.data.error.code, "INVALID_DASHBOARD_RESPONSE");
  assert.equal(failed.data.success, false);
  assert.doesNotMatch(failed.output, /secret-must-not-leak/);
}
for (const status of [403, 404, 409, 503]) {
  const failed = await run(null, { status });
  assert.equal(failed.code, 1);
  assert.equal(failed.data.success, false);
  if (status === 404) assert.equal(failed.data.error.code, "SERVER_UPGRADE_REQUIRED");
}
const missingProject = await run(null, { status: 404, detail: "Project not found" });
assert.notEqual(missingProject.data.error.code, "SERVER_UPGRADE_REQUIRED");
console.log("typed Dashboard CLI contracts: ok");
