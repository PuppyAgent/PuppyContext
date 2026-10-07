"""Content API — REST HTTP shell for ProductOperationAdapter.

Provides POSIX-like file system operations (ls, cat, write, mkdir, mv, rm)
on the version content tree. Used by the frontend Web UI and internal services.

Split into sub-modules for maintainability:
  content_read.py     — ls, cat, stat, tree
  content_write.py    — write, mkdir, mv, rm, bulk-write
  content_history.py  — versions, version-content, diff, rollback
"""

from __future__ import annotations

from fastapi import APIRouter

from src.version_engine.entrypoints.http.content_history import history_router
from src.version_engine.entrypoints.http.content_operations import operations_router
from src.version_engine.entrypoints.http.repository_management import management_router
from src.version_engine.entrypoints.http.content_read import read_router
from src.version_engine.entrypoints.http.content_write import write_router

router = APIRouter(
    prefix="/content",
    tags=["content"],
)

router.include_router(read_router)
router.include_router(write_router)
router.include_router(history_router)
router.include_router(operations_router)
router.include_router(management_router)
