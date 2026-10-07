"""Retired Scope credentials cannot expand into native Project-root authority.

Historical root-first/scoped writer cases are superseded by this boundary and
repository_hosting's native snapshot, ref CAS, and migration acceptance suites.
"""

from unittest.mock import Mock

import pytest

from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.models import ResolvedRepositoryView, ScopeTarget
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from tests.repository_hosting.integration.test_ref_transaction_service import grant


@pytest.mark.asyncio
async def test_product_scope_write_is_rejected_before_repository_access():
    manager = Mock()
    ops = ProductOperationAdapter(manager).for_grant(grant("p"))
    with pytest.raises(PermissionError, match="Scope"):
        await ops.write_file("p", "secret", b"bytes", scope="docs")
    manager.get_native_service.assert_not_called()


def test_scope_runtime_cannot_be_reinterpreted_as_root():
    target = ScopeTarget(project_id="p", scope_id="docs")
    scoped = RuntimeGrant(
        principal=RuntimePrincipal(principal_id="credential", credential_kind="git_http_token"),
        target=target,
        repository_view=ResolvedRepositoryView(
            target=target, path_prefix="docs", excludes=(), max_mode="rw"
        ),
        mode=RuntimeMode.READ_WRITE,
    )
    manager = Mock()
    ops = ProductOperationAdapter(manager).for_grant(scoped)
    with pytest.raises(PermissionError):
        ops.read_file("p", "outside.txt")
    manager.get_native_service.assert_not_called()
