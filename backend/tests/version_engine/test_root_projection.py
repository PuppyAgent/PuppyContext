"""Native reads never reconstruct or repair Project roots from old Scope state."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from tests.repository_hosting.integration.test_ref_transaction_service import grant


@pytest.mark.parametrize("failure", [None, RuntimeError("authority unavailable")])
def test_missing_native_authority_never_enters_old_root_repair(failure):
    manager = SimpleNamespace(
        get_native_service=Mock(return_value=None, side_effect=failure),
        get_repo=Mock(side_effect=AssertionError("old root read")),
        get_server_repo=Mock(side_effect=AssertionError("old root repair")),
    )
    ops = ProductOperationAdapter(manager).for_grant(grant("p"))
    with pytest.raises(RuntimeError):
        ops.list_dir("p")
    manager.get_repo.assert_not_called()
    manager.get_server_repo.assert_not_called()
