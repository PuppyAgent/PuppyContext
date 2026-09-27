"""Guard the public repository's independence from the private payment service.

These source checks catch direct coupling early. The database CI separately
replays SQL and upgrades populated fixtures with no PuppyPay schema installed.
"""

import ast
import re
from pathlib import Path

from src.config import Settings

ROOT = Path(__file__).resolve().parents[3]


def test_default_self_host_has_no_commercial_service_requirement(monkeypatch):
    # Other imported legacy modules may load the operator's local .env into
    # os.environ. This test checks defaults, not that machine's configured mode.
    for name in (
        "PUPPYPAY_BASE_URL",
        "PUPPYPAY_INTERNAL_API_SECRET",
        "MANAGED_AI_ENABLED",
        "BILLING_UI_ENABLED",
        "BILLING_WRITES_ENABLED",
        "ENTITLEMENTS_MODE",
        "BILLING_ENFORCEMENT",
        "SEAT_BILLING_MODE",
        "RUNTIME_METERING_MODE",
        "STORAGE_ENFORCEMENT_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    value = Settings(_env_file=None)
    assert value.PUPPYPAY_BASE_URL == ""
    assert value.PUPPYPAY_INTERNAL_API_SECRET == ""
    assert value.MANAGED_AI_ENABLED is False
    assert value.BILLING_UI_ENABLED is False
    assert value.BILLING_WRITES_ENABLED is False
    for field in (
        "ENTITLEMENTS_MODE",
        "BILLING_ENFORCEMENT",
        "SEAT_BILLING_MODE",
        "RUNTIME_METERING_MODE",
        "STORAGE_ENFORCEMENT_MODE",
    ):
        assert getattr(value, field) == "disabled", field


def test_public_runtime_cannot_import_private_payment_package():
    for path in (ROOT / "backend/src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(name.split(".")[0] != "puppypay" for name in names), path


def test_public_migrations_do_not_require_private_schema_or_gateway():
    paths = list((ROOT / "supabase/migrations").glob("*.sql"))
    paths += list((ROOT / "supabase/archive").rglob("*.sql"))
    paths += list((ROOT / "supabase/archive").rglob("*.py"))
    paths += list((ROOT / "supabase/data_migrations").rglob("*.sql"))
    paths += list((ROOT / "supabase/data_migrations").rglob("*.py"))
    for path in paths:
        source = path.read_text()
        if path.suffix == ".sql":
            source = re.sub(r"/\*.*?\*/|--[^\n]*", "", source, flags=re.S)
        assert not re.search(r'(?i)\bpuppypay"?\s*\.', source), path
        assert not re.search(r"(?i)\bPUPPYPAY_(?:BASE_URL|INTERNAL_API_SECRET)\b", source), path
