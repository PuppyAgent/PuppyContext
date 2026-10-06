"""An uncertain HEAD acknowledgement must still validate the request digest."""

import base64
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from postgrest.exceptions import APIError

from src.version_engine.entrypoints.http.repository_management import (
    RepositoryHeadUpdate,
    change_head,
)
from tests.repository_hosting.integration.test_ref_transaction_service import grant

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("outcome", ["terminal_conflict", "lost_ack", "replay_conflict", "missing"])
def test_head_recovery_requires_validated_original_request(outcome):
    payload = RepositoryHeadUpdate(
        request_key=uuid4(),
        generation=1,
        expected={
            "kind": "symbolic",
            "target_b64": base64.b64encode(b"refs/heads/main").decode(),
        },
        target_branch="feature",
    )
    original = {"status": "committed", "request_key": str(payload.request_key)}
    conflict = APIError({"code": "22023", "message": "request_key_reused"})
    lost_ack = ConnectionError("acknowledgement lost")
    outcomes = {
        "terminal_conflict": [conflict],
        "lost_ack": [lost_ack, original],
        "replay_conflict": [lost_ack, conflict],
        "missing": [lost_ack],
    }
    service = SimpleNamespace(
        object_format="sha1",
        submit=Mock(side_effect=outcomes[outcome]),
        control=SimpleNamespace(
            recover_result=Mock(return_value=None if outcome == "missing" else original)
        ),
    )
    manager = SimpleNamespace(get_native_service=lambda _: service)
    actor = grant("project")
    if outcome == "lost_ack":
        assert change_head(manager, actor, payload) == original
    else:
        expected = ConnectionError if outcome == "missing" else APIError
        with pytest.raises(expected) as caught:
            change_head(manager, actor, payload)
        assert caught.value is (lost_ack if outcome == "missing" else conflict)

    calls = service.submit.call_args_list
    assert len(calls) == (2 if outcome in {"lost_ack", "replay_conflict"} else 1)
    if len(calls) == 2:
        # The callback has no side effects; every digest-bearing argument must
        # be identical, including expected state and repository generation.
        assert calls[0].args == calls[1].args == (actor,)
        assert {k: v for k, v in calls[0].kwargs.items() if k != "prepare"} == {
            k: v for k, v in calls[1].kwargs.items() if k != "prepare"
        }
    assert service.control.recover_result.call_count == (outcome != "terminal_conflict")
