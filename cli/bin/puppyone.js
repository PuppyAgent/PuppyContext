#!/usr/bin/env node

import { program } from "commander";
import { version } from "../src/version.js";

// ── Core commands ─────────────────────────────────────────
import { registerAuth, registerLegacyAuthAliases } from "../src/commands/auth.js";
import { registerOrg } from "../src/commands/org.js";
import { registerProject } from "../src/commands/project.js";
import { registerTemplate } from "../src/commands/template.js";
import { registerAccess } from "../src/commands/access.js";
import { registerEntryPoints } from "../src/commands/entrypoints.js";
import { registerGateway } from "../src/commands/gateway.js";
import { registerChat } from "../src/commands/chat.js";
import { registerConfig } from "../src/commands/config-cmd.js";
import { registerAp } from "../src/commands/ap/index.js";
import { registerFs } from "../src/commands/fs/index.js";
import { registerGlobalCommands } from "../src/commands/global.js";

program
  .name("puppyone")
  .description("PuppyOne CLI — cloud file system for AI agents")
  .version(version, "-V, --version")
  .enablePositionalOptions();

program
  .option("-u, --api-url <url>", "PuppyOne API URL (overrides config)")
  .option("-k, --api-key <key>", "API key / token (overrides config)")
  .option("--json", "output as JSON (for AI / scripts)")
  .option("-v, --verbose", "verbose output")
  .option("-p, --project <id>", "project ID (overrides active project)")
  .option("-o, --org <id>", "organization ID (overrides active org)");

// ─── Control Plane Commands ──────────────────────────────
registerAuth(program);
registerOrg(program);
registerProject(program);
registerTemplate(program);
registerAccess(program);
registerEntryPoints(program);
registerGateway(program);
registerChat(program);
registerConfig(program);

// ─── Filesystem Commands ─────────────────────────────────
registerAp(program);
registerFs(program);

// ─── Backward Compatibility ─────────────────────────────
registerLegacyAuthAliases(program);
registerGlobalCommands(program);

// Show animated banner on first-ever run (fresh install) or no subcommand
if (process.stdout.isTTY) {
  const { existsSync } = await import("node:fs");
  const { join } = await import("node:path");
  const { homedir } = await import("node:os");
  const configFile = join(homedir(), ".puppyone", "config.json");
  const isFirstRun = !existsSync(configFile);
  const noSubcommand = process.argv.length <= 2;

  if (isFirstRun || noSubcommand) {
    const { showBanner } = await import("../src/banner.js");
    await showBanner();
  }
}

program.parse();
