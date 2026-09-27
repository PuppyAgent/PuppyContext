-- ============================================================================
-- Context entrypoint target tables: Upload / Import / Connect / Access
-- ============================================================================
-- Final product vocabulary
--   Upload  = local files/folders enter a workspace once.
--   Import  = external snapshot enters a workspace once.
--   Connect = durable external relationship; syncs happen through runs.
--   Access  = scoped workspace entry point for humans/tools/runtimes.
--
-- This migration creates the final target tables and stops new runtime writes
-- from being produced by the old repo_scopes -> connectors trigger. Historical
-- tables remain in the database only as historical data; product code should
-- read and write the target tables below.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- Common timestamp trigger
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public._context_entrypoint_bump_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_create_builtin_connectors ON public.repo_scopes;
DROP FUNCTION IF EXISTS public.create_builtin_connectors_for_scope();

-- ----------------------------------------------------------------------------
-- Upload: local bytes entering the workspace
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.upload_jobs (
    id                  text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    org_id              text REFERENCES public.organizations(id) ON DELETE SET NULL,
    project_id          text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    created_by          uuid REFERENCES auth.users(id) ON DELETE SET NULL,

    target_path         text NOT NULL DEFAULT '',
    source_kind         text NOT NULL DEFAULT 'browser'
                            CHECK (source_kind IN ('browser', 'desktop', 'cli')),
    mode                text NOT NULL DEFAULT 'raw'
                            CHECK (mode IN ('raw', 'ocr_parse', 'structured')),

    status              text NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    phase               text NOT NULL DEFAULT 'queued',
    progress            integer NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    message             text,
    error_message       text,

    policy_summary      jsonb NOT NULL DEFAULT '{}'::jsonb,
    config              jsonb NOT NULL DEFAULT '{}'::jsonb,

    result_path         text,
    result_commit_id    text,
    worker_job_id       text,

    started_at          timestamptz,
    completed_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT upload_jobs_target_path_canonical CHECK (
        target_path = '' OR (
            target_path NOT LIKE '/%' AND
            target_path NOT LIKE '%/' AND
            target_path NOT LIKE '%//%'
        )
    )
);

CREATE TABLE IF NOT EXISTS public.upload_items (
    id                  text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    upload_job_id       text NOT NULL REFERENCES public.upload_jobs(id) ON DELETE CASCADE,

    relative_path       text NOT NULL,
    original_name       text NOT NULL,
    size_bytes          bigint NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    mime_type           text,

    s3_key              text,
    content_hash        text,

    status              text NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'uploaded', 'processing', 'completed', 'failed', 'cancelled', 'skipped')),
    skip_reason         text,
    result_path         text,
    error_message       text,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT upload_items_relative_path_canonical CHECK (
        relative_path <> ''
        AND relative_path NOT LIKE '/%'
        AND relative_path NOT LIKE '%/'
        AND relative_path NOT LIKE '%//%'
        AND relative_path NOT LIKE '../%'
        AND relative_path NOT LIKE '%/../%'
    )
);

CREATE INDEX IF NOT EXISTS idx_upload_jobs_project_created
    ON public.upload_jobs(project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_upload_jobs_project_active
    ON public.upload_jobs(project_id, status, created_at DESC)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS idx_upload_jobs_created_by
    ON public.upload_jobs(created_by, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_upload_items_job
    ON public.upload_items(upload_job_id, relative_path);

CREATE INDEX IF NOT EXISTS idx_upload_items_job_status
    ON public.upload_items(upload_job_id, status);

DROP TRIGGER IF EXISTS trg_upload_jobs_updated_at ON public.upload_jobs;
CREATE TRIGGER trg_upload_jobs_updated_at
    BEFORE UPDATE ON public.upload_jobs
    FOR EACH ROW
    EXECUTE FUNCTION public._context_entrypoint_bump_updated_at();

DROP TRIGGER IF EXISTS trg_upload_items_updated_at ON public.upload_items;
CREATE TRIGGER trg_upload_items_updated_at
    BEFORE UPDATE ON public.upload_items
    FOR EACH ROW
    EXECUTE FUNCTION public._context_entrypoint_bump_updated_at();

-- ----------------------------------------------------------------------------
-- Import: one-shot external snapshots
-- ----------------------------------------------------------------------------
-- `import_jobs` already exists and remains the canonical Import table. These
-- additive columns make the source identity explicit without changing existing
-- callers.

ALTER TABLE public.import_jobs
    ADD COLUMN IF NOT EXISTS source_kind text,
    ADD COLUMN IF NOT EXISTS source_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS idempotency_key text;

ALTER TABLE public.import_jobs
    DROP CONSTRAINT IF EXISTS import_jobs_source_kind_check;
ALTER TABLE public.import_jobs
    ADD CONSTRAINT import_jobs_source_kind_check CHECK (
        source_kind IS NULL OR source_kind IN ('repository', 'url', 'website', 'template', 'document', 'other')
    );

WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY project_id, provider, idempotency_key
            ORDER BY created_at ASC, id ASC
        ) AS rn
    FROM public.import_jobs
    WHERE idempotency_key IS NOT NULL
)
UPDATE public.import_jobs j
SET idempotency_key = NULL
FROM ranked r
WHERE j.id = r.id
  AND r.rn > 1;

DROP INDEX IF EXISTS public.idx_import_jobs_idempotency;
CREATE UNIQUE INDEX idx_import_jobs_idempotency
    ON public.import_jobs(project_id, provider, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- ----------------------------------------------------------------------------
-- Connect: durable external relationships
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.connections (
    id                      text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    org_id                  text REFERENCES public.organizations(id) ON DELETE SET NULL,
    project_id              text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    scope_id                text REFERENCES public.repo_scopes(id) ON DELETE SET NULL,

    provider                text NOT NULL,
    name                    text NOT NULL,
    direction               text NOT NULL
                                CHECK (direction IN ('inbound', 'outbound', 'bidirectional')),

    external_resource_id    text,
    external_resource_label text,
    external_url            text,

    oauth_connection_id     bigint REFERENCES public.oauth_connections(id) ON DELETE SET NULL,
    credential_ref          text,

    config                  jsonb NOT NULL DEFAULT '{}'::jsonb,

    trigger_type            text NOT NULL DEFAULT 'manual'
                                CHECK (trigger_type IN ('manual', 'scheduled', 'webhook', 'realtime')),
    trigger_config          jsonb NOT NULL DEFAULT '{}'::jsonb,

    status                  text NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'paused', 'syncing', 'error', 'disabled')),

    cursor                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    remote_hash             text,
    external_version        text,
    last_sync_run_id        text,
    last_synced_at          timestamptz,
    last_sync_commit_id     text,
    error_message           text,

    created_by              uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT connections_provider_check CHECK (provider <> ''),
    CONSTRAINT connections_name_check CHECK (name <> '')
);

-- `connections` existed before this target model. `CREATE TABLE IF NOT EXISTS`
-- is not enough on upgraded databases, so normalize the existing table shape
-- explicitly.
ALTER TABLE public.connections
    ADD COLUMN IF NOT EXISTS org_id text REFERENCES public.organizations(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS scope_id text REFERENCES public.repo_scopes(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS name text,
    ADD COLUMN IF NOT EXISTS external_resource_id text,
    ADD COLUMN IF NOT EXISTS external_resource_label text,
    ADD COLUMN IF NOT EXISTS external_url text,
    ADD COLUMN IF NOT EXISTS oauth_connection_id bigint REFERENCES public.oauth_connections(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS credential_ref text,
    ADD COLUMN IF NOT EXISTS credentials_ref text,
    ADD COLUMN IF NOT EXISTS trigger jsonb,
    ADD COLUMN IF NOT EXISTS trigger_type text,
    ADD COLUMN IF NOT EXISTS trigger_config jsonb,
    ADD COLUMN IF NOT EXISTS external_version text,
    ADD COLUMN IF NOT EXISTS last_sync_run_id text,
    ADD COLUMN IF NOT EXISTS last_sync_commit_id text,
    ADD COLUMN IF NOT EXISTS created_by uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS user_id uuid;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'connections'
          AND column_name = 'cursor'
          AND data_type <> 'jsonb'
    ) THEN
        ALTER TABLE public.connections
            ALTER COLUMN cursor DROP DEFAULT,
            ALTER COLUMN cursor TYPE jsonb
            USING CASE
                WHEN cursor IS NULL THEN '{}'::jsonb
                ELSE jsonb_build_object('value', cursor)
            END;
    END IF;
END $$;

ALTER TABLE public.connections DROP CONSTRAINT IF EXISTS chk_syncs_status;
ALTER TABLE public.connections DROP CONSTRAINT IF EXISTS connections_status_check;

UPDATE public.connections c
SET
    org_id = COALESCE(c.org_id, p.org_id),
    name = COALESCE(
        NULLIF(c.name, ''),
        NULLIF(c.config->>'name', ''),
        NULLIF(c.config->>'sync_url', ''),
        c.provider
    ),
    credential_ref = COALESCE(c.credential_ref, c.credentials_ref),
    trigger_type = COALESCE(NULLIF(c.trigger_type, ''), c.trigger->>'type', 'manual'),
    trigger_config = COALESCE(c.trigger_config, c.trigger - 'type', '{}'::jsonb),
    external_resource_id = COALESCE(c.external_resource_id, c.config->>'external_resource_id'),
    external_resource_label = COALESCE(c.external_resource_label, c.config->>'name'),
    external_url = COALESCE(
        c.external_url,
        c.config->>'source_url',
        c.config->>'sync_url',
        c.config->>'url',
        c.config->>'external_url'
    ),
    remote_hash = COALESCE(c.remote_hash, c.config->>'remote_hash'),
    last_sync_commit_id = COALESCE(c.last_sync_commit_id, c.config->>'last_sync_commit_id'),
    created_by = COALESCE(c.created_by, c.user_id)
FROM public.projects p
WHERE p.id = c.project_id;

UPDATE public.connections
SET
    status = CASE
        WHEN status = 'inactive' THEN 'disabled'
        WHEN status IN ('active', 'paused', 'syncing', 'error', 'disabled') THEN status
        ELSE 'active'
    END,
    trigger_type = CASE
        WHEN trigger_type IN ('manual', 'scheduled', 'webhook', 'realtime') THEN trigger_type
        WHEN trigger_type = 'cron' THEN 'scheduled'
        ELSE 'manual'
    END,
    trigger_config = COALESCE(trigger_config, '{}'::jsonb),
    cursor = COALESCE(cursor, '{}'::jsonb),
    name = COALESCE(NULLIF(name, ''), provider);

-- Old sync-era rows stored their mount directly on connections.path. The final
-- model stores scope_id and reads the path from repo_scopes.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'connections'
          AND column_name = 'path'
    ) THEN
        EXECUTE $sql$
            INSERT INTO public.repo_scopes (
                project_id,
                name,
                path,
                exclude,
                mode,
                is_root,
                access_key
            )
            SELECT DISTINCT
                c.project_id,
                CASE
                    WHEN btrim(COALESCE(c.path, ''), '/') = '' THEN 'Root'
                    ELSE split_part(
                        btrim(COALESCE(c.path, ''), '/'),
                        '/',
                        array_length(
                            string_to_array(btrim(COALESCE(c.path, ''), '/'), '/'),
                            1
                        )
                    )
                END,
                btrim(COALESCE(c.path, ''), '/'),
                '[]'::jsonb,
                'rw',
                btrim(COALESCE(c.path, ''), '/') = '',
                'cli_' || translate(
                    encode(gen_random_bytes(24), 'base64'),
                    '+/=',
                    '___'
                )
            FROM public.connections c
            WHERE c.scope_id IS NULL
              AND c.project_id IS NOT NULL
            ON CONFLICT (project_id, path) DO NOTHING
        $sql$;

        EXECUTE $sql$
            UPDATE public.connections c
            SET scope_id = s.id
            FROM public.repo_scopes s
            WHERE c.scope_id IS NULL
              AND s.project_id = c.project_id
              AND s.path = btrim(COALESCE(c.path, ''), '/')
        $sql$;
    END IF;
END $$;

ALTER TABLE public.connections
    ALTER COLUMN name SET NOT NULL,
    ALTER COLUMN cursor SET DEFAULT '{}'::jsonb,
    ALTER COLUMN cursor SET NOT NULL,
    ALTER COLUMN trigger_type SET DEFAULT 'manual',
    ALTER COLUMN trigger_type SET NOT NULL,
    ALTER COLUMN trigger_config SET DEFAULT '{}'::jsonb,
    ALTER COLUMN trigger_config SET NOT NULL;

ALTER TABLE public.connections
    ADD CONSTRAINT connections_status_check
    CHECK (status IN ('active', 'paused', 'syncing', 'error', 'disabled'));

ALTER TABLE public.connections DROP CONSTRAINT IF EXISTS connections_trigger_type_check;
ALTER TABLE public.connections
    ADD CONSTRAINT connections_trigger_type_check
    CHECK (trigger_type IN ('manual', 'scheduled', 'webhook', 'realtime'));

ALTER TABLE public.connections DROP CONSTRAINT IF EXISTS chk_syncs_direction;
ALTER TABLE public.connections DROP CONSTRAINT IF EXISTS connections_direction_check;
ALTER TABLE public.connections
    ADD CONSTRAINT connections_direction_check
    CHECK (direction IN ('inbound', 'outbound', 'bidirectional'));

ALTER TABLE public.connections DROP CONSTRAINT IF EXISTS connections_provider_check;
ALTER TABLE public.connections
    ADD CONSTRAINT connections_provider_check CHECK (provider <> '');

ALTER TABLE public.connections DROP CONSTRAINT IF EXISTS connections_name_check;
ALTER TABLE public.connections
    ADD CONSTRAINT connections_name_check CHECK (name <> '');

ALTER TABLE public.connections DROP CONSTRAINT IF EXISTS chk_syncs_authority;
ALTER TABLE public.connections DROP CONSTRAINT IF EXISTS chk_syncs_conflict_strategy;
ALTER TABLE public.connections DROP CONSTRAINT IF EXISTS syncs_user_id_fkey;

DROP INDEX IF EXISTS public.idx_syncs_access_key;
DROP INDEX IF EXISTS public.idx_syncs_node;
DROP INDEX IF EXISTS public.idx_syncs_one_authority_per_node;
DROP INDEX IF EXISTS public.idx_syncs_project;
DROP INDEX IF EXISTS public.idx_syncs_provider;
DROP INDEX IF EXISTS public.idx_syncs_provider_agent;
DROP INDEX IF EXISTS public.idx_syncs_status;
DROP INDEX IF EXISTS public.idx_syncs_user_id;

ALTER TABLE public.connections
    DROP COLUMN IF EXISTS node_id,
    DROP COLUMN IF EXISTS path,
    DROP COLUMN IF EXISTS authority,
    DROP COLUMN IF EXISTS credentials_ref,
    DROP COLUMN IF EXISTS access_key,
    DROP COLUMN IF EXISTS trigger,
    DROP COLUMN IF EXISTS conflict_strategy,
    DROP COLUMN IF EXISTS last_sync_version,
    DROP COLUMN IF EXISTS user_id;

CREATE INDEX IF NOT EXISTS idx_connections_project
    ON public.connections(project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_connections_scope
    ON public.connections(scope_id);

CREATE INDEX IF NOT EXISTS idx_connections_provider_project
    ON public.connections(project_id, provider);

CREATE INDEX IF NOT EXISTS idx_connections_status
    ON public.connections(project_id, status);

CREATE INDEX IF NOT EXISTS idx_connections_oauth
    ON public.connections(oauth_connection_id)
    WHERE oauth_connection_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_connections_external_resource
    ON public.connections(project_id, provider, external_resource_id)
    WHERE external_resource_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_connections_updated_at ON public.connections;
CREATE TRIGGER trg_connections_updated_at
    BEFORE UPDATE ON public.connections
    FOR EACH ROW
    EXECUTE FUNCTION public._context_entrypoint_bump_updated_at();

-- Backfill durable external source relationships from legacy connector rows.
-- Import-only connectors stay historical; Access-style rows are handled by
-- `access_surfaces` below.
INSERT INTO public.connections (
    id,
    org_id,
    project_id,
    scope_id,
    provider,
    name,
    direction,
    external_resource_id,
    external_resource_label,
    external_url,
    oauth_connection_id,
    credential_ref,
    config,
    trigger_type,
    trigger_config,
    status,
    cursor,
    remote_hash,
    last_synced_at,
    last_sync_commit_id,
    error_message,
    created_by,
    created_at,
    updated_at
)
SELECT
    c.id,
    p.org_id,
    c.project_id,
    c.scope_id,
    c.provider,
    COALESCE(NULLIF(c.name, ''), NULLIF(c.config->>'name', ''), c.provider),
    c.direction,
    c.config->>'external_resource_id',
    c.config->>'name',
    COALESCE(c.config->>'source_url', c.config->>'sync_url', c.config->>'url', c.config->>'external_url'),
    c.oauth_connection_id,
    COALESCE(c.config->>'credentials_ref', c.oauth_connection_id::text),
    c.config,
    CASE
        WHEN c.trigger->>'type' IN ('manual', 'scheduled', 'webhook', 'realtime') THEN c.trigger->>'type'
        WHEN c.trigger->>'type' = 'cron' THEN 'scheduled'
        ELSE 'manual'
    END,
    COALESCE(c.trigger - 'type', '{}'::jsonb),
    CASE
        WHEN c.status IN ('active', 'paused', 'syncing', 'error', 'disabled') THEN c.status
        ELSE 'active'
    END,
    CASE
        WHEN c.config ? 'cursor' THEN jsonb_build_object('value', c.config->'cursor')
        ELSE '{}'::jsonb
    END,
    c.config->>'remote_hash',
    c.last_run_at,
    c.config->>'last_sync_commit_id',
    c.error_message,
    c.created_by,
    c.created_at,
    c.updated_at
FROM public.connectors c
JOIN public.projects p ON p.id = c.project_id
WHERE c.provider NOT IN ('cli', 'agent', 'filesystem', 'mcp', 'sandbox')
  AND COALESCE(c.trigger->>'type', 'manual') <> 'import_once'
ON CONFLICT (id) DO UPDATE SET
    org_id = EXCLUDED.org_id,
    scope_id = EXCLUDED.scope_id,
    provider = EXCLUDED.provider,
    name = EXCLUDED.name,
    direction = EXCLUDED.direction,
    external_resource_id = EXCLUDED.external_resource_id,
    external_resource_label = EXCLUDED.external_resource_label,
    external_url = EXCLUDED.external_url,
    oauth_connection_id = EXCLUDED.oauth_connection_id,
    credential_ref = EXCLUDED.credential_ref,
    config = EXCLUDED.config,
    trigger_type = EXCLUDED.trigger_type,
    trigger_config = EXCLUDED.trigger_config,
    status = EXCLUDED.status,
    cursor = EXCLUDED.cursor,
    remote_hash = EXCLUDED.remote_hash,
    last_synced_at = EXCLUDED.last_synced_at,
    last_sync_commit_id = EXCLUDED.last_sync_commit_id,
    error_message = EXCLUDED.error_message,
    created_by = EXCLUDED.created_by,
    updated_at = EXCLUDED.updated_at;

-- ----------------------------------------------------------------------------
-- SyncRun: one execution of a connection
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.sync_runs (
    id                  text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    connection_id       text NOT NULL REFERENCES public.connections(id) ON DELETE CASCADE,
    project_id          text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,

    triggered_by        text NOT NULL
                            CHECK (triggered_by IN ('manual', 'scheduled', 'webhook', 'realtime', 'initial', 'push')),
    triggered_by_user_id uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    direction           text NOT NULL
                            CHECK (direction IN ('inbound', 'outbound', 'bidirectional')),

    status              text NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'conflict', 'skipped')),
    phase               text NOT NULL DEFAULT 'queued',
    progress            integer NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    message             text,
    error_message       text,
    stdout              text,
    exit_code           integer,
    duration_ms         integer,

    worker_job_id       text,

    external_version    text,
    remote_hash         text,
    result_path         text,
    result_commit_id    text,
    files_changed       integer,
    result              jsonb NOT NULL DEFAULT '{}'::jsonb,

    started_at          timestamptz,
    finished_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_connection_recent
    ON public.sync_runs(connection_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sync_runs_project_recent
    ON public.sync_runs(project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sync_runs_project_active
    ON public.sync_runs(project_id, status, created_at DESC)
    WHERE status IN ('queued', 'running');

ALTER TABLE public.connections
    DROP CONSTRAINT IF EXISTS connections_last_sync_run_fk;

-- Earlier migrations renamed `sync_runs` to `connector_runs`, but some
-- databases still carry the original `sync_id` column. Normalize it before
-- copying legacy history into the new `sync_runs` table.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'connector_runs'
          AND column_name = 'sync_id'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'connector_runs'
          AND column_name = 'connector_id'
    ) THEN
        ALTER TABLE public.connector_runs
            RENAME COLUMN sync_id TO connector_id;
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_sync_runs_updated_at ON public.sync_runs;
CREATE TRIGGER trg_sync_runs_updated_at
    BEFORE UPDATE ON public.sync_runs
    FOR EACH ROW
    EXECUTE FUNCTION public._context_entrypoint_bump_updated_at();

-- Preserve legacy run history for connector rows that became durable
-- connections above.
INSERT INTO public.sync_runs (
    id,
    connection_id,
    project_id,
    triggered_by,
    direction,
    status,
    phase,
    progress,
    message,
    error_message,
    stdout,
    exit_code,
    duration_ms,
    started_at,
    finished_at,
    created_at,
    updated_at
)
SELECT
    cr.id,
    cr.connector_id,
    con.project_id,
    CASE
        WHEN cr.trigger_type IN ('manual', 'scheduled', 'webhook', 'realtime', 'initial', 'push') THEN cr.trigger_type
        WHEN cr.trigger_type = 'cron' THEN 'scheduled'
        ELSE 'manual'
    END,
    con.direction,
    CASE
        WHEN cr.status = 'success' THEN 'completed'
        WHEN cr.status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'conflict', 'skipped') THEN cr.status
        ELSE 'running'
    END,
    CASE
        WHEN cr.status = 'success' THEN 'completed'
        WHEN cr.status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'conflict', 'skipped') THEN cr.status
        ELSE 'running'
    END,
    CASE
        WHEN cr.status IN ('success', 'completed', 'failed', 'cancelled', 'conflict', 'skipped') THEN 100
        ELSE 0
    END,
    cr.result_summary,
    cr.error,
    cr.stdout,
    cr.exit_code,
    cr.duration_ms,
    cr.started_at,
    cr.finished_at,
    cr.created_at,
    COALESCE(cr.finished_at, cr.created_at)
FROM public.connector_runs cr
JOIN public.connections con ON con.id = cr.connector_id
ON CONFLICT (id) DO NOTHING;

UPDATE public.connections con
SET last_sync_run_id = c.last_run_id
FROM public.connectors c
WHERE c.id = con.id
  AND c.last_run_id IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM public.sync_runs sr WHERE sr.id = c.last_run_id
  );

UPDATE public.connections con
SET last_sync_run_id = NULL
WHERE con.last_sync_run_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM public.sync_runs sr WHERE sr.id = con.last_sync_run_id
  );

ALTER TABLE public.connections
    ADD CONSTRAINT connections_last_sync_run_fk
    FOREIGN KEY (last_sync_run_id)
    REFERENCES public.sync_runs(id)
    ON DELETE SET NULL;

-- ----------------------------------------------------------------------------
-- Access: scoped workspace entry points
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.access_surfaces (
    id              text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    org_id          text REFERENCES public.organizations(id) ON DELETE SET NULL,
    project_id      text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    scope_id        text NOT NULL REFERENCES public.repo_scopes(id) ON DELETE CASCADE,

    kind            text NOT NULL
                        CHECK (kind IN ('git_remote', 'cli', 'filesystem', 'agent', 'mcp', 'sandbox')),
    name            text NOT NULL,
    status          text NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'paused', 'error', 'disabled')),

    principal_type  text,
    principal_id    text,
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_by      uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT access_surfaces_name_check CHECK (name <> '')
);

CREATE INDEX IF NOT EXISTS idx_access_surfaces_project
    ON public.access_surfaces(project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_access_surfaces_scope
    ON public.access_surfaces(scope_id);

CREATE INDEX IF NOT EXISTS idx_access_surfaces_kind
    ON public.access_surfaces(project_id, kind);

CREATE UNIQUE INDEX IF NOT EXISTS idx_access_surfaces_builtin_one_per_scope
    ON public.access_surfaces(scope_id, kind)
    WHERE kind IN ('git_remote', 'cli', 'filesystem');

DROP TRIGGER IF EXISTS trg_access_surfaces_updated_at ON public.access_surfaces;
CREATE TRIGGER trg_access_surfaces_updated_at
    BEFORE UPDATE ON public.access_surfaces
    FOR EACH ROW
    EXECUTE FUNCTION public._context_entrypoint_bump_updated_at();

-- Backfill target Access surfaces from the current repo_scopes/connectors
-- model. Legacy rows remain the runtime compatibility path during rollout.
INSERT INTO public.access_surfaces (
    org_id,
    project_id,
    scope_id,
    kind,
    name,
    status,
    principal_type,
    principal_id,
    config,
    created_by,
    created_at,
    updated_at
)
SELECT
    p.org_id,
    rs.project_id,
    rs.id,
    'git_remote',
    'Git Remote',
    'active',
    'scope',
    rs.id,
    jsonb_build_object('access_key', rs.access_key),
    p.created_by,
    rs.created_at,
    rs.updated_at
FROM public.repo_scopes rs
JOIN public.projects p ON p.id = rs.project_id
WHERE NOT EXISTS (
    SELECT 1
    FROM public.access_surfaces a
    WHERE a.scope_id = rs.id
      AND a.kind = 'git_remote'
);

INSERT INTO public.access_surfaces (
    org_id,
    project_id,
    scope_id,
    kind,
    name,
    status,
    principal_type,
    principal_id,
    config,
    created_by,
    created_at,
    updated_at
)
SELECT
    p.org_id,
    c.project_id,
    c.scope_id,
    CASE c.provider
        WHEN 'cli' THEN 'cli'
        WHEN 'filesystem' THEN 'filesystem'
        WHEN 'agent' THEN 'agent'
        WHEN 'mcp' THEN 'mcp'
        WHEN 'sandbox' THEN 'sandbox'
    END,
    c.name,
    CASE
        WHEN c.status IN ('active', 'paused', 'error', 'disabled') THEN c.status
        WHEN c.status = 'syncing' THEN 'active'
        ELSE 'active'
    END,
    'connector',
    c.id,
    (c.config || jsonb_build_object('legacy_connector_id', c.id)),
    c.created_by,
    c.created_at,
    c.updated_at
FROM public.connectors c
JOIN public.projects p ON p.id = c.project_id
WHERE c.provider IN ('cli', 'filesystem', 'agent', 'mcp', 'sandbox')
  AND c.scope_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM public.access_surfaces a
      WHERE a.scope_id = c.scope_id
        AND a.kind = CASE c.provider
            WHEN 'cli' THEN 'cli'
            WHEN 'filesystem' THEN 'filesystem'
            WHEN 'agent' THEN 'agent'
            WHEN 'mcp' THEN 'mcp'
            WHEN 'sandbox' THEN 'sandbox'
        END
        AND (
            c.provider IN ('cli', 'filesystem')
            OR a.config->>'legacy_connector_id' = c.id
        )
  );

-- ----------------------------------------------------------------------------
-- Activity aggregation view
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.context_activity_items
WITH (security_invoker = true) AS
SELECT
    u.id,
    'upload'::text AS kind,
    u.project_id,
    u.created_by,
    COALESCE(NULLIF(u.target_path, ''), 'Upload') AS label,
    u.status,
    u.phase,
    u.progress,
    u.message,
    u.error_message,
    u.result_path,
    u.result_commit_id,
    u.created_at,
    u.completed_at
FROM public.upload_jobs u
UNION ALL
SELECT
    i.id,
    'import'::text AS kind,
    i.project_id,
    i.created_by,
    COALESCE(i.name, i.source_url, i.provider) AS label,
    i.status,
    i.phase,
    i.progress,
    i.message,
    i.error_message,
    i.result_path,
    i.result_commit_id,
    i.created_at,
    i.completed_at
FROM public.import_jobs i
UNION ALL
SELECT
    s.id,
    'sync_run'::text AS kind,
    s.project_id,
    c.created_by,
    COALESCE(c.name, c.provider) AS label,
    s.status,
    s.phase,
    s.progress,
    s.message,
    s.error_message,
    s.result_path,
    s.result_commit_id,
    s.created_at,
    s.finished_at AS completed_at
FROM public.sync_runs s
JOIN public.connections c ON c.id = s.connection_id;

-- ----------------------------------------------------------------------------
-- RLS: service_role is the backend access path. User-level authorization stays
-- in FastAPI services where project membership and scope permissions are known.
-- ----------------------------------------------------------------------------

ALTER TABLE public.upload_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.upload_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.access_surfaces ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'upload_jobs'
          AND policyname = 'upload_jobs_service_role_all'
    ) THEN
        CREATE POLICY "upload_jobs_service_role_all"
            ON public.upload_jobs
            FOR ALL
            TO service_role
            USING (true)
            WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'upload_items'
          AND policyname = 'upload_items_service_role_all'
    ) THEN
        CREATE POLICY "upload_items_service_role_all"
            ON public.upload_items
            FOR ALL
            TO service_role
            USING (true)
            WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'connections'
          AND policyname = 'connections_service_role_all'
    ) THEN
        CREATE POLICY "connections_service_role_all"
            ON public.connections
            FOR ALL
            TO service_role
            USING (true)
            WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'sync_runs'
          AND policyname = 'sync_runs_service_role_all'
    ) THEN
        CREATE POLICY "sync_runs_service_role_all"
            ON public.sync_runs
            FOR ALL
            TO service_role
            USING (true)
            WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'access_surfaces'
          AND policyname = 'access_surfaces_service_role_all'
    ) THEN
        CREATE POLICY "access_surfaces_service_role_all"
            ON public.access_surfaces
            FOR ALL
            TO service_role
            USING (true)
            WITH CHECK (true);
    END IF;
END $$;

COMMIT;
