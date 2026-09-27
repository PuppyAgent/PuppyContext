-- Empty placeholders are not credentials. Nonempty values must still pass
-- the separate credential backfill before their fields can be retired.
UPDATE public.access_surfaces AS surface
SET config = surface.config - ARRAY(
    SELECT field.key
    FROM jsonb_each(surface.config) AS field
    WHERE field.key IN ('api_key', 'mcp_api_key', 'access_key')
      AND field.value IN ('null'::jsonb, '""'::jsonb)
)
WHERE surface.kind IN ('agent', 'sandbox')
  AND EXISTS (
      SELECT 1
      FROM jsonb_each(surface.config) AS field
      WHERE field.key IN ('api_key', 'mcp_api_key', 'access_key')
        AND field.value IN ('null'::jsonb, '""'::jsonb)
  );
