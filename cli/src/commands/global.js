/**
 * Global commands: status (project dashboard)
 *
 * Top-level shortcuts are registered by registerGlobalCommands().
 */

import { ApiError, createClient } from "../api.js";
import { requireProject } from "../helpers.js";
import { createOutput } from "../output.js";
import { getResourceDashboard } from "../resource-dashboard.js";

function timeAgo(isoString) {
  if (!isoString) return "never";
  const diff = Date.now() - new Date(isoString).getTime();
  if (diff < 0) return "just now";
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

// ── Provider display helpers ─────────────────────────────────

const STATUS_ICONS = { active: "\u25CF", syncing: "\u25CF", paused: "\u25CB", error: "\u2717" };

function statusLabel(s) {
  const icon = STATUS_ICONS[s] || "\u25CF";
  return `${icon} ${s}`;
}

// ── Project dashboard ────────────────────────────────────────

export async function dashboardAction(path, opts, cmd) {
  const out = createOutput(cmd);

  try {
    const d = await getResourceDashboard(createClient(cmd), requireProject(cmd));
    if (out.json) {
      out.success({ dashboard: d });
      return;
    }

    out.info("");
    out.info(`  PuppyOne \u2014 ${d.project.name} (${d.project.id.slice(0, 12)}...)`);
    out.info(`  ${"─".repeat(50)}`);
    out.info("");

    // Content
    out.info("  Content");
    out.info(`    ${d.nodes.total} nodes (${d.nodes.folders} folders, ${d.nodes.files} files)`);
    out.info("");

    // Distinct resource kinds preserve equal IDs/names/paths across domains.
    const resources = d.resources;
    if (resources.length > 0) {
      out.info(`  Resources (${resources.length})`);
      const columns = [
        { key: "identity", label: "RESOURCE (KIND:ID)" },
        { key: "type", label: "PROVIDER / KIND" },
        { key: "name", label: "NAME" },
        { key: "target", label: "TARGET" },
        { key: "status", label: "STATUS" },
        { key: "activity", label: "LAST ACTIVITY" },
      ];
      out.table(resources.map(row => ({
        identity: `${row.resource_kind}:${row.resource_id}`,
        type: row.resource_kind === "synchronize" ? row.provider : row.kind,
        name: (row.name || "").slice(0, 30),
        target: row.resource_kind === "synchronize" ? `path:${row.path || "/"}`
          : row.target.kind === "scope" ? `scope:${row.target.scope_id}` : "project_root",
        status: statusLabel(row.status),
        activity: row.last_activity_at ? timeAgo(row.last_activity_at) : "\u2014",
      })), columns);
      const errors = resources.filter(row => row.status === "error");
      if (errors.length > 0) {
        out.info("");
        out.warn(`  ${errors.length} resource(s) in error:`);
        for (const row of errors) {
          out.info(`    - ${row.resource_kind}:${row.resource_id}: ${row.error_message || "unknown"}`);
        }
      }
    } else {
      out.info("  Resources: (none)");
      out.info("    One-time snapshot: puppyone import create <url>");
      out.info("    Persistent source: puppyone synchronize add <provider> <url> --folder <path>");
      out.info("    Access surface: puppyone access add agent|mcp|sandbox <name>");
    }
    out.info("");

    // Tools
    if (d.tools.length > 0) {
      out.info(`  Tools (${d.tools.length})`);
      const toolCols = [
        { key: "name", label: "NAME" },
        { key: "type", label: "TYPE" },
        { key: "index", label: "INDEX" },
      ];
      const toolRows = d.tools.map(t => {
        let idx = "\u2014";
        if (t.type === "search") {
          if (t.index_status === "ready") {
            idx = `\u2713 ready (${t.chunks_count ?? 0} chunks)`;
          } else if (t.index_status === "indexing") {
            const pct = t.total_files && t.indexed_files != null
              ? ` (${t.indexed_files}/${t.total_files})`
              : "";
            idx = `\u21BB indexing${pct}`;
          } else if (t.index_status === "error") {
            idx = `\u2717 error`;
          } else if (t.index_status === "pending") {
            idx = `\u25CB pending`;
          }
        }
        return { name: t.name, type: t.type || "\u2014", index: idx };
      });
      out.table(toolRows, toolCols);
    } else {
      out.info("  Tools: (none)");
    }
    out.info("");

    // Uploads
    const activeUploads = d.uploads || [];
    if (activeUploads.length > 0) {
      out.info(`  Uploads (${activeUploads.length} in progress)`);
      for (const u of activeUploads) {
        out.info(`    - ${u.type} ${u.status} ${u.progress}%${u.message ? " — " + u.message : ""}`);
      }
      out.info("");
    }
  } catch (e) {
    if (e instanceof ApiError) {
      out.error(e.code, e.message, e.hint);
    } else {
      out.error("UNEXPECTED", e.message);
    }
  }
}

// ============================================================
// Register top-level shortcuts
// ============================================================

export function registerGlobalCommands(program) {
  program
    .command("status")
    .description("Project resource dashboard (Synchronize bindings and Access surfaces)")
    .action((opts, cmd) => dashboardAction(null, opts, cmd));
}
