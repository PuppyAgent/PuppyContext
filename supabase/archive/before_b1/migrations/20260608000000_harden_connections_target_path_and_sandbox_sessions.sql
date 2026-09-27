-- Harden the post-FS-removal Access/Integration schema.
--
-- 1. connections.target_path is the canonical Integration write destination.
--    Keep it non-null and in the same slash-canonical shape used by repo
--    scopes and upload jobs.
-- 2. scope_sandbox_sessions is one row per repo scope. Cascade it with the
--    scope so deleting a non-root scope cannot leave durable sandbox orphans.

BEGIN;

ALTER TABLE public.connections
    ADD COLUMN IF NOT EXISTS target_path text;

UPDATE public.connections
SET target_path = ''
WHERE target_path IS NULL;

UPDATE public.connections
SET target_path = regexp_replace(
    btrim(replace(target_path, E'\\', '/'), '/'),
    '/+',
    '/',
    'g'
)
WHERE target_path IS DISTINCT FROM regexp_replace(
    btrim(replace(target_path, E'\\', '/'), '/'),
    '/+',
    '/',
    'g'
);

ALTER TABLE public.connections
    ALTER COLUMN target_path SET DEFAULT '';

ALTER TABLE public.connections
    ALTER COLUMN target_path SET NOT NULL;

ALTER TABLE public.connections
    DROP CONSTRAINT IF EXISTS connections_target_path_canonical;

ALTER TABLE public.connections
    ADD CONSTRAINT connections_target_path_canonical CHECK (
        target_path = '' OR (
            target_path NOT LIKE '/%' AND
            target_path NOT LIKE '%/' AND
            target_path NOT LIKE '%//%'
        )
    );

DELETE FROM public.scope_sandbox_sessions s
WHERE NOT EXISTS (
    SELECT 1
    FROM public.repo_scopes rs
    WHERE rs.id = s.scope_id
);

ALTER TABLE public.scope_sandbox_sessions
    DROP CONSTRAINT IF EXISTS scope_sandbox_sessions_scope_id_fkey;

ALTER TABLE public.scope_sandbox_sessions
    ADD CONSTRAINT scope_sandbox_sessions_scope_id_fkey
    FOREIGN KEY (scope_id)
    REFERENCES public.repo_scopes(id)
    ON DELETE CASCADE;

COMMIT;
