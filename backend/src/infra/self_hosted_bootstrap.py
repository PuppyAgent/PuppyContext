"""Initialize a NEW self-hosted bucket without rewriting existing inventory.

This command only admits empty projects + empty storage. Existing installations
use the explicit, reviewed inventory operator, which may perform cleanup.
"""

from botocore.exceptions import ClientError

from src.infra.s3.service import S3Service
from src.platform.project.storage_inventory import (
    ProjectStorageInventoryRepository,
    observe_project_storage_inventory,
)


def initialize_storage(client, bucket, repository):
    completed = repository.checkpoint().get("inventory_complete")
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
            raise
        if completed or repository.live_project_ids():
            raise RuntimeError(
                "Existing installation's bucket is missing; refusing to create an empty replacement"
            ) from error
        client.create_bucket(Bucket=bucket)
    if completed:
        return

    # No cleanup operation is reachable from this fresh-install path.
    def require_empty():
        if (
            repository.live_project_ids()
            or client.list_objects_v2(Bucket=bucket, MaxKeys=1).get("Contents")
            or client.list_multipart_uploads(Bucket=bucket, MaxUploads=1).get("Uploads")
        ):
            raise RuntimeError(
                "Existing storage requires explicit operator inventory; nothing was deleted"
            )

    require_empty()
    first = observe_project_storage_inventory(client, bucket)
    second = observe_project_storage_inventory(client, bucket)
    require_empty()
    if first != second or first.object_count or first.multipart_count:
        raise RuntimeError("Storage changed during initialization")
    if repository.finalize_scan(first).get("outcome") not in {"finalized", "already_complete"}:
        raise RuntimeError("Storage initialization was rejected")
    if repository.verify_scan(second).get("outcome") != "verified":
        raise RuntimeError("Storage verification failed")
    if repository.complete().get("outcome") not in {"completed", "replayed"}:
        raise RuntimeError("Storage completion failed")


if __name__ == "__main__":
    storage = S3Service()
    initialize_storage(storage.client, storage.bucket_name, ProjectStorageInventoryRepository())
    print("Self-hosted storage initialization verified")
