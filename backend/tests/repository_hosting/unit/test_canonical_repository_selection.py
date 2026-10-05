"""Canonical manager selection follows PG, never a cached legacy root or caller flag."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.models import (
    ProjectRootTarget,
    ResolvedRepositoryView,
    ScopeTarget,
)
from src.version_engine.derived.object_gc import collect_object_gc_roots
from src.version_engine.entrypoints.git.native import select_native_git_endpoint
from src.version_engine.infrastructure.supabase.gc_repository import (
    ObjectGcInventory,
    ObjectGcRepository,
)
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    AdmittedRefAuthorityRepository,
)
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager

pytestmark = pytest.mark.hosting_component


def manager_with(snapshot):
    client = Mock()
    client.rpc.return_value.execute.return_value.data = snapshot
    db = SimpleNamespace(client=client)
    return VersionRepoManager(Mock(), db), client


def native(format='sha1'):
    return {'project_id': 'p', 'authority': 'native', 'object_format': format,
            'generation': 1, 'write_state': 'active'}


@pytest.mark.parametrize('format', ['sha1', 'sha256'])
def test_canonical_native_selection_requires_all_checked_admission(format):
    manager, client = manager_with(native(format))
    service = manager.get_native_service('p')
    assert service.project_id == 'p' and service.object_format == format
    assert isinstance(service.control, AdmittedRefAuthorityRepository)
    assert service.control.client is client
    assert service.capacity.control is service.control
    assert service.billing.control is service.control
    assert service.policy.control is service.control
    assert service.backend.publication_project_id == 'p'
    assert not manager._cache
    client.rpc.assert_called_once_with('get_version_repository_snapshot', {'p_project_id': 'p'})


@pytest.mark.parametrize('snapshot', [None, {**native(), 'authority': 'shadow'}])
def test_canonical_legacy_selection_is_explicit(snapshot):
    manager, _ = manager_with(snapshot)
    assert manager.get_native_service('p') is None


@pytest.mark.parametrize('snapshot', [False, {}, {**native(), 'project_id': 'foreign'},
                                      {**native(), 'authority': 'unknown'},
                                      {**native(), 'object_format': 'unknown'}])
@pytest.mark.parametrize('factory', ['get_native_service', 'get_gc_repo'])
def test_canonical_selection_invalid_metadata_cannot_downgrade(snapshot, factory):
    manager, _ = manager_with(snapshot)
    with pytest.raises(RuntimeError):
        getattr(manager, factory)('p')


@pytest.mark.parametrize('factory', ['get_native_service', 'get_gc_repo'])
def test_canonical_selection_rpc_outage_cannot_downgrade(factory):
    manager, client = manager_with(None)
    client.rpc.side_effect = RuntimeError('metadata unavailable')
    with pytest.raises(RuntimeError, match='metadata unavailable'):
        getattr(manager, factory)('p')


def runtime_grant(project='p', *, scope=False, mode=RuntimeMode.READ_WRITE):
    target = ScopeTarget(project, 'scope') if scope else ProjectRootTarget(project)
    return RuntimeGrant(RuntimePrincipal('credential', 'git_http_token'), target,
                        ResolvedRepositoryView(target, 'docs' if scope else '', (), 'rw'), mode)


@pytest.mark.parametrize('grant', [None, runtime_grant('foreign'), runtime_grant(scope=True)])
def test_native_dispatch_rejects_missing_foreign_or_scope_grant(grant):
    manager, _ = manager_with(native())
    with pytest.raises(PermissionError):
        select_native_git_endpoint(manager, 'p', {'_runtime_grant': grant})


def test_native_dispatch_legacy_locator_cannot_become_full_repository():
    manager, _ = manager_with(native())
    with pytest.raises(HTTPException) as denied:
        select_native_git_endpoint(manager, 'p', {'_runtime_grant': runtime_grant()}, legacy_route=True)
    assert denied.value.status_code == 503


def test_native_dispatch_uses_authenticated_principal_not_display_actor():
    manager, _ = manager_with(native())
    grant = runtime_grant(mode=RuntimeMode.READ)
    endpoint = select_native_git_endpoint(manager, 'p', {
        '_runtime_grant': grant, 'agent': 'user:spoof', '_credential_id': 'spoof',
    })
    assert endpoint.actor == 'runtime:credential' and endpoint.grant is grant


@pytest.mark.parametrize('snapshot', [None, native('sha1'), native('sha256')])
def test_gc_inventory_factory_is_not_a_current_tree_or_write_facade(snapshot):
    manager, _ = manager_with(snapshot)
    repo = manager.get_gc_repo('p')
    assert repo._project_id == 'p'
    assert repo.store._backend.publication_project_id == 'p'
    assert repo.store.object_format == (snapshot['object_format'] if snapshot else 'sha1')
    assert not manager._cache
    for name in ('get_root_hash', 'get_head_commit_id', 'get_all_scope_hashes',
                 'publish_version', 'cas_update_root_hash'):
        assert not hasattr(repo, name) and not hasattr(repo.history, name)
    assert callable(repo.history.list_object_gc_roots)
    assert callable(repo.history.native_ref_authority)


def test_gc_inventory_preserves_legacy_retention_sources_without_current_tree_facade():
    history = SimpleNamespace(
        list_object_gc_roots=lambda: ['1'*40, '2'*40],
        list_version_index_roots=lambda: [{'source_commit_id': '3'*40}],
        list_pending_outbox_roots=lambda: [{'commit_id': '4'*40}],
        list_pending_conflict_roots=lambda: [{'proposed_tree_hash': '5'*40}],
        list_version_ref_roots=lambda: ['6'*40],
        list_shadow_snapshot_roots=lambda: ['7'*40],
    )
    repo = ObjectGcRepository('p', Mock(), ObjectGcInventory(history))
    errors = []
    assert collect_object_gc_roots(repo, errors=errors) == {str(i)*40 for i in range(1, 8)}
    assert errors == []


def test_gc_missing_scope_inventory_is_not_treated_as_empty():
    repo = SimpleNamespace(history=SimpleNamespace())
    errors = []
    assert collect_object_gc_roots(repo, errors=errors) == set()
    assert any('Scope retention inventory unavailable' in error for error in errors)


def test_legacy_repo_cache_cannot_outlive_native_authority():
    manager, client = manager_with(None)
    cached = object()
    manager._cache['p'] = cached
    assert manager.get_repo('p') is cached
    client.rpc.return_value.execute.return_value.data = native()
    with pytest.raises(RuntimeError, match='native repository requires authority-aware access'):
        manager.get_repo('p')
    client.rpc.side_effect = RuntimeError('metadata unavailable')
    with pytest.raises(RuntimeError, match='metadata unavailable'):
        manager.get_repo('p')
