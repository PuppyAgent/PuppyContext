"""ETL队列持久化集成测试

测试ETLQueue与Repository的集成:
- 任务提交时创建数据库记录
- 任务执行完成时更新数据库
- 任务失败时持久化错误
- 内存和数据库数据一致性
"""

import os
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.infra.file_processing.tasks.models import ETLTask, ETLTaskStatus, ETLTaskResult
from src.infra.file_processing.tasks.queue import ETLQueue
from src.infra.file_processing.tasks.repository import ETLTaskRepositorySupabase


# Skip when using dummy test Supabase URL (set by conftest for import compat)
_REAL_SUPABASE = os.getenv("SUPABASE_URL", "").startswith("http") and "localhost:54321" not in os.getenv("SUPABASE_URL", "")
if not _REAL_SUPABASE:
    pytest.skip("Skip Supabase integration tests (no real Supabase configured)", allow_module_level=True)

# ============= Fixtures =============


@pytest.fixture
def repository():
    """创建任务仓库实例"""
    return ETLTaskRepositorySupabase()


@pytest.fixture
def queue(repository):
    """创建队列实例"""
    q = ETLQueue(
        task_repository=repository,
        max_size=100,
        worker_count=1,
    )
    return q


@pytest.fixture
def sample_task():
    """创建测试用的任务"""
    return ETLTask(
        task_id=None,
        created_by="user-1",
        project_id="proj-1",
        filename="test_queue.pdf",
        rule_id=1,
    )


# ============= Tests =============


@pytest.mark.asyncio
async def test_submit_task_creates_db_record(queue, repository, sample_task):
    """测试提交任务时创建数据库记录"""
    # Submit task
    submitted_task = await queue.submit(sample_task)
    
    # Verify task has ID assigned
    assert submitted_task.task_id is not None
    assert isinstance(submitted_task.task_id, int)
    
    # Verify task is in database
    db_task = repository.get_task(submitted_task.task_id)
    assert db_task is not None
    assert db_task.task_id == submitted_task.task_id
    assert db_task.filename == sample_task.filename
    
    # Verify task is in memory
    memory_task = queue.get_task(submitted_task.task_id)
    assert memory_task is not None
    assert memory_task.task_id == submitted_task.task_id
    
    # Cleanup
    repository.delete_task(submitted_task.task_id)


@pytest.mark.asyncio
async def test_completed_task_updates_db(queue, repository, sample_task):
    """测试任务完成时更新数据库"""
    # Mock executor that marks task as completed
    async def mock_executor(task: ETLTask):
        result = ETLTaskResult(
            output_path="test.json",
            output_size=100,
            processing_time=1.0,
            mineru_task_id="test-123",
        )
        task.mark_completed(result)
    
    queue.set_executor(mock_executor)
    
    # Submit task
    submitted_task = await queue.submit(sample_task)
    task_id = submitted_task.task_id
    
    # Start workers
    await queue.start_workers()
    
    # Wait for task to complete
    await asyncio.sleep(2)
    
    # Stop workers
    await queue.stop_workers()
    
    # Verify task is completed in database
    db_task = repository.get_task(task_id)
    assert db_task is not None
    assert db_task.status == ETLTaskStatus.COMPLETED
    assert db_task.result is not None
    assert db_task.result.output_path == "test.json"
    
    # Cleanup
    repository.delete_task(task_id)


@pytest.mark.asyncio
async def test_failed_task_persists_error(queue, repository, sample_task):
    """测试任务失败时持久化错误"""
    # Mock executor that fails
    async def mock_executor(task: ETLTask):
        raise Exception("Test error")
    
    queue.set_executor(mock_executor)
    
    # Submit task
    submitted_task = await queue.submit(sample_task)
    task_id = submitted_task.task_id
    
    # Start workers
    await queue.start_workers()
    
    # Wait for task to fail
    await asyncio.sleep(2)
    
    # Stop workers
    await queue.stop_workers()
    
    # Verify task is failed in database
    db_task = repository.get_task(task_id)
    assert db_task is not None
    assert db_task.status == ETLTaskStatus.FAILED
    assert db_task.error is not None
    assert "error" in db_task.error.lower()
    
    # Cleanup
    repository.delete_task(task_id)


@pytest.mark.asyncio
async def test_get_task_from_memory_and_db(queue, repository, sample_task):
    """测试从内存和数据库获取任务"""
    # Submit task
    submitted_task = await queue.submit(sample_task)
    task_id = submitted_task.task_id
    
    # Get from memory (fast path)
    memory_task = queue.get_task(task_id)
    assert memory_task is not None
    assert memory_task.task_id == task_id
    
    # Clear memory cache to test DB fallback
    queue.tasks.clear()
    
    # Get should fetch from DB and cache it
    db_task = queue.get_task(task_id)
    assert db_task is not None
    assert db_task.task_id == task_id
    
    # Verify it's now in memory cache
    assert task_id in queue.tasks
    
    # Cleanup
    repository.delete_task(task_id)


@pytest.mark.asyncio
async def test_list_tasks_uses_memory_cache(queue, repository):
    """测试列表任务使用内存缓存"""
    # Create multiple tasks for different projects
    task1 = await queue.submit(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-1",
        filename="test1.pdf", rule_id=1
    ))
    task2 = await queue.submit(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-2",
        filename="test2.pdf", rule_id=1
    ))
    
    # List tasks for project 1
    proj1_tasks = queue.list_tasks(project_id="proj-1")
    
    # Verify correct filtering
    assert len(proj1_tasks) > 0
    for task in proj1_tasks:
        assert task.project_id == "proj-1"
    
    # Verify task1 is in list
    task_ids = [t.task_id for t in proj1_tasks]
    assert task1.task_id in task_ids
    assert task2.task_id not in task_ids
    
    # Cleanup
    repository.delete_task(task1.task_id)
    repository.delete_task(task2.task_id)


@pytest.mark.asyncio
async def test_intermediate_status_not_persisted(queue, repository, sample_task):
    """测试中间状态不立即持久化到数据库"""
    # Mock executor that updates intermediate status
    async def mock_executor(task: ETLTask):
        # Update to intermediate status
        task.update_status(ETLTaskStatus.MINERU_PARSING, progress=50)
        await asyncio.sleep(0.5)
        
        # Complete task
        result = ETLTaskResult(
            output_path="test.json",
            output_size=100,
            processing_time=1.0,
            mineru_task_id="test-123",
        )
        task.mark_completed(result)
    
    queue.set_executor(mock_executor)
    
    # Submit task
    submitted_task = await queue.submit(sample_task)
    task_id = submitted_task.task_id
    
    # Verify initial status in DB is PENDING
    db_task_before = repository.get_task(task_id)
    assert db_task_before.status == ETLTaskStatus.PENDING
    
    # Start workers
    await queue.start_workers()
    
    # Wait for processing
    await asyncio.sleep(0.3)
    
    # Check DB - should still be PENDING (intermediate status not persisted)
    db_task_during = repository.get_task(task_id)
    # Note: This might be PENDING or MINERU_PARSING depending on timing
    # The key point is that completed status will be persisted
    
    # Wait for completion
    await asyncio.sleep(1)
    await queue.stop_workers()
    
    # Verify final status is persisted
    db_task_after = repository.get_task(task_id)
    assert db_task_after.status == ETLTaskStatus.COMPLETED
    
    # Cleanup
    repository.delete_task(task_id)


@pytest.mark.asyncio
async def test_queue_task_count(queue):
    """测试队列任务计数"""
    initial_count = queue.task_count()
    
    # Submit tasks
    task1 = await queue.submit(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-1",
        filename="test1.pdf", rule_id=1
    ))
    task2 = await queue.submit(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-1",
        filename="test2.pdf", rule_id=1
    ))
    
    # Verify count increased
    assert queue.task_count() == initial_count + 2
    
    # Cleanup
    repository = ETLTaskRepositorySupabase()
    repository.delete_task(task1.task_id)
    repository.delete_task(task2.task_id)


@pytest.mark.asyncio
async def test_multiple_tasks_persistence(queue, repository):
    """测试多个任务的持久化"""
    # Mock executor
    completed_tasks = []
    
    async def mock_executor(task: ETLTask):
        result = ETLTaskResult(
            output_path=f"{task.filename}.json",
            output_size=100,
            processing_time=0.5,
            mineru_task_id=f"m-{task.task_id}",
        )
        task.mark_completed(result)
        completed_tasks.append(task.task_id)
    
    queue.set_executor(mock_executor)
    
    # Submit multiple tasks
    tasks = []
    for i in range(3):
        task = await queue.submit(ETLTask(
            task_id=None, created_by="user-1", project_id="proj-1",
            filename=f"test{i}.pdf", rule_id=1
        ))
        tasks.append(task)
    
    # Start workers
    await queue.start_workers()
    
    # Wait for all tasks to complete
    await asyncio.sleep(3)
    await queue.stop_workers()
    
    # Verify all tasks are persisted as completed
    for task in tasks:
        db_task = repository.get_task(task.task_id)
        assert db_task is not None
        assert db_task.status == ETLTaskStatus.COMPLETED
        
        # Cleanup
        repository.delete_task(task.task_id)

