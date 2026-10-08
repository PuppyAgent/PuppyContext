"""Existing single-file rename/copy semantics on typed DAGs, not expanded paths."""
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.platform.billing.storage import oversized_new_logical_file
from src.version_engine.admission.file_policy import (
    newly_oversized_logical_files,
    oversized_blob_occurrences,
)
from src.version_engine.infrastructure.supabase.billing_repository import RepositoryBilling
from src.version_engine.infrastructure.supabase.file_policy_repository import RepositoryFilePolicy
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import (
    MODE_DIR,
    MODE_FILE,
    MODE_GITLINK,
    MODE_SYMLINK,
    TreeEntry,
)
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, RefTransactionService
from src.version_engine.write_engine.tree import write_tree
from tests.repository_hosting.unit.test_billing_admission import reader

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize('fmt', ['sha1', 'sha256'])
def test_native_file_policy_matches_existing_rename_and_copy(tmp_path, fmt):
    store = ObjectStore(tmp_path, object_format=fmt)
    blob = store.put_blob(b'oversized-body')
    old = write_tree(store, [TreeEntry('a', MODE_FILE, blob), TreeEntry('b', MODE_SYMLINK, blob)])
    moved = write_tree(store, [TreeEntry('renamed', MODE_FILE, blob), TreeEntry('b', MODE_SYMLINK, blob),
                               TreeEntry('external', MODE_GITLINK, blob)])
    copied = write_tree(store, [TreeEntry('a', MODE_FILE, blob), TreeEntry('b', MODE_SYMLINK, blob),
                                TreeEntry('copy', MODE_FILE, blob)])
    verifier = ClosureVerifier(store._backend, object_format=fmt)
    proof = verifier.verify({old: 'tree', moved: 'tree', copied: 'tree'})
    assert not newly_oversized_logical_files(proof, old, proof, moved, 8)
    assert oversized_new_logical_file(store, old, moved, 8) is None
    assert newly_oversized_logical_files(proof, old, proof, copied, 8) == {blob: 1}
    assert oversized_new_logical_file(store, old, copied, 8) == ('copy', len(b'oversized-body'))


@pytest.mark.parametrize('depth', [52, 63])
def test_native_file_policy_shared_dag_has_bounded_work(tmp_path, depth):
    store = ObjectStore(tmp_path)
    blob = store.put_blob(b'oversized')
    tree = write_tree(store, [TreeEntry('file', MODE_FILE, blob)])
    for _ in range(depth):
        tree = write_tree(store, [TreeEntry('a', MODE_DIR, tree), TreeEntry('b', MODE_DIR, tree)])
    proof = ClosureVerifier(store._backend).verify({tree: 'tree'})
    if depth == 63:
        with pytest.raises(OverflowError):
            oversized_blob_occurrences(proof, tree, 1)
    else:
        assert oversized_blob_occurrences(proof, tree, 1) == {blob: 2**depth}


def test_native_file_policy_does_not_expand_in_limit_branches(tmp_path):
    store = ObjectStore(tmp_path)
    zero, large = store.put_blob(b''), store.put_blob(b'large')
    tree = write_tree(store, [TreeEntry('zero', MODE_FILE, zero)])
    for _ in range(100):
        tree = write_tree(store, [TreeEntry('a', MODE_DIR, tree), TreeEntry('b', MODE_DIR, tree)])
    root = write_tree(store, [TreeEntry('tree', MODE_DIR, tree), TreeEntry('large', MODE_FILE, large)])
    proof = ClosureVerifier(store._backend).verify({root: 'tree'})
    assert oversized_blob_occurrences(proof, root, 1) == {large: 1}
    broken = replace(proof, objects={root: proof.objects[root]})
    with pytest.raises(ValueError, match='complete typed tree'):
        oversized_blob_occurrences(broken, root, 1)
    cyclic = replace(proof, objects={root: replace(proof.objects[root], edges=((root, 'tree'),))})
    with pytest.raises(ValueError, match='cycle'):
        oversized_blob_occurrences(cyclic, root, 1)


@pytest.mark.parametrize('invalid', [None, {}, {'project_id': 'foreign'}, {'org_id': ''},
                                    {'source_revision': True}, {'source_revision': 0},
                                    {'file_limit': True}, {'file_limit': -1}, {'file_limit': 2**63}])
def test_native_file_policy_rejects_invalid_contract(invalid):
    good = dict(project_id='project', org_id='org', source_revision=1, file_limit=None)
    response = good | invalid if isinstance(invalid, dict) and invalid else invalid
    control = SimpleNamespace(apply_policy=lambda *args: None, call=lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError, match='invalid repository file policy contract'):
        RepositoryFilePolicy(control).check('project')


def test_native_file_policy_requires_complete_admission_and_replay_is_read():
    with pytest.raises(ValueError, match='checked publication'):
        RepositoryFilePolicy(SimpleNamespace())
    calls, original = [], {'status': 'committed'}
    def apply(*args, **kwargs):
        calls.append(kwargs)
        return original
    control = SimpleNamespace(apply_policy=apply, apply_billed=lambda *args: pytest.fail('unmetered policy'),
                              result=lambda *args: original, publication_context=lambda *args: {"result": original})
    policy = RepositoryFilePolicy(control)
    with pytest.raises(ValueError, match='matching control and billing'):
        RefTransactionService(control, SimpleNamespace(), project_id='project', policy=policy)
    service = RefTransactionService(control, SimpleNamespace(), project_id='project',
                                    capacity=SimpleNamespace(control=control), billing=RepositoryBilling(control), policy=policy)
    assert service.submit(reader(), request_key=str(uuid.uuid4()), generation=1,
                          edits=[RefEdit(b'HEAD', RefState(target=b'refs/heads/main'), RefState(target=b'refs/heads/topic'))],
                          roots={}, prepare=lambda: pytest.fail('replay prepared objects')) is original
    assert calls == [{'usage': None, 'policy': None}]


def test_native_file_policy_billing_fallback_measures_current_tree_not_history(tmp_path):
    store = ObjectStore(tmp_path)
    blob = store.put_blob(b'abc')
    tree = write_tree(store, [TreeEntry('file', MODE_FILE, blob)])
    commit = store.put_commit(f'tree {tree}\nparent {"f"*40}\nauthor Test <test@example.test> 0 +0000\n'
                              'committer Test <test@example.test> 0 +0000\n\nTest\n'.encode())
    released = []
    def begin(project, actor, pin):
        return dict(project_id=project, pin_id=pin, authority='native', object_format='sha1', generation=1,
                    ref_sequence=1, refs=[{'name_b64': 'SEVBRA==', 'state': RefState(target=b'refs/heads/main').wire('sha1')},
                                         {'name_b64': 'cmVmcy9oZWFkcy9tYWlu', 'state': RefState(oid=commit).wire('sha1')}])
    control = SimpleNamespace(apply_billed=lambda *args: None, begin_read=begin, release=lambda *args: released.append(args))
    service = RefTransactionService(control, store._backend, project_id='project',
                                    capacity=SimpleNamespace(control=control), billing=RepositoryBilling(control))
    measured = service.billing.measure(service, reader(), [RefEdit(b'refs/heads/main', RefState(oid=commit), RefState())],
                                       {'org_id': 'org', 'source_revision': 1})
    assert measured['old_bytes'] == 3 and measured['new_bytes'] == 0
    assert len(released) == 1


def test_native_file_policy_check_only_head_guard_is_not_deletion():
    released = []
    def begin(project, actor, pin):
        return dict(project_id=project, pin_id=pin, authority='native', object_format='sha1', generation=1,
                    ref_sequence=1, refs=[{'name_b64': 'SEVBRA==', 'state': RefState(target=b'refs/heads/main').wire('sha1')},
                                         {'name_b64': 'cmVmcy9oZWFkcy9tYWlu', 'state': RefState(oid='a'*40).wire('sha1')}])
    control = SimpleNamespace(apply_policy=lambda *args: None, apply_billed=lambda *args: None,
                              begin_read=begin, release=lambda *args: released.append(args))
    service = RefTransactionService(control, SimpleNamespace(publication_project_id='project'), project_id='project',
                                    capacity=SimpleNamespace(control=control), billing=RepositoryBilling(control),
                                    policy=RepositoryFilePolicy(control))
    proof = service.policy.verify(service, reader(), [RefEdit(b'HEAD', RefState(target=b'refs/heads/main'))],
                                   {}, {'org_id': 'org', 'source_revision': 1, 'file_limit': 8})
    assert proof['old_head_oid'] == proof['new_head_oid'] == 'a'*40
    assert len(released) == 1
