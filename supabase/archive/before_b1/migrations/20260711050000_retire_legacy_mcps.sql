-- ISSUE-017: preserve legacy MCP metadata in the canonical access model, then
-- remove the plaintext-key legacy table. Credentials are intentionally not
-- copied: migrated endpoints are disabled and require explicit key issuance.
BEGIN;

INSERT INTO public.access_surfaces (
    org_id, project_id, scope_id, kind, name, status, principal_type,
    principal_id, config, created_by, created_at, updated_at
)
SELECT
    p.org_id,
    m.project_id,
    root_scope.id,
    'mcp',
    COALESCE(NULLIF(m.name, ''), 'Migrated MCP Endpoint'),
    'disabled',
    'legacy_mcp',
    m.id::text,
    jsonb_strip_nulls(jsonb_build_object(
        'migration_source', 'mcps',
        'legacy_id', m.id,
        'json_path', NULLIF(m.json_path, ''),
        'tools_definition', m.tools_definition,
        'register_tools', m.register_tools,
        'requires_key_regeneration', true
    )),
    m.user_id,
    m.created_at,
    m.updated_at
FROM public.mcps m
JOIN public.projects p ON p.id = m.project_id
JOIN LATERAL (
    SELECT rs.id FROM public.repo_scopes rs
    WHERE rs.project_id = m.project_id AND rs.is_root = true
    ORDER BY rs.created_at ASC LIMIT 1
) root_scope ON true
WHERE NOT EXISTS (
    SELECT 1 FROM public.access_surfaces a
    WHERE a.principal_type = 'legacy_mcp' AND a.principal_id = m.id::text
);

DROP TABLE IF EXISTS public.mcps CASCADE;
DROP SEQUENCE IF EXISTS public.mcps_id_seq;

COMMIT;
