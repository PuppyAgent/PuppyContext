"""Legacy acknowledged roots survive physical loss observed by production reads.

Actual owned S3/PostgREST/PG, synthetic legacy publication actor. No native
activation or claim of end-user authorization/repair acceptance.
"""
import json
from types import SimpleNamespace

import httpx
import pytest
from supabase import ClientOptions, create_client

from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.read.tree_reader import VersionTreeReader
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.write_engine.git_object_format import decode_object, encode_object
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.s3_service import owned_s3

pytestmark = pytest.mark.hosting_s3


@pytest.mark.parametrize('damage', ['tree', 'blob'])
def test_s3_listing_damage_preserves_acknowledged_root_and_recovers_without_metadata_repair(pg_project, tmp_path, damage):
    pg, project = pg_project
    git = Git.init(tmp_path / 'client')
    commit = git.commit({'outside.md': b'acknowledged outside scope',
                         'docs/good.md': b'acknowledged scoped file',
                         'broken/lost.md': b'acknowledged unavailable file'})
    root = git.run('rev-parse', 'HEAD^{tree}').stdout.strip().decode()
    docs = git.run('rev-parse', 'HEAD:docs').stdout.strip().decode()
    missing = git.run('rev-parse', 'HEAD:broken' if damage == 'tree' else 'HEAD:outside.md').stdout.strip().decode()
    objects = git.objects()
    with owned_s3() as (s3, api), httpx.Client(timeout=15, trust_env=False) as http:
        sdk = create_client(str(api.client.base_url), api.headers('service_role')['apikey'],
                            ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False))
        db = SimpleNamespace(client=sdk)
        backend = S3StorageBackend(s3, project, supabase=db)
        for oid, (kind, body) in objects.items():
            backend.put(oid, encode_object(kind, body)[1])
        assert json.loads(pg.value(pg.publish(project, '1'*40, root, commit)))['published']
        pg.sql(f'INSERT INTO public.version_scope_state(project_id,scope_path,scope_hash,head_commit_id) '
               f'VALUES({literal(project)},\'docs\',{literal(docs)},{literal(commit)})')
        before = pg.value(f'SELECT row_to_json(p) FROM public.projects p WHERE id={literal(project)}')
        s3.client.delete_object(Bucket=s3.bucket_name, Key=backend._key_for(missing))
        reader = VersionTreeReader(VersionRepoManager(s3, db))
        shown = reader.list_dir(project)
        damaged_name = 'broken' if damage == 'tree' else 'outside.md'
        assert next(entry for entry in shown if entry.name == damaged_name).integrity_status == 'damaged'
        assert {entry.name for entry in shown} == {'outside.md', 'docs', 'broken'}
        assert pg.value(f'SELECT row_to_json(p) FROM public.projects p WHERE id={literal(project)}') == before
        assert reader.read_file(project, 'docs/good.md') == b'acknowledged scoped file'
        # Restore only the unavailable physical bytes; no root/index/history repair.
        kind, body = objects[missing]
        backend.put(missing, encode_object(kind, body)[1])
        assert decode_object(backend.get_durable(missing)) == (kind, body)
        cold = VersionTreeReader(VersionRepoManager(s3, db))
        assert cold.read_file(project, 'outside.md') == b'acknowledged outside scope'
        assert cold.read_file(project, 'broken/lost.md') == b'acknowledged unavailable file'
        assert cold.get_root_hash(project) == root
        assert pg.value(f'SELECT row_to_json(p) FROM public.projects p WHERE id={literal(project)}') == before
