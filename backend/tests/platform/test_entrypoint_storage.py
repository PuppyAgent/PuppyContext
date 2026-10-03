"""Canonical persistence still produces the existing public task/binding shapes."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.infra.search.index_task import SearchIndexTaskUpsert
from src.infra.search.index_task_repository import SearchIndexTaskRepository, _row_to_task
from src.platform.access.adapters.agent.config.repository import _row_to_tool
from src.platform.project.dashboard_router import _fetch_uploads
from src.platform.synchronize.github.repository import _to_api_row

NOW = '2026-09-27T00:00:00+00:00'


def test_canonical_access_identifier_keeps_agent_wire_contract():
    row = dict(id='link', access_surface_id='surface', tool_id='tool', created_at=NOW)
    result = _row_to_tool(row).model_dump()
    assert result['agent_id'] == 'surface'
    assert result['tool_id'] == 'tool'
    assert 'access_surface_id' not in result


def test_github_log_preserves_canonical_parent_without_alias_or_mutation():
    row = dict(id='log', synchronize_github_binding_id='binding', version_commit_id='commit', status='success')
    result = _to_api_row(row)
    assert result == row and result is not row
    assert result['synchronize_github_binding_id'] == 'binding'
    assert 'binding_id' not in result and 'integration_id' not in result
    assert result['version_commit_id'] == 'commit'


@pytest.mark.parametrize(('stored', 'public'), [
    ('pending', 'pending'), ('running', 'indexing'), ('completed', 'ready'),
    ('failed', 'error'), ('cancelled', 'error'),
])
def test_search_tasks_preserve_public_status_owner_and_counters(stored, public):
    row = dict(id='tool-1', created_by='user-1', project_id='project-1', path='docs',
               config={'json_path': '/items', 'folder_path': 'docs'}, status=stored,
               result={'chunks_count': 10, 'indexed_chunks_count': 3, 'total_files': 5, 'indexed_files': 2},
               created_at=NOW, updated_at=NOW)
    task = _row_to_task(row)
    assert task.tool_id == 'tool-1'
    assert task.user_id == 'user-1'
    assert (task.status, task.chunks_count, task.indexed_chunks_count) == (public, 10, 3)
    assert (task.folder_path, task.total_files, task.indexed_files) == ('docs', 5, 2)


def test_search_upsert_round_trips_creator_using_the_real_column_name():
    client = MagicMock()
    writes = []

    def upsert(payload, *, on_conflict):
        writes.append(deepcopy(payload))
        assert on_conflict == 'id'
        # A missing/incorrect user_id column used to fail against the B1 schema.
        assert payload['created_by'] == 'user-1'
        assert 'user_id' not in payload
        assert 'type' not in payload
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[{**payload, 'created_at': NOW}]))

    client.table.return_value.upsert.side_effect = upsert
    repo = SearchIndexTaskRepository(client)
    task = repo.upsert(SearchIndexTaskUpsert(
        tool_id='tool-1', user_id='user-1', project_id='project-1', path='docs',
        status='indexing', total_files=10, indexed_files=3,
    ))
    assert task.user_id == 'user-1'
    assert task.status == 'indexing'
    assert writes[0]['progress'] == 30
    client.table.assert_called_once_with('search_index_tasks')


def test_dashboard_combines_storage_without_duplicate_transition_mirrors():
    uploads = MagicMock()
    search = MagicMock()
    # Model Supabase chain return values while asserting the consumer boundary.
    for query in (uploads, search):
        for method in ('select','neq','eq','in_','limit'):
            getattr(query, method).return_value = query
    uploads.execute.return_value.data = [dict(id='upload-1',status='running',type='file_ocr',progress=20)]
    search.execute.return_value.data = [dict(id='tool-1',status='running',progress=30)]
    client = MagicMock()
    client.table.side_effect = lambda name: {'uploads': uploads, 'search_index_tasks': search}[name]
    result = _fetch_uploads(client, 'project-1')
    assert [(row.id, row.type) for row in result] == [('upload-1','file_ocr'),('tool-1','search_index')]
    uploads.neq.assert_called_once_with('type','search_index')
    uploads.eq.assert_called_once_with('project_id','project-1')
    search.eq.assert_called_once_with('project_id','project-1')
