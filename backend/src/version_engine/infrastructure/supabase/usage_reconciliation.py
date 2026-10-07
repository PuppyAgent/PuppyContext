"""Complete, paged logical reconciliation using physical current trees.

The scheduler is a backend service, not a user/RuntimeGrant entrypoint. Captured
SQL inventories bind every measurement to authority/generation/HEAD/root. Final
SQL compares the complete inventory again before replacing the existing metric.
"""
from __future__ import annotations

import base64
import uuid
from contextlib import suppress

from src.platform.billing.storage import logical_verified_tree_bytes
from src.version_engine.read.repository_snapshot import RepositorySnapshot
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object


class _LegacyObjectReader:
    """Keep deployed legacy namespaces/ordinal chunks readable, never native proof."""

    def __init__(self, backend):
        self.backend = backend

    def get_durable(self, oid):
        return self.backend.get(oid)


class RepositoryUsageReconciler:
    def __init__(self, control, backend_factory, *, max_projects=1_000_000,
                 max_objects=1_000_000, max_bytes=8 * 1024**3):
        if (any(type(v) is not int or v < 1 for v in (max_projects, max_objects))
                or type(max_bytes) is not int or max_bytes < 0):
            raise ValueError('invalid storage reconciliation budget')
        self.control, self.backend_factory = control, backend_factory
        self.max_projects, self.max_objects, self.max_bytes = max_projects, max_objects, max_bytes

    def prune(self):
        removed = self.control.call('prune_version_storage_measurements', p_limit=200)
        if type(removed) is not int or not 0 <= removed <= 200:
            raise RuntimeError('invalid storage measurement cleanup receipt')
        return removed

    def measure(self, project_id, state):
        if (not isinstance(state, dict) or state.get('authority') not in {'legacy', 'shadow', 'native'}
                or state.get('object_format') not in {'sha1', 'sha256'}
                or type(state.get('generation')) is not int or state['generation'] < 0
                or 'root' not in state or 'head' not in state):
            raise RuntimeError('invalid storage inventory state')
        fmt, root = state['object_format'], state['root']
        if root is not None and (not isinstance(root, str) or len(root) != (40 if fmt == 'sha1' else 64)
                                 or not set(root) <= set('0123456789abcdef') or not root.strip('0')):
            raise RuntimeError('invalid storage inventory root')
        backend = self.backend_factory(project_id)
        bound = getattr(backend, 'publication_project_id', None)
        if bound is not None and bound != project_id:
            raise ValueError('storage inventory Project binding mismatch')
        verifier = ClosureVerifier(backend, object_format=fmt, max_objects=self.max_objects, max_bytes=self.max_bytes)
        empty, _ = encode_object('tree', b'', object_format=fmt)

        def size(tree, progress=None):
            if tree is None or tree == empty:
                return 0
            return logical_verified_tree_bytes(verifier.verify({tree: 'tree'}, progress=progress), tree)

        if state['authority'] != 'native':
            # SQL captures the root; the production factory gives a fresh reader.
            # Legacy compatibility placements are not native durability evidence.
            verifier.backend = _LegacyObjectReader(backend)
            return size(root)
        pin = str(uuid.uuid4())
        actor = 'system:storage-reconciliation'
        wire = self.control.begin_read(project_id, actor, pin)
        try:
            snapshot = RepositorySnapshot(self.control, backend, project_id=project_id, actor=actor,
                                          pin=pin, wire=wire, max_bytes=self.max_bytes)
        except BaseException:
            with suppress(Exception):
                self.control.release(project_id, actor, pin)
            raise
        try:
            head = snapshot.refs[b'HEAD']
            target = base64.b64encode(head.target).decode('ascii') if head.target is not None else None
            if (snapshot.generation != state['generation'] or snapshot.object_format != fmt
                    or state['head'] != {'oid': head.oid, 'target': target}):
                raise RuntimeError('storage reconciliation snapshot changed')
            revision = snapshot.revision()
            if revision.commit_oid != root:
                raise RuntimeError('storage reconciliation snapshot changed')
            # Share the byte/object budget with the verified selector commit.
            # Only the current tree, not parents/transport materialization.
            verifier.max_bytes = snapshot._remaining_bytes
            verifier.max_objects -= len(snapshot._verified_sizes)
            if verifier.max_objects < 0:
                raise RuntimeError('storage reconciliation object budget exceeded')
            return size(revision.tree_oid, snapshot.check_live)
        finally:
            snapshot.close()

    def reconcile(self, org_id, *, request_key=None):
        key = str(uuid.UUID(request_key)) if request_key is not None else str(uuid.uuid4())
        try:
            return self._reconcile(org_id, key)
        except BaseException:
            # Metadata only. A committed/lost-ACK result is never cancelled;
            # SQL serializes cancellation with an unknown in-flight finish.
            with suppress(Exception):
                self.control.call('cancel_version_storage_reconciliation', p_org_id=org_id, p_id=key)
            raise

    def _reconcile(self, org_id, key):
        job = self.control.call('begin_version_storage_reconciliation', p_org_id=org_id, p_id=key)
        if (not isinstance(job, dict) or job.get('id') != key or job.get('org_id') != org_id
                or type(job.get('project_count')) is not int or not 0 <= job['project_count'] <= self.max_projects
                or 'result' not in job):
            raise RuntimeError('invalid storage reconciliation contract')
        if job['result'] is not None:
            return self._result(job['result'])
        after, count = '', 0
        while True:
            page = self.control.call('get_version_storage_reconciliation_page', p_org_id=org_id,
                                     p_id=key, p_after=after, p_limit=200)
            if (not isinstance(page, dict) or page.get('id') != key or page.get('org_id') != org_id
                    or not isinstance(page.get('projects'), list) or len(page['projects']) > 200):
                raise RuntimeError('invalid storage reconciliation page')
            rows = page['projects']
            if not rows:
                break
            values = {}
            for row in rows:
                project_id = row.get('project_id') if isinstance(row, dict) else None
                if not isinstance(project_id, str) or project_id <= after or count >= job['project_count']:
                    raise RuntimeError('invalid storage reconciliation order')
                count += 1
                after = project_id
                value = row.get('logical_bytes')
                if value is None:
                    value = self.measure(project_id, row.get('state'))
                if type(value) is not int or not 0 <= value <= 2**63-1:
                    raise RuntimeError('invalid storage measurement')
                values[project_id] = value
            receipt = self.control.call('record_version_storage_measurements', p_org_id=org_id, p_id=key, p_values=values)
            if not isinstance(receipt, dict) or type(receipt.get('recorded')) is not int or receipt['recorded'] != len(values):
                raise RuntimeError('invalid storage measurement receipt')
        if count != job['project_count']:
            raise RuntimeError('incomplete storage reconciliation inventory')
        result = self.control.call('finish_version_storage_reconciliation', p_org_id=org_id, p_id=key)
        return self._result(result)

    @staticmethod
    def _result(result):
        if (not isinstance(result, dict) or result.get('outcome') not in {'reconciled', 'idempotent'}
                or type(result.get('value')) is not int or not 0 <= result['value'] <= 2**63-1):
            raise RuntimeError('invalid storage reconciliation result')
        return result
