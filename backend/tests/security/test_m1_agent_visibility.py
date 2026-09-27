"""Agent visibility only narrows an already-authorized ProjectGrant."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.platform.access.adapters.agent.config.repository import AgentRepository
from src.platform.access.adapters.agent.config.service import AgentConfigService


def _build_repo_with_rows(agent_rows: list[dict]):
    repo = AgentRepository.__new__(AgentRepository)
    repo._client = MagicMock()  # type: ignore[attr-defined]
    repo.TABLE = "access_surfaces"

    def fake_table(name):
        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.limit.return_value = chain
        chain.order.return_value = chain
        chain.execute.return_value = MagicMock(
            data=agent_rows if name == "access_surfaces" else []
        )
        return chain

    repo._client.table.side_effect = fake_table  # type: ignore[attr-defined]
    return repo


def test_org_visible_agent_does_not_add_a_project_membership_check():
    repo = _build_repo_with_rows(
        [{
            "id": "ag-1",
            "project_id": "p-1",
            "config": {"visibility": "org"},
            "created_by": "alice",
        }]
    )
    assert repo.is_visible_to("ag-1", "bob") is True
    queried_tables = [call.args[0] for call in repo._client.table.call_args_list]
    assert queried_tables == ["access_surfaces"]


def test_private_agent_visible_only_to_owner():
    repo = _build_repo_with_rows(
        [{
            "id": "ag-private",
            "project_id": "p-1",
            "config": {"visibility": "private"},
            "created_by": "alice",
        }]
    )
    assert repo.is_visible_to("ag-private", "alice") is True
    assert repo.is_visible_to("ag-private", "bob") is False


def test_missing_visibility_defaults_to_org():
    repo = _build_repo_with_rows(
        [{
            "id": "ag-legacy",
            "project_id": "p-1",
            "config": {},
            "created_by": "alice",
        }]
    )
    assert repo.is_visible_to("ag-legacy", "bob") is True


def test_list_filters_private_agents_for_non_owner():
    rows = [
        {
            "id": "ag-org",
            "project_id": "p",
            "config": {"visibility": "org", "name": "OrgA"},
            "created_by": "alice",
            "trigger": {},
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        },
        {
            "id": "ag-private",
            "project_id": "p",
            "config": {"visibility": "private", "name": "PrivA"},
            "created_by": "alice",
            "trigger": {},
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        },
    ]
    repo = _build_repo_with_rows(rows)
    repo.get_tools_by_agent_id_for_mcp = MagicMock(return_value=[])
    visible_for_bob = repo.get_by_project_id_with_accesses(
        "p", viewer_user_id="bob"
    )
    visible_for_alice = repo.get_by_project_id_with_accesses(
        "p", viewer_user_id="alice"
    )
    assert {agent.id for agent in visible_for_bob} == {"ag-org"}
    assert {agent.id for agent in visible_for_alice} == {"ag-org", "ag-private"}


def test_setting_default_cannot_mutate_an_invisible_private_agent():
    repo = MagicMock()
    repo.get_by_id.return_value = SimpleNamespace(
        id="ag-visible", project_id="p-1"
    )
    repo.get_default_agent.return_value = SimpleNamespace(id="ag-private")
    repo.is_visible_to.side_effect = lambda agent_id, _user_id: (
        agent_id == "ag-visible"
    )

    result = AgentConfigService(repo).update_agent(
        "ag-visible", "bob", is_default=True
    )

    assert result is None
    repo.update.assert_not_called()
