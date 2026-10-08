"""Current usage must not inherit the selected history conversion budget."""
import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    'current_tree_artifact', ROOT / 'supabase/data_migrations/20261008_qubits_agent_current_tree/run.py'
)
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)


def reader(objects):
    value = artifact.CurrentTree(None, None, 'test-bucket', 'test-project')
    requests = Counter()
    def source(oid):
        requests[oid] += 1
        return objects[oid]
    value.source = source
    return value, requests


def git_tree(entries):
    return artifact.loose('tree', b''.join(
        mode + b' ' + name + b'\0' + bytes.fromhex(oid)
        for mode, name, oid in entries
    ))


def test_current_usage_larger_than_256_mib_releases_payloads():
    objects, entries = {}, []
    for index in range(5):
        oid, raw = artifact.loose('blob', bytes([65 + index]) * (54 * 1024**2))
        objects[oid] = raw
        entries.append((b'100644', f'file-{index}'.encode(), oid))
    root, raw = git_tree(entries)
    objects[root] = raw
    current, requests = reader(objects)
    expected = 270 * 1024**2
    assert expected > artifact.MAX_GRAPH
    assert current.measure({'root': root, 'authority': 'legacy'}) == expected
    assert all(count == 1 for count in requests.values())
    assert all(isinstance(size, int) for size in current.sizes.values())
    assert current.objects == {}  # Measuring usage never materializes new Git objects.


def test_native_head_does_not_read_parent_history_and_counts_duplicate_paths():
    blob, raw_blob = artifact.loose('blob', b'hello')
    tree, raw_tree = git_tree([(b'100644', b'a', blob), (b'120000', b'b', blob),
                               (b'160000', b'submodule', 'e' * 40)])
    head, raw_head = artifact.loose('commit',
        f'tree {tree}\nparent {"f" * 40}\nauthor Test <test@example.test> 0 +0000\ncommitter Test <test@example.test> 0 +0000\n\nfixture\n'.encode())
    current, requests = reader({blob: raw_blob, tree: raw_tree, head: raw_head})
    assert current.measure({'root': head, 'authority': 'native'}) == 10
    assert requests[blob] == 1
    assert requests['f' * 40] == requests['e' * 40] == 0


def test_legacy_raw_and_repeated_subtree_sizes_are_preserved():
    root, tree, blob = 'a' * 16, 'b' * 16, 'c' * 16
    objects = {root: json.dumps({'one': ['T', tree], 'two': {'type': 'folder', 'hash': tree}}).encode(),
               tree: json.dumps({'file': ['B', blob]}).encode(), blob: b'abcdef'}
    current, requests = reader(objects)
    assert current.measure({'root': root, 'authority': 'legacy'}) == 12
    assert requests[tree] == requests[blob] == 1


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'wrong_type', 'budget'])
def test_invalid_current_data_never_produces_a_usage_value(failure):
    blob, raw_blob = artifact.loose('blob', b'hello')
    tree, raw_tree = git_tree([(b'100644', b'file', blob)])
    objects = {blob: raw_blob, tree: raw_tree}
    if failure == 'missing': del objects[blob]
    if failure == 'corrupt': objects[blob] = b'not a Git object'
    if failure == 'wrong_type': tree = blob
    current, _ = reader(objects)
    if failure == 'budget': current.MAX_READ_BYTES = 1
    with pytest.raises((ValueError, KeyError, artifact.zlib.error)):
        current.measure({'root': tree, 'authority': 'legacy'})


def test_cycle_and_visit_budget_are_bounded():
    root = 'a' * 16
    current, _ = reader({root: json.dumps({'cycle': ['T', root]}).encode()})
    with pytest.raises(ValueError, match='cyclic'):
        current.measure({'root': root, 'authority': 'legacy'})
    current, _ = reader({root: b'{}'})
    current.MAX_VISITS = 0
    with pytest.raises(ValueError, match='traversal limit'):
        current.measure({'root': root, 'authority': 'legacy'})
