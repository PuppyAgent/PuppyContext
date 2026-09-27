#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
history_tmp="$(mktemp -d "${TMPDIR:-/tmp}/puppy-history.XXXXXX")"
history_workdir="$history_tmp/project"
python3 "$repository_root/scripts/database_history.py" stage --output "$history_workdir"
contract_rel="supabase/migrations/20260715000000_project_owned_repository_targets_contract_cutover.sql"
contract_path="$history_workdir/$contract_rel"
saved_contract="$(mktemp "${TMPDIR:-/tmp}/issue039-contract.XXXXXX.sql")"
removal_rel="supabase/migrations/20260716000000_remove_workspace_binding.sql"
removal_path="$history_workdir/$removal_rel"
saved_removal="$(mktemp "${TMPDIR:-/tmp}/issue039-removal.XXXXXX.sql")"
initialization_rel="supabase/migrations/20260716010000_project_initialization_control_plane.sql"
initialization_path="$history_workdir/$initialization_rel"
saved_initialization="$(mktemp "${TMPDIR:-/tmp}/issue039-initialization.XXXXXX.sql")"
closure_rel="supabase/migrations/20260716020000_project_deletion_storage_and_org_guard.sql"
closure_path="$history_workdir/$closure_rel"
saved_closure="$(mktemp "${TMPDIR:-/tmp}/issue039-deletion-closure.XXXXXX.sql")"
fence_rel="supabase/migrations/20260717000000_project_deletion_admission_fence.sql"
fence_path="$history_workdir/$fence_rel"
saved_fence="$(mktemp "${TMPDIR:-/tmp}/issue039-deletion-fence.XXXXXX.sql")"
inventory_repair_rel="supabase/migrations/20260718000000_repair_project_storage_inventory_control_plane.sql"
inventory_repair_path="$history_workdir/$inventory_repair_rel"
saved_inventory_repair="$(mktemp "${TMPDIR:-/tmp}/issue039-inventory-repair.XXXXXX.sql")"
inventory_status_rel="supabase/migrations/20260720000000_project_storage_inventory_status_rpc.sql"
inventory_status_path="$history_workdir/$inventory_status_rel"
saved_inventory_status="$(mktemp "${TMPDIR:-/tmp}/issue039-inventory-status.XXXXXX.sql")"
database_url="${DATABASE_URL:-postgresql://postgres:postgres@127.0.0.1:54322/postgres}"
export DATA_MIGRATION_DATABASE_URL="${DATA_MIGRATION_DATABASE_URL:-$database_url}"

contract_is_saved=false
removal_is_saved=false
initialization_is_saved=false
closure_is_saved=false
fence_is_saved=false
inventory_repair_is_saved=false
inventory_status_is_saved=false
restore_contract() {
    if [[ "$contract_is_saved" == true && -f "$saved_contract" ]]; then
        mv "$saved_contract" "$contract_path"
        contract_is_saved=false
    fi
}
restore_removal() {
    if [[ "$removal_is_saved" == true && -f "$saved_removal" ]]; then
        mv "$saved_removal" "$removal_path"
        removal_is_saved=false
    fi
}
restore_initialization() {
    if [[ "$initialization_is_saved" == true && -f "$saved_initialization" ]]; then
        mv "$saved_initialization" "$initialization_path"
        initialization_is_saved=false
    fi
}
restore_closure() {
    if [[ "$closure_is_saved" == true && -f "$saved_closure" ]]; then
        mv "$saved_closure" "$closure_path"
        closure_is_saved=false
    fi
}
restore_fence() {
    if [[ "$fence_is_saved" == true && -f "$saved_fence" ]]; then
        mv "$saved_fence" "$fence_path"
        fence_is_saved=false
    fi
}
restore_inventory_repair() {
    if [[ "$inventory_repair_is_saved" == true && -f "$saved_inventory_repair" ]]; then
        mv "$saved_inventory_repair" "$inventory_repair_path"
        inventory_repair_is_saved=false
    fi
}
restore_inventory_status() {
    if [[ "$inventory_status_is_saved" == true && -f "$saved_inventory_status" ]]; then
        mv "$saved_inventory_status" "$inventory_status_path"
        inventory_status_is_saved=false
    fi
}
cleanup() {
    restore_contract
    restore_removal
    restore_initialization
    restore_closure
    restore_fence
    restore_inventory_repair
    restore_inventory_status
    rm -rf "$history_tmp"
    rm -f "$saved_contract" "$saved_removal" "$saved_initialization" "$saved_closure" "$saved_fence" \
        "$saved_inventory_repair" "$saved_inventory_status"
}
trap cleanup EXIT

save_contract() {
    mv "$contract_path" "$saved_contract"
    contract_is_saved=true
}
save_removal() {
    mv "$removal_path" "$saved_removal"
    removal_is_saved=true
}
save_initialization() {
    mv "$initialization_path" "$saved_initialization"
    initialization_is_saved=true
}
save_closure() {
    mv "$closure_path" "$saved_closure"
    closure_is_saved=true
}
save_fence() {
    mv "$fence_path" "$saved_fence"
    fence_is_saved=true
}
save_inventory_repair() {
    mv "$inventory_repair_path" "$saved_inventory_repair"
    inventory_repair_is_saved=true
}
save_inventory_status() {
    mv "$inventory_status_path" "$saved_inventory_status"
    inventory_status_is_saved=true
}

run_data_migration() {
    local migration_id="$1"
    (
        cd "$repository_root/backend"
        uv run puppyone-db run "$migration_id"
    )
}

save_contract
save_removal
save_initialization
save_closure
save_fence
save_inventory_repair
save_inventory_status
(
    cd "$repository_root"
    supabase db reset --local --no-seed --workdir "$history_workdir"
    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -f supabase/test_fixtures/repository_target_legacy_upgrade.sql
)

run_data_migration 20260712_repo_user_permissions_to_project_members
run_data_migration 20260712_repo_user_permissions_to_project_members
run_data_migration 20260715_project_owned_repository_targets_preflight
run_data_migration 20260715_project_owned_repository_targets_preflight

restore_contract
(
    cd "$repository_root"
    supabase migration up --local --include-all --workdir "$history_workdir"

    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -c "INSERT INTO public.repository_scopes (id, project_id, name, path, exclude, max_mode) VALUES ('issue039-concurrent-scope', 'issue039-project', 'Concurrent Scope', 'concurrent/scope', '[]', 'rw')"

    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -c "SELECT count(*) FROM public.ensure_repository_target_access_surfaces('issue039-project', 'issue039-concurrent-scope', '00000000-0000-0000-0000-000000039001'::uuid, NULL, NULL)" &
    first_pid=$!
    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -c "SELECT count(*) FROM public.ensure_repository_target_access_surfaces('issue039-project', 'issue039-concurrent-scope', '00000000-0000-0000-0000-000000039001'::uuid, NULL, NULL)" &
    second_pid=$!
    wait "$first_pid"
    wait "$second_pid"

    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -c "DO \$\$ BEGIN IF (SELECT count(*) FROM public.access_surfaces WHERE project_id = 'issue039-project' AND scope_id = 'issue039-concurrent-scope' AND kind IN ('git_remote', 'cli')) <> 2 THEN RAISE EXCEPTION 'concurrent enable did not converge'; END IF; END \$\$"
    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -f supabase/test_fixtures/repository_target_upgrade_assert.sql
)

# The previous assertion proves the Issue 039 geometry cutover on its own.
# Apply the final architecture migration separately and prove that no checkout
# registration entity or credential foreign key survives.
restore_removal
restore_initialization
restore_closure
restore_fence
restore_inventory_repair
restore_inventory_status
(
    cd "$repository_root"
    supabase migration up --local --include-all --workdir "$history_workdir"
    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -f supabase/test_fixtures/workspace_binding_removal_assert.sql
)

# A non-empty installation cannot bypass the immutable data-preflight receipt.
save_contract
save_removal
save_initialization
save_closure
save_fence
save_inventory_repair
save_inventory_status
(
    cd "$repository_root"
    supabase db reset --local --no-seed --workdir "$history_workdir"
    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -f supabase/test_fixtures/repository_target_legacy_upgrade.sql
)
restore_contract
if (
    cd "$repository_root"
    supabase migration up --local --include-all --workdir "$history_workdir"
); then
    echo "expected contract migration to reject a missing preflight receipt" >&2
    exit 1
fi
(
    cd "$repository_root"
    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -c "DO \$\$ BEGIN IF to_regclass('public.repo_scopes') IS NULL OR to_regclass('public.repository_scopes') IS NOT NULL THEN RAISE EXCEPTION 'receipt gate failure mutated the schema'; END IF; END \$\$"
)
restore_removal
restore_initialization
restore_closure
restore_fence
restore_inventory_repair
restore_inventory_status

save_contract
save_removal
save_initialization
save_closure
save_fence
save_inventory_repair
save_inventory_status
(
    cd "$repository_root"
    supabase db reset --local --no-seed --workdir "$history_workdir"
    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -f supabase/test_fixtures/repository_target_corrupt_missing_root.sql
)

if run_data_migration 20260715_project_owned_repository_targets_preflight; then
    echo "expected repository target preflight to reject a missing root" >&2
    exit 1
fi

(
    cd "$repository_root"
    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -c "DO \$\$ BEGIN IF to_regclass('public.repo_scopes') IS NULL THEN RAISE EXCEPTION 'failed preflight mutated the schema'; END IF; IF EXISTS (SELECT 1 FROM public.migration_log WHERE name = '20260715_project_owned_repository_targets_preflight') THEN RAISE EXCEPTION 'failed preflight wrote a receipt'; END IF; END \$\$"
)

# The released repair restores the missing root, never touches healthy
# Projects, is idempotent, and unblocks the previously rejected preflight.
run_data_migration 20260717_repair_missing_root_scopes
run_data_migration 20260717_repair_missing_root_scopes
(
    cd "$repository_root"
    psql "$database_url" -X -v ON_ERROR_STOP=1 \
        -f supabase/test_fixtures/root_scope_repair_assert.sql
)
run_data_migration 20260715_project_owned_repository_targets_preflight

restore_contract
restore_removal
restore_initialization
restore_closure
restore_fence
restore_inventory_repair
restore_inventory_status
