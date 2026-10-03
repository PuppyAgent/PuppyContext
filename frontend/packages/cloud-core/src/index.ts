/**
 * @puppyone/cloud-core — shared, transport-agnostic cloud domain (ISSUE-022).
 *
 * Single source of truth for the cloud entities + API contracts that the web
 * frontend and the desktop cloud panel both use. Platform-specific auth/HTTP is
 * injected via {@link CloudTransport}; nothing here imports Next.js, Electron,
 * Supabase, or any platform runtime (see check-data-ui-boundaries).
 */
export type { CloudTransport } from "./transport";
export {
  repositoryTargetKey,
  sameRepositoryTarget,
  projectRootRepositoryView,
  repositoryScopeView,
  repositoryViewKey,
  matchRepositoryViewForPath,
  REPOSITORY_TARGET_CONTRACT_HEADER,
  REPOSITORY_TARGET_CONTRACT_VERSION,
} from "./repositoryTargets";
export type {
  ProjectRootTarget,
  ScopeTarget,
  RepositoryTarget,
  RepositoryView,
} from "./repositoryTargets";

// ── Access-provider registry (pure domain data + helpers) ────────────────
export * from "./accessProviders";

// ── Synchronize bindings and runs ────────────────────────────────────────
export * from "./endpoints/synchronize";
export * from "./endpoints/synchronizeGithub";
export * from "./endpoints/importDatabase";

// ── MCP endpoints ─────────────────────────────────────────────────────────
export { createMcpEndpointsApi } from "./endpoints/mcpEndpoints";
export type {
  McpEndpoint,
  McpToolsConfig,
  CreateMcpEndpointParams,
  UpdateMcpEndpointParams,
} from "./endpoints/mcpEndpoints";

// ── Sandbox endpoints ─────────────────────────────────────────────────────
export { createSandboxEndpointsApi } from "./endpoints/sandboxEndpoints";
export type {
  SandboxEndpoint,
  SandboxMount,
  SandboxMountPermissions,
  SandboxResourceLimits,
  SandboxRuntime,
  CreateSandboxEndpointParams,
  UpdateSandboxEndpointParams,
} from "./endpoints/sandboxEndpoints";

// ── Repo scopes ───────────────────────────────────────────────────────────
export { createScopesApi, matchScopeForPath, isWithinScope } from "./endpoints/scopes";
export type { RepositoryScope, ScopeMode } from "./endpoints/scopes";

// ── Access resources and independent repository identity ───────────────────
export * from "./endpoints/accessSurfaces";
export * from "./endpoints/repositoryIdentity";
export * from "./endpoints/activity";
export * from "./endpoints/resourceDashboard";
