from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.exceptions import NotFoundException, PermissionException
from src.platform.upload.handlers import _authorize_owned_upload_task
from src.platform.auth.models import CurrentUser
from src.platform.authorization.service import AuthorizationService
from tests.authorization_fakes import StaticAuthorizationRepository, authorization_for


USER = CurrentUser(user_id="user-1", role="authenticated")
TASK = SimpleNamespace(
    task_id="task-1",
    project_id="project-1",
    created_by=USER.user_id,
)


def test_upload_continuation_rechecks_current_editor_grant():
    assert _authorize_owned_upload_task(
        TASK,
        current_user=USER,
        authorization=authorization_for("project-1", role="editor"),
    ) is TASK


def test_upload_continuation_denies_viewer_after_role_downgrade():
    with pytest.raises(PermissionException):
        _authorize_owned_upload_task(
            TASK,
            current_user=USER,
            authorization=authorization_for("project-1", role="viewer"),
        )


def test_upload_continuation_denies_after_membership_revocation():
    with pytest.raises(NotFoundException):
        _authorize_owned_upload_task(
            TASK,
            current_user=USER,
            authorization=AuthorizationService(StaticAuthorizationRepository([])),
        )


def test_upload_task_ownership_is_an_additional_restriction():
    with pytest.raises(HTTPException) as exc_info:
        _authorize_owned_upload_task(
            SimpleNamespace(
                task_id="task-2",
                project_id="project-1",
                created_by="another-user",
            ),
            current_user=USER,
            authorization=authorization_for("project-1", role="admin"),
        )
    assert exc_info.value.status_code == 404
