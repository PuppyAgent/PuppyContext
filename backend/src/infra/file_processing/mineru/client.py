"""
MineRU Client

Client for interacting with MineRU document parsing API.
"""

import asyncio
import logging
import zipfile
from pathlib import Path

import httpx

from src.infra.file_processing.mineru.config import mineru_config
from src.infra.file_processing.mineru.exceptions import (
    MineRUAPIError,
    MineRUAPIKeyError,
    MineRUTaskFailedError,
    MineRUTimeoutError,
)
from src.infra.file_processing.mineru.schemas import (
    CreateTaskRequest,
    CreateTaskResponse,
    MineRUModelVersion,
    MineRUTaskState,
    ParsedResult,
    TaskStatusResponse,
)

logger = logging.getLogger(__name__)


class MineRUClient:
    """Client for MineRU API."""

    def __init__(self, api_key: str | None = None):
        """
        Initialize MineRU client.

        Args:
            api_key: MineRU API key (defaults to config)

        Raises:
            MineRUAPIKeyError: If API key is not provided
        """
        self.api_key = api_key or mineru_config.mineru_api_key
        if not self.api_key:
            raise MineRUAPIKeyError()

        self.base_url = mineru_config.mineru_api_base_url
        self.poll_interval = mineru_config.mineru_poll_interval
        self.max_wait_time = mineru_config.mineru_max_wait_time
        self.cache_dir = Path(mineru_config.mineru_cache_dir)

        # Ensure cache directory exists
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"MineRUClient initialized with base_url: {self.base_url}")

    async def create_task(
        self,
        file_url: str,
        model_version: MineRUModelVersion = MineRUModelVersion.VLM,
        data_id: str | None = None,
    ) -> CreateTaskResponse:
        """
        Create a MineRU parsing task.

        Args:
            file_url: Presigned URL of the file to parse
            model_version: Model version to use
            data_id: Optional data identifier

        Returns:
            CreateTaskResponse with task_id and trace_id

        Raises:
            MineRUAPIError: If API call fails
        """
        request = CreateTaskRequest(
            url=file_url,
            model_version=model_version,
            data_id=data_id,
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            try:
                logger.info(f"Creating MineRU task for URL: {file_url[:50]}...")
                response = await client.post(
                    f"{self.base_url}/extract/task",
                    json=request.model_dump(),
                    headers=headers,
                    timeout=30.0,
                )

                if response.status_code != 200:
                    error_msg = response.text
                    logger.error(
                        f"MineRU API error: status={response.status_code}, message={error_msg}"
                    )
                    raise MineRUAPIError(response.status_code, error_msg)

                data = response.json()
                result = CreateTaskResponse(**data)

                # Check the code field in API response
                if result.code != 0:
                    error_msg = f"{result.msg} (code: {result.code})"
                    logger.error(f"MineRU API error: {error_msg}")
                    raise MineRUAPIError(result.code, error_msg)

                logger.info(f"MineRU task created: task_id={result.task_id}")
                return result

            except httpx.HTTPError as e:
                logger.error(f"HTTP error during MineRU task creation: {e}")
                raise MineRUAPIError(0, str(e)) from e

    async def get_task_status(self, task_id: str) -> TaskStatusResponse:
        """
        Get status of a MineRU task.

        Args:
            task_id: Task ID to query

        Returns:
            TaskStatusResponse with task status

        Raises:
            MineRUAPIError: If API call fails
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/extract/task/{task_id}",
                    headers=headers,
                    timeout=30.0,
                )

                if response.status_code != 200:
                    error_msg = response.text
                    logger.error(
                        f"MineRU API error: status={response.status_code}, message={error_msg}"
                    )
                    raise MineRUAPIError(response.status_code, error_msg)

                data = response.json()
                result = TaskStatusResponse(**data)

                # Check the code field in API response
                if result.code != 0:
                    error_msg = f"{result.msg} (code: {result.code})"
                    logger.error(f"MineRU API error: {error_msg}")
                    raise MineRUAPIError(result.code, error_msg)

                return result

            except httpx.HTTPError as e:
                logger.error(f"HTTP error during status query: {e}")
                raise MineRUAPIError(0, str(e)) from e

    async def wait_for_completion(self, task_id: str) -> TaskStatusResponse:
        """
        Wait for a MineRU task to complete by polling.

        Args:
            task_id: Task ID to wait for

        Returns:
            TaskStatusResponse when task completes

        Raises:
            MineRUTimeoutError: If task doesn't complete within max_wait_time
            MineRUTaskFailedError: If task fails
        """
        start_time = asyncio.get_event_loop().time()
        logger.info(
            f"Waiting for MineRU task {task_id} to complete (max wait: {self.max_wait_time}s)"
        )

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > self.max_wait_time:
                logger.error(f"MineRU task {task_id} timed out after {self.max_wait_time}s")
                raise MineRUTimeoutError(task_id, self.max_wait_time)

            status = await self.get_task_status(task_id)
            logger.info(
                f"MineRU task {task_id} status: {status.state}, "
                f"progress: {status.extract_progress}%"
            )

            if status.state == MineRUTaskState.COMPLETED:
                logger.info(f"MineRU task {task_id} completed successfully")
                return status

            if status.state == MineRUTaskState.FAILED:
                error_msg = status.err_msg or "Unknown error"
                logger.error(f"MineRU task {task_id} failed: {error_msg}")
                raise MineRUTaskFailedError(task_id, error_msg)

            # Continue waiting
            await asyncio.sleep(self.poll_interval)

    async def download_result(self, task_id: str, zip_url: str) -> Path:
        """
        Download and extract MineRU result ZIP file.

        Args:
            task_id: Task ID for cache directory naming
            zip_url: URL to download the ZIP file

        Returns:
            Path to extracted cache directory

        Raises:
            MineRUAPIError: If download fails
        """
        task_cache_dir = self.cache_dir / task_id
        task_cache_dir.mkdir(parents=True, exist_ok=True)

        zip_path = task_cache_dir / "result.zip"

        # Download ZIP file with extended timeout and retry logic
        # Configure httpx client for better network environment support
        client_kwargs = {
            "timeout": httpx.Timeout(300.0, connect=60.0),
            "follow_redirects": True,
            "verify": True,  # Verify SSL certificates
        }

        # If proxy environment variables are set, httpx will use them automatically
        # Supported: HTTP_PROXY, HTTPS_PROXY, ALL_PROXY

        async with httpx.AsyncClient(**client_kwargs) as client:
            try:
                logger.info(f"Downloading MineRU result for task {task_id} from {zip_url[:100]}...")
                response = await client.get(zip_url)

                if response.status_code != 200:
                    raise MineRUAPIError(
                        response.status_code,
                        f"Failed to download result: {response.text}",
                    )

                # Write ZIP file
                with open(zip_path, "wb") as f:
                    f.write(response.content)

                logger.info(f"Downloaded {len(response.content)} bytes to {zip_path}")

            except httpx.ConnectError as e:
                error_msg = (
                    f"Unable to connect to MineRU CDN server ({zip_url[:50]}...).\n"
                    f"Possible causes:\n"
                    f"  1. Network connection issue or proxy configuration needed\n"
                    f"  2. CDN server temporarily unreachable\n"
                    f"  3. Firewall or security policy blocking the connection\n"
                    f"Suggestion: Check network connection, or configure HTTP_PROXY/HTTPS_PROXY environment variables"
                )
                logger.error(error_msg)
                raise MineRUAPIError(0, error_msg) from e
            except httpx.TimeoutException as e:
                error_msg = f"MineRU result download timed out (300s): {e}"
                logger.error(error_msg)
                raise MineRUAPIError(0, error_msg) from e
            except httpx.HTTPError as e:
                logger.error(f"HTTP error during result download: {e}")
                raise MineRUAPIError(0, str(e)) from e

        # Extract ZIP file
        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(task_cache_dir)
            logger.info(f"Extracted ZIP to {task_cache_dir}")

            # Clean up ZIP file
            zip_path.unlink()

        except zipfile.BadZipFile as e:
            logger.error(f"Bad ZIP file: {e}")
            raise MineRUAPIError(0, f"Bad ZIP file: {e}") from e

        return task_cache_dir

    async def extract_markdown(self, cache_dir: Path) -> str:
        """
        Extract Markdown content from cache directory.

        Args:
            cache_dir: Path to extracted cache directory

        Returns:
            Markdown content as string

        Raises:
            MineRUAPIError: If Markdown file not found
        """
        # Try multiple possible Markdown file paths
        possible_paths = [
            cache_dir / "full.md",  # MineRU new version
            cache_dir / "auto" / "auto.md",  # Legacy version path
            cache_dir / "result.md",  # Fallback path
        ]

        markdown_path = None
        for path in possible_paths:
            if path.exists():
                markdown_path = path
                break

        # If none of the above paths exist, use glob to find any .md file
        if not markdown_path:
            md_files = list(cache_dir.glob("*.md"))
            if md_files:
                markdown_path = md_files[0]
                logger.info(f"Found markdown file via glob: {markdown_path}")
            else:
                # Recursive search
                md_files = list(cache_dir.glob("**/*.md"))
                if md_files:
                    markdown_path = md_files[0]
                    logger.info(f"Found markdown file via recursive glob: {markdown_path}")

        if not markdown_path:
            # List directory contents for debugging
            dir_contents = list(cache_dir.rglob("*"))
            logger.error(
                f"Markdown file not found in {cache_dir}. "
                f"Directory contents: {[str(p.relative_to(cache_dir)) for p in dir_contents[:10]]}"
            )
            raise MineRUAPIError(0, "Markdown file not found in result")

        # Read Markdown content
        with open(markdown_path, encoding="utf-8") as f:
            content = f.read()

        logger.info(f"Extracted Markdown from {markdown_path} ({len(content)} characters)")
        return content

    async def parse_document(
        self,
        file_url: str,
        model_version: MineRUModelVersion = MineRUModelVersion.VLM,
        data_id: str | None = None,
    ) -> ParsedResult:
        """
        Complete pipeline: create task, wait, download, extract Markdown.

        Args:
            file_url: Presigned URL of the file to parse
            model_version: Model version to use
            data_id: Optional data identifier

        Returns:
            ParsedResult with task_id, paths, and Markdown content

        Raises:
            MineRUAPIError: If any step fails
            MineRUTimeoutError: If task times out
            MineRUTaskFailedError: If task fails
        """
        # Create task
        create_response = await self.create_task(file_url, model_version, data_id)
        task_id = create_response.task_id

        # Wait for completion
        status = await self.wait_for_completion(task_id)

        return await self.materialize_task(task_id, status)

    async def materialize_task(
        self,
        task_id: str,
        status: TaskStatusResponse,
    ) -> ParsedResult:
        """Download and materialize an already completed MineRU task."""

        if status.task_id != task_id:
            raise MineRUAPIError(0, "MineRU completion task ID does not match handle")
        if status.state != MineRUTaskState.COMPLETED:
            raise MineRUAPIError(0, f"MineRU task is not complete: {status.state}")
        if not status.full_zip_url:
            raise MineRUAPIError(0, "No ZIP URL in completed task")

        # Download and extract
        cache_dir = await self.download_result(task_id, status.full_zip_url)

        # Extract Markdown
        markdown_content = await self.extract_markdown(cache_dir)

        # Find the actual markdown file path
        possible_paths = [
            cache_dir / "full.md",
            cache_dir / "auto" / "auto.md",
            cache_dir / "result.md",
        ]

        markdown_path = None
        for path in possible_paths:
            if path.exists():
                markdown_path = path
                break

        # If none exist, use glob to find
        if not markdown_path:
            md_files = list(cache_dir.glob("*.md"))
            if md_files:
                markdown_path = md_files[0]
            else:
                md_files = list(cache_dir.glob("**/*.md"))
                if md_files:
                    markdown_path = md_files[0]

        # Default path (if not found)
        if not markdown_path:
            markdown_path = cache_dir / "full.md"

        return ParsedResult(
            task_id=task_id,
            cache_dir=str(cache_dir),
            markdown_path=str(markdown_path),
            markdown_content=markdown_content,
        )
