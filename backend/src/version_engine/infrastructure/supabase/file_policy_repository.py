"""Checked full-repository file policy; no enrollment or transport authority."""
from __future__ import annotations

from contextlib import nullcontext

from src.version_engine.admission.file_policy import oversized_blob_occurrences
from src.version_engine.infrastructure.supabase.billing_repository import verified_current_tree


class RepositoryFilePolicy:
    def __init__(self, control):
        if not callable(getattr(control, 'apply_policy', None)):
            raise ValueError('file policy requires checked publication')
        self.control = control

    def check(self, project_id):
        value = self.control.call('check_version_repository_file_policy', p_project_id=project_id)
        return self.validate(project_id, value)

    @staticmethod
    def validate(project_id, value):
        if (not isinstance(value, dict) or value.get('project_id') != project_id
                or not isinstance(value.get('org_id'), str) or not value['org_id']
                or type(value.get('source_revision')) is not int or value['source_revision'] <= 0
                or 'file_limit' not in value
                or (value['file_limit'] is not None and
                    (type(value['file_limit']) is not int or not 0 <= value['file_limit'] <= 2**63-1))):
            raise RuntimeError('invalid repository file policy contract')
        return value

    def verify(self, service, grant, edits, roots, context, manifest=None, *, snapshot=None):
        from src.version_engine.read.repository_snapshot import repository_snapshot
        from src.version_engine.write_engine.ref_transaction import RefState

        result = dict(context)
        limit = context['file_limit']
        read = nullcontext(snapshot) if snapshot is not None else repository_snapshot(
            self.control, service.backend, grant, project_id=service.project_id,
            max_bytes=service.verifier.max_bytes)
        with read as snapshot:
            snapshot.check_live()
            if snapshot.project_id != service.project_id:
                raise ValueError("repository snapshot Project mismatch")
            if snapshot.object_format != service.object_format:
                raise ValueError('repository object format mismatch')
            if manifest is None and roots:
                manifest = service.verifier.verify(roots, progress=snapshot.check_live)
            result['manifest_sha256'] = manifest.digest if manifest is not None else None
            states = dict(snapshot.refs)

            def selected():
                head = states[b'HEAD']
                return states.get(head.target, RefState()).oid if head.target else head.oid

            before = selected()
            for edit in edits:
                if edit.new is not None:
                    states[edit.name] = edit.new
            after = selected()
            result.update(old_head_oid=before, new_head_oid=after)
            if limit is None:
                return result
            oversized = {oid for oid, record in manifest.objects.items()
                         if record.kind == 'blob' and record.size > limit} if manifest is not None else set()
            if oversized:
                # Existing published history may be grandfathered after a plan
                # downgrade. A rejected receipt/allocation is NOT that proof.
                published = self.control.call('get_version_repository_published_roots',
                                              p_project_id=service.project_id)
                if not isinstance(published, dict):
                    raise RuntimeError('invalid published roots contract')
                prior = service.verifier.verify(published, progress=snapshot.check_live) if published else None
                if prior is None or any(oid not in prior.objects or prior.objects[oid].kind != 'blob'
                                        for oid in oversized):
                    raise PermissionError('file_size_limit_exceeded')
            if before != after:
                proof, tree = verified_current_tree(service, snapshot, after, manifest)
                new_files = oversized_blob_occurrences(proof, tree, limit)
                if new_files:
                    old_proof, old_tree = verified_current_tree(service, snapshot, before, manifest)
                    if new_files - oversized_blob_occurrences(old_proof, old_tree, limit):
                        raise PermissionError('file_size_limit_exceeded')
            snapshot.check_live()
        return result

    def apply(self, *args, usage=None, policy=None):
        return self.control.apply_policy(*args, usage=usage, policy=policy)
