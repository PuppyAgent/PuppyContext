-- ISSUE-016: credential rotation must never expose a surface with no active
-- credential when the replacement insert fails.
CREATE OR REPLACE FUNCTION public.rotate_access_surface_bearer_token(
    p_access_surface_id text,
    p_org_id text,
    p_project_id text,
    p_key_prefix text,
    p_key_last4 text,
    p_key_hash text,
    p_hash_alg text,
    p_created_by uuid DEFAULT NULL,
    p_expires_at timestamptz DEFAULT NULL
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    new_id text;
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.access_surfaces s
        WHERE s.id = p_access_surface_id
          AND s.project_id = p_project_id
          AND (
              p_org_id IS NULL
              OR COALESCE(
                  s.org_id,
                  (SELECT p.org_id FROM public.projects p WHERE p.id = s.project_id)
              ) IS NOT DISTINCT FROM p_org_id
          )
    ) THEN
        RAISE EXCEPTION 'access surface/project/org mismatch';
    END IF;

    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = now()
     WHERE access_surface_id = p_access_surface_id
       AND credential_type = 'bearer_token'
       AND status = 'active';

    INSERT INTO public.access_surface_credentials (
        org_id, project_id, access_surface_id, credential_type,
        key_prefix, key_last4, key_hash, hash_alg, status,
        created_by, expires_at
    ) VALUES (
        p_org_id, p_project_id, p_access_surface_id, 'bearer_token',
        p_key_prefix, p_key_last4, p_key_hash, p_hash_alg, 'active',
        p_created_by, p_expires_at
    )
    RETURNING id INTO new_id;

    RETURN new_id;
END;
$$;

REVOKE ALL ON FUNCTION public.rotate_access_surface_bearer_token(
    text, text, text, text, text, text, text, uuid, timestamptz
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_access_surface_bearer_token(
    text, text, text, text, text, text, text, uuid, timestamptz
) TO service_role;
