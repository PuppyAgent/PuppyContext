"""Schemas for the unified context-activity feed."""

from __future__ import annotations

from pydantic import BaseModel


class ActivityItemResponse(BaseModel):
    """One row of the ``context_activity_items`` view.

    ``kind`` is ``upload`` | ``import`` | ``synchronize_run``. The remaining fields
    are normalized across the three source tables by the view, so the client
    renders them uniformly without knowing which table produced the row.
    """

    id: str
    kind: str
    project_id: str
    created_by: str | None = None
    label: str | None = None
    status: str | None = None
    phase: str | None = None
    progress: int | None = None
    message: str | None = None
    error_message: str | None = None
    result_path: str | None = None
    result_commit_id: str | None = None
    created_at: str | None = None
    completed_at: str | None = None
