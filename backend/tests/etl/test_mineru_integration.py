"""MineRU 客户端集成测试

测试 MineRU 客户端与真实 API 的交互流程。

## 测试要求

1. **必需**：有效的 MINERU_API_KEY 环境变量
2. **必需**：可访问的 MineRU API 端点
3. **重要**：使用真实的 S3 服务（不能使用 LocalStack）
   - MineRU API 需要从公网访问 S3 URL
   - LocalStack 的 localhost URL 无法被外部 API 访问
   - 支持 AWS S3 或 Supabase Storage (S3兼容)

## 运行方式

### 基础测试（不需要真实 API，使用 moto 模拟）
```bash
pytest tests/etl/test_mineru_integration.py::test_mineru_client_initialization -v
pytest tests/etl/test_mineru_integration.py::test_mineru_config_limits -v
```

### 完整集成测试（需要真实 MineRU API + 真实 S3）

#### 使用 AWS S3
```bash
export MINERU_API_KEY="your-api-key"
export USE_REAL_S3=true
export S3_ENDPOINT_URL=""  # 空字符串表示使用 AWS S3
export S3_BUCKET_NAME="your-bucket"
export S3_REGION="us-east-1"
export S3_ACCESS_KEY_ID="your-aws-key"
export S3_SECRET_ACCESS_KEY="your-aws-secret"

pytest tests/etl/test_mineru_integration.py -v -s
```

#### 使用 Supabase Storage (S3兼容)
```bash
export MINERU_API_KEY="your-api-key"
export USE_REAL_S3=true
export S3_ENDPOINT_URL="https://xxx.storage.supabase.co"
export S3_BUCKET_NAME="your-bucket"
export S3_REGION="us-east-1"
export S3_ACCESS_KEY_ID="your-project-ref"
export S3_SECRET_ACCESS_KEY="your-anon-key"

pytest tests/etl/test_mineru_integration.py -v -s
```

## 常见问题

### 404 错误
如果看到 404 错误，可能是：
- Bucket 名称错误
- Bucket 不存在或未正确配置
- 权限不足（需要有 bucket 的读写权限）
- Endpoint URL 配置错误

### SSL 错误
如果看到 SSL 错误，可能是：
- 网络连接问题
- 防火墙或代理设置
- Endpoint URL 格式不正确

## 测试说明

如果没有配置 MINERU_API_KEY，大部分测试将被跳过。
使用 LocalStack 的测试会因为 MineRU API 无法访问 localhost 而被自动跳过。
"""

import os
from pathlib import Path

import pytest
from moto import mock_aws

from src.infra.file_processing.mineru.client import MineRUClient
from src.infra.file_processing.mineru.config import mineru_config
from src.infra.file_processing.mineru.exceptions import (
    MineRUAPIError,
    MineRUAPIKeyError,
    MineRUTaskFailedError,
    MineRUTimeoutError,
)
from src.infra.file_processing.mineru.schemas import MineRUModelVersion, MineRUTaskState
from src.infra.s3.service import S3Service

# 测试文件路径
TEST_PDF_PATH = Path(__file__).parent / "artifact" / "test_pdf.pdf"

# 检查是否配置了 MineRU API Key
MINERU_API_KEY = os.getenv("MINERU_API_KEY")
SKIP_INTEGRATION_TEST = not MINERU_API_KEY


# ============= Fixtures =============


@pytest.fixture
def mineru_client():
    """创建 MineRU 客户端实例"""
    if not MINERU_API_KEY:
        pytest.skip("MINERU_API_KEY not configured")
    return MineRUClient(api_key=MINERU_API_KEY)


@pytest.fixture
async def s3_service():
    """创建 S3 服务实例
    
    如果设置了 USE_REAL_S3=true 环境变量，将使用真实的 S3 服务。
    否则使用 moto 模拟的 LocalStack。
    """
    use_real_s3 = os.getenv("USE_REAL_S3", "").lower() in ("true", "1", "yes")
    
    if use_real_s3:
        # 使用真实的 S3 服务（从 .env 读取配置）
        print("\n使用真实的 S3 服务")
        service = S3Service()
        
        # 验证配置
        print(f"  Bucket: {service.bucket_name}")
        print(f"  Region: {service.region}")
        print(f"  Endpoint: {service.endpoint_url or 'AWS S3'}")
        
        # 尝试访问 bucket 以验证配置是否正确
        try:
            # 检查 bucket 是否可访问（通过列出文件）
            await service.list_files(max_keys=1)
            print(f"  ✓ Bucket '{service.bucket_name}' 可访问")
        except Exception as e:
            print(f"  ✗ 警告: 无法访问 bucket '{service.bucket_name}': {e}")
            print(f"  请检查 S3 配置是否正确")
        
        yield service
    else:
        # 使用 moto 模拟的 S3
        print("\n使用 moto 模拟的 S3 服务")
        with mock_aws():
            service = S3Service()
            # 创建测试桶
            service.client.create_bucket(Bucket=service.bucket_name)
            yield service


# ============= 客户端初始化测试 =============


def test_mineru_client_initialization():
    """测试 MineRU 客户端初始化"""
    if not MINERU_API_KEY:
        pytest.skip("MINERU_API_KEY not configured")

    client = MineRUClient(api_key=MINERU_API_KEY)

    assert client.api_key == MINERU_API_KEY
    assert client.base_url == mineru_config.mineru_api_base_url
    assert client.poll_interval == mineru_config.mineru_poll_interval
    assert client.max_wait_time == mineru_config.mineru_max_wait_time
    assert client.cache_dir.exists()


def test_mineru_client_initialization_without_api_key():
    """测试没有 API Key 时的初始化失败"""
    # 临时修改 config 来模拟没有 API key 的情况
    from src.infra.file_processing.mineru.config import mineru_config
    
    original_key = mineru_config.mineru_api_key
    mineru_config.mineru_api_key = None
    
    try:
        with pytest.raises(MineRUAPIKeyError) as exc_info:
            # 传入 None 会使用 config，此时 config 也是 None
            MineRUClient(api_key=None)

        assert "API key is missing" in str(exc_info.value)
    
    finally:
        # 恢复原始配置
        mineru_config.mineru_api_key = original_key


def test_mineru_client_cache_directory_creation():
    """测试缓存目录自动创建"""
    if not MINERU_API_KEY:
        pytest.skip("MINERU_API_KEY not configured")

    # 使用临时缓存目录
    import tempfile
    temp_dir = tempfile.mkdtemp()
    cache_dir = Path(temp_dir) / "test_cache"

    # 确保目录不存在
    assert not cache_dir.exists()

    # 创建客户端（应该自动创建缓存目录）
    original_cache = mineru_config.mineru_cache_dir
    mineru_config.mineru_cache_dir = str(cache_dir)

    try:
        client = MineRUClient(api_key=MINERU_API_KEY)
        assert client.cache_dir.exists()
        assert client.cache_dir == cache_dir

    finally:
        # 清理并恢复配置
        if cache_dir.exists():
            import shutil
            shutil.rmtree(cache_dir)
        mineru_config.mineru_cache_dir = original_cache


# ============= 完整流程集成测试 =============


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_parse_document_complete_flow(mineru_client, s3_service):
    """测试完整的文档解析流程
    
    这个测试覆盖整个 ETL 流程：
    1. 将测试 PDF 上传到 S3
    2. 生成预签名 URL
    3. 使用 MineRU 解析文档
    4. 验证解析结果
    
    注意：这是一个真实的集成测试，会调用 MineRU API。
    警告：如果使用 LocalStack，MineRU API 无法访问 localhost URL，测试会失败。
    """
    # 步骤 1: 读取测试 PDF 文件
    assert TEST_PDF_PATH.exists(), f"Test PDF not found: {TEST_PDF_PATH}"

    with open(TEST_PDF_PATH, "rb") as f:
        pdf_content = f.read()

    print(f"\n测试 PDF 文件大小: {len(pdf_content)} bytes")

    # 步骤 2: 上传到 S3
    s3_key = "test/mineru/test_pdf.pdf"
    upload_result = await s3_service.upload_file(
        key=s3_key,
        content=pdf_content,
        content_type="application/pdf",
    )

    assert upload_result.key == s3_key
    print(f"已上传到 S3: {s3_key}")

    # 步骤 3: 生成预签名 URL
    presigned_url = await s3_service.generate_presigned_download_url(
        key=s3_key,
    )

    assert presigned_url is not None
    assert isinstance(presigned_url, str)
    print(f"生成预签名 URL: {presigned_url}...")

    # 检查是否使用 LocalStack
    if "localhost" in presigned_url or "127.0.0.1" in presigned_url:
        pytest.skip(
            "跳过测试: MineRU API 无法访问 LocalStack URL。"
            "请使用真实的 AWS S3 进行集成测试。"
        )

    # 步骤 4: 使用 MineRU 解析文档
    print("\n开始 MineRU 解析...")
    parsed_result = await mineru_client.parse_document(
        file_url=presigned_url,
        model_version=MineRUModelVersion.VLM,
        data_id="test_integration",
    )

    # 步骤 5: 验证解析结果
    assert parsed_result is not None
    assert parsed_result.task_id is not None
    assert parsed_result.cache_dir is not None
    assert parsed_result.markdown_path is not None
    assert parsed_result.markdown_content is not None

    print(f"\n解析完成!")
    print(f"Task ID: {parsed_result.task_id}")
    print(f"缓存目录: {parsed_result.cache_dir}")
    print(f"Markdown 路径: {parsed_result.markdown_path}")
    print(f"Markdown 内容长度: {len(parsed_result.markdown_content)} 字符")

    # 验证 Markdown 内容不为空
    assert len(parsed_result.markdown_content) > 0

    # 验证缓存目录存在
    cache_dir = Path(parsed_result.cache_dir)
    assert cache_dir.exists()
    assert cache_dir.is_dir()

    # 验证 Markdown 文件存在
    markdown_path = Path(parsed_result.markdown_path)
    assert markdown_path.exists()
    assert markdown_path.is_file()

    # 打印前 500 个字符的 Markdown 内容
    print(f"\nMarkdown 内容预览:")
    print("=" * 80)
    print(parsed_result.markdown_content[:500])
    print("=" * 80)


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_create_task_and_poll(mineru_client, s3_service):
    """测试创建任务和轮询状态
    
    这个测试分步骤测试：
    1. 创建 MineRU 任务
    2. 轮询任务状态
    3. 下载结果
    4. 提取 Markdown
    
    警告：如果使用 LocalStack，MineRU API 无法访问 localhost URL，测试会失败。
    """
    # 准备测试文件
    with open(TEST_PDF_PATH, "rb") as f:
        pdf_content = f.read()

    # 上传到 S3
    s3_key = "test/mineru/test_poll.pdf"
    await s3_service.upload_file(
        key=s3_key,
        content=pdf_content,
        content_type="application/pdf",
    )

    # 生成预签名 URL
    presigned_url = await s3_service.generate_presigned_download_url(
        key=s3_key,
        expires_in=3600,
    )

    # 检查是否使用 LocalStack
    if "localhost" in presigned_url or "127.0.0.1" in presigned_url:
        pytest.skip(
            "跳过测试: MineRU API 无法访问 LocalStack URL。"
            "请使用真实的 AWS S3 进行集成测试。"
        )

    # 步骤 1: 创建任务
    print("\n创建 MineRU 任务...")
    create_response = await mineru_client.create_task(
        file_url=presigned_url,
        model_version=MineRUModelVersion.VLM,
        data_id="test_poll",
    )

    assert create_response is not None
    assert create_response.task_id is not None
    assert create_response.trace_id is not None

    task_id = create_response.task_id
    print(f"任务已创建: {task_id}")

    # 步骤 2: 轮询任务状态
    print(f"等待任务完成...")
    status = await mineru_client.wait_for_completion(task_id)

    assert status is not None
    assert status.state == MineRUTaskState.COMPLETED
    assert status.full_zip_url is not None

    print(f"任务完成! ZIP URL: {status.full_zip_url[:100]}...")

    # 步骤 3: 下载结果
    print("下载解析结果...")
    cache_dir = await mineru_client.download_result(task_id, status.full_zip_url)

    assert cache_dir.exists()
    assert cache_dir.is_dir()

    print(f"结果已下载到: {cache_dir}")

    # 步骤 4: 提取 Markdown
    print("提取 Markdown 内容...")
    markdown_content = await mineru_client.extract_markdown(cache_dir)

    assert markdown_content is not None
    assert len(markdown_content) > 0

    print(f"Markdown 内容长度: {len(markdown_content)} 字符")


# ============= 错误处理测试 =============


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_create_task_with_invalid_url(mineru_client):
    """测试使用无效 URL 创建任务
    
    注意: MineRU API 在创建任务时不会立即验证 URL 的有效性,
    只会返回 task_id。URL 的验证会在后续处理时进行,
    此时任务状态会变为 failed。
    """
    invalid_url = "https://invalid-url-that-does-not-exist.com/file.pdf"

    # MineRU API 会接受任何格式正确的 URL,不会在创建时抛出错误
    # 错误会在任务执行时才出现
    response = await mineru_client.create_task(
        file_url=invalid_url,
        model_version=MineRUModelVersion.VLM,
    )
    
    # 任务创建应该成功
    assert response is not None
    assert response.task_id is not None
    
    # 但是轮询状态时应该会失败
    with pytest.raises(MineRUTaskFailedError):
        await mineru_client.wait_for_completion(response.task_id)


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_get_task_status_invalid_task_id(mineru_client):
    """测试查询不存在的任务状态"""
    invalid_task_id = "nonexistent-task-id-12345"

    with pytest.raises(MineRUAPIError) as exc_info:
        await mineru_client.get_task_status(invalid_task_id)

    # 应该返回 404 或其他错误状态码
    assert exc_info.value.status_code != 200 or exc_info.value.status_code == 0


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_wait_for_completion_timeout(mineru_client):
    """测试任务超时
    
    注意：这个测试需要修改超时时间配置，否则会等待很久。
    """
    # 临时设置较短的超时时间
    original_timeout = mineru_client.max_wait_time
    mineru_client.max_wait_time = 5  # 5秒超时

    try:
        # 使用一个假的任务ID，它会一直处于 pending 状态
        fake_task_id = "fake-pending-task-id"

        # 这应该会超时
        with pytest.raises((MineRUTimeoutError, MineRUAPIError)):
            await mineru_client.wait_for_completion(fake_task_id)

    finally:
        # 恢复原始超时时间
        mineru_client.max_wait_time = original_timeout


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_download_result_invalid_url(mineru_client):
    """测试下载无效的结果 URL"""
    task_id = "test_task"
    invalid_zip_url = "https://invalid-url.com/nonexistent.zip"

    with pytest.raises(MineRUAPIError) as exc_info:
        await mineru_client.download_result(task_id, invalid_zip_url)

    assert "download" in str(exc_info.value).lower() or exc_info.value.status_code != 200


def test_extract_markdown_missing_file(mineru_client):
    """测试从不存在的缓存目录提取 Markdown"""
    if not MINERU_API_KEY:
        pytest.skip("MINERU_API_KEY not configured")

    import tempfile
    nonexistent_dir = Path(tempfile.mkdtemp()) / "nonexistent"

    # 使用 pytest.raises 并指定 async 上下文
    import asyncio
    with pytest.raises(MineRUAPIError) as exc_info:
        asyncio.run(mineru_client.extract_markdown(nonexistent_dir))

    assert "not found" in str(exc_info.value).lower()


# ============= 不同模型版本测试 =============


@pytest.mark.skipif(SKIP_INTEGRATION_TEST, reason="MINERU_API_KEY not configured")
@pytest.mark.asyncio
async def test_parse_document_different_models(mineru_client, s3_service):
    """测试使用不同的模型版本解析文档
    
    注意：这个测试会调用多次 API，可能需要较长时间。
    如果 API 配额有限，可以跳过此测试。
    """
    pytest.skip("跳过多模型测试以节省 API 配额")

    # 准备测试文件
    with open(TEST_PDF_PATH, "rb") as f:
        pdf_content = f.read()

    # 测试不同的模型版本
    model_versions = [
        MineRUModelVersion.VLM,
        MineRUModelVersion.DOCUMENT,
        MineRUModelVersion.OCR,
    ]

    for model_version in model_versions:
        print(f"\n测试模型版本: {model_version}")

        # 上传到 S3
        s3_key = f"test/mineru/{model_version}_test.pdf"
        await s3_service.upload_file(
            key=s3_key,
            content=pdf_content,
            content_type="application/pdf",
        )

        # 生成预签名 URL
        presigned_url = await s3_service.generate_presigned_download_url(
            key=s3_key,
            expires_in=3600,
        )

        # 解析文档
        parsed_result = await mineru_client.parse_document(
            file_url=presigned_url,
            model_version=model_version,
            data_id=f"test_{model_version}",
        )

        # 验证结果
        assert parsed_result is not None
        assert parsed_result.task_id is not None
        assert len(parsed_result.markdown_content) > 0

        print(f"模型 {model_version} 解析成功，Markdown 长度: {len(parsed_result.markdown_content)}")


# ============= 性能和限制测试 =============


def test_mineru_config_limits():
    """测试 MineRU 配置限制"""
    # 验证配置的合理性
    assert mineru_config.mineru_max_file_size_mb > 0
    assert mineru_config.mineru_max_file_size_mb <= 200  # MineRU 限制

    assert mineru_config.mineru_max_pages > 0
    assert mineru_config.mineru_max_pages <= 600  # MineRU 限制

    assert mineru_config.mineru_poll_interval > 0
    assert mineru_config.mineru_max_wait_time > 0

    print(f"\nMineRU 配置:")
    print(f"  最大文件大小: {mineru_config.mineru_max_file_size_mb} MB")
    print(f"  最大页数: {mineru_config.mineru_max_pages}")
    print(f"  轮询间隔: {mineru_config.mineru_poll_interval} 秒")
    print(f"  最大等待时间: {mineru_config.mineru_max_wait_time} 秒")

