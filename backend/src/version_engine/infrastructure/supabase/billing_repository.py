"""Checked existing logical-usage settlement, not a native activation switch."""
from __future__ import annotations

from contextlib import nullcontext

from src.platform.billing.storage import logical_verified_tree_bytes
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_graph import object_edges


def verified_current_tree(service, snapshot, oid, manifest=None):
    """Reuse incoming proof, otherwise read only the pinned current tree."""
    if oid is None:
        return None, None
    if manifest is not None and oid in manifest.objects:
        commit = manifest.objects[oid]
        trees = [child for child, kind in commit.edges if kind == 'tree']
        if commit.kind != 'commit' or len(trees) != 1:
            raise ValueError('logical billing requires a verified commit tree')
        return manifest, trees[0]
    kind, body = snapshot.object(oid)
    if kind != 'commit':
        raise ValueError('logical billing requires a verified commit tree')
    tree = next(child for child, child_kind in object_edges(kind, body, object_format=service.object_format)
                if child_kind == 'tree')
    if tree == snapshot.empty_tree:
        return None, None
    verifier = ClosureVerifier(service.backend, object_format=service.object_format,
                               max_objects=service.verifier.max_objects-len(snapshot._verified_sizes),
                               max_bytes=snapshot._remaining_bytes)
    return verifier.verify({tree: 'tree'}, progress=snapshot.check_live), tree


class RepositoryBilling:
    def __init__(self, control):
        if not callable(getattr(control, 'apply_billed', None)):
            raise ValueError('admitted billing control required')
        self.control = control

    def check(self, project_id):
        value = self.control.call('check_version_repository_billing', p_project_id=project_id)
        return self.validate(project_id, value)

    @staticmethod
    def validate(project_id, value):
        if (not isinstance(value, dict) or value.get('project_id') != project_id
                or value.get('metric') != 'storage.logical_bytes'
                or not isinstance(value.get('org_id'), str) or not value['org_id']
                or any(type(value.get(key)) is not int or value[key] < minimum
                       for key, minimum in [('source_revision', 1), ('version', 1), ('value', 0)])
                or (value.get('storage_limit') is not None
                    and (type(value['storage_limit']) is not int or value['storage_limit'] < 0))
                or 'storage_limit' not in value):
            raise RuntimeError('invalid repository billing contract')
        return value

    def measure(self, service, grant, edits, context, manifest=None, *, snapshot=None):
        # Local import avoids the snapshot/ref-service type dependency cycle.
        from src.version_engine.read.repository_snapshot import repository_snapshot

        def head(states):
            value = states[b'HEAD']
            if value.target is not None:
                value = states.get(value.target)
            return value.oid if value is not None else None

        read = nullcontext(snapshot) if snapshot is not None else repository_snapshot(
            self.control, service.backend, grant, project_id=service.project_id,
            max_bytes=service.verifier.max_bytes)
        with read as snapshot:
            snapshot.check_live()
            if snapshot.project_id != service.project_id:
                raise ValueError("repository snapshot Project mismatch")
            if snapshot.object_format != service.object_format:
                raise ValueError('repository object format mismatch')
            states = dict(snapshot.refs)
            before = head(states)
            for edit in edits:
                if edit.new is not None:
                    states[edit.name] = edit.new
            after = head(states)
            value = {'org_id': context['org_id'], 'source_revision': context['source_revision'],
                     'old_head_oid': before, 'new_head_oid': after}
            if before == after:
                return value  # Named refs/identical default OID: no object I/O.

            def measure(oid):
                proof, tree = verified_current_tree(service, snapshot, oid, manifest)
                return logical_verified_tree_bytes(proof, tree) if tree is not None else 0

            value.update(old_bytes=measure(before), new_bytes=measure(after))
            return value

    def apply(self, *args, usage=None):
        # No fallback to the older unmetered publisher, even during replay.
        return self.control.apply_billed(*args, usage=usage)
