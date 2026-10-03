"""ETL任务持久化单元测试

测试ETLTaskRepositorySupabase的功能:
- 创建任务(自动分配ID)
- 读取任务
- 更新任务
- 列表和过滤
- 删除任务
"""

import os
import pytest
from datetime import datetime

from src.platform.upload.tasks.models import ETLTask, ETLTaskStatus, ETLTaskResult
from src.platform.upload.tasks.repository import ETLTaskRepositorySupabase


# 需要真实 Supabase 环境变量；未配置时跳过，避免本地/CI 失败
_REAL_SUPABASE = os.getenv("SUPABASE_URL", "") and "localhost:54321" not in os.getenv("SUPABASE_URL", "")
if not _REAL_SUPABASE:
    pytest.skip("Skip Supabase-dependent tests (SUPABASE_URL/KEY not set)", allow_module_level=True)

# ============= Fixtures =============


@pytest.fixture
def repository():
    """创建任务仓库实例"""
    return ETLTaskRepositorySupabase()


@pytest.fixture
def sample_task():
    """创建测试用的任务"""
    return ETLTask(
        task_id=None,  # Will be assigned by database
        created_by="user-1",
        project_id="proj-1",
        filename="test.pdf",
        rule_id=1,
        status=ETLTaskStatus.PENDING,
        progress=0,
    )


# ============= Tests =============


def test_create_task(repository, sample_task):
    """测试创建任务并分配ID"""
    # Create task
    created_task = repository.create_task(sample_task)
    
    # Verify task was created with ID
    assert created_task.task_id is not None
    assert isinstance(created_task.task_id, int)
    assert created_task.created_by == sample_task.created_by
    assert created_task.project_id == sample_task.project_id
    assert created_task.filename == sample_task.filename
    assert created_task.rule_id == sample_task.rule_id
    assert created_task.status == ETLTaskStatus.PENDING
    
    # Cleanup
    repository.delete_task(created_task.task_id)


def test_get_task(repository, sample_task):
    """测试通过ID获取任务"""
    # Create task first
    created_task = repository.create_task(sample_task)
    task_id = created_task.task_id
    
    # Get task
    retrieved_task = repository.get_task(task_id)
    
    # Verify
    assert retrieved_task is not None
    assert retrieved_task.task_id == task_id
    assert retrieved_task.created_by == created_task.created_by
    assert retrieved_task.filename == created_task.filename
    
    # Cleanup
    repository.delete_task(task_id)


def test_get_nonexistent_task(repository):
    """测试获取不存在的任务"""
    # Try to get non-existent task
    task = repository.get_task(999999)
    
    # Should return None
    assert task is None


def test_update_task_status(repository, sample_task):
    """测试更新任务状态"""
    # Create task
    created_task = repository.create_task(sample_task)
    task_id = created_task.task_id
    
    # Update status
    created_task.update_status(ETLTaskStatus.MINERU_PARSING, progress=20)
    updated_task = repository.update_task(created_task)
    
    # Verify
    assert updated_task is not None
    assert updated_task.status == ETLTaskStatus.MINERU_PARSING
    assert updated_task.progress == 20
    
    # Cleanup
    repository.delete_task(task_id)


def test_update_task_with_result(repository, sample_task):
    """测试更新任务并添加结果"""
    # Create task
    created_task = repository.create_task(sample_task)
    task_id = created_task.task_id
    
    # Mark as completed with result
    result = ETLTaskResult(
        output_path="users/1/processed/1/test.pdf.json",
        output_size=1024,
        processing_time=5.5,
        mineru_task_id="mineru-123",
    )
    created_task.mark_completed(result)
    
    # Update in database
    updated_task = repository.update_task(created_task)
    
    # Verify
    assert updated_task is not None
    assert updated_task.status == ETLTaskStatus.COMPLETED
    assert updated_task.progress == 100
    assert updated_task.result is not None
    assert updated_task.result.output_path == result.output_path
    
    # Cleanup
    repository.delete_task(task_id)


def test_update_task_with_error(repository, sample_task):
    """测试更新任务标记为失败"""
    # Create task
    created_task = repository.create_task(sample_task)
    task_id = created_task.task_id
    
    # Mark as failed
    error_msg = "Test error message"
    created_task.mark_failed(error_msg)
    
    # Update in database
    updated_task = repository.update_task(created_task)
    
    # Verify
    assert updated_task is not None
    assert updated_task.status == ETLTaskStatus.FAILED
    assert updated_task.error == error_msg
    assert updated_task.progress == 0
    
    # Cleanup
    repository.delete_task(task_id)


def test_list_tasks_no_filter(repository):
    """测试列出所有任务"""
    # Create multiple tasks
    task1 = repository.create_task(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-1",
        filename="test1.pdf", rule_id=1
    ))
    task2 = repository.create_task(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-1",
        filename="test2.pdf", rule_id=1
    ))
    
    # List all tasks
    tasks = repository.list_tasks(limit=10)
    
    # Verify we got at least our tasks
    assert len(tasks) >= 2
    task_ids = [t.task_id for t in tasks]
    assert task1.task_id in task_ids
    assert task2.task_id in task_ids
    
    # Cleanup
    repository.delete_task(task1.task_id)
    repository.delete_task(task2.task_id)


def test_list_tasks_filter_by_project(repository):
    """测试按项目ID过滤任务"""
    # Create tasks for different projects
    task1 = repository.create_task(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-1",
        filename="test1.pdf", rule_id=1
    ))
    task2 = repository.create_task(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-2",
        filename="test2.pdf", rule_id=1
    ))
    
    # List tasks for project 1
    tasks = repository.list_tasks(project_id="proj-1", limit=10)
    
    # Verify only project 1's tasks
    for task in tasks:
        assert task.project_id == "proj-1"
    
    # Cleanup
    repository.delete_task(task1.task_id)
    repository.delete_task(task2.task_id)


def test_list_tasks_filter_by_status(repository):
    """测试按状态过滤任务"""
    # Create tasks with different statuses
    task1 = repository.create_task(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-1",
        filename="test1.pdf", rule_id=1, status=ETLTaskStatus.PENDING
    ))
    
    task2 = repository.create_task(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-1",
        filename="test2.pdf", rule_id=1, status=ETLTaskStatus.PENDING
    ))
    
    # Mark task2 as completed
    task2.mark_completed(ETLTaskResult(
        output_path="test.json", output_size=100,
        processing_time=1.0, mineru_task_id="m1"
    ))
    repository.update_task(task2)
    
    # List pending tasks
    pending_tasks = repository.list_tasks(status=ETLTaskStatus.PENDING, limit=10)
    
    # Verify
    pending_ids = [t.task_id for t in pending_tasks if t.project_id == "proj-1"]
    assert task1.task_id in pending_ids
    assert task2.task_id not in pending_ids
    
    # Cleanup
    repository.delete_task(task1.task_id)
    repository.delete_task(task2.task_id)


def test_count_tasks(repository):
    """测试统计任务数量"""
    # Create some tasks
    task1 = repository.create_task(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-1",
        filename="test1.pdf", rule_id=1
    ))
    task2 = repository.create_task(ETLTask(
        task_id=None, created_by="user-1", project_id="proj-1",
        filename="test2.pdf", rule_id=1
    ))
    
    # Count all tasks
    count = repository.count_tasks()
    assert count >= 2
    
    # Count tasks for specific project
    proj_count = repository.count_tasks(project_id="proj-1")
    assert proj_count >= 2
    
    # Cleanup
    repository.delete_task(task1.task_id)
    repository.delete_task(task2.task_id)


def test_delete_task(repository, sample_task):
    """测试删除任务"""
    # Create task
    created_task = repository.create_task(sample_task)
    task_id = created_task.task_id
    
    # Verify it exists
    assert repository.get_task(task_id) is not None
    
    # Delete it
    success = repository.delete_task(task_id)
    assert success is True
    
    # Verify it's gone
    assert repository.get_task(task_id) is None


def test_delete_nonexistent_task(repository):
    """测试删除不存在的任务"""
    # Try to delete non-existent task
    success = repository.delete_task(999999)
    
    # Should return False
    assert success is False


def test_task_to_dict_from_dict(sample_task):
    """测试任务的序列化和反序列化"""
    # Convert to dict
    task_dict = sample_task.to_dict()
    
    # Verify dict structure
    assert task_dict["created_by"] == sample_task.created_by
    assert task_dict["project_id"] == sample_task.project_id
    assert task_dict["filename"] == sample_task.filename
    assert task_dict["rule_id"] == sample_task.rule_id
    assert task_dict["status"] == sample_task.status.value
    
    # Add id for from_dict
    task_dict["id"] = 123
    
    # Convert back to task
    restored_task = ETLTask.from_dict(task_dict)
    
    # Verify
    assert restored_task.task_id == 123
    assert restored_task.created_by == sample_task.created_by
    assert restored_task.project_id == sample_task.project_id
    assert restored_task.filename == sample_task.filename
    assert restored_task.status == sample_task.status


def test_task_with_result_serialization():
    """测试带结果的任务序列化"""
    result = ETLTaskResult(
        output_path="test.json",
        output_size=1024,
        processing_time=5.5,
        mineru_task_id="mineru-123",
    )
    
    task = ETLTask(
        task_id=100,
        created_by="user-1",
        project_id="proj-1",
        filename="test.pdf",
        rule_id=1,
        status=ETLTaskStatus.COMPLETED,
        result=result,
    )
    
    # Serialize
    task_dict = task.to_dict()
    
    # Verify result is included
    assert "result" in task_dict
    assert task_dict["result"]["output_path"] == result.output_path
    
    # Deserialize
    restored_task = ETLTask.from_dict(task_dict)
    
    # Verify result restored
    assert restored_task.result is not None
    assert restored_task.result.output_path == result.output_path
    assert restored_task.result.mineru_task_id == result.mineru_task_id

