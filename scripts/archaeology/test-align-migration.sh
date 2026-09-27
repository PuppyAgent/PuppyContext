#!/usr/bin/env bash
# Local validation for 20260418040000_align_legacy_drift.sql
#
# Two scenarios:
#   1. Fresh DB: apply ALL 22 migrations -> should succeed with clean schema
#   2. Drift sim: apply 21 migrations (excluding the new one), inject
#      prod-like drift, apply the new migration, verify cleanup

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@17/bin}"
PORT="${PORT:-54400}"
DATA_DIR="/tmp/puppyone-align-test-$$"
LOG_DIR="${REPO_ROOT}/docs/archaeology/test-align-$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "${LOG_DIR}"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '\033[32m[OK]\033[0m %s\n' "$*"; }
err()  { printf '\033[31m[FAIL]\033[0m %s\n' "$*"; }
hdr()  { printf '\n========================================\n %s \n========================================\n' "$*"; }

cleanup() {
  if [[ -f "${DATA_DIR}/postmaster.pid" ]]; then
    "${PG_BIN}/pg_ctl" -D "${DATA_DIR}" -m immediate stop >/dev/null 2>&1 || true
  fi
  rm -rf "${DATA_DIR}"
}
trap cleanup EXIT

start_pg() {
  rm -rf "${DATA_DIR}"
  "${PG_BIN}/initdb" -D "${DATA_DIR}" -U postgres -A trust --no-sync --encoding=UTF8 >/dev/null 2>&1
  echo "port = ${PORT}" >> "${DATA_DIR}/postgresql.conf"
  echo "unix_socket_directories = '/tmp'" >> "${DATA_DIR}/postgresql.conf"
  echo "listen_addresses = ''" >> "${DATA_DIR}/postgresql.conf"
  "${PG_BIN}/pg_ctl" -D "${DATA_DIR}" -l "${DATA_DIR}/server.log" start >/dev/null 2>&1
  for _ in {1..10}; do
    if "${PG_BIN}/psql" -h /tmp -p "${PORT}" -U postgres -d postgres -c 'select 1' >/dev/null 2>&1; then
      return
    fi
    sleep 0.3
  done
  err "Postgres failed to start"; exit 1
}

run_psql() {
  PGPASSWORD= "${PG_BIN}/psql" -h /tmp -p "${PORT}" -U postgres -d postgres \
    --no-psqlrc --set ON_ERROR_STOP=on "$@"
}

install_stubs() {
  run_psql -c "
    CREATE SCHEMA IF NOT EXISTS auth;
    CREATE SCHEMA IF NOT EXISTS storage;
    CREATE SCHEMA IF NOT EXISTS extensions;
    CREATE SCHEMA IF NOT EXISTS graphql;
    CREATE SCHEMA IF NOT EXISTS vault;
    CREATE SCHEMA IF NOT EXISTS supabase_migrations;
    CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY, email text, raw_user_meta_data jsonb, created_at timestamptz DEFAULT now());
    CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS \$\$ SELECT NULL::uuid \$\$;
    CREATE OR REPLACE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS \$\$ SELECT 'service_role'::text \$\$;
    CREATE OR REPLACE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS \$\$ SELECT '{}'::jsonb \$\$;
    CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;
    CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\" WITH SCHEMA extensions;
    DO \$\$ BEGIN CREATE PUBLICATION supabase_realtime; EXCEPTION WHEN duplicate_object THEN NULL; END \$\$;
    DO \$\$ BEGIN CREATE ROLE anon NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END \$\$;
    DO \$\$ BEGIN CREATE ROLE authenticated NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END \$\$;
    DO \$\$ BEGIN CREATE ROLE service_role NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END \$\$;
    DO \$\$ BEGIN CREATE ROLE supabase_auth_admin NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END \$\$;
    CREATE TABLE IF NOT EXISTS supabase_migrations.schema_migrations (version text PRIMARY KEY, name text, statements text[]);
  " >/dev/null 2>&1
}

apply_migrations() {
  local up_to="${1:-9999999999}"  # apply only files with version <= this
  local applied=0 failed=0 skipped=0 expected_fail=0
  for f in "${REPO_ROOT}"/supabase/archive/before_b1/migrations/*.sql; do
    local fn version
    fn="$(basename "$f")"
    version="${fn%%_*}"
    if [[ "${version}" > "${up_to}" ]]; then
      skipped=$((skipped+1))
      continue
    fi

    # The prod_alignment migration intentionally fails on fresh DB
    if [[ "${fn}" == "20260308000000_prod_alignment.sql" ]]; then
      expected_fail=$((expected_fail+1))
      continue
    fi

    local tmp_sql
    tmp_sql="$(mktemp)"
    sed -E '/^[[:space:]]*CREATE EXTENSION IF NOT EXISTS "?(pg_net|pg_graphql|pg_stat_statements|pg_jsonschema|supabase_vault|http|pgsodium)"?/Id' "$f" > "$tmp_sql"

    if PGPASSWORD= "${PG_BIN}/psql" -h /tmp -p "${PORT}" -U postgres -d postgres \
         --no-psqlrc --set ON_ERROR_STOP=on -q -f "$tmp_sql" \
         >> "${LOG_DIR}/apply.log" 2>&1; then
      applied=$((applied+1))
    else
      failed=$((failed+1))
      err "FAILED: ${fn}"
      tail -20 "${LOG_DIR}/apply.log" || true
    fi
    rm -f "$tmp_sql"
  done
  echo "Applied=${applied}, Failed=${failed}, Skipped=${skipped}, Expected-fail=${expected_fail}"
  if [[ "${failed}" -gt 0 ]]; then return 1; fi
}

verify_schema() {
  local label="$1"
  local tables fns triggers indexes ; local null_proj null_tools
  local missing_fks stale_names user_fk_action
  tables=$(run_psql -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
  fns=$(run_psql -tAc "SELECT count(*) FROM information_schema.routines WHERE routine_schema='public' AND routine_type='FUNCTION'")
  triggers=$(run_psql -tAc "SELECT count(*) FROM information_schema.triggers WHERE trigger_schema='public'")
  indexes=$(run_psql -tAc "SELECT count(*) FROM pg_indexes WHERE schemaname='public'")
  null_proj=$(run_psql -tAc "SELECT count(*) FROM public.projects WHERE org_id IS NULL")
  null_tools=$(run_psql -tAc "SELECT count(*) FROM public.tools WHERE org_id IS NULL")

  missing_fks=$(run_psql -tAc "
    SELECT count(*) FROM (VALUES
      ('access_logs_agent_id_fkey'),
      ('agent_execution_log_agent_id_fkey'),
      ('agent_logs_agent_id_fkey'),
      ('agent_tool_agent_id_fkey'),
      ('chat_sessions_agent_id_fkey'),
      ('etl_rule_org_id_fkey'),
      ('organizations_created_by_fkey'),
      ('project_org_id_fkey'),
      ('tool_org_id_fkey'),
      ('uploads_user_id_fkey')
    ) v(name)
    WHERE NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = v.name)")

  stale_names=$(run_psql -tAc "
    SELECT count(*) FROM pg_constraint
    WHERE conname IN ('connections_pkey','connections_project_id_fkey','connections_user_id_fkey')")

  user_fk_action=$(run_psql -tAc "SELECT confdeltype FROM pg_constraint WHERE conname='syncs_user_id_fkey'")

  printf '%-30s tables=%s functions=%s triggers=%s indexes=%s null_proj=%s null_tools=%s missing_fks=%s stale_names=%s user_fk=%s\n' \
    "${label}" "${tables}" "${fns}" "${triggers}" "${indexes}" "${null_proj}" "${null_tools}" "${missing_fks}" "${stale_names}" "${user_fk_action}"

  if [[ "${null_proj}" -gt 0 || "${null_tools}" -gt 0 || "${missing_fks}" -gt 0 || "${stale_names}" -gt 0 ]]; then
    err "${label} has lingering drift!"
    return 1
  fi
  if [[ "${user_fk_action}" != "n" ]]; then
    err "${label}: syncs_user_id_fkey ON DELETE is '${user_fk_action}', expected 'n' (SET NULL)"
    return 1
  fi
  return 0
}

# ============================================================================
# SCENARIO 1: Fresh DB, apply all 22 migrations
# ============================================================================
hdr "SCENARIO 1: Fresh DB + ALL 22 migrations"
start_pg
install_stubs
log "Applying all migrations..."
apply_migrations
verify_schema "scenario1_fresh_all"

# ============================================================================
# SCENARIO 2: Apply 21 migrations, inject prod-like drift, apply alignment
# ============================================================================
hdr "SCENARIO 2: 21 migrations + prod-like drift + alignment migration"
cleanup
start_pg
install_stubs
log "Applying first 21 migrations (up to 20260418030000)..."
apply_migrations "20260418030000"

log "Injecting prod-like drift state..."

# Insert a fake org and user to make data realistic
run_psql -c "
  INSERT INTO auth.users (id, email) VALUES ('11111111-1111-1111-1111-111111111111', 'sim@test.com');
  INSERT INTO public.organizations (id, name, slug, created_by) VALUES ('019cd091-cc71-71c4-9786-7f24e5161a4e', 'Sim Org', 'sim-org', '11111111-1111-1111-1111-111111111111');
" >/dev/null

# Drop NOT NULL on org_id columns (prod's looser state)
run_psql -c "
  ALTER TABLE public.projects ALTER COLUMN org_id DROP NOT NULL;
  ALTER TABLE public.tools    ALTER COLUMN org_id DROP NOT NULL;
" >/dev/null

# Add NOT NULL on created_by columns (prod's stricter state)
# Need backfill data first since some tables may have rows already
run_psql -c "
  UPDATE public.context_publishes SET created_by = '11111111-1111-1111-1111-111111111111' WHERE created_by IS NULL;
  UPDATE public.etl_rules         SET created_by = '11111111-1111-1111-1111-111111111111' WHERE created_by IS NULL;
  UPDATE public.mcp               SET created_by = '11111111-1111-1111-1111-111111111111' WHERE created_by IS NULL;
  UPDATE public.tools             SET created_by = '11111111-1111-1111-1111-111111111111' WHERE created_by IS NULL;
  ALTER TABLE public.context_publishes ALTER COLUMN created_by SET NOT NULL;
  ALTER TABLE public.etl_rules         ALTER COLUMN created_by SET NOT NULL;
  ALTER TABLE public.mcp               ALTER COLUMN created_by SET NOT NULL;
  ALTER TABLE public.tools             ALTER COLUMN created_by SET NOT NULL;
" >/dev/null

# Insert NULL-org projects (matching prod state: empty, no commits)
run_psql -c "
  INSERT INTO public.projects (id, name, org_id, created_by, created_at, updated_at)
  VALUES
    ('019c46d0-86e2-7799-b05d-4325054d7378', 'Get Started', NULL, '11111111-1111-1111-1111-111111111111', now(), now()),
    ('019c46d7-85ed-7a1f-80c2-f8236dfa94d1', 'test',        NULL, '11111111-1111-1111-1111-111111111111', now(), now()),
    ('019c4bbf-523c-7e00-9a88-72a82a04b2ea', 'Get Started', NULL, '11111111-1111-1111-1111-111111111111', now(), now());
" >/dev/null

# Add some NULL-org tools attached to NULL projects
run_psql -c "
  INSERT INTO public.tools (id, name, type, project_id, created_by, json_path, created_at)
  VALUES
    ('019c9e5b-d02c-7faf-8e27-68c02099e6ad', 'orphan_tool_1', 'builtin', '019c46d7-85ed-7a1f-80c2-f8236dfa94d1', '11111111-1111-1111-1111-111111111111', '', now()),
    ('019c9e5b-d02c-7faf-8e27-68c02099e6ae', 'orphan_tool_2', 'builtin', '019c46d7-85ed-7a1f-80c2-f8236dfa94d1', '11111111-1111-1111-1111-111111111111', '', now());
" >/dev/null

# Drop the 10 FKs that are missing on prod
run_psql -c "
  ALTER TABLE public.access_logs           DROP CONSTRAINT IF EXISTS access_logs_agent_id_fkey;
  ALTER TABLE public.agent_execution_logs  DROP CONSTRAINT IF EXISTS agent_execution_log_agent_id_fkey;
  ALTER TABLE public.agent_logs            DROP CONSTRAINT IF EXISTS agent_logs_agent_id_fkey;
  ALTER TABLE public.access_tools          DROP CONSTRAINT IF EXISTS agent_tool_agent_id_fkey;
  ALTER TABLE public.chat_sessions         DROP CONSTRAINT IF EXISTS chat_sessions_agent_id_fkey;
  ALTER TABLE public.etl_rules             DROP CONSTRAINT IF EXISTS etl_rule_org_id_fkey;
  ALTER TABLE public.organizations         DROP CONSTRAINT IF EXISTS organizations_created_by_fkey;
  ALTER TABLE public.projects              DROP CONSTRAINT IF EXISTS project_org_id_fkey;
  ALTER TABLE public.tools                 DROP CONSTRAINT IF EXISTS tool_org_id_fkey;
  ALTER TABLE public.uploads               DROP CONSTRAINT IF EXISTS uploads_user_id_fkey;
" >/dev/null

# Rename PK/FKs to stale connections_* names
run_psql -c "
  ALTER TABLE public.access_points RENAME CONSTRAINT syncs_pkey TO connections_pkey;
  ALTER TABLE public.access_points RENAME CONSTRAINT syncs_project_id_fkey TO connections_project_id_fkey;
  ALTER TABLE public.access_points RENAME CONSTRAINT syncs_user_id_fkey TO connections_user_id_fkey;
" >/dev/null

verify_schema "scenario2_after_drift_inject" || true  # expected to fail

log "Now applying alignment migration..."
if PGPASSWORD= "${PG_BIN}/psql" -h /tmp -p "${PORT}" -U postgres -d postgres \
     --no-psqlrc --set ON_ERROR_STOP=on -f "${REPO_ROOT}/supabase/archive/before_b1/migrations/20260418040000_align_legacy_drift.sql" \
     2>&1 | tee -a "${LOG_DIR}/apply.log" | grep -E '(NOTICE|ERROR|FATAL)' | head -30; then
  echo "(alignment migration completed)"
fi

log "Now applying soften FK migration..."
if PGPASSWORD= "${PG_BIN}/psql" -h /tmp -p "${PORT}" -U postgres -d postgres \
     --no-psqlrc --set ON_ERROR_STOP=on -f "${REPO_ROOT}/supabase/archive/before_b1/migrations/20260418050000_soften_access_points_user_fk.sql" \
     2>&1 | tee -a "${LOG_DIR}/apply.log" | grep -E '(NOTICE|ERROR|FATAL)' | head -10; then
  echo "(soften FK migration completed)"
fi

verify_schema "scenario2_after_both_migrations"

# ============================================================================
# SCENARIO 3: Idempotent re-run (apply both again, should be no-op)
# ============================================================================
hdr "SCENARIO 3: Re-apply both migrations (idempotency check)"
log "Applying alignment migration AGAIN..."
PGPASSWORD= "${PG_BIN}/psql" -h /tmp -p "${PORT}" -U postgres -d postgres \
  --no-psqlrc --set ON_ERROR_STOP=on -q -f "${REPO_ROOT}/supabase/archive/before_b1/migrations/20260418040000_align_legacy_drift.sql" \
  >> "${LOG_DIR}/apply.log" 2>&1
log "Applying soften FK migration AGAIN..."
PGPASSWORD= "${PG_BIN}/psql" -h /tmp -p "${PORT}" -U postgres -d postgres \
  --no-psqlrc --set ON_ERROR_STOP=on -q -f "${REPO_ROOT}/supabase/archive/before_b1/migrations/20260418050000_soften_access_points_user_fk.sql" \
  >> "${LOG_DIR}/apply.log" 2>&1
verify_schema "scenario3_idempotent_rerun"

ok "All scenarios passed!"
echo "Logs: ${LOG_DIR}/apply.log"
