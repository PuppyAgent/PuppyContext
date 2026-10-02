/** Import and Synchronize resources. Access IDs are never accepted as binding IDs.
 * The current server still exposes Synchronize at /integrations; ISSUE-058 owns
 * the coordinated HTTP rename. This module is the single CLI cutover boundary.
 */
import { ApiError, createClient } from "../api.js";
import { requireProject, withErrors } from "../helpers.js";
import { createOutput } from "../output.js";

export const SYNCHRONIZE_ROOT = "/integrations";

export function sourceConfig(opts = {}) {
  let config;
  try { config = opts.config ? JSON.parse(opts.config) : {}; }
  catch { throw new ApiError(0, "INVALID_CONFIG", "--config must be a JSON object."); }
  if (!config || Array.isArray(config) || typeof config !== "object") {
    throw new ApiError(0, "INVALID_CONFIG", "--config must be a JSON object.");
  }
  for (const pair of opts.set || []) {
    const at = pair.indexOf("=");
    if (at <= 0) throw new ApiError(0, "INVALID_CONFIG", "--set requires key=value.");
    const keys = pair.slice(0, at).split(".");
    if (keys.some(key => !key || ["__proto__", "constructor", "prototype"].includes(key))) {
      throw new ApiError(0, "INVALID_CONFIG", "Unsafe or empty configuration key.");
    }
    let value = pair.slice(at + 1);
    try { value = JSON.parse(value); } catch { /* plain string */ }
    let target = config;
    for (const key of keys.slice(0, -1)) {
      if (target[key] == null) target[key] = {};
      if (Array.isArray(target[key]) || typeof target[key] !== "object") {
        throw new ApiError(0, "INVALID_CONFIG", `--set cannot descend into ${key}.`);
      }
      target = target[key];
    }
    target[keys.at(-1)] = value;
  }
  return config;
}

const ALIASES = { gh: "github", web: "url", gcal: "google_calendar", gdrive: "google_drive",
  gdocs: "google_docs", gsheets: "google_sheets", gsc: "google_search_console" };
export function providerName(value) {
  const name = String(value || "").toLowerCase().replaceAll("-", "_");
  return ALIASES[name] || name;
}

function checkLegacyOptions(opts) {
  if (opts.gateway) {
    throw new ApiError(0, "RETIRED_GATEWAY_OPTION", "--gateway is not a source credential.",
      "Use your authorized Provider account; the server resolves its credentials.");
  }
}

export function synchronizeTrigger(opts) {
  if (!["manual", "scheduled"].includes(opts.mode)) {
    throw new ApiError(0, "INVALID_SYNC_MODE", "Use manual or scheduled; snapshots belong to Import.");
  }
  if (opts.mode === "scheduled") {
    if (!opts.schedule || opts.schedule.trim().split(/\s+/).length !== 5) {
      throw new ApiError(0, "MISSING_SCHEDULE", "Scheduled synchronization requires --schedule '<5-field cron>'.");
    }
    return { type: "scheduled", schedule: opts.schedule, timezone: opts.timezone || "UTC" };
  }
  if (opts.schedule || opts.timezone) throw new ApiError(0, "INVALID_TRIGGER", "Schedule options require --mode scheduled.");
  return { type: "manual" };
}

export async function createSource(client, projectId, provider, source, opts = {}) {
  checkLegacyOptions(opts);
  const mode = opts.mode || "import_once";
  const name = providerName(provider);
  if (name === "database") {
    throw new ApiError(0, "DATABASE_SOURCE_COMMAND_REQUIRED", "Database queries are not Access surfaces or URL imports.",
      "Use the Database Import source/table workflow; access add database is retired.");
  }
  const config = sourceConfig(opts);
  if (mode === "import_once") {
    if (opts.schedule || opts.timezone) throw new ApiError(0, "INVALID_TRIGGER", "A one-time Import cannot have a schedule.");
    if (!source) throw new ApiError(0, "MISSING_SOURCE", "Import requires an external source URL.");
    const job = await client.post("/imports", {
      project_id: projectId, provider: name || undefined, source_url: source,
      target_path: opts.folder ?? opts.scope ?? "", name: opts.name,
      idempotency_key: opts.idempotencyKey, config,
    });
    return { resource_kind: "import_job", job };
  }
  if (!["manual", "scheduled"].includes(mode)) {
    throw new ApiError(0, "INVALID_SYNC_MODE", "Synchronize supports manual or scheduled, never import_once.",
      "Use `puppyone import create <url>` for a one-time snapshot.");
  }
  if (name === "github") {
    throw new ApiError(0, "GITHUB_BINDING_REQUIRED", "GitHub continuous synchronization uses its dedicated binding, not the snapshot adapter.",
      "Use the project's GitHub Synchronize binding workflow; use import create for a snapshot.");
  }
  const target = opts.folder ?? opts.scope;
  if (target == null) throw new ApiError(0, "MISSING_TARGET", "Synchronize requires --folder <project-path>.");
  const externalSource = { ...(config.source || {}) };
  if (source) externalSource.resource_url = source;
  // --set accepts source.* and options.*. Flat Import options are not silently
  // reinterpreted as a different lifecycle's configuration.
  for (const key of Object.keys(config)) {
    if (!["source", "options", "materialization_schema"].includes(key)) {
      throw new ApiError(0, "INVALID_SYNC_CONFIG", `Synchronize config must use source/options; unexpected ${key}.`);
    }
  }
  const result = await client.post(`${SYNCHRONIZE_ROOT}/connections`, {
    project_id: projectId, provider: name, target_path: target,
    direction: opts.direction || "inbound", sync_mode: mode,
    trigger: synchronizeTrigger({ ...opts, mode }),
    config: { ...config, source: externalSource, options: config.options || {} },
  });
  return { resource_kind: "synchronize_binding", binding: result };
}

export function printResource(cmd, data) {
  const out = createOutput(cmd);
  if (out.json) out.success(data);
  else out.info(JSON.stringify(data, null, 2));
}

function sourceOptions(command) {
  return command.option("--name <name>", "display name")
    .option("--folder <path>", "target path inside the project")
    .option("--config <json>", "provider configuration as a JSON object")
    .option("--set <key=value...>", "provider configuration; dotted keys are supported");
}

export function registerEntryPoints(program) {
  const imports = program.command("import").description("Create/manage one-time external snapshots (ImportJob)");
  sourceOptions(imports.command("create").argument("<source>", "external URL")
    .option("--provider <provider>", "provider override; otherwise detected by server")
    .option("--idempotency-key <key>", "stable key for retrying a create request"))
    .action(withErrors(async (source, opts, cmd) => {
      printResource(cmd, await createSource(createClient(cmd), requireProject(cmd), opts.provider, source, opts));
    }));
  imports.command("providers").action(withErrors(async (_opts, cmd) => {
    printResource(cmd, { providers: await createClient(cmd).get("/imports/providers") });
  }));
  imports.command("ls").action(withErrors(async (_opts, cmd) => {
    printResource(cmd, await createClient(cmd).get("/imports", { project_id: requireProject(cmd) }));
  }));
  for (const [action, method] of [["info", "get"], ["cancel", "del"]]) {
    imports.command(action).argument("<job-id>").action(withErrors(async (id, _opts, cmd) => {
      printResource(cmd, { job: await createClient(cmd)[method](`/imports/${encodeURIComponent(id)}`) });
    }));
  }

  const sync = program.command("synchronize").description("Manage durable external source bindings and runs");
  sourceOptions(sync.command("add").argument("<provider>").argument("[source]", "external resource URL")
    .option("--mode <mode>", "manual | scheduled", "manual")
    .option("--schedule <cron>", "five-field cron (required for scheduled mode)")
    .option("--timezone <zone>", "schedule timezone; defaults to UTC")
    .option("--direction <direction>", "inbound | outbound | bidirectional", "inbound"))
    .action(withErrors(async (provider, source, opts, cmd) => {
      if (opts.mode === "import_once") throw new ApiError(0, "INVALID_SYNC_MODE", "Use `puppyone import create` for a snapshot.");
      printResource(cmd, await createSource(createClient(cmd), requireProject(cmd), provider, source, opts));
    }));
  sync.command("providers").action(withErrors(async (_opts, cmd) => {
    printResource(cmd, { providers: await createClient(cmd).get(`${SYNCHRONIZE_ROOT}/connectors`) });
  }));
  sync.command("ls").action(withErrors(async (_opts, cmd) => {
    printResource(cmd, { bindings: await createClient(cmd).get(`${SYNCHRONIZE_ROOT}/connections`, { project_id: requireProject(cmd) }) });
  }));
  for (const action of ["refresh", "pause", "resume"]) {
    sync.command(action).argument("<binding-id>", "Synchronize binding ID, never an Access surface ID")
      .action(withErrors(async (id, _opts, cmd) => {
        printResource(cmd, { result: await createClient(cmd).post(`${SYNCHRONIZE_ROOT}/connections/${encodeURIComponent(id)}/${action}`) });
      }));
  }
  sync.command("trigger").argument("<binding-id>").argument("<mode>", "manual | scheduled")
    .option("--schedule <cron>").option("--timezone <zone>")
    .action(withErrors(async (id, mode, opts, cmd) => {
      const trigger = synchronizeTrigger({ ...opts, mode });
      printResource(cmd, { binding: await createClient(cmd).patch(`${SYNCHRONIZE_ROOT}/connections/${encodeURIComponent(id)}/trigger`, {
        sync_mode: mode, trigger,
      }) });
    }));
  sync.command("rm").argument("<binding-id>").action(withErrors(async (id, _opts, cmd) => {
    printResource(cmd, { result: await createClient(cmd).del(`${SYNCHRONIZE_ROOT}/connections/${encodeURIComponent(id)}`) });
  }));
  sync.command("runs").argument("<binding-id>").action(withErrors(async (id, _opts, cmd) => {
    printResource(cmd, { runs: await createClient(cmd).get(`${SYNCHRONIZE_ROOT}/connections/${encodeURIComponent(id)}/runs`) });
  }));
}
