from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from src.infra.self_hosted_bootstrap import initialize_storage


def fixture():
    client, repo = MagicMock(), MagicMock()
    client.list_objects_v2.return_value = {"Contents": []}
    client.list_multipart_uploads.return_value = {"Uploads": []}
    repo.checkpoint.return_value = {}
    repo.live_project_ids.return_value = set()
    repo.finalize_scan.return_value = {"outcome": "finalized"}
    repo.verify_scan.return_value = {"outcome": "verified"}
    repo.complete.return_value = {"outcome": "completed"}
    return client, repo


def test_empty_storage_requires_two_matching_observations(monkeypatch):
    client, repo = fixture()
    proof = SimpleNamespace(object_count=0, multipart_count=0, digest="empty")
    observe = MagicMock(return_value=proof)
    monkeypatch.setattr(
        "src.infra.self_hosted_bootstrap.observe_project_storage_inventory", observe
    )
    initialize_storage(client, "fresh", repo)
    assert observe.call_count == 2
    repo.verify_scan.assert_called_once_with(proof)
    repo.complete.assert_called_once()
    client.delete_object.assert_not_called()
    client.delete_objects.assert_not_called()


@pytest.mark.parametrize("existing", ["project", "object", "multipart"])
def test_existing_data_never_automatically_completes_inventory(existing):
    client, repo = fixture()
    if existing == "project":
        repo.live_project_ids.return_value = {"old-project"}
    elif existing == "object":
        client.list_objects_v2.return_value = {"Contents": [{"Key": "old-file"}]}
    else:
        client.list_multipart_uploads.return_value = {"Uploads": [{"Key": "in-progress"}]}
    with pytest.raises(RuntimeError, match="explicit operator"):
        initialize_storage(client, "existing", repo)
    repo.complete.assert_not_called()
    client.delete_objects.assert_not_called()


def test_repeat_install_preserves_existing_completed_inventory():
    client, repo = fixture()
    repo.checkpoint.return_value = {"inventory_complete": True}
    initialize_storage(client, "existing", repo)
    repo.complete.assert_not_called()
    repo.finalize_scan.assert_not_called()


def test_access_denied_is_not_treated_as_missing_bucket():
    client, repo = fixture()
    client.head_bucket.side_effect = ClientError(
        {"ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadBucket"
    )
    with pytest.raises(ClientError):
        initialize_storage(client, "existing", repo)
    client.create_bucket.assert_not_called()


@pytest.mark.parametrize("completed", [True, False])
def test_existing_installation_never_gets_an_empty_replacement_bucket(completed):
    client, repo = fixture()
    repo.checkpoint.return_value = {"inventory_complete": completed}
    repo.live_project_ids.return_value = {"existing-project"}
    client.head_bucket.side_effect = ClientError(
        {"ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadBucket"
    )
    with pytest.raises(RuntimeError, match="empty replacement"):
        initialize_storage(client, "wrong-bucket", repo)
    client.create_bucket.assert_not_called()


def test_changed_inventory_cannot_be_marked_complete(monkeypatch):
    client, repo = fixture()
    monkeypatch.setattr(
        "src.infra.self_hosted_bootstrap.observe_project_storage_inventory",
        MagicMock(
            side_effect=[
                SimpleNamespace(object_count=0, multipart_count=0, digest="a"),
                SimpleNamespace(object_count=1, multipart_count=0, digest="b"),
            ]
        ),
    )
    with pytest.raises(RuntimeError, match="changed"):
        initialize_storage(client, "fresh", repo)
    repo.complete.assert_not_called()
