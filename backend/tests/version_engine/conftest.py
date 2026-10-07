"""Shared fixtures for version_engine tests.

Lifted here from ``test_git_native_write_engine/engine.py`` so newer test
modules (``test_engine_resolve.py``) can reuse the in-memory
``server_repo`` / ``repo_manager`` without copy-pasting the boilerplate.
The original test file keeps its own copies because pytest still picks
up the closer-scoped fixtures when both exist; conftest acts as a
fallback for everyone else.
"""

from __future__ import annotations

import pytest

from src.version_engine.storage.object_store import ObjectStore


@pytest.fixture(autouse=True)
def isolate_git_entitlement_lookup(monkeypatch):
    """Keep version-engine tests hermetic while retaining the hard Git cap.

    Entitlement cap calculation is covered by the security suite. Route tests
    in this package exercise Git protocol behavior and must not depend on a
    live project/organization database.
    """
    from src.version_engine.entrypoints.git import router as git_router

    monkeypatch.setattr(
        git_router,
        "_git_receive_max_body_bytes",
        lambda _project_id: git_router._effective_receive_pack_cap(None),
    )


@pytest.fixture
def memory_store(tmp_path) -> ObjectStore:
    obj_dir = tmp_path / "objects"
    obj_dir.mkdir()
    return ObjectStore(obj_dir)
