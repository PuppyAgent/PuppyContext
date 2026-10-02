import json
from pathlib import Path

import pytest

from src.exceptions import AppException, ErrorCode
from src.platform.repository_target.protocol import require_repository_target_contract
from src.platform.repository_target.schemas import (
    ProjectRootTargetSchema,
    ScopeTargetSchema,
)
from src.repo.scope_repository import _row_to_scope

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = (
    ROOT / "supabase/archive/before_b1/migrations/20260716000000_remove_workspace_binding.sql"
)
TARGET_CUTOVER = ROOT / (
    "supabase/archive/before_b1/migrations/20260715000000_project_owned_repository_targets_contract_cutover.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_repository_scope_rows_are_non_root_path_boundaries_only():
    scope = _row_to_scope(
        {
            "id": "scope-docs",
            "project_id": "project-1",
            "name": "Docs",
            "path": "docs",
            "exclude": ["docs/private"],
            "max_mode": "r",
        }
    )

    assert scope.path == "docs"
    assert scope.max_mode == "r"
    assert not hasattr(scope, "is_root")
    assert not hasattr(scope, "access_key")


def test_cross_client_repository_target_v2_fixture_matches_wire_models():
    fixture = json.loads((ROOT / "contracts/repository-target-v2.json").read_text(encoding="utf-8"))

    assert fixture["version"] == 2
    assert fixture["request_header"] == {
        "name": "X-PuppyOne-Repository-Contract",
        "value": "2",
    }
    assert fixture["errors"] == {
        "client_upgrade_required": ErrorCode.CLIENT_UPGRADE_REQUIRED.value,
        "target_kind_mismatch": ErrorCode.TARGET_KIND_MISMATCH.value,
        "scope_not_found": ErrorCode.SCOPE_NOT_FOUND.value,
        "repository_storage_unavailable": ErrorCode.REPOSITORY_STORAGE_UNAVAILABLE.value,
    }
    assert fixture["association_rows"] == {
        "project_root": {"project_id": "project-1", "scope_id": None},
        "scope": {"project_id": "project-1", "scope_id": "scope-docs"},
    }
    assert (
        ProjectRootTargetSchema.model_validate(fixture["targets"]["project_root"]).model_dump()
        == fixture["targets"]["project_root"]
    )
    assert (
        ScopeTargetSchema.model_validate(fixture["targets"]["scope"]).model_dump()
        == fixture["targets"]["scope"]
    )


@pytest.mark.parametrize("version", [None, 1, 3])
def test_repository_target_contract_rejects_missing_or_wrong_version(version):
    with pytest.raises(AppException) as caught:
        require_repository_target_contract(version)

    assert caught.value.status_code == 426
    assert caught.value.code is ErrorCode.CLIENT_UPGRADE_REQUIRED
    assert caught.value.details == {"required_repository_contract": 2}


def test_repository_target_contract_accepts_exact_v2():
    assert require_repository_target_contract(2) == 2


def test_cutover_makes_project_the_root_target_and_removes_dual_identity():
    sql = TARGET_CUTOVER.read_text(encoding="utf-8")

    assert "DELETE FROM public.repo_scopes WHERE is_root = true" in sql
    assert "ALTER TABLE public.repo_scopes RENAME TO repository_scopes" in sql
    assert "RENAME COLUMN mode TO max_mode" in sql
    assert "DROP COLUMN is_root" in sql
    assert "DROP COLUMN binding_kind" in sql
    assert "path <> ''" in sql
    assert "NULLS NOT DISTINCT" in sql


def test_cutover_preflight_blocks_ambiguous_or_dangling_legacy_geometry():
    sql = TARGET_CUTOVER.read_text(encoding="utf-8")

    assert "DATA_MIGRATION_REQUIRED:20260715_project_owned_repository_targets_preflight" in sql
    assert "summary ->> 'artifact_checksum'" in sql
    assert "Projects lack exactly one legacy root" in sql
    assert "malformed legacy Scope rows" in sql
    assert "invalid Access Surface targets" in sql
    assert "invalid Workspace Binding targets" in sql
    assert "invalid credential target chains" in sql


def test_runtime_resolver_is_hash_only_and_returns_explicit_target_facts():
    resolver = _sql().split("CREATE FUNCTION public.resolve_git_runtime_credential", 1)[1]

    assert "p_key_hash text" in resolver
    assert "raw_token" not in resolver
    assert "SECURITY DEFINER" in resolver
    assert "c.key_hash = p_key_hash" in resolver
    assert "c.credential_type = 'git_http_token'" in resolver
    assert "target_kind text" in resolver
    assert "path_prefix text" in resolver
    assert "target_max_mode text" in resolver
    assert "CASE WHEN s.scope_id IS NULL THEN 'project_root' ELSE 'scope' END" in resolver
    assert "s.scope_id IS NULL OR rs.id IS NOT NULL" in resolver
    assert "REVOKE ALL ON FUNCTION public.resolve_git_runtime_credential(text)" in resolver


def test_user_git_credential_uses_one_nullable_target_fk_without_checkout_identity():
    sql = _sql()
    creation = sql.split("CREATE FUNCTION public.issue_user_git_http_credential", 1)[1].split(
        "CREATE FUNCTION public.resolve_git_runtime_credential",
        1,
    )[0]

    assert "p_scope_id text" in creation
    assert "scope_id IS NOT DISTINCT FROM p_scope_id" in creation
    assert "p_scope_id IS NULL" in creation
    assert "'kind', 'project_root'" in creation
    assert "'kind', 'scope'" in creation
    assert "workspace_instance" not in creation
    assert "binding_id" not in creation
    assert "credential_lifecycle" in creation
    assert "'user'" in creation
    assert "ensure_repository_target_access_surfaces" in creation
    assert "revoke_user_git_http_credential" in creation


def test_explicit_target_enable_is_atomic_and_concurrency_safe():
    sql = TARGET_CUTOVER.read_text(encoding="utf-8")
    enable = sql.split(
        "CREATE OR REPLACE FUNCTION public.ensure_repository_target_access_surfaces",
        1,
    )[1].split(
        "CREATE OR REPLACE FUNCTION public.rotate_access_surface_git_http_token",
        1,
    )[0]

    assert "pg_advisory_xact_lock" in enable
    assert "p_scope_id IS NOT NULL" in enable
    assert "ON CONFLICT DO NOTHING" in enable
    assert "'git_remote'" in enable
    assert "'cli'" in enable
    assert "RETURNS SETOF public.access_surfaces" in enable


def test_shared_rotation_accepts_project_root_without_a_scope_join():
    sql = _sql()
    rotation = sql.split(
        "CREATE OR REPLACE FUNCTION public.rotate_access_surface_git_http_token", 1
    )[1].split("CREATE FUNCTION public.issue_user_git_http_credential", 1)[0]

    assert "s.scope_id IS NULL OR rs.id IS NOT NULL" in rotation
    assert "p_grant_mode = 'r' OR s.scope_id IS NULL OR rs.max_mode = 'rw'" in rotation
    assert "credential_lifecycle = 'shared'" in rotation


def test_application_issues_user_git_credentials_without_binding_rpcs():
    repository = (ROOT / "backend/src/platform/repository_context/repository.py").read_text(
        encoding="utf-8"
    )

    assert '"issue_user_git_http_credential_idempotent"' in repository
    assert "workspace_instance" not in repository
    assert "binding_id" not in repository


def test_runtime_has_no_local_checkout_registration_module_or_identity_fields():
    source_root = ROOT / "backend/src"
    assert not (source_root / "platform/workspace_binding").exists()
    assert not any("workspace_binding" in path.as_posix() for path in source_root.rglob("*"))

    identity_sources = [
        source_root / "main.py",
        source_root / "platform/authorization/manifest.py",
        source_root / "platform/authorization/models.py",
        source_root / "platform/repository_context/models.py",
        source_root / "platform/repository_context/repository.py",
        source_root / "platform/repository_context/router.py",
        source_root / "platform/repository_context/schemas.py",
        source_root / "platform/repository_context/service.py",
        source_root / "repo/access_credentials.py",
        source_root / "version_engine/entrypoints/git/auth.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in identity_sources)
    # External-source bindings are not local checkout registrations. Allow only
    # this exact canonical HTTP placeholder; keep rejecting binding_id in all
    # other identity sources and routes (including workspace/device records).
    combined = combined.replace(
        "/api/v1/synchronize/bindings/{synchronize_binding_id}",
        "/api/v1/synchronize/bindings/{id}",
    )
    for forbidden in (
        "WorkspaceBinding",
        "workspace_binding",
        "workspace_instance_id",
        "binding_id",
        "project_workspace_bindings",
    ):
        assert forbidden not in combined


def test_repository_context_accepts_only_normal_project_target_identity():
    schemas = (ROOT / "backend/src/platform/repository_context/schemas.py").read_text(
        encoding="utf-8"
    )
    router = (ROOT / "backend/src/platform/repository_context/router.py").read_text(
        encoding="utf-8"
    )

    context = schemas.split("class RepositoryProjectContextOut", 1)[1]
    assert "target: RepositoryTargetSchema" in context
    assert "remote_url" not in schemas
    assert "requires_confirmation" not in schemas
    assert "response_model=ApiResponse[RepositoryProjectContextOut]" in router
    assert '"/projects/{project_id}/repository-context"' in router
    assert "resolve-legacy-remote" not in router


def test_final_migration_physically_removes_workspace_binding_schema():
    sql = _sql()

    assert "DROP TABLE public.project_workspace_bindings CASCADE" in sql
    assert "DROP COLUMN workspace_binding_id" in sql
    assert "credential_type <> 'git_http_token'" in sql
    assert "status = CASE WHEN b.status = 'active' THEN c.status ELSE 'revoked' END" in sql
    assert "credential_lifecycle IN ('shared', 'session', 'user')" in sql
    assert "CREATE FUNCTION public.revoke_user_git_http_credential" in sql
    runtime = sql.split("CREATE FUNCTION public.resolve_git_runtime_credential", 1)[1].split(
        "REVOKE ALL ON FUNCTION public.rotate_access_surface_bearer_token", 1
    )[0]
    assert "user_id uuid" in runtime
    assert "workspace_binding_id" not in runtime


def test_canonical_and_legacy_routes_converge_after_target_resolution():
    router = (ROOT / "backend/src/version_engine/entrypoints/git/router.py").read_text(
        encoding="utf-8"
    )

    assert "class _ResolvedGitTarget" in router
    assert "_git_info_refs_for_target(" in router
    assert "_git_rebuild_for_target(" in router
    assert "_git_receive_pack_for_target(" in router
    assert "_git_upload_pack_for_target(" in router
    assert '@router.get("/ap/{access_key}.git/info/refs")' in router
    assert '@router.get("/{project_id}/scopes/{scope_id}.git/info/refs")' in router


def test_web_git_health_uses_human_control_plane_not_git_runtime_routes():
    api = (ROOT / "frontend/lib/gitHealthApi.ts").read_text(encoding="utf-8")

    assert "/api/v1/projects/${encodeURIComponent(projectId)}/git-view/health" in api
    assert "/api/v1/projects/${encodeURIComponent(projectId)}/git-view/rebuild-cache" in api
    assert "`/git/${encodeURIComponent(projectId)}" not in api
    assert "getGitScopeHealth" not in api
    assert "rebuildGitScopeCache" not in api


def test_railway_smoke_uses_human_git_control_plane_and_current_snapshot_cap():
    smoke = (ROOT / "backend/scripts/railway_smoke.py").read_text(encoding="utf-8")

    assert "/api/v1/projects/{self.test_project_id}/git-view/health" in smoke
    assert "/api/v1/projects/{self.test_project_id}/git-view/rebuild-cache" in smoke
    assert "X-PuppyOne-Repository-Contract" in smoke
    assert "range(100_001)" in smoke
    assert "cleanup must be accepted by the deployment" in smoke


def test_web_one_time_git_credential_is_bound_to_displayed_target_and_mode():
    panel = (
        ROOT / "frontend/features/files/components/access-points/"
        "connect-methods/GitCredentialIssuePanel.tsx"
    ).read_text(encoding="utf-8")

    assert "GIT_CREDENTIAL_PATTERN" in panel
    assert "crypto.getRandomValues" in panel
    assert "crypto.randomUUID()" in panel
    assert "'Idempotency-Key': intent.operationKey" in panel
    assert "/git-credentials`" in panel
    assert "/regenerate-key" not in panel
    assert "result.remote.username !== 'x-puppyone-token'" in panel
    assert "result.mode !== intent.mode" in panel
    assert "!sameRepositoryTarget(intent.target, result.remote.target)" in panel
    assert "!isSameCanonicalGitUrl(gitUrl, result.remote.url)" in panel


def test_legacy_access_router_cannot_issue_server_generated_human_git_secrets():
    router = (ROOT / "backend/src/platform/access/router.py").read_text(encoding="utf-8")
    git_branch = router.split('if provider == "git_remote":', 1)[1].split(
        'if provider == "cli":', 1
    )[0]

    assert "HTTP_410_GONE" in git_branch
    assert "/projects/{project_id}/git-credentials" in git_branch
    assert "issue_git_http_token" not in git_branch

    unified_create = router.split("# ── Unified Create", 1)[1]
    assert 'if provider == "direct":' in unified_create
    assert "HTTP_410_GONE" in unified_create
    assert "legacy_direct_access_removed" in unified_create
    assert "/projects/{project_id}/git-credentials" in unified_create
    assert "issue_git_http_token" not in unified_create
    assert '"provider": "direct"' not in router.split("# ── Unified Create", 1)[0]


@pytest.mark.parametrize(
    "forbidden",
    [
        "BindingKind",
        "RepoScopeRepository",
        "binding_kind",
        "root_scope_id",
        "is_root",
        '"_scope"',
    ],
)
def test_new_runtime_source_does_not_reintroduce_legacy_identity_types(forbidden):
    backend_source = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "backend/src").rglob("*.py")
    )
    frontend_source = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (ROOT / "frontend",)
        for path in root.rglob("*.ts*")
        if "node_modules" not in path.parts
    )
    assert forbidden not in backend_source
    assert forbidden not in frontend_source
