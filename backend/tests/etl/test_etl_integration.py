"""ETL 完整流程集成测试

测试从上传文件到解析出JSON结构的完整ETL流程。

## 测试流程
1. 创建ETL规则（JSON Schema + System Prompt）
2. 上传测试PDF文件到S3
3. 提交ETL任务
4. 等待任务完成
5. 验证输出的JSON结构

## 运行要求

### 基础要求
1. **必需**：有效的 MINERU_API_KEY 环境变量
2. **必需**：可访问的 MineRU API 端点
3. **必需**：真实的 S3 服务（不能使用 LocalStack）
4. **必需**：LLM API 配置（OpenAI 或兼容的服务）

### 运行方式

#### 使用真实服务运行完整集成测试
```bash
# 设置环境变量
export MINERU_API_KEY="your-mineru-api-key"
export USE_REAL_S3=true
export S3_ENDPOINT_URL="https://xxx.storage.supabase.co"
export S3_BUCKET_NAME="your-bucket"
export S3_REGION="us-east-1"
export S3_ACCESS_KEY_ID="your-key"
export S3_SECRET_ACCESS_KEY="your-secret"

# LLM 配置（OpenAI 或兼容服务）
export LLM_API_KEY="your-llm-api-key"
export LLM_BASE_URL="https://api.openai.com/v1"  # 或其他兼容的端点
export LLM_MODEL="gpt-4o-mini"

# 运行测试
pytest tests/etl/test_etl_integration.py -v -s
```

## 测试说明

这是一个完整的端到端集成测试，会：
- 调用真实的 MineRU API 解析 PDF
- 调用真实的 LLM API 进行数据转换
- 使用真实的 S3 服务存储文件

测试可能需要较长时间（1-3分钟），取决于：
- PDF 文件大小和页数
- MineRU 解析速度
- LLM API 响应速度
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from moto import mock_aws

# 测试文件路径
TEST_PDF_PATH = Path(__file__).parent / "artifact" / "test_pdf.pdf"

# 检查是否配置了必需的 API Keys
MINERU_API_KEY = os.getenv("MINERU_API_KEY")
SKIP_INTEGRATION_TEST = not MINERU_API_KEY

# 这是一份需要真实外部依赖的端到端测试。
# 默认（未配置 MINERU_API_KEY）直接跳过，避免在 CI/本地未配置环境下导入/运行失败。
if SKIP_INTEGRATION_TEST:
    pytest.skip("Skip ETL integration test (MINERU_API_KEY not set)", allow_module_level=True)

from src.infra.file_processing.dependencies import get_etl_service  # noqa: E402
from src.infra.file_processing.mineru.client import MineRUClient  # noqa: E402
from src.infra.file_processing.rules.schemas import RuleCreateRequest  # noqa: E402
from src.infra.file_processing.service import ETLService  # noqa: E402
from src.infra.file_processing.tasks.models import ETLTaskStatus  # noqa: E402
from src.infra.llm.service import LLMService  # noqa: E402
from src.infra.s3.service import S3Service  # noqa: E402


# ============= Fixtures =============


@pytest.fixture
async def s3_service():
    """创建 S3 服务实例
    
    如果设置了 USE_REAL_S3=true 环境变量，将使用真实的 S3 服务。
    否则使用 moto 模拟的 LocalStack。
    """
    use_real_s3 = os.getenv("USE_REAL_S3", "").lower() in ("true", "1", "yes")
    
    if use_real_s3:
        # 使用真实的 S3 服务
        print("\n✓ 使用真实的 S3 服务")
        service = S3Service()
        
        # 验证配置
        print(f"  Bucket: {service.bucket_name}")
        print(f"  Region: {service.region}")
        print(f"  Endpoint: {service.endpoint_url or 'AWS S3'}")
        
        # 检查 bucket 是否可访问
        try:
            await service.list_files(max_keys=1)
            print(f"  ✓ Bucket '{service.bucket_name}' 可访问")
        except Exception as e:
            print(f"  ✗ 警告: 无法访问 bucket '{service.bucket_name}': {e}")
            pytest.skip(f"无法访问 S3 bucket: {e}")
        
        yield service
    else:
        # 使用 moto 模拟的 S3
        print("\n使用 moto 模拟的 S3 服务")
        with mock_aws():
            service = S3Service()
            service.client.create_bucket(Bucket=service.bucket_name)
            yield service


@pytest.fixture
def mineru_client():
    """创建 MineRU 客户端实例"""
    if not MINERU_API_KEY:
        pytest.skip("MINERU_API_KEY not configured")
    return MineRUClient(api_key=MINERU_API_KEY)


@pytest.fixture
def llm_service():
    """创建 LLM 服务实例"""
    return LLMService()


@pytest.fixture
def rule_repository():
    """创建规则仓库实例（使用临时目录）"""
    import tempfile
    import shutil
    
    # 创建临时规则目录
    temp_dir = tempfile.mkdtemp(prefix="etl_rules_test_")
    repository = RuleRepository(rules_dir=temp_dir)
    
    yield repository
    
    # 清理临时目录
    if Path(temp_dir).exists():
        shutil.rmtree(temp_dir)


@pytest.fixture
async def etl_service(s3_service, llm_service, mineru_client, rule_repository):
    """创建 ETL 服务实例"""
    service = ETLService(
        s3_service=s3_service,
        llm_service=llm_service,
        mineru_client=mineru_client,
        rule_repository=rule_repository,
    )
    
    # 启动 ETL workers
    await service.start()
    
    yield service
    
    # 停止 ETL workers
    await service.stop()


# ============= 辅助函数 =============


def create_test_rule(repository: RuleRepository) -> str:
    """创建一个通用的测试规则
    
    返回规则ID
    """
    # 定义一个通用的JSON Schema，适用于提取文档的基本信息
    json_schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "文档标题"
            },
            "summary": {
                "type": "string",
                "description": "文档摘要或主要内容概述"
            },
            "sections": {
                "type": "array",
                "description": "文档的主要章节",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {
                            "type": "string",
                            "description": "章节标题"
                        },
                        "content": {
                            "type": "string",
                            "description": "章节内容摘要"
                        }
                    },
                    "required": ["heading", "content"]
                }
            },
            "key_points": {
                "type": "array",
                "description": "文档中的关键要点",
                "items": {
                    "type": "string"
                }
            }
        },
        "required": ["title", "summary", "sections", "key_points"]
    }
    
    # 定义system prompt
    system_prompt = """你是一个专业的文档分析助手。你的任务是分析提供的Markdown格式文档，并提取关键信息。

请仔细阅读文档内容，然后按照指定的JSON格式提取以下信息：
1. 文档标题（title）：文档的主标题或主题
2. 摘要（summary）：用1-2句话概括文档的主要内容
3. 章节（sections）：识别文档中的主要章节，提取每个章节的标题和内容摘要
4. 关键要点（key_points）：列出文档中的3-5个最重要的要点

确保输出的JSON格式严格符合要求的schema。"""
    
    # 创建规则
    rule_request = RuleCreateRequest(
        name="通用文档信息提取",
        description="从文档中提取标题、摘要、章节和关键要点",
        json_schema=json_schema,
        system_prompt=system_prompt,
    )
    
    rule = repository.create_rule(rule_request)
    print(f"\n✓ 创建测试规则: {rule.name} (ID: {rule.rule_id})")
    
    return rule.rule_id


# ============= 集成测试 =============


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_etl_complete_flow(etl_service, s3_service, rule_repository):
    """测试完整的ETL流程：上传文件 -> 解析 -> 转换 -> 输出JSON
    
    这是一个端到端的集成测试，涵盖：
    1. 创建ETL规则
    2. 上传PDF文件到S3
    3. 提交ETL任务
    4. 等待MineRU解析完成
    5. 等待LLM转换完成
    6. 验证输出的JSON结构
    7. 验证JSON存储到S3
    """
    print("\n" + "=" * 80)
    print("开始ETL完整流程集成测试")
    print("=" * 80)
    
    # ========== 步骤 1: 创建ETL规则 ==========
    print("\n[步骤 1/7] 创建ETL规则...")
    rule_id = create_test_rule(rule_repository)
    
    # ========== 步骤 2: 准备测试文件 ==========
    print("\n[步骤 2/7] 读取测试PDF文件...")
    assert TEST_PDF_PATH.exists(), f"测试PDF文件不存在: {TEST_PDF_PATH}"
    
    with open(TEST_PDF_PATH, "rb") as f:
        pdf_content = f.read()
    
    print(f"✓ PDF文件大小: {len(pdf_content)} bytes")
    
    # ========== 步骤 3: 上传文件到S3 ==========
    print("\n[步骤 3/7] 上传文件到S3...")
    user_id = "test_user_001"
    project_id = "test_project_001"
    filename = "test_document.pdf"
    
    s3_key = f"users/{user_id}/raw/{project_id}/{filename}"
    upload_result = await s3_service.upload_file(
        key=s3_key,
        content=pdf_content,
        content_type="application/pdf",
    )
    
    assert upload_result.key == s3_key
    print(f"✓ 文件已上传: {s3_key}")
    
    # 检查是否使用 LocalStack（如果是，跳过测试）
    presigned_url = await s3_service.generate_presigned_download_url(key=s3_key)
    if "localhost" in presigned_url or "127.0.0.1" in presigned_url:
        pytest.skip(
            "跳过测试: MineRU API 无法访问 LocalStack URL。"
            "请使用真实的 S3 服务进行集成测试。"
        )
    
    # ========== 步骤 4: 提交ETL任务 ==========
    print("\n[步骤 4/7] 提交ETL任务...")
    task = await etl_service.submit_etl_task(
        user_id=user_id,
        project_id=project_id,
        filename=filename,
        rule_id=rule_id,
    )
    
    assert task is not None
    assert task.task_id is not None
    assert task.status == ETLTaskStatus.PENDING
    
    print(f"✓ 任务已提交: {task.task_id}")
    print(f"  状态: {task.status}")
    
    # ========== 步骤 5: 等待任务完成 ==========
    print("\n[步骤 5/7] 等待任务处理...")
    print("  (这可能需要1-3分钟，取决于PDF大小和API响应速度)")
    
    max_wait_time = 300  # 最多等待5分钟
    poll_interval = 5  # 每5秒检查一次
    elapsed_time = 0
    
    last_status = None
    while elapsed_time < max_wait_time:
        # 获取任务状态
        task_status = await etl_service.get_task_status(task.task_id)
        
        # 打印状态变化
        if task_status.status != last_status:
            print(f"  [{elapsed_time}s] 状态: {task_status.status} (进度: {task_status.progress}%)")
            last_status = task_status.status
        
        # 检查是否完成
        if task_status.status == ETLTaskStatus.COMPLETED:
            print(f"\n✓ 任务完成! 总耗时: {elapsed_time}s")
            break
        
        # 检查是否失败
        if task_status.status == ETLTaskStatus.FAILED:
            print(f"\n✗ 任务失败: {task_status.error}")
            pytest.fail(f"ETL任务失败: {task_status.error}")
        
        # 等待下一次检查
        await asyncio.sleep(poll_interval)
        elapsed_time += poll_interval
    
    # 超时检查
    if elapsed_time >= max_wait_time:
        pytest.fail(f"任务超时: 超过{max_wait_time}秒仍未完成")
    
    # 获取最终任务状态
    final_task = await etl_service.get_task_status(task.task_id)
    assert final_task.status == ETLTaskStatus.COMPLETED
    assert final_task.result is not None
    
    # ========== 步骤 6: 验证任务结果 ==========
    print("\n[步骤 6/7] 验证任务结果...")
    result = final_task.result
    
    # 验证结果字段
    assert result.output_path is not None
    assert result.output_size > 0
    assert result.processing_time > 0
    assert result.mineru_task_id is not None
    
    print(f"✓ 输出路径: {result.output_path}")
    print(f"✓ 输出大小: {result.output_size} bytes")
    print(f"✓ 处理时间: {result.processing_time:.2f}s")
    print(f"✓ MineRU任务ID: {result.mineru_task_id}")
    
    # ========== 步骤 7: 验证输出的JSON ==========
    print("\n[步骤 7/7] 验证输出的JSON结构...")
    
    # 从S3下载输出的JSON
    output_content = await s3_service.download_file(key=result.output_path)
    output_json = json.loads(output_content)
    
    print(f"✓ 成功解析JSON输出")
    
    # 验证JSON结构符合规则的schema
    assert "title" in output_json, "缺少 'title' 字段"
    assert "summary" in output_json, "缺少 'summary' 字段"
    assert "sections" in output_json, "缺少 'sections' 字段"
    assert "key_points" in output_json, "缺少 'key_points' 字段"
    
    print(f"✓ JSON结构符合预期schema")
    
    # 验证内容不为空
    assert isinstance(output_json["title"], str) and len(output_json["title"]) > 0
    assert isinstance(output_json["summary"], str) and len(output_json["summary"]) > 0
    assert isinstance(output_json["sections"], list) and len(output_json["sections"]) > 0
    assert isinstance(output_json["key_points"], list) and len(output_json["key_points"]) > 0
    
    print(f"✓ 所有字段包含有效内容")
    
    # 打印提取的信息
    print("\n" + "=" * 80)
    print("提取的文档信息:")
    print("=" * 80)
    print(f"\n标题: {output_json['title']}")
    print(f"\n摘要: {output_json['summary']}")
    print(f"\n章节数量: {len(output_json['sections'])}")
    print(f"关键要点数量: {len(output_json['key_points'])}")
    
    # 打印前3个章节
    print("\n前3个章节:")
    for i, section in enumerate(output_json['sections'][:3], 1):
        print(f"  {i}. {section['heading']}")
        print(f"     {section['content'][:100]}...")
    
    # 打印所有关键要点
    print("\n关键要点:")
    for i, point in enumerate(output_json['key_points'], 1):
        print(f"  {i}. {point}")
    
    print("\n" + "=" * 80)
    print("✓ ETL完整流程测试通过!")
    print("=" * 80)


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_etl_with_custom_rule(etl_service, s3_service, rule_repository):
    """测试使用自定义规则的ETL流程
    
    这个测试创建一个更简单的规则，只提取标题和摘要。
    """
    print("\n" + "=" * 80)
    print("测试自定义规则的ETL流程")
    print("=" * 80)
    
    # 创建简单的规则
    simple_schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "文档标题"
            },
            "summary": {
                "type": "string",
                "description": "文档摘要（50-100字）"
            }
        },
        "required": ["title", "summary"]
    }
    
    simple_prompt = """提取文档的标题和摘要。摘要应该在50-100字之间，准确概括文档的主要内容。"""
    
    rule_request = RuleCreateRequest(
        name="简单文档摘要",
        description="只提取标题和摘要",
        json_schema=simple_schema,
        system_prompt=simple_prompt,
    )
    
    rule = rule_repository.create_rule(rule_request)
    print(f"\n✓ 创建自定义规则: {rule.name}")
    
    # 上传测试文件
    with open(TEST_PDF_PATH, "rb") as f:
        pdf_content = f.read()
    
    user_id = "test_user_002"
    project_id = "test_project_002"
    filename = "simple_test.pdf"
    s3_key = f"users/{user_id}/raw/{project_id}/{filename}"
    
    await s3_service.upload_file(
        key=s3_key,
        content=pdf_content,
        content_type="application/pdf",
    )
    
    # 检查LocalStack
    presigned_url = await s3_service.generate_presigned_download_url(key=s3_key)
    if "localhost" in presigned_url or "127.0.0.1" in presigned_url:
        pytest.skip("跳过测试: 需要真实的S3服务")
    
    # 提交任务
    task = await etl_service.submit_etl_task(
        user_id=user_id,
        project_id=project_id,
        filename=filename,
        rule_id=rule.rule_id,
    )
    
    print(f"✓ 任务已提交: {task.task_id}")
    
    # 等待完成
    max_wait_time = 300
    poll_interval = 5
    elapsed_time = 0
    
    while elapsed_time < max_wait_time:
        task_status = await etl_service.get_task_status(task.task_id)
        
        if task_status.status == ETLTaskStatus.COMPLETED:
            print(f"✓ 任务完成! 耗时: {elapsed_time}s")
            break
        
        if task_status.status == ETLTaskStatus.FAILED:
            pytest.fail(f"任务失败: {task_status.error}")
        
        await asyncio.sleep(poll_interval)
        elapsed_time += poll_interval
    
    if elapsed_time >= max_wait_time:
        pytest.fail("任务超时")
    
    # 验证输出
    final_task = await etl_service.get_task_status(task.task_id)
    output_content = await s3_service.download_file(key=final_task.result.output_path)
    output_json = json.loads(output_content)
    
    # 验证简单的schema
    assert "title" in output_json
    assert "summary" in output_json
    assert isinstance(output_json["title"], str)
    assert isinstance(output_json["summary"], str)
    assert len(output_json["summary"]) >= 20  # 至少20个字符
    
    print(f"\n提取结果:")
    print(f"  标题: {output_json['title']}")
    print(f"  摘要: {output_json['summary']}")
    
    print("\n✓ 自定义规则测试通过!")


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_etl_task_status_tracking(etl_service, s3_service, rule_repository):
    """测试ETL任务状态追踪
    
    验证任务在各个阶段的状态变化。
    """
    print("\n" + "=" * 80)
    print("测试ETL任务状态追踪")
    print("=" * 80)
    
    # 创建规则
    rule_id = create_test_rule(rule_repository)
    
    # 上传文件
    with open(TEST_PDF_PATH, "rb") as f:
        pdf_content = f.read()
    
    user_id = "test_user_003"
    project_id = "test_project_003"
    filename = "status_test.pdf"
    s3_key = f"users/{user_id}/raw/{project_id}/{filename}"
    
    await s3_service.upload_file(
        key=s3_key,
        content=pdf_content,
        content_type="application/pdf",
    )
    
    # 检查LocalStack
    presigned_url = await s3_service.generate_presigned_download_url(key=s3_key)
    if "localhost" in presigned_url or "127.0.0.1" in presigned_url:
        pytest.skip("跳过测试: 需要真实的S3服务")
    
    # 提交任务
    task = await etl_service.submit_etl_task(
        user_id=user_id,
        project_id=project_id,
        filename=filename,
        rule_id=rule_id,
    )
    
    # 追踪状态变化
    status_history = []
    max_wait_time = 300
    poll_interval = 2
    elapsed_time = 0
    
    while elapsed_time < max_wait_time:
        task_status = await etl_service.get_task_status(task.task_id)
        
        # 记录状态变化
        if not status_history or status_history[-1][0] != task_status.status:
            status_history.append((task_status.status, task_status.progress, elapsed_time))
            print(f"  [{elapsed_time}s] {task_status.status} - 进度: {task_status.progress}%")
        
        if task_status.status == ETLTaskStatus.COMPLETED:
            break
        
        if task_status.status == ETLTaskStatus.FAILED:
            pytest.fail(f"任务失败: {task_status.error}")
        
        await asyncio.sleep(poll_interval)
        elapsed_time += poll_interval
    
    if elapsed_time >= max_wait_time:
        pytest.fail("任务超时")
    
    # 验证状态历史
    print("\n状态变化历史:")
    for status, progress, time in status_history:
        print(f"  {time}s: {status} ({progress}%)")
    
    # 应该至少经历以下状态
    statuses = [s[0] for s in status_history]
    assert ETLTaskStatus.PENDING in statuses or ETLTaskStatus.MINERU_PARSING in statuses
    assert ETLTaskStatus.COMPLETED in statuses
    
    # 验证进度是递增的（除了FAILED状态）
    for i in range(len(status_history) - 1):
        if status_history[i+1][0] != ETLTaskStatus.FAILED:
            assert status_history[i+1][1] >= status_history[i][1], "进度应该递增"
    
    # 最终进度应该是100%
    assert status_history[-1][1] == 100, "完成状态的进度应该是100%"
    
    print("\n✓ 状态追踪测试通过!")


# ============= 错误处理测试 =============


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_etl_with_nonexistent_rule(etl_service, s3_service):
    """测试使用不存在的规则提交任务"""
    print("\n测试不存在的规则...")
    
    from src.infra.file_processing.exceptions import RuleNotFoundError
    
    # 尝试使用不存在的规则ID
    with pytest.raises(RuleNotFoundError):
        await etl_service.submit_etl_task(
            user_id="test_user",
            project_id="test_project",
            filename="test.pdf",
            rule_id="nonexistent-rule-id-12345",
        )
    
    print("✓ 正确抛出了 RuleNotFoundError")


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_etl_with_nonexistent_file(etl_service, rule_repository):
    """测试处理不存在的S3文件
    
    注意：这个测试会提交任务，但任务会在MineRU阶段失败。
    """
    print("\n测试不存在的文件...")
    
    # 创建规则
    rule_id = create_test_rule(rule_repository)
    
    # 提交任务（文件不存在）
    task = await etl_service.submit_etl_task(
        user_id="test_user",
        project_id="test_project",
        filename="nonexistent_file.pdf",
        rule_id=rule_id,
    )
    
    # 等待任务失败
    max_wait_time = 60
    poll_interval = 2
    elapsed_time = 0
    
    while elapsed_time < max_wait_time:
        task_status = await etl_service.get_task_status(task.task_id)
        
        if task_status.status == ETLTaskStatus.FAILED:
            print(f"✓ 任务正确失败: {task_status.error}")
            assert task_status.error is not None
            return
        
        await asyncio.sleep(poll_interval)
        elapsed_time += poll_interval
    
    pytest.fail("任务应该失败但超时了")


if __name__ == "__main__":
    # 允许直接运行此文件进行快速测试
    pytest.main([__file__, "-v", "-s"])

