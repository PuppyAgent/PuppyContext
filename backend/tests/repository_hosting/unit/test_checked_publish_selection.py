"""New writers must not silently use a schema that ignores root head CAS."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.version_engine.infrastructure.supabase.history_repository import SupabaseHistoryManager

pytestmark = pytest.mark.hosting_component


def publish_args():
    return dict(
        old_root_hash="1" * 40, new_root_hash="1" * 40, commit_id="a" * 40,
        who="test:writer", message="metadata only", changes=[], conflicts=None,
        created_at_iso="", audit_event_type="access_git_push", audit_agent_id="test:writer",
        audit_detail={}, source_channel="access_git",
    )


@pytest.mark.parametrize("expected", [None, "", "b" * 40])
@pytest.mark.parametrize("metered", [False, True])
def test_history_manager_selects_checked_rpc_even_for_expected_absence(expected, metered):
    client = MagicMock()
    client.rpc.return_value.execute.return_value = SimpleNamespace(data=[{"published": True, "txn_id": 123}])
    manager = SupabaseHistoryManager(SimpleNamespace(client=client), "test-project")
    measurement = {"org_id": "test-org", "old_value": 0, "delta": 0, "limit": None, "enforce": False}
    assert manager.publish_project_update(
        **publish_args(), expected_scope_head_commit_id=expected,
        storage_measurement=measurement if metered else None,
    ) == (True, 123)
    rpc, body = client.rpc.call_args.args
    wanted = "publish_version_project_update" + ("_with_usage" if metered else "")
    assert rpc == wanted + ("_checked" if expected is not None else "")
    assert body["p_expected_scope_head_commit_id"] == expected


def test_missing_checked_rpc_fails_closed_without_legacy_fallback():
    client = MagicMock()
    client.rpc.return_value.execute.side_effect = RuntimeError("PGRST202 checked function unavailable")
    manager = SupabaseHistoryManager(SimpleNamespace(client=client), "test-project")
    with pytest.raises(RuntimeError, match="atomic publish RPC not available"):
        manager.publish_project_update(**publish_args(), expected_scope_head_commit_id="")
    assert client.rpc.call_count == 1
    assert client.rpc.call_args.args[0] == "publish_version_project_update_checked"
