/** Access surface management. External source aliases dispatch to their owners. */
import { ApiError, createClient } from "../api.js";
import { createOutput } from "../output.js";
import { requireProject, withErrors } from "../helpers.js";
import { buildMcpConnection } from "../mcp-config.js";
import { createSource, sourceConfig, providerName, printResource } from "./entrypoints.js";

const ACCESS_ROOT = "/access/surfaces";
const KINDS = new Set(["agent", "mcp", "sandbox"]);
const FILTER_KINDS = new Set([...KINDS, "git_remote", "cli", "mcp_endpoint", "sandbox_endpoint"]);
const OAUTH = {
  github: "github", gmail: "gmail", google_drive: "google-drive", google_docs: "google-docs",
  google_sheets: "google-sheets", google_calendar: "google-calendar",
  google_search_console: "google-search-console",
};
const surfacePath = id => `/${encodeURIComponent(id)}`;

async function accessRequest(client, method, resource = "", ...args) {
  try {
    return await client[method](`${ACCESS_ROOT}${resource}`, ...args);
  } catch (error) {
    // Old /access/{id} shadows the new collection: GET treats "surfaces" as an
    // ID and POST returns 405. Do not confuse a real resource/project 404 with it.
    if (error instanceof ApiError && (error.status === 405 || (error.status === 404
        && (error.message === "Not Found" || (resource === "" && error.message === "Access connection not found"))))) {
      throw new ApiError(error.status, "SERVER_UPGRADE_REQUIRED", "This server does not expose the canonical Access API.",
        "Use a server containing ISSUE-058's /access/surfaces contract. No legacy mutation was attempted.");
    }
    throw error;
  }
}

export function registerAccess(program) {
  const access = program.command("access").description("Manage Agent/MCP/Sandbox Access surfaces");
  access.command("add").argument("<type>", "agent | mcp | sandbox; external-source aliases are deprecated")
    .argument("[source]", "surface name or external URL for the deprecated source alias")
    .option("--name <name>").option("--folder <path>", "project target folder for a source")
    .option("--scope <path>", "project path exposed by the Access surface")
    .option("--permission <permission>", "retired; configure grants through the owning Access API")
    .option("--mode <mode>", "legacy external source intent: import_once | manual | scheduled", "import_once")
    .option("--schedule <cron>", "cron for explicit scheduled Synchronize aliases")
    .option("--timezone <zone>")
    .option("--model <model>", "unsupported at this creation boundary; rejected")
    .option("--system-prompt <prompt>", "unsupported at this creation boundary; rejected")
    .option("--type <type>", "Agent implementation type")
    .option("--config <json>").option("--set <key=value...>")
    .option("--gateway <id>", "retired; rejected rather than reinterpreted")
    .option("--idempotency-key <key>", "stable key when forwarding a snapshot import")
    .action(withErrors(async (type, source, opts, cmd) => {
      const kind = providerName(type);
      const out = createOutput(cmd);
      if (opts.model != null || opts.systemPrompt != null) {
        throw new ApiError(0, "UNSUPPORTED_AGENT_CONFIGURATION", "--model and --system-prompt are not consumed by Access creation.",
          "Configure supported Agent settings through the Agent workflow; no resource was created.");
      }
      if (opts.type != null && kind !== "agent") {
        throw new ApiError(0, "INVALID_ACCESS_OPTION", "--type only configures Agents; for Sandbox use --set runtime=<runtime>.");
      }
      if (opts.scope != null && opts.folder != null) {
        throw new ApiError(0, "AMBIGUOUS_TARGET", "Supply only one of --scope or --folder.");
      }
      if (opts.permission != null) {
        throw new ApiError(0, "UNSUPPORTED_PERMISSION_OPTION", "--permission cannot safely configure these Access grants.",
          "Configure permissions through the owning Access/Agent surface workflow; no resource was created.");
      }
      if (kind === "agent" && (opts.scope != null || opts.folder != null)) {
        throw new ApiError(0, "AGENT_SCOPE_CONFIGURATION_REQUIRED", "Agent scope is not the generic Access path field.",
          "Configure Agent access bindings through the Agent workflow; no unscoped agent was created.");
      }
      if (["direct", "cli", "git_remote", "filesystem"].includes(kind)) {
        throw new ApiError(0, "RETIRED_ACCESS_CREATION", `access add ${kind} is not supported.`,
          "Use the project's credential-free Git remote and separately issued Git credentials.");
      }
      if (!KINDS.has(kind)) {
        out.warn("External sources no longer belong to Access; forwarding to Import/Synchronize without changing the requested mode.");
        printResource(cmd, await createSource(createClient(cmd), requireProject(cmd), kind, source, opts));
        return;
      }
      if (opts.gateway != null || opts.schedule != null || opts.timezone != null || opts.idempotencyKey != null
          || cmd.getOptionValueSource("mode") === "cli") {
        throw new ApiError(0, "INVALID_ACCESS_OPTION", "Source modes, schedules, gateway and Import idempotency keys do not configure Access surfaces.");
      }
      const config = sourceConfig(opts);
      if (kind === "agent" && ["model", "llm_model", "system_prompt"].some(key => Object.hasOwn(config, key))) {
        throw new ApiError(0, "UNSUPPORTED_AGENT_CONFIGURATION", "Access creation does not consume model/llm_model/system_prompt configuration.",
          "Use the supported Agent settings workflow; no resource was created.");
      }
      if (opts.type) config.type = opts.type;
      const client = createClient(cmd);
      const created = await accessRequest(client, "post", "", {
        project_id: requireProject(cmd), kind, name: opts.name || source || kind,
        path: opts.scope ?? opts.folder ?? null, config,
      });
      printResource(cmd, { resource_kind: "access_surface", access: created });
      if (kind === "mcp" && !out.json) {
        const connection = buildMcpConnection(created, client.baseUrl);
        if (connection) out.info(JSON.stringify(connection, null, 2));
      }
    }));

  access.command("ls").option("--kind <kind>", "surface kind filter")
    .option("--provider <kind>", "deprecated alias for --kind")
    .option("--status <status>").action(withErrors(async (opts, cmd) => {
      if (opts.kind != null && opts.provider != null) {
        throw new ApiError(0, "AMBIGUOUS_KIND", "Supply --kind or its deprecated --provider alias, not both.");
      }
      const selected = opts.kind ?? opts.provider;
      const kind = selected == null ? undefined : providerName(selected);
      if (kind != null && !FILTER_KINDS.has(kind)) {
        throw new ApiError(0, "INVALID_ACCESS_KIND", "Select an Access kind; external sources belong to import/synchronize ls.");
      }
      if (opts.provider != null) createOutput(cmd).warn("--provider is deprecated for Access; use --kind.");
      printResource(cmd, { access: await accessRequest(createClient(cmd), "get", "", {
        project_id: requireProject(cmd), kind, status: opts.status,
      }) });
    }));
  access.command("info").argument("<surface-id>").action(withErrors(async (id, _opts, cmd) => {
    printResource(cmd, { access: await accessRequest(createClient(cmd), "get", surfacePath(id)) });
  }));
  for (const [action, status] of [["pause", "paused"], ["resume", "active"]]) {
    access.command(action).argument("<surface-id>").action(withErrors(async (id, _opts, cmd) => {
      printResource(cmd, { access: await accessRequest(createClient(cmd), "patch", surfacePath(id), { status }) });
    }));
  }
  access.command("rm").argument("<surface-id>").action(withErrors(async (id, _opts, cmd) => {
    printResource(cmd, { result: await accessRequest(createClient(cmd), "del", surfacePath(id)) });
  }));
  access.command("update").argument("<surface-id>").option("--config <json>").option("--set <key=value...>")
    .action(withErrors(async (id, opts, cmd) => {
      if (!opts.config && !opts.set?.length) throw new ApiError(0, "MISSING_CONFIG", "Supply --config or --set.");
      // The owning service merges config. Do not replay a redacted/stale GET snapshot.
      printResource(cmd, { access: await accessRequest(createClient(cmd), "patch", surfacePath(id), { config: sourceConfig(opts) }) });
    }));
  access.command("key").argument("<surface-id>").option("--regenerate")
    .action(withErrors(async (id, opts, cmd) => {
      const client = createClient(cmd);
      if (!opts.regenerate) throw new ApiError(0, "SECRET_NOT_RECOVERABLE", "Credentials are revealed only on issuance.", "Use access key <surface-id> --regenerate to rotate explicitly.");
      printResource(cmd, { credential: await accessRequest(client, "post", `${surfacePath(id)}/regenerate-key`) });
    }));

  // An Access ID cannot be forwarded to Synchronize by guessing that IDs match.
  for (const action of ["refresh", "run", "logs", "trigger"]) {
    const command = access.command(action).argument("<surface-id>")
      .description("Retired mixed-resource operation; use the owning resource command");
    if (action === "trigger") command.argument("<mode>");
    if (action === "logs") command.option("--limit <n>").option("--run <id>");
    command.action(withErrors(async () => {
      throw new ApiError(0, "RESOURCE_KIND_REQUIRED", `access ${action} cannot use an Access surface ID as a synchronization binding ID.`,
        "Find the binding with `puppyone synchronize ls`, then use synchronize refresh/runs/pause/resume.");
    }));
  }
  access.command("providers").description("Discover server-supported Access surface kinds")
    .action(withErrors(async (_opts, cmd) => {
      const types = await accessRequest(createClient(cmd), "get", "/types");
      printResource(cmd, { kinds: types.map(item => item.kind), types });
    }));
  access.command("schema").argument("<provider>").description("Legacy provider discovery; external snapshots use Import")
    .action(withErrors(async (provider, _opts, cmd) => {
      const name = providerName(provider);
      if (KINDS.has(name)) {
        const types = await accessRequest(createClient(cmd), "get", "/types");
        const type = types.find(item => item.kind === name);
        if (!type) throw new ApiError(0, "INVALID_ACCESS_KIND", `This server does not advertise Access kind: ${name}`);
        printResource(cmd, { kind: name, surface_type: type, config: "Use --config or --set; see server Access schema." });
        return;
      }
      const specs = await createClient(cmd).get("/imports/providers");
      const spec = specs.find(item => item.provider === name);
      if (!spec) throw new ApiError(0, "UNKNOWN_PROVIDER", `No Import provider: ${name}`);
      printResource(cmd, { provider: spec });
    }));
  // OAuth authorization is shared source infrastructure, not Access creation.
  for (const [action, endpoint] of [["auth", "authorize"], ["auth-status", "status"]]) {
    access.command(action).argument("<provider>").description("Legacy alias for shared Provider OAuth")
      .action(withErrors(async (provider, _opts, cmd) => {
        const name = providerName(provider);
        if (!OAUTH[name]) throw new ApiError(0, "UNSUPPORTED_OAUTH", `No OAuth flow for ${name}.`);
        printResource(cmd, { authorization: await createClient(cmd).get(`/oauth/${OAUTH[name]}/${endpoint}`) });
      }));
  }
}
