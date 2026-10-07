"""The single application coordinator for publishing a Project.

Every production creation path enters through this module.  The database
transaction first creates an unpublished Project plus its creator grant and a
durable operation.  The existing L5 ``VersionWriteEngine`` then installs the
canonical root.  Only the completion transaction changes the Project to
``ready``.

Contentful workflows use ``deferred`` publication: their callback runs while
the Project is still invisible, and any reported failure creates a durable
object-cleanup job before removing the unpublished database aggregate.  An
uncertain/crashed request remains hidden and is eventually aborted by the
initialization reconciler.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from src.exceptions import AppException, ErrorCode
from src.platform.project.control_plane import (
    IdempotentProjectResult,
    ProjectControlPlaneService,
    ProjectPublicationMode,
)
from src.platform.project.models import Project
from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseFactory
from src.version_engine.write_engine.engine import VersionWriteEngine

logger = logging.getLogger(__name__)

ProjectInitializer = Callable[[Project], Awaitable[None]]


def initialize_project_tree_sync(
    version_engine: VersionWriteEngine,
    project_id: str,
) -> str:
    """Run L5 on a worker-owned event loop.

    ``initialize_project_tree`` retains an async public API, but its repository
    boundary includes synchronous Supabase work.  Request event loops therefore
    enter it through ``asyncio.to_thread`` and this small worker-loop adapter.
    """

    return asyncio.run(version_engine.initialize_project_tree(project_id))


async def create_project_with_tree(
    *,
    control_plane: ProjectControlPlaneService,
    version_engine: VersionWriteEngine,
    operation_key: str,
    name: str,
    description: str | None,
    org_id: str,
    created_by: str,
    project_limit: int | float | None,
    publication_mode: ProjectPublicationMode,
    source_fingerprint: dict[str, Any],
    request_fingerprint: dict[str, Any] | None = None,
    result_metadata: dict[str, Any] | None = None,
    initialize: ProjectInitializer | None = None,
    write_lease_factory: ProjectWriteLeaseFactory = ProjectWriteLease,
    native_repository: dict[str, str] | None = None,
) -> IdempotentProjectResult:
    """Publish exactly one ready Project through the durable control plane.

    ``empty`` is the canonical Git-hosting bootstrap and is safely resumable by
    both the caller and the reconciler because L5 initialization is idempotent.
    ``deferred`` is for composite products such as template or landing claims;
    the caller's full initializer must finish before the Project is published.
    """

    if publication_mode == "deferred" and initialize is None:
        raise ValueError("deferred Project publication requires an initializer")

    native_options = {"native_repository": native_repository} if native_repository is not None else {}
    result = await asyncio.to_thread(
        control_plane.create_project,
        operation_key=operation_key,
        name=name,
        description=description,
        org_id=org_id,
        actor_user_id=created_by,
        publication_mode=publication_mode,
        source_fingerprint=source_fingerprint,
        project_limit=project_limit,
        request_fingerprint=request_fingerprint,
        result_metadata=result_metadata,
        **native_options,
    )
    if result.ready:
        return result

    # Only the request that created a deferred operation may execute its
    # non-idempotent composite initializer.  Concurrent/retried requests fail
    # closed while the original request owns publication; a crashed owner is
    # eventually cleaned by the durable reconciler rather than duplicated.
    if publication_mode == "deferred" and result.replayed:
        raise AppException(
            code=ErrorCode.VERSION_CONFLICT,
            status_code=409,
            message="Project publication is already in progress",
            details={"code": "project_publication_in_progress"},
        )

    project_id = str(result.project.id)
    try:
        async with write_lease_factory(
            project_id,
            "project.initialize",
            initialization_operation_key=operation_key,
            initialization_actor=created_by,
        ):
            await asyncio.to_thread(
                initialize_project_tree_sync,
                version_engine,
                project_id,
            )
            if initialize is not None:
                await initialize(result.project)
            return await asyncio.to_thread(
                control_plane.complete_project_initialization,
                project_id=project_id,
                operation_key=operation_key,
                actor_user_id=created_by,
                replayed=result.replayed,
            )
    except Exception:
        if publication_mode == "deferred":
            try:
                await asyncio.shield(
                    asyncio.to_thread(
                        control_plane.abort_deferred_publication,
                        project_id=project_id,
                        operation_key=operation_key,
                        actor_user_id=created_by,
                    )
                )
            except Exception:
                # Do not replace the workflow's original failure.  The hidden
                # operation remains durable and the reconciler will abort it
                # after its publication deadline.
                logger.exception(
                    "Unable to abort failed deferred Project publication %s",
                    project_id,
                )
        raise
