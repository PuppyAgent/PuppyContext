"""Initialization must never reinterpret unavailable storage as an empty repo."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.version_engine.infrastructure.supabase.history_repository import SupabaseHistoryManager
from src.version_engine.write_engine.engine import VersionWriteEngine

pytestmark = [pytest.mark.hosting_component, pytest.mark.asyncio]


@pytest.mark.parametrize('existing', ['a'*40, 'b'*40])
async def test_project_root_initialization_preserves_ack_even_when_storage_is_missing(existing):
    writes = []
    history = SimpleNamespace(get_root_hash=lambda: existing, set_root_hash=writes.append,
                              initialize_root_hash=lambda: existing)
    backend = SimpleNamespace(async_exists=AsyncMock(return_value=False))
    repo = SimpleNamespace(history=history, store=SimpleNamespace(_backend=backend, object_format='sha1'))
    manager = SimpleNamespace(get_repo=lambda _: repo)
    assert await VersionWriteEngine(manager).initialize_project_tree('project') == existing
    assert writes == []
    backend.async_exists.assert_not_called()


async def test_project_root_initialization_missing_checked_capability_does_not_write():
    writes = []
    history = SimpleNamespace(get_root_hash=lambda: '', set_root_hash=writes.append)
    repo = SimpleNamespace(history=history, store=SimpleNamespace(object_format='sha1'))
    with pytest.raises(RuntimeError, match='checked root initialization unavailable'):
        await VersionWriteEngine(SimpleNamespace(get_repo=lambda _: repo)).initialize_project_tree('project')
    assert writes == []


@pytest.mark.parametrize('invalid', [None, True, '', '0'*40, 'A'*40, 'a'*64])
async def test_project_root_initialization_rejects_invalid_rpc_response(invalid):
    calls = []
    def rpc(name, params):
        calls.append((name, params))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=invalid))
    history = SupabaseHistoryManager(SimpleNamespace(client=SimpleNamespace(rpc=rpc)), 'project')
    with pytest.raises(RuntimeError, match='invalid checked root initialization result'):
        history.initialize_root_hash()
    assert calls == [('initialize_legacy_version_project_root', {'p_project_id': 'project', 'p_lease_id': None, 'p_holder_id': None})]


async def test_project_root_initialization_missing_rpc_has_no_legacy_update_fallback():
    calls = []
    def rpc(name, params):
        calls.append(name)
        raise RuntimeError('RPC unavailable')
    client = SimpleNamespace(rpc=rpc, table=lambda *_: pytest.fail('fallback updated Project'))
    history = SupabaseHistoryManager(SimpleNamespace(client=client), 'project')
    with pytest.raises(RuntimeError, match='RPC unavailable'):
        history.initialize_root_hash()
    assert calls == ['initialize_legacy_version_project_root']
