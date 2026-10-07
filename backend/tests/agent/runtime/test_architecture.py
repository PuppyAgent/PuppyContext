"""Keep data access constraints enforceable at every future call site."""

import ast
from pathlib import Path

BASE = Path(__file__).resolve().parents[3] / "src/platform/access/adapters/agent"


def test_application_and_heartbeat_cannot_bypass_named_ports():
    files = [BASE / "service.py", BASE / "router.py", *BASE.joinpath("runtime").glob("*.py")]
    for path in files:
        if path.name in {"repository.py", "worker.py"}:
            continue  # Composition, not business orchestration.
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"table", "rpc", "from_"}, (path.name, node.lineno)
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    (
                        "src.infra.supabase.client",
                        "src.infra.supabase.dependencies",
                        "postgrest",
                        "supabase",
                    )
                ), path
                if path.name == "heartbeat.py":
                    assert not any(
                        name in (node.module or "")
                        for name in ("admission", "config", "persistence")
                    )
    heartbeat = (BASE / "runtime/heartbeat.py").read_text()
    assert "RenewalPort" in heartbeat
    assert "recheck" not in heartbeat


def test_context_contains_no_implicit_storage_access():
    tree = ast.parse((BASE / "runtime/context.py").read_text())
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert all(
        not any(part in (module or "") for part in ("repository", "supabase", "dependencies"))
        for module in imports
    )
    assert "frozen=True" in (BASE / "runtime/context.py").read_text()


def test_query_adapter_contains_no_table_mutations():
    tree = ast.parse((BASE / "runtime/persistence/queries.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"insert", "update", "upsert", "delete"}
