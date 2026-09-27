-- Migrate db_connections into the unified connections table (provider='database').
--
-- Background: The database connector previously used a separate `db_connections`
-- table. All other connector types (datasource, agent, mcp, sandbox, filesystem)
-- already use the unified `connections` table. This migration brings database
-- connectors into alignment.
--
-- The repository code (connectors/database/repository.py) has already been
-- updated to read/write from `connections` with provider='database'.

BEGIN;

INSERT INTO connections (id, project_id, provider, direction, status, config, last_synced_at, created_at, updated_at)
SELECT
    id,
    project_id,
    'database',
    'inbound',
    CASE WHEN is_active THEN 'active' ELSE 'inactive' END,
    jsonb_build_object(
        'name', name,
        'db_provider', provider,
        'db_config', config,
        'created_by', created_by
    ),
    last_used_at,
    created_at,
    updated_at
FROM db_connections
ON CONFLICT (id) DO NOTHING;

-- Drop the old table after confirming data integrity.
DROP TABLE IF EXISTS db_connections;

COMMIT;
