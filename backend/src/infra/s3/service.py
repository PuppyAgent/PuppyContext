"""S3 storage service core business logic"""

import asyncio
import copy
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import boto3
from botocore.exceptions import ClientError

from src.infra.s3.config import s3_settings
from src.infra.s3.exceptions import (
    S3Error,
    S3FileNotFoundError,
    S3FileSizeExceededError,
    S3MultipartError,
    S3OperationError,
)
from src.infra.s3.schemas import (
    BatchDeleteResult,
    FileListItem,
    FileMetadata,
    FileUploadResponse,
    MultipartCompleteResponse,
    MultipartPartListItem,
    MultipartUploadListItem,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
_PROJECT_KEY_PATTERNS = (
    re.compile(rf"^(?:version|projects|shadow-snapshots)/(?P<project>{_SEGMENT})/"),
    re.compile(
        rf"^users/{_SEGMENT}/(?:etl_artifacts|processed|raw)/"
        rf"(?P<project>{_SEGMENT})/"
    ),
)


def project_id_from_owned_s3_key(key: str) -> str | None:
    for pattern in _PROJECT_KEY_PATTERNS:
        matched = pattern.match(key)
        if matched:
            return matched.group("project")
    return None


class S3Service:
    """S3 storage service class"""

    def __init__(self):
        """Initialize S3 service"""
        self.bucket_name = s3_settings.S3_BUCKET_NAME
        self.region = s3_settings.S3_REGION
        self.endpoint_url = s3_settings.S3_ENDPOINT_URL
        self.access_key_id = s3_settings.S3_ACCESS_KEY_ID
        self.secret_access_key = s3_settings.S3_SECRET_ACCESS_KEY

        # File size limit configuration
        self.max_file_size = s3_settings.S3_MAX_FILE_SIZE
        self.multipart_threshold = s3_settings.S3_MULTIPART_THRESHOLD
        self.multipart_chunksize = s3_settings.S3_MULTIPART_CHUNKSIZE

        # Create boto3 client (thread-safe)
        from botocore.config import Config

        # Configure signature version to v4 (required by Supabase Storage)
        config = Config(
            signature_version="s3v4",
            s3={
                "addressing_style": "path"  # Use path style (bucket/key)
            },
            # Increase retry count and timeout to avoid SSL errors
            retries={"max_attempts": 5, "mode": "adaptive"},
            connect_timeout=60,
            read_timeout=300,
            # Disable SSL verification (if using self-signed certificates or dev environment)
            # Note: Production should use valid SSL certificates
            # verify=False
        )

        client_kwargs = {
            "service_name": "s3",
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
            "region_name": self.region,
            "config": config,
        }
        if self.endpoint_url:
            client_kwargs["endpoint_url"] = self.endpoint_url

        self.client = boto3.client(**client_kwargs)
        self._single_attempt_clients = []
        self._single_attempt_lock = threading.Lock()
        self._single_attempt_io = False

    def for_single_attempt_io(self) -> "S3Service":
        """Isolated mutation client; never change the shared retry configuration.

        Callers fencing uncertain I/O cannot infer quiescence from an SDK retry's
        eventual success. Single PUTs also avoid hidden multipart worker/retry
        lifetimes. Read/connect budgets, endpoint and proxy isolation are retained.
        """
        from botocore.config import Config

        source = self.client
        with self._single_attempt_lock:
            client = next(
                (target for origin, target in self._single_attempt_clients if origin is source),
                None,
            )
            if client is None:
                credentials = source._request_signer._credentials
                frozen = credentials.get_frozen_credentials()
                client = boto3.client(
                    "s3",
                    endpoint_url=source.meta.endpoint_url,
                    region_name=source.meta.region_name,
                    aws_access_key_id=frozen.access_key,
                    aws_secret_access_key=frozen.secret_key,
                    aws_session_token=frozen.token,
                    config=source.meta.config.merge(
                        Config(
                            retries={"total_max_attempts": 1, "mode": "standard"},
                        )
                    ),
                )
                # Preserve the source's refreshable/session credentials without
                # re-resolving ambient environment or another endpoint's identity.
                client._request_signer._credentials = credentials
                # Bind to the source client, not merely the service instance.
                # A replaced endpoint must not keep using the previous client;
                # older in-flight clients remain alive until service shutdown.
                self._single_attempt_clients.append((source, client))
        view = copy.copy(self)
        view.client = client
        view._single_attempt_io = True
        view.max_file_size = min(self.max_file_size, 5 * 1024**3)
        view.multipart_threshold = view.max_file_size
        return view

    def close(self) -> None:
        self.client.close()
        for _source, client in self._single_attempt_clients:
            client.close()

    def _check_mutation_attempts(self, response: dict) -> None:
        if not getattr(self, "_single_attempt_io", False):
            return
        attempts = response.get("ResponseMetadata", {}).get("RetryAttempts")
        # Other SDK handlers (such as region redirects) can request a retry.
        # Missing evidence or any retry retains the caller's uncertainty fence.
        if type(attempts) is not int or attempts != 0:
            raise S3OperationError("single-attempt mutation has uncertain retry history")

    async def _run_sync(self, func: Callable[..., T], *args, **kwargs) -> T:
        """
        Wrap a synchronous function for async execution.

        Args:
            func: Synchronous function
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function execution result
        """
        task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # boto3 work in a thread cannot be cancelled. Keep the surrounding
            # Project lease alive until the physical I/O actually stops, then
            # propagate cancellation to the request/worker.
            try:
                await task
            finally:
                raise

    @asynccontextmanager
    async def _project_write_guard(self, key: str, operation: str):
        project_id = project_id_from_owned_s3_key(key)
        if project_id is None:
            yield
            return
        from src.platform.project.write_lease import (
            PHYSICAL_S3_WRITE_LEASE_TTL_SECONDS,
            ProjectWriteLease,
            active_project_write_lease,
        )

        # Physical storage admission is intentionally independent from the
        # logical Product lease. A ContextVar is copied into background tasks,
        # and boto3 threads outlive asyncio cancellation; reusing only the
        # outer project id would therefore permit a late PUT after the outer
        # lease was released. The child claim lives through the real boto
        # future and inherits initializer proof only from a currently-live
        # outer lease.
        parent = active_project_write_lease(project_id)
        proof = parent.initialization_proof() if parent is not None else {}
        async with ProjectWriteLease(
            project_id,
            f"s3.{operation}",
            ttl_seconds=PHYSICAL_S3_WRITE_LEASE_TTL_SECONDS,
            reuse_active=False,
            **proof,
        ):
            yield

    def _handle_client_error(self, error: ClientError, operation: str) -> None:
        """Handle boto3 ClientError"""
        error_code = error.response.get("Error", {}).get("Code", "Unknown")
        error_message = error.response.get("Error", {}).get("Message", str(error))
        http_status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)

        logger.error(
            f"S3 {operation} failed: {error_code} - {error_message} (HTTP {http_status})",
            extra={
                "error_code": error_code,
                "operation": operation,
                "http_status": http_status,
            },
        )

        # Handle various error codes
        if error_code == "NoSuchKey" or error_code == "404" or http_status == 404:
            # 404 could mean file not found or bucket not found
            if operation in ["upload_file", "create_multipart_upload"]:
                raise S3OperationError(
                    f"Bucket '{self.bucket_name}' may not exist or is not accessible: {error_message}"
                )
            else:
                raise S3FileNotFoundError(error_message)
        elif error_code == "NoSuchBucket":
            raise S3OperationError(f"Bucket '{self.bucket_name}' does not exist")
        elif error_code == "AccessDenied" or http_status == 403:
            raise S3OperationError(f"Access denied: {error_message}")
        else:
            raise S3OperationError(f"{operation} failed: {error_message}")

    # ============= File Upload =============

    async def upload_file(
        self,
        key: str,
        content: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> FileUploadResponse:
        """
        Upload a single file to S3 (automatically choosing single or multipart upload).

        Args:
            key: S3 object key
            content: File content
            content_type: Content type
            metadata: Custom metadata

        Returns:
            FileUploadResponse: Upload result

        Raises:
            S3FileSizeExceededError: File size exceeded
            S3OperationError: Upload failed
        """
        # Check file size
        file_size = len(content)
        if file_size > self.max_file_size:
            raise S3FileSizeExceededError(file_size, self.max_file_size)

        # If file size exceeds multipart upload threshold, use multipart upload to avoid SSL errors
        if file_size > self.multipart_threshold:
            logger.info(
                f"File size ({file_size} bytes) exceeds threshold ({self.multipart_threshold} bytes), "
                f"using multipart upload for {key}"
            )
            return await self._upload_file_multipart(key, content, content_type, metadata)

        # Small files use single upload
        try:
            extra_args = {}
            if content_type:
                extra_args["ContentType"] = content_type
            if metadata:
                extra_args["Metadata"] = metadata

            async with self._project_write_guard(key, "put_object"):
                response = await self._run_sync(
                    self.client.put_object,
                    Bucket=self.bucket_name,
                    Key=key,
                    Body=content,
                    **extra_args,
                )

            self._check_mutation_attempts(response)
            logger.info(f"File uploaded successfully: {key} ({file_size} bytes)")

            return FileUploadResponse(
                key=key,
                bucket=self.bucket_name,
                size=file_size,
                etag=response["ETag"].strip('"'),
                content_type=content_type,
            )

        except ClientError as e:
            self._handle_client_error(e, "upload_file")
            raise  # Never reached, but required by type checker

    async def _upload_file_multipart(
        self,
        key: str,
        content: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> FileUploadResponse:
        """
        Upload a large file using multipart upload.

        Args:
            key: S3 object key
            content: File content
            content_type: Content type
            metadata: Custom metadata

        Returns:
            FileUploadResponse: Upload result
        """
        file_size = len(content)
        upload_id = None

        try:
            # 1. Create multipart upload
            upload_id = await self.create_multipart_upload(key, content_type, metadata)

            # 2. Upload parts
            parts = []
            part_number = 1
            offset = 0

            while offset < file_size:
                # Calculate current part size
                chunk_size = min(self.multipart_chunksize, file_size - offset)
                chunk_data = content[offset : offset + chunk_size]

                # Upload part
                etag = await self.upload_part(key, upload_id, part_number, chunk_data)
                parts.append((part_number, etag))

                logger.info(
                    f"Uploaded part {part_number}/{(file_size + self.multipart_chunksize - 1) // self.multipart_chunksize} "
                    f"for {key} ({chunk_size} bytes)"
                )

                offset += chunk_size
                part_number += 1

            # 3. Complete multipart upload
            result = await self.complete_multipart_upload(key, upload_id, parts)

            logger.info(f"Multipart upload completed for {key} ({file_size} bytes)")

            return FileUploadResponse(
                key=key,
                bucket=self.bucket_name,
                size=file_size,
                etag=result.etag,
                content_type=content_type,
            )

        except Exception as e:
            # If upload failed, abort multipart upload
            if upload_id:
                try:
                    await self.abort_multipart_upload(key, upload_id)
                    logger.info(f"Aborted multipart upload for {key}: {upload_id}")
                except Exception as abort_error:
                    logger.error(f"Failed to abort multipart upload for {key}: {abort_error}")

            # Re-raise original error
            raise S3OperationError(f"Multipart upload failed for {key}: {e}")

    async def upload_files_batch(
        self, files: list[tuple[str, bytes, str | None]]
    ) -> list[tuple[str, bool, str | None, FileUploadResponse | None]]:
        """
        Batch upload files.

        Args:
            files: File list [(key, content, content_type), ...]

        Returns:
            list: Result for each file [(key, success, message, response), ...]
        """
        results = []

        for key, content, content_type in files:
            try:
                response = await self.upload_file(key, content, content_type)
                results.append((key, True, None, response))
            except S3Error as e:
                results.append((key, False, str(e), None))
            except Exception as e:
                logger.error(f"Unexpected error uploading {key}: {e}")
                results.append((key, False, f"Unexpected error: {e}", None))

        return results

    # ============= File Download =============

    async def download_file(self, key: str) -> bytes:
        """
        Download file and return full content.

        Args:
            key: S3 object key

        Returns:
            bytes: File content

        Raises:
            S3FileNotFoundError: File not found
            S3OperationError: Download failed
        """
        try:
            response = await self._run_sync(
                self.client.get_object, Bucket=self.bucket_name, Key=key
            )

            # Read full content
            content = await self._run_sync(response["Body"].read)

            logger.info(f"File downloaded successfully: {key} ({len(content)} bytes)")
            return content

        except ClientError as e:
            self._handle_client_error(e, "download_file")
            raise

    async def download_file_range(
        self, key: str, start: int = 0, limit: int | None = None
    ) -> tuple[bytes, int]:
        """
        Download a byte range from a file using S3's native Range support.

        Returns:
            tuple: (content, total_object_size)
        """
        if start < 0:
            start = 0

        try:
            metadata = await self.get_file_metadata(key)
            total = metadata.size
            if start >= total or limit == 0:
                return b"", total

            kwargs = {
                "Bucket": self.bucket_name,
                "Key": key,
            }
            if start > 0 or limit is not None:
                end = total - 1 if limit is None else min(total - 1, start + limit - 1)
                kwargs["Range"] = f"bytes={start}-{end}"

            response = await self._run_sync(self.client.get_object, **kwargs)
            content = await self._run_sync(response["Body"].read)

            logger.info(
                "File range downloaded successfully: %s (%s/%s bytes)",
                key,
                len(content),
                total,
            )
            return content, total

        except ClientError as e:
            self._handle_client_error(e, "download_file")
            raise

    async def download_file_stream(self, key: str, chunk_size: int = 8192) -> AsyncIterator[bytes]:
        """
        Stream download a file.

        Args:
            key: S3 object key
            chunk_size: Size of each chunk

        Yields:
            bytes: File data chunks

        Raises:
            S3FileNotFoundError: File not found
            S3OperationError: Download failed
        """

        def _get_response():
            """Get S3 response"""
            return self.client.get_object(Bucket=self.bucket_name, Key=key)

        try:
            # Get response in thread pool
            response = await self._run_sync(_get_response)
            stream = response["Body"]

            try:
                # Read data chunk by chunk
                while True:
                    chunk = await self._run_sync(stream.read, chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                stream.close()

            logger.info(f"File downloaded successfully: {key}")

        except ClientError as e:
            self._handle_client_error(e, "download_file")
            raise

    # ============= File Existence Check =============

    async def file_exists(self, key: str) -> bool:
        """
        Check if a file exists.

        Args:
            key: S3 object key

        Returns:
            bool: Whether the file exists
        """
        try:
            await self._run_sync(self.client.head_object, Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code in ["404", "NoSuchKey"]:
                return False
            logger.error(f"Error checking file existence for {key}: {e}")
            return False

    # ============= File Deletion =============

    async def delete_file(self, key: str) -> None:
        """
        Delete a single file.

        Args:
            key: S3 object key

        Raises:
            S3FileNotFoundError: File not found
            S3OperationError: Delete failed
        """
        # Strict DELETE is idempotent and must not turn an ambiguous HEAD error
        # into absence. The ordinary compatibility profile keeps its old contract.
        if not getattr(self, "_single_attempt_io", False) and not await self.file_exists(key):
            raise S3FileNotFoundError(key)

        try:
            response = await self._run_sync(
                self.client.delete_object, Bucket=self.bucket_name, Key=key
            )
            self._check_mutation_attempts(response)
            logger.info(f"File deleted successfully: {key}")

        except ClientError as e:
            self._handle_client_error(e, "delete_file")
            raise

    async def delete_files_batch(self, keys: list[str]) -> list[BatchDeleteResult]:
        """
        Batch delete files.

        Args:
            keys: List of keys to delete

        Returns:
            list[BatchDeleteResult]: Delete result for each file
        """
        results = []

        for key in keys:
            try:
                await self.delete_file(key)
                results.append(BatchDeleteResult(key=key, success=True, message=None))
            except S3FileNotFoundError:
                results.append(BatchDeleteResult(key=key, success=False, message="File not found"))
            except S3Error as e:
                results.append(BatchDeleteResult(key=key, success=False, message=str(e)))
            except Exception as e:
                logger.error(f"Unexpected error deleting {key}: {e}")
                results.append(
                    BatchDeleteResult(key=key, success=False, message=f"Unexpected error: {e}")
                )

        return results

    # ============= File Listing =============

    async def list_files(
        self,
        prefix: str = "",
        delimiter: str | None = None,
        max_keys: int = 1000,
        continuation_token: str | None = None,
    ) -> tuple[list[FileListItem], list[str], str | None, bool]:
        """
        List files.

        Args:
            prefix: Key prefix
            delimiter: Delimiter (for simulating folders)
            max_keys: Maximum number of results
            continuation_token: Pagination token

        Returns:
            tuple: (file list, common prefix list, next page token, is truncated)
        """
        try:
            kwargs = {
                "Bucket": self.bucket_name,
                "Prefix": prefix,
                "MaxKeys": max_keys,
            }

            if delimiter:
                kwargs["Delimiter"] = delimiter
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token

            response = await self._run_sync(self.client.list_objects_v2, **kwargs)

            # Parse file list
            files = []
            for obj in response.get("Contents", []):
                files.append(
                    FileListItem(
                        key=obj["Key"],
                        size=obj["Size"],
                        last_modified=obj["LastModified"],
                        etag=obj["ETag"].strip('"'),
                    )
                )

            # Parse common prefixes (folders)
            common_prefixes = [cp["Prefix"] for cp in response.get("CommonPrefixes", [])]

            # Pagination info
            next_token = response.get("NextContinuationToken")
            is_truncated = response.get("IsTruncated", False)

            return files, common_prefixes, next_token, is_truncated

        except ClientError as e:
            self._handle_client_error(e, "list_files")
            raise

    # ============= File Metadata =============

    async def get_file_metadata(self, key: str) -> FileMetadata:
        """
        Get file metadata.

        Args:
            key: S3 object key

        Returns:
            FileMetadata: File metadata

        Raises:
            S3FileNotFoundError: File not found
            S3OperationError: Retrieval failed
        """
        try:
            response = await self._run_sync(
                self.client.head_object, Bucket=self.bucket_name, Key=key
            )

            return FileMetadata(
                key=key,
                bucket=self.bucket_name,
                size=response["ContentLength"],
                etag=response["ETag"].strip('"'),
                last_modified=response["LastModified"],
                content_type=response.get("ContentType"),
                metadata=response.get("Metadata", {}),
            )

        except ClientError as e:
            self._handle_client_error(e, "get_file_metadata")
            raise

    # ============= Presigned URLs =============

    async def generate_presigned_upload_url(
        self, key: str, expires_in: int = 3600, content_type: str | None = None
    ) -> str:
        """
        Generate a presigned upload URL.

        Args:
            key: Target object key
            expires_in: Expiration time (seconds)
            content_type: Restricted content type

        Returns:
            str: Presigned URL
        """
        if project_id_from_owned_s3_key(key) is not None:
            raise S3OperationError(
                "Presigned upload is unavailable for Project-owned storage; "
                "use the leased multipart upload API"
            )
        try:
            params = {"Bucket": self.bucket_name, "Key": key}

            if content_type:
                params["ContentType"] = content_type

            url = await self._run_sync(
                self.client.generate_presigned_url,
                ClientMethod="put_object",
                Params=params,
                ExpiresIn=expires_in,
            )

            logger.info(f"Generated presigned upload URL for: {key}")
            return url

        except ClientError as e:
            self._handle_client_error(e, "generate_presigned_upload_url")
            raise

    async def generate_presigned_download_url(
        self,
        key: str,
        expires_in: int = 3600,
        response_content_disposition: str | None = None,
    ) -> str:
        """
        Generate a presigned download URL.

        Args:
            key: Object key
            expires_in: Expiration time (seconds)
            response_content_disposition: Response Content-Disposition header

        Returns:
            str: Presigned URL
        """
        try:
            params = {"Bucket": self.bucket_name, "Key": key}

            if response_content_disposition:
                params["ResponseContentDisposition"] = response_content_disposition

            url = await self._run_sync(
                self.client.generate_presigned_url,
                ClientMethod="get_object",
                Params=params,
                ExpiresIn=expires_in,
            )

            logger.info(f"Generated presigned download URL for: {key}")
            return url

        except ClientError as e:
            self._handle_client_error(e, "generate_presigned_download_url")
            raise

    # ============= Multipart Upload =============

    async def create_multipart_upload(
        self,
        key: str,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """
        Create a multipart upload.

        Args:
            key: Target object key
            content_type: Content type
            metadata: Custom metadata

        Returns:
            str: Upload session ID

        Raises:
            S3OperationError: Creation failed
        """
        try:
            kwargs = {"Bucket": self.bucket_name, "Key": key}

            if content_type:
                kwargs["ContentType"] = content_type
            if metadata:
                kwargs["Metadata"] = metadata

            async with self._project_write_guard(key, "create_multipart"):
                response = await self._run_sync(self.client.create_multipart_upload, **kwargs)
            upload_id = response["UploadId"]

            logger.info(f"Created multipart upload for {key}: {upload_id}")
            return upload_id

        except ClientError as e:
            self._handle_client_error(e, "create_multipart_upload")
            raise

    async def upload_part(self, key: str, upload_id: str, part_number: int, data: bytes) -> str:
        """
        Upload a single part.

        Args:
            key: Object key
            upload_id: Upload session ID
            part_number: Part number
            data: Part data

        Returns:
            str: Part ETag

        Raises:
            S3InvalidPartSizeError: Invalid part size
            S3MultipartError: Upload failed
        """
        part_size = len(data)

        # Part size validation (except for the last part)
        min_part_size = 5 * 1024 * 1024  # 5MB
        if part_size < min_part_size:
            # Note: Simplified handling here; in practice the last part can be smaller than 5MB
            logger.warning(
                f"Part {part_number} size {part_size} is less than {min_part_size} bytes"
            )

        try:
            async with self._project_write_guard(key, "upload_part"):
                response = await self._run_sync(
                    self.client.upload_part,
                    Bucket=self.bucket_name,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=data,
                )

            etag = response["ETag"].strip('"')
            logger.info(f"Uploaded part {part_number} for {key}: {etag}")
            return etag

        except ClientError as e:
            logger.error(f"Failed to upload part {part_number} for {key}: {e}")
            raise S3MultipartError(f"Failed to upload part {part_number}: {e}")

    async def complete_multipart_upload(
        self, key: str, upload_id: str, parts: list[tuple[int, str]]
    ) -> MultipartCompleteResponse:
        """
        Complete a multipart upload.

        Args:
            key: Object key
            upload_id: Upload session ID
            parts: Parts list [(part_number, etag), ...]

        Returns:
            MultipartCompleteResponse: Completion response

        Raises:
            S3MultipartError: Completion failed
        """
        try:
            multipart_upload = {
                "Parts": [{"PartNumber": part_num, "ETag": etag} for part_num, etag in parts]
            }

            async with self._project_write_guard(key, "complete_multipart"):
                response = await self._run_sync(
                    self.client.complete_multipart_upload,
                    Bucket=self.bucket_name,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload=multipart_upload,
                )

            logger.info(f"Completed multipart upload for {key}")

            # Get file size
            try:
                metadata = await self.get_file_metadata(key)
                file_size = metadata.size
            except Exception:
                file_size = None

            return MultipartCompleteResponse(
                key=key,
                bucket=self.bucket_name,
                etag=response["ETag"].strip('"'),
                size=file_size,
            )

        except ClientError as e:
            logger.error(f"Failed to complete multipart upload for {key}: {e}")
            raise S3MultipartError(f"Failed to complete multipart upload: {e}")

    async def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        """
        Abort a multipart upload.

        Args:
            key: Object key
            upload_id: Upload session ID

        Raises:
            S3MultipartError: Abort failed
        """
        try:
            await self._run_sync(
                self.client.abort_multipart_upload,
                Bucket=self.bucket_name,
                Key=key,
                UploadId=upload_id,
            )

            logger.info(f"Aborted multipart upload for {key}: {upload_id}")

        except ClientError as e:
            logger.error(f"Failed to abort multipart upload for {key}: {e}")
            raise S3MultipartError(f"Failed to abort multipart upload: {e}")

    async def list_multipart_uploads(
        self, prefix: str = "", max_uploads: int = 1000
    ) -> tuple[list[MultipartUploadListItem], str | None]:
        """
        List in-progress multipart uploads.

        Args:
            prefix: Key prefix
            max_uploads: Maximum number of results

        Returns:
            tuple: (upload list, next page token)
        """
        try:
            response = await self._run_sync(
                self.client.list_multipart_uploads,
                Bucket=self.bucket_name,
                Prefix=prefix,
                MaxUploads=max_uploads,
            )

            uploads = []
            for upload in response.get("Uploads", []):
                uploads.append(
                    MultipartUploadListItem(
                        key=upload["Key"],
                        upload_id=upload["UploadId"],
                        initiated=upload["Initiated"],
                    )
                )

            next_token = response.get("NextKeyMarker")
            return uploads, next_token

        except ClientError as e:
            self._handle_client_error(e, "list_multipart_uploads")
            raise

    # ============= Server-side copy & existence =============

    async def copy_object(
        self,
        src_key: str,
        dst_key: str,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """
        Server-side copy from ``src_key`` to ``dst_key`` within the
        same bucket. Bytes never leave S3 — no egress, no client-side
        memory, takes ~50–200ms regardless of object size (within the
        5 GB single-call limit).

        Used by the upload finalize path to move a freshly uploaded
        object from its staging key (``projects/.../uploads/...``) to
        its content-addressed location in the version object store
        (``version/{project}/objects/<hh>/<rest>``) without round-tripping
        the bytes through the FastAPI process. The version write that
        follows sees the blob is already
        there, so no part of the file payload travels over the
        Python process — only the small JSON tree nodes do.

        Args:
            src_key: Object key to copy from (must be in this bucket).
            dst_key: Destination object key in this bucket.
            content_type: Optional override for the destination
                ``Content-Type``. When ``None`` we replace the metadata
                anyway (S3 requires REPLACE if any metadata field is
                set, COPY otherwise — keep things explicit).
            metadata: Optional user metadata to attach to the
                destination. ``None`` keeps the source's metadata.

        Raises:
            S3OperationError: copy failed (source missing, permission
                denied, etc.).

        Note on >5 GB objects: ``CopyObject`` caps at 5 GB per call.
        For larger objects we'd need ``UploadPartCopy`` (multipart
        copy). Punted for now — typical uploads are tens of MB.
        """
        kwargs: dict = {
            "Bucket": self.bucket_name,
            "Key": dst_key,
            "CopySource": {"Bucket": self.bucket_name, "Key": src_key},
        }
        if content_type or metadata:
            kwargs["MetadataDirective"] = "REPLACE"
            if content_type:
                kwargs["ContentType"] = content_type
            if metadata:
                kwargs["Metadata"] = metadata

        try:
            async with self._project_write_guard(dst_key, "copy_object"):
                await self._run_sync(self.client.copy_object, **kwargs)
            logger.info(f"Copied object {src_key} -> {dst_key} (server-side, no egress)")
        except ClientError as e:
            self._handle_client_error(e, "copy_object")
            raise

    async def object_exists(self, key: str) -> bool:
        """
        Return ``True`` iff an object exists at ``key`` in this
        bucket. Cheap (HEAD only). Treats ``NoSuchKey`` / 404 as
        "not present" rather than an error.

        Used by the finalize path to skip ``copy_object`` when the
        destination (which is content-addressed by SHA-256, so any
        prior write of the same bytes lands at the same key) is
        already populated. Saves one S3 round-trip per duplicate
        upload.
        """
        try:
            await self._run_sync(self.client.head_object, Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            http_status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if error_code in {"NoSuchKey", "404", "NotFound"} or http_status == 404:
                return False
            self._handle_client_error(e, "object_exists")
            raise

    # ============= Multipart parts listing =============

    async def list_parts(
        self, key: str, upload_id: str, max_parts: int = 1000
    ) -> tuple[list[MultipartPartListItem], int | None]:
        """
        List uploaded parts.

        Args:
            key: Object key
            upload_id: Upload session ID
            max_parts: Maximum number of results

        Returns:
            tuple: (parts list, next page part number marker)
        """
        try:
            response = await self._run_sync(
                self.client.list_parts,
                Bucket=self.bucket_name,
                Key=key,
                UploadId=upload_id,
                MaxParts=max_parts,
            )

            parts = []
            for part in response.get("Parts", []):
                parts.append(
                    MultipartPartListItem(
                        part_number=part["PartNumber"],
                        size=part["Size"],
                        etag=part["ETag"].strip('"'),
                        last_modified=part["LastModified"],
                    )
                )

            next_marker = response.get("NextPartNumberMarker")
            return parts, next_marker

        except ClientError as e:
            self._handle_client_error(e, "list_parts")
            raise


# Create global service instance (lazy initialization, avoid creating immediately on module import)
_s3_service_instance = None


def get_s3_service_instance() -> S3Service:
    """
    Get S3 service singleton.

    Returns:
        S3Service instance
    """
    global _s3_service_instance
    if _s3_service_instance is None:
        _s3_service_instance = S3Service()
    return _s3_service_instance


# Backward-compat: `s3_service` lazily proxies to the singleton.
# Prefer get_s3_service_instance() / get_s3_service() in new code.
class _S3ServiceProxy:
    def __getattr__(self, name):
        return getattr(get_s3_service_instance(), name)


s3_service = _S3ServiceProxy()
