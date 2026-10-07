"""One admitted native Git revision supplied by a Product reader."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectWriteState:
    """Admitted snapshot needed to start a native Product write.

    This is intentionally a request/input object, not a cache. It carries
    project authorization and the pinned native ref revision,
    then lets the SQL CAS at publish time remain the correctness boundary.
    """

    project_id: str
    project_name: str
    org_id: str = ""
    visibility: str = "org"
    role: str = ""
    can_write: bool = False
    root_hash: str = ""
    head_commit_id: str = ""
    repository_grant: object | None = None
    repository_revision: dict | None = None
