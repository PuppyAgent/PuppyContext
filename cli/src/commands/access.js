/** Access surface management. External source aliases dispatch to their owners. */
import { ApiError, createClient } from "../api.js";
import { createOutput } from "../output.js";
import { requireProject, withErrors } from "../helpers.js";
import { buildMcpConnection } from "../mcp-config.js";
import { createSource, sourceConfig, providerName, printResource } from "./entrypoints.js";

const ACCESS_ROOT = "/access"; // Public-path cutover is coordinated by ISSUE-058.
const KINDS = new Set(["agent", "mcp", "sandbox"]);
const OAUTH = {
  github: "github", gmail: "gmail", google_drive: "google-drive", google_docs: "google-docs",
  google_sheets: "google-sheets", google_calendar: "google-calendar",
  google_search_console: "google-search-console",
};
const surfacePath = id => `${ACCESS_ROOT}/${encodeURIComponent(id)}`;

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
    .option("--model <model>").option("--system-prompt <prompt>")
    .option("--type <type>", "agent/sandbox implementation")
    .option("--config <json>").option("--set <key=value...>")
    .option("--gateway <id>", "retired; rejected rather than reinterpreted")
    .option("--idempotency-key <key>", "stable key when forwarding a snapshot import")
    .action(withErrors(async (type, source, opts, cmd) => {
      const kind = providerName(type);
      const out = createOutput(cmd);
      if (opts.permission) {
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
      if (opts.gateway || opts.schedule || opts.timezone || cmd.getOptionValueSource("mode") === "cli") {
        throw new ApiError(0, "INVALID_ACCESS_OPTION", "--mode and --gateway describe external sources, not Access surfaces.");
      }
      const config = sourceConfig(opts);
      if (opts.type) config.type = opts.type;
      if (opts.model) config.model = opts.model;
      if (opts.systemPrompt) config.system_prompt = opts.systemPrompt;
      const client = createClient(cmd);
      const created = await client.post(ACCESS_ROOT, {
        project_id: requireProject(cmd), provider: kind, name: opts.name || source || kind,
        path: opts.scope ?? opts.folder ?? null, config,
      });
      printResource(cmd, { resource_kind: "access_surface", access: created });
      if (kind === "mcp" && !out.json) {
        const connection = buildMcpConnection(created, client.baseUrl);
        if (connection) out.info(JSON.stringify(connection, null, 2));
      }
    }));

  access.command("ls").option("--provider <kind>", "surface kind filter")
    .option("--status <status>").action(withErrors(async (opts, cmd) => {
      printResource(cmd, { access: await createClient(cmd).get(ACCESS_ROOT, {
        project_id: requireProject(cmd), provider: opts.provider, status: opts.status,
      }) });
    }));
  access.command("info").argument("<surface-id>").action(withErrors(async (id, _opts, cmd) => {
    printResource(cmd, { access: await createClient(cmd).get(surfacePath(id)) });
  }));
  for (const [action, status] of [["pause", "paused"], ["resume", "active"]]) {
    access.command(action).argument("<surface-id>").action(withErrors(async (id, _opts, cmd) => {
      printResource(cmd, { access: await createClient(cmd).patch(surfacePath(id), { status }) });
    }));
  }
  access.command("rm").argument("<surface-id>").action(withErrors(async (id, _opts, cmd) => {
    printResource(cmd, { result: await createClient(cmd).del(surfacePath(id)) });
  }));
  access.command("update").argument("<surface-id>").option("--config <json>").option("--set <key=value...>")
    .action(withErrors(async (id, opts, cmd) => {
      if (!opts.config && !opts.set?.length) throw new ApiError(0, "MISSING_CONFIG", "Supply --config or --set.");
      const client = createClient(cmd);
      const current = await client.get(surfacePath(id));
      printResource(cmd, { access: await client.patch(surfacePath(id), { config: { ...current.config, ...sourceConfig(opts) } }) });
    }));
  access.command("key").argument("<surface-id>").option("--regenerate")
    .action(withErrors(async (id, opts, cmd) => {
      const client = createClient(cmd);
      if (!opts.regenerate) throw new ApiError(0, "SECRET_NOT_RECOVERABLE", "Credentials are revealed only on issuance.", "Use access key <surface-id> --regenerate to rotate explicitly.");
      printResource(cmd, { credential: await client.post(`${surfacePath(id)}/regenerate-key`) });
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
  access.command("providers").description("List creatable Access surface kinds")
    .action(withErrors(async (_opts, cmd) => printResource(cmd, { kinds: [...KINDS] })));
  access.command("schema").argument("<provider>").description("Legacy provider discovery; external snapshots use Import")
    .action(withErrors(async (provider, _opts, cmd) => {
      const name = providerName(provider);
      if (KINDS.has(name)) {
        printResource(cmd, { kind: name, config: "Use --config or --set; see server Access schema." });
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
