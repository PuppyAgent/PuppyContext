from __future__ import annotations

from contextlib import nullcontext

import pytest
from fastapi import HTTPException

from src.platform.auth.models import CurrentUser
from src.version_engine.entrypoints.http import content_write
from src.version_engine.entrypoints.http.schemas import MoveRequest
from tests.authorization_fakes import authorization_for


class _FakeOps:
    def open_read(self, project_id, grant):
        assert grant.project_id == project_id
        return nullcontext(self)

    def get_read_revision(self, _project_id):
        return {"captured": "base"}


class _FakeCommands:
    def __init__(self):
        self.ops = _FakeOps()
        self.move_args: tuple[str, str, str] | None = None

    async def native_operation(self, project_id, grant, *, operation, arguments, base, **kwargs):
        assert base == {"captured": "base"} and operation == "move"
        self.move_args = (project_id, arguments["old_path"], arguments["new_path"])
        raise ValueError("cannot move 'old' into its own subtree: 'old/sub/old'")


@pytest.mark.asyncio
async def test_move_route_returns_400_for_invalid_move_destination():
    commands = _FakeCommands()

    with pytest.raises(HTTPException) as exc:
        await content_write.move(
            "test-proj",
            MoveRequest(old_path="old", new_path="old/sub/old"),
            commands=commands,
            authorization=authorization_for("test-proj"),
            current_user=CurrentUser(user_id="u1", role="authenticated"),
        )

    assert exc.value.status_code == 400
    assert "own subtree" in exc.value.detail
    assert commands.move_args == ("test-proj", "old", "old/sub/old")
