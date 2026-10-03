## Context

Global Access currently includes enriched list/detail/credential management and adapter-backed Agent/MCP/Sandbox creation. Project Connector routes manage explicit RepositoryTargets through AccessService. Both already own AccessSurface rows, not Synchronize bindings. Preserve those existing operations rather than implementing another store or task dispatcher.

## Decisions

- Mount `/api/v1/access/surfaces` before legacy `/access/{connection_id}` so the dynamic legacy route cannot consume `surfaces`. Register static kind discovery before surface-ID matching.
- Project routes live at `/api/v1/projects/{project_id}/access/surfaces`; existing enable-target, activate-agent, pause/resume and CRUD policies retain their actual resource owner and contract-v2 validation.
- Public resource responses use id (own primary key), project_id, target and kind. Expose ordinary display/config/policy/state/timestamp fields without ambiguous source-run references. Map existing activity timestamps to last_activity_at; do not invent an AccessRun lifecycle.
- Global create supports the existing Agent/MCP/Sandbox adapter configuration inputs: project_id, kind, name, path, config, accesses and tools_config. Do not accept ignored legacy datasource or target fields. Project-target creation/enable requires the existing structured RepositoryTarget and validates its Project/Scope rather than inferring a root from a bad target.
- Global create returns AccessSurfaceCreated with its id/project_id/kind and existing explicit one-time mcp_api_key/mcp_server_url fields when applicable. This is separate from ordinary metadata DTOs. Do not revive direct server-issued human Git credentials.
- Credential rotation returns access_surface_id plus credential and optional target/hint, under CREDENTIAL_MANAGE. Existing Git client-generated-credential upgrade errors remain errors.
- Canonical metadata mutation uses existing AccessService behavior, including admission-cache invalidation. Shared serialization redacts secrets; reject secret-bearing metadata patches rather than silently persisting them. Do not allow the legacy Project serializer to remain a credential-leaking bypass.
- Ordinary lists contain actual ongoing Access kinds, not legacy external-source/import rows. Direct management of a legacy non-Access row is blocked with an actionable migration error after authorization. No classification or movement of historical rows occurs here.
- No canonical generic `/run` or Access-to-Synchronize bridge: the old unsupported source-run route remains only for its existing upgrade/error response until coordinated retirement.
- New leaf clients use canonical DTOs and paths directly. Product UI may retain local composition naming, but may not map an Access ID to a binding or silently fall back to old transport.

## Risks and rollout

The repository still contains legacy APIs and mixed historical metadata. Use typed shallow projections and preserve user config except existing credential redaction. Exact additive OpenAPI fingerprints retain the historical contract fixture. Test Viewer/foreign-project denial, wrong resource IDs, credential non-disclosure, immutable targets, duplicate/invalid query selectors and real client-to-HTTP behavior.

Deploy canonical backend before migrated Desktop/Web/CLI clients. Legacy server retirement, installed-client versions, real database/provider/runtime evidence and release/rollback operations remain NOT_VERIFIED. Do not archive this change as deployed based on local source tests.
