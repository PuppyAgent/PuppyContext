import uuid
from types import SimpleNamespace

import pytest

from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.models import ProjectRootTarget, ResolvedRepositoryView
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    AdmittedRefAuthorityRepository,
)
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, RefTransactionService

pytestmark = pytest.mark.hosting_component


def test_missing_guard_rpc_never_falls_back_to_unguarded_publication():
    calls = []
    def rpc(name, _parameters):
        calls.append(name)
        raise RuntimeError('RPC unavailable')
    control = AdmittedRefAuthorityRepository(SimpleNamespace(rpc=rpc), lease_provider=lambda _: None)
    with pytest.raises(RuntimeError, match='RPC unavailable'):
        control.apply('p', 'user:u', 'key', 1, [], None, '')
    assert calls == ['apply_admitted_version_ref_transaction']
    calls.clear()
    with pytest.raises(RuntimeError, match='RPC unavailable'):
        control.begin('p', 'user:u', 'pin', 1, {})
    assert calls == ['begin_admitted_version_object_publication']
    calls.clear()
    with pytest.raises(RuntimeError, match='RPC unavailable'):
        control.read_snapshot('p', 'user:u')
    assert calls == ['get_admitted_version_repository_snapshot']
    calls.clear()
    with pytest.raises(RuntimeError, match='RPC unavailable'):
        control.renew('p', 'user:u', 'pin')
    assert calls == ['renew_admitted_version_object_pin']


def test_current_read_grant_can_replay_but_cannot_start_publication():
    target = ProjectRootTarget('p')
    grant = RuntimeGrant(RuntimePrincipal('credential', 'git_http_token'), target,
                         ResolvedRepositoryView(target, '', (), 'rw'), RuntimeMode.READ)
    result = {'status': 'committed'}
    control = SimpleNamespace(result=lambda *_args: result, apply=lambda *_args: result)
    service = RefTransactionService(control, SimpleNamespace(), project_id='p')
    args = dict(request_key=str(uuid.uuid4()), generation=1,
                edits=[RefEdit(b'HEAD', RefState(target=b'refs/heads/main'), RefState(target=b'refs/heads/topic'))],
                roots={}, prepare=lambda: pytest.fail('read-only replay prepared objects'))
    assert service.submit(grant, **args) is result
    control.result = lambda *_args: None
    with pytest.raises(PermissionError, match='read-only'):
        service.submit(grant, **args)


def test_foreign_live_lease_is_rejected_before_rpc():
    lease = SimpleNamespace(is_active=True, project_id='other')
    def forbidden(*_args):
        pytest.fail('foreign lease reached RPC')
    control = AdmittedRefAuthorityRepository(SimpleNamespace(rpc=forbidden), lease_provider=lambda _: lease)
    with pytest.raises(ValueError, match='binding mismatch'):
        control.apply('p', 'user:u', 'key', 1, [], None, '')


def test_released_context_cannot_supply_a_live_write_lease():
    calls = []
    lease = SimpleNamespace(is_active=False, project_id='p', lease_id='stale', holder_id='stale')
    def rpc(name, parameters):
        calls.append((name, parameters))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data={'status': 'committed'}))
    control = AdmittedRefAuthorityRepository(SimpleNamespace(rpc=rpc), lease_provider=lambda _: lease)
    control.apply('p', 'user:u', 'key', 1, [], None, '')
    assert calls[0][0] == 'apply_admitted_version_ref_transaction'
    # SQL, not this context, decides whether a stored result can be replayed.
    assert calls[0][1]['p_lease_id'] is None and calls[0][1]['p_holder_id'] is None
