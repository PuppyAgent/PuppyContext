"""
ETL Task Queue

Manages asynchronous ETL task queue and workers.
"""

import asyncio
import logging
from collections.abc import Callable

from src.infra.file_processing.tasks.models import ETLTask, ETLTaskStatus
from src.infra.file_processing.tasks.repository import ETLTaskRepositoryBase

logger = logging.getLogger(__name__)


class ETLQueue:
    """Asynchronous task queue for ETL processing."""

    def __init__(
        self,
        task_repository: ETLTaskRepositoryBase,
        max_size: int = 1000,
        worker_count: int = 3,
    ):
        """
        Initialize ETL queue.

        Args:
            task_repository: Repository for task persistence
            max_size: Maximum queue size
            worker_count: Number of worker tasks
        """
        self.task_repository = task_repository
        self.queue: asyncio.Queue[int] = asyncio.Queue(maxsize=max_size)
        self.tasks: dict[int, ETLTask] = {}
        self.worker_count = worker_count
        self.workers: list[asyncio.Task] = []
        self.executor: Callable[[ETLTask], None] | None = None
        self.is_running = False

        logger.info(
            f"ETLQueue initialized with max_size={max_size}, "
            f"worker_count={worker_count}"
        )

    def set_executor(self, executor: Callable[[ETLTask], None]):
        """
        Set the executor function for processing tasks.

        Args:
            executor: Async function that takes ETLTask and processes it
        """
        self.executor = executor
        logger.info("ETL queue executor set")

    async def submit(self, task: ETLTask) -> ETLTask:
        """
        Submit a task to the queue.

        Args:
            task: ETL task to submit (task_id will be assigned if None)

        Returns:
            Task with assigned task_id

        Raises:
            asyncio.QueueFull: If queue is full
        """
        # Create task in database first to get task_id
        task_with_id = self.task_repository.create_task(task)

        # Add to memory cache
        self.tasks[task_with_id.task_id] = task_with_id

        # Add to queue
        await self.queue.put(task_with_id.task_id)

        logger.info(f"Task {task_with_id.task_id} submitted to queue")
        return task_with_id

    def get_task(self, task_id: str | int) -> ETLTask | None:
        """
        Get task by ID from memory cache.

        Args:
            task_id: Task identifier

        Returns:
            ETLTask if found, None otherwise
        """
        # First try memory cache
        task = self.tasks.get(task_id)

        # If not in memory, try database
        if task is None:
            task = self.task_repository.get_task(task_id)
            if task:
                # Cache it in memory
                self.tasks[task_id] = task

        return task

    def list_tasks(
        self,
        project_id: str | None = None,
        status: ETLTaskStatus | None = None,
    ) -> list[ETLTask]:
        """
        List tasks with optional filters.

        Merges task lists from database and in-memory cache.

        Args:
            project_id: Filter by project ID
            status: Filter by status

        Returns:
            List of matching tasks
        """
        # Get task list from database
        db_tasks = self.task_repository.list_tasks(
            project_id=project_id, status=status
        )

        # Create task dictionary (keyed by task_id)
        task_dict = {task.task_id: task for task in db_tasks}

        # Override database status with in-memory task status (in-memory status is more up-to-date)
        for task_id, memory_task in self.tasks.items():
            # Apply filter conditions (use project_id for scoping)
            if project_id is not None and memory_task.project_id != project_id:
                continue
            if status is not None and memory_task.status != status:
                # If in-memory task does not match filter conditions, remove from results
                if task_id in task_dict:
                    del task_dict[task_id]
                continue

            # Override or add with in-memory task
            task_dict[task_id] = memory_task

        return list(task_dict.values())

    async def start_workers(self):
        """Start worker tasks."""
        if self.is_running:
            logger.warning("Workers already running")
            return

        if not self.executor:
            raise RuntimeError("Executor not set. Call set_executor() first.")

        self.is_running = True
        logger.info(f"Starting {self.worker_count} workers")

        for i in range(self.worker_count):
            worker = asyncio.create_task(self._worker(i))
            self.workers.append(worker)

        logger.info(f"{self.worker_count} workers started")

    async def stop_workers(self):
        """Stop all worker tasks gracefully."""
        if not self.is_running:
            logger.warning("Workers not running")
            return

        logger.info("Stopping workers...")
        self.is_running = False

        # Cancel all workers
        for worker in self.workers:
            worker.cancel()

        # Wait for workers to finish
        await asyncio.gather(*self.workers, return_exceptions=True)

        self.workers.clear()
        logger.info("All workers stopped")

    async def _worker(self, worker_id: int):
        """
        Worker task that processes items from the queue.

        Args:
            worker_id: Worker identifier for logging
        """
        logger.info(f"Worker {worker_id} started")

        while self.is_running:
            try:
                # Get task from queue with timeout
                try:
                    task_id = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except TimeoutError:
                    continue

                task = self.tasks.get(task_id)
                if not task:
                    logger.error(f"Worker {worker_id}: Task {task_id} not found")
                    self.queue.task_done()
                    continue

                logger.info(
                    f"Worker {worker_id} processing task {task_id} "
                    f"(created_by={task.created_by}, filename={task.filename})"
                )

                # Execute task
                try:
                    if self.executor:
                        await self.executor(task)

                        # Update database if task completed or failed
                        # (intermediate states are only in memory)
                        if task.status in (
                            ETLTaskStatus.COMPLETED,
                            ETLTaskStatus.FAILED,
                        ):
                            try:
                                self.task_repository.update_task(task)
                                logger.info(
                                    f"Task {task_id} status persisted: {task.status.value}"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to persist task {task_id} status: {e}"
                                )
                except Exception as e:
                    logger.error(
                        f"Worker {worker_id}: Error processing task {task_id}: {e}",
                        exc_info=True,
                    )
                    task.mark_failed(f"Worker error: {e!s}")

                    # Persist failure to database
                    try:
                        self.task_repository.update_task(task)
                    except Exception as db_error:
                        logger.error(
                            f"Failed to persist task {task_id} failure: {db_error}"
                        )

                self.queue.task_done()

            except asyncio.CancelledError:
                logger.info(f"Worker {worker_id} cancelled")
                break
            except Exception as e:
                logger.error(
                    f"Worker {worker_id}: Unexpected error: {e}", exc_info=True
                )
                await asyncio.sleep(1)

        logger.info(f"Worker {worker_id} stopped")

    async def wait_empty(self):
        """Wait for queue to be empty."""
        await self.queue.join()

    def queue_size(self) -> int:
        """Get current queue size."""
        return self.queue.qsize()

    def task_count(self) -> int:
        """Get total number of tasks."""
        return len(self.tasks)
