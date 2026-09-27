-- ISSUE-017: access_tools is the sole MCP custom-tool binding store.

-- Remove historical duplicates before installing the integrity boundary.
DELETE FROM public.access_tools a
USING public.access_tools b
WHERE a.access_point_id = b.access_point_id
  AND a.tool_id = b.tool_id
  AND a.id > b.id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_access_tools_surface_tool_unique
    ON public.access_tools(access_point_id, tool_id);

-- Backfill endpoint bindings previously embedded in tools_policy JSON.
WITH policy_items AS (
    SELECT p.access_surface_id, item
    FROM public.access_surface_policies p
    JOIN public.access_surfaces s
      ON s.id = p.access_surface_id AND s.kind = 'mcp'
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE
            WHEN jsonb_typeof(p.tools_policy) = 'array' THEN p.tools_policy
            WHEN jsonb_typeof(p.tools_policy -> 'custom_tools') = 'array'
                THEN p.tools_policy -> 'custom_tools'
            WHEN jsonb_typeof(p.tools_policy -> 'bound_tools') = 'array'
                THEN p.tools_policy -> 'bound_tools'
            WHEN jsonb_typeof(p.tools_policy -> 'external_tools') = 'array'
                THEN p.tools_policy -> 'external_tools'
            ELSE '[]'::jsonb
        END
    ) AS item
), valid_items AS (
    SELECT DISTINCT
        access_surface_id,
        item ->> 'tool_id' AS tool_id,
        COALESCE((item ->> 'enabled')::boolean, true) AS enabled
    FROM policy_items
    WHERE NULLIF(item ->> 'tool_id', '') IS NOT NULL
)
INSERT INTO public.access_tools (
    id, access_point_id, tool_id, enabled, mcp_exposed
)
SELECT
    gen_random_uuid()::text,
    v.access_surface_id,
    v.tool_id,
    v.enabled,
    true
FROM valid_items v
JOIN public.tools t ON t.id = v.tool_id
ON CONFLICT (access_point_id, tool_id) DO UPDATE
SET enabled = EXCLUDED.enabled,
    mcp_exposed = true;

UPDATE public.access_surface_policies
SET tools_policy = CASE
    WHEN jsonb_typeof(tools_policy) = 'object'
        THEN tools_policy - 'custom_tools' - 'bound_tools' - 'external_tools'
    ELSE '{}'::jsonb
END
WHERE access_surface_id IN (
    SELECT id FROM public.access_surfaces WHERE kind = 'mcp'
);

-- Endpoint policy and bindings must change in one transaction. This avoids a
-- window where a policy update succeeds but its binding replacement fails.
CREATE OR REPLACE FUNCTION public.replace_mcp_surface_policy(
    p_surface_id text,
    p_accesses jsonb,
    p_tools_policy jsonb,
    p_bindings jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_policy public.access_surface_policies%ROWTYPE;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.access_surfaces
        WHERE id = p_surface_id AND kind = 'mcp'
    ) THEN
        RAISE EXCEPTION 'MCP access surface not found';
    END IF;

    INSERT INTO public.access_surface_policies (
        access_surface_id, version, fs_policy, tools_policy,
        shell_policy, network_policy
    ) VALUES (
        p_surface_id,
        1,
        jsonb_build_object('accesses', COALESCE(p_accesses, '[]'::jsonb)),
        COALESCE(p_tools_policy, '{}'::jsonb),
        jsonb_build_object('enabled', false),
        '{}'::jsonb
    )
    ON CONFLICT (access_surface_id) DO UPDATE SET
        version = EXCLUDED.version,
        fs_policy = EXCLUDED.fs_policy,
        tools_policy = EXCLUDED.tools_policy,
        shell_policy = EXCLUDED.shell_policy,
        network_policy = EXCLUDED.network_policy
    RETURNING * INTO v_policy;

    DELETE FROM public.access_tools WHERE access_point_id = p_surface_id;
    INSERT INTO public.access_tools (
        id, access_point_id, tool_id, enabled, mcp_exposed
    )
    SELECT
        gen_random_uuid()::text,
        p_surface_id,
        binding ->> 'tool_id',
        COALESCE((binding ->> 'enabled')::boolean, true),
        true
    FROM jsonb_array_elements(COALESCE(p_bindings, '[]'::jsonb)) binding
    JOIN public.tools t ON t.id = binding ->> 'tool_id'
    WHERE NULLIF(binding ->> 'tool_id', '') IS NOT NULL;

    RETURN to_jsonb(v_policy);
END;
$$;

REVOKE ALL ON FUNCTION public.replace_mcp_surface_policy(text, jsonb, jsonb, jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.replace_mcp_surface_policy(text, jsonb, jsonb, jsonb)
    TO service_role;
