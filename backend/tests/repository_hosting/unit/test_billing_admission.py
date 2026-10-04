"""Logical billing capabilities fail closed, and replay performs no new work."""
import uuid
from types import SimpleNamespace

import pytest

from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.models import ProjectRootTarget, ResolvedRepositoryView
from src.version_engine.infrastructure.supabase.billing_repository import RepositoryBilling
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, RefTransactionService

pytestmark = pytest.mark.hosting_component


def reader():
    target = ProjectRootTarget('project')
    return RuntimeGrant(RuntimePrincipal('credential', 'git_http_token'), target,
                        ResolvedRepositoryView(target, '', (), 'rw'), RuntimeMode.READ)


@pytest.mark.parametrize('invalid', [None, {}, {'project_id': 'foreign'}, {'metric': 'body_bytes'},
                                  {'source_revision': True}, {'version': 0}, {'storage_limit': -1}])
def test_logical_billing_rejects_missing_or_invalid_contract(invalid):
    good = dict(project_id='project', org_id='org', metric='storage.logical_bytes', value=0,
                version=1, source_revision=1, storage_limit=None)
    response = good | invalid if isinstance(invalid, dict) and invalid else invalid
    control = SimpleNamespace(apply_billed=lambda *args: None, call=lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError, match='invalid repository billing contract'):
        RepositoryBilling(control).check('project')


def test_logical_billing_requires_admitted_control_and_matching_capacity():
    with pytest.raises(ValueError, match='admitted billing control required'):
        RepositoryBilling(SimpleNamespace())
    control = SimpleNamespace(apply_billed=lambda *args: None)
    with pytest.raises(ValueError, match='retained capacity admission'):
        RefTransactionService(control, SimpleNamespace(publication_project_id='project'), project_id='project',
                              billing=RepositoryBilling(control))


def test_logical_billing_original_replay_does_not_measure_reserve_or_prepare():
    calls = []
    original = {'status': 'committed'}

    def apply(*args, usage):
        calls.append(usage)
        return original

    control = SimpleNamespace(apply_billed=apply, result=lambda *args: original)
    service = RefTransactionService(control, SimpleNamespace(publication_project_id='project'), project_id='project',
                                    capacity=SimpleNamespace(control=control), billing=RepositoryBilling(control))
    result = service.submit(reader(), request_key=str(uuid.uuid4()), generation=1,
                            edits=[RefEdit(b'HEAD', RefState(target=b'refs/heads/main'),
                                           RefState(target=b'refs/heads/topic'))], roots={},
                            prepare=lambda: pytest.fail('replay prepared objects'))
    assert result is original and calls == [None]


def test_logical_billing_unchanged_default_has_no_object_io():
    released = []

    def begin(project, actor, pin):
        return dict(project_id=project, pin_id=pin, authority='native', object_format='sha1', generation=1,
                    ref_sequence=1, refs=[{'name_b64': 'SEVBRA==', 'state': RefState(target=b'refs/heads/main').wire('sha1')},
                                         {'name_b64': 'cmVmcy9oZWFkcy9tYWlu', 'state': RefState(oid='a'*40).wire('sha1')}])

    control = SimpleNamespace(apply_billed=lambda *args: None, begin_read=begin,
                              release=lambda *args: released.append(args))
    service = RefTransactionService(control, SimpleNamespace(publication_project_id='project'), project_id='project',
                                    capacity=SimpleNamespace(control=control), billing=RepositoryBilling(control))
    result = service.billing.measure(service, reader(), [RefEdit(b'refs/tags/tag', RefState(), RefState(oid='b'*40))],
                                     {'org_id': 'org', 'source_revision': 1})
    assert result == {'org_id': 'org', 'source_revision': 1, 'old_head_oid': 'a'*40, 'new_head_oid': 'a'*40}
    assert len(released) == 1
