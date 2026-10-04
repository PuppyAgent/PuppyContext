"""The Product journal has one checked RPC path, including read-only replay."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    AdmittedRefAuthorityRepository,
)

pytestmark = pytest.mark.hosting_component


def invoke(control, operation):
    if operation == "begin":
        return control.begin_product_operation("project", "user:actor", "request", "d" * 64, 7)
    return control.prepare_product_operation("project", "user:actor", "request", "d" * 64, {"updates": []})


@pytest.mark.parametrize("operation", ["begin", "prepare"])
@pytest.mark.parametrize("leased", [False, True])
def test_product_journal_uses_checked_rpc_and_current_lease_provenance(operation, leased):
    client = Mock()
    result = {"from": "checked RPC"}
    client.rpc.return_value.execute.return_value.data = result
    lease = SimpleNamespace(is_active=True, project_id="project", lease_id="lease", holder_id="holder")
    control = AdmittedRefAuthorityRepository(client, lease_provider=lambda _: lease if leased else None)
    assert invoke(control, operation) is result
    name, args = client.rpc.call_args.args
    assert name == f"{operation}_admitted_version_product_operation"
    assert args == {
        "p_project_id": "project", "p_actor": "user:actor", "p_request_key": "request",
        "p_input_sha256": "d" * 64,
        "p_lease_id": "lease" if leased else None, "p_holder_id": "holder" if leased else None,
        **({"p_generation": 7} if operation == "begin" else {"p_proposal": {"updates": []}}),
    }
    assert client.rpc.call_count == 1


@pytest.mark.parametrize("operation", ["begin", "prepare"])
def test_product_journal_rejects_a_foreign_lease_before_rpc(operation):
    client = Mock()
    lease = SimpleNamespace(is_active=True, project_id="foreign", lease_id="lease", holder_id="holder")
    control = AdmittedRefAuthorityRepository(client, lease_provider=lambda _: lease)
    with pytest.raises(ValueError, match="Project binding mismatch"):
        invoke(control, operation)
    client.rpc.assert_not_called()


@pytest.mark.parametrize("operation", ["begin", "prepare"])
def test_product_journal_outage_never_downgrades_to_a_setter_or_primitive(operation):
    client = Mock()
    client.rpc.return_value.execute.side_effect = RuntimeError("metadata unavailable")
    control = AdmittedRefAuthorityRepository(client, lease_provider=lambda _: None)
    with pytest.raises(RuntimeError, match="metadata unavailable"):
        invoke(control, operation)
    assert client.rpc.call_count == 1
    client.table.assert_not_called()
