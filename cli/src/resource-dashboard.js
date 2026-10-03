/** Canonical, read-only inventory. A resource identity is (kind, id), never id alone. */
import { ApiError } from "./api.js";

const ACCESS_KINDS = new Set(["git_remote", "cli", "agent", "mcp", "mcp_endpoint", "sandbox", "sandbox_endpoint"]);
const SECRET_FIELDS = ["access_key", "api_key", "mcp_api_key", "credential"];
const text = value => typeof value === "string" && value.trim().length > 0;
const invalid = () => new ApiError(0, "INVALID_DASHBOARD_RESPONSE",
  "Dashboard returned invalid resource identities or metadata.",
  "Use a matching canonical backend; no legacy inventory was substituted.");

export function validateResourceDashboard(data, projectId) {
  if (!data || data.project?.id !== projectId || typeof data.project?.name !== "string"
      || !Array.isArray(data.resources) || !Array.isArray(data.tools) || !Array.isArray(data.uploads)
      || !["total", "folders", "files"].every(key => Number.isInteger(data.nodes?.[key]) && data.nodes[key] >= 0)) {
    throw invalid();
  }
  const seen = new Set();
  for (const row of data.resources) {
    if (!row || !text(row.resource_id) || row.project_id !== projectId || !text(row.status)
        || (row.name != null && typeof row.name !== "string")
        || !["synchronize", "access"].includes(row.resource_kind)
        || SECRET_FIELDS.some(key => Object.hasOwn(row, key))) throw invalid();
    if (row.resource_kind === "synchronize") {
      if (!text(row.provider) || typeof row.path !== "string") throw invalid();
    } else {
      const target = row.target;
      if (!ACCESS_KINDS.has(row.kind) || !target || target.project_id !== projectId
          || !["project_root", "scope"].includes(target.kind)
          || (target.kind === "scope" && !text(target.scope_id))) throw invalid();
      const allowed = target.kind === "scope" ? ["kind", "project_id", "scope_id"] : ["kind", "project_id"];
      if (Object.keys(target).some(key => !allowed.includes(key))) throw invalid();
    }
    const key = JSON.stringify([row.resource_kind, row.resource_id]);
    if (seen.has(key)) throw invalid();
    seen.add(key);
  }
  return data;
}

export async function getResourceDashboard(client, projectId) {
  if (!text(projectId)) throw new ApiError(0, "PROJECT_REQUIRED", "Select an explicit project.");
  try {
    return validateResourceDashboard(
      await client.get(`/projects/${encodeURIComponent(projectId)}/dashboard/resources`), projectId,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && error.message === "Not Found") {
      throw new ApiError(404, "SERVER_UPGRADE_REQUIRED", "This server does not expose the resource Dashboard API.",
        "Upgrade the server to the canonical /dashboard/resources contract; no legacy request was attempted.");
    }
    throw error;
  }
}
