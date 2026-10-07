"""JMESPath 查询功能测试

测试 Table Service 中的 JMESPath 查询功能
"""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from src.content.table.models import Table
from src.content.table.service import TableService
from src.exceptions import BusinessException, NotFoundException

# ============= Fixtures =============


@pytest.fixture
def mock_repository():
    """Mock Repository"""
    repo = Mock()
    return repo


@pytest.fixture
def table_service(mock_repository):
    """Isolate query semantics with an explicit content-reader double.

    Native authorization/storage are verified by the engine suites; this fixture
    does not claim a production fallback to the table metadata row.
    """
    svc = TableService(repo=mock_repository, repo_manager=Mock())
    svc._read_table_data = Mock(side_effect=lambda table: table.data)
    return svc


@pytest.fixture
def sample_table_with_data():
    """示例表格，包含复杂的嵌套数据"""
    return Table(
        id="1",
        name="测试表格",
        project_id="1",
        description="测试用表格",
        data={
            "users": [
                {"name": "Alice", "age": 30, "city": "Beijing"},
                {"name": "Bob", "age": 25, "city": "Shanghai"},
                {"name": "Charlie", "age": 35, "city": "Beijing"},
            ],
            "products": [
                {"id": 1, "name": "Laptop", "price": 5000, "stock": 10},
                {"id": 2, "name": "Mouse", "price": 100, "stock": 50},
                {"id": 3, "name": "Keyboard", "price": 300, "stock": 30},
            ],
            "metadata": {"version": "1.0", "author": "admin"},
        },
        created_at=datetime.now(UTC),
    )


# ============= JMESPath 查询基本功能测试 =============


def test_query_simple_field(table_service, mock_repository, sample_table_with_data):
    """测试查询简单字段"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="metadata.version"
    )

    assert result == "1.0"


def test_query_array_length(table_service, mock_repository, sample_table_with_data):
    """测试查询数组长度"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="length(users)"
    )

    assert result == 3


def test_query_array_filter(table_service, mock_repository, sample_table_with_data):
    """测试过滤数组元素"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="users[?age > `28`]"
    )

    assert len(result) == 2
    assert result[0]["name"] == "Alice"
    assert result[1]["name"] == "Charlie"


def test_query_array_projection(table_service, mock_repository, sample_table_with_data):
    """测试数组投影（提取特定字段）"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="users[*].name"
    )

    assert result == ["Alice", "Bob", "Charlie"]


def test_query_with_multiple_filters(table_service, mock_repository, sample_table_with_data):
    """测试多条件过滤"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="users[?age > `25` && city == 'Beijing']"
    )

    assert len(result) == 2
    assert all(u["city"] == "Beijing" for u in result)


def test_query_sort_by(table_service, mock_repository, sample_table_with_data):
    """测试排序"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="sort_by(users, &age)[*].name"
    )

    assert result == ["Bob", "Alice", "Charlie"]


def test_query_with_pipe(table_service, mock_repository, sample_table_with_data):
    """测试管道操作"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="users[?age > `25`] | [*].name"
    )

    assert set(result) == {"Alice", "Charlie"}


def test_query_max_min_functions(table_service, mock_repository, sample_table_with_data):
    """测试最大值和最小值函数"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    max_age = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="max_by(users, &age).name"
    )
    assert max_age == "Charlie"

    min_age = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="min_by(users, &age).name"
    )
    assert min_age == "Bob"


def test_query_sum_function(table_service, mock_repository, sample_table_with_data):
    """测试求和函数"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="sum(products[*].stock)"
    )

    assert result == 90  # 10 + 50 + 30


# ============= JMESPath 查询嵌套路径测试 =============


def test_query_at_nested_path(table_service, mock_repository, sample_table_with_data):
    """测试在嵌套路径上执行查询"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="/users", query="[?city == 'Beijing']"
    )

    assert len(result) == 2


def test_query_products_at_nested_path(table_service, mock_repository, sample_table_with_data):
    """测试在产品路径上执行查询"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="/products", query="[?price < `500`] | [*].name"
    )

    assert set(result) == {"Mouse", "Keyboard"}


# ============= JMESPath 查询空结果测试 =============


def test_query_returns_none(table_service, mock_repository, sample_table_with_data):
    """测试查询返回None（没有匹配的结果）"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="users[?age > `100`]"
    )

    assert result == []


def test_query_nonexistent_field(table_service, mock_repository, sample_table_with_data):
    """测试查询不存在的字段"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="nonexistent_field"
    )

    assert result is None


# ============= JMESPath 查询错误处理测试 =============


def test_query_table_not_found(table_service, mock_repository):
    """测试查询不存在的表格"""
    mock_repository.get_by_id.return_value = None

    with pytest.raises(NotFoundException) as exc_info:
        table_service.query_context_data_with_jmespath(
            table_id="999", json_pointer_path="", query="users"
        )

    assert "Table not found: 999" in str(exc_info.value)


def test_query_invalid_json_pointer_path(table_service, mock_repository, sample_table_with_data):
    """测试无效的JSON指针路径"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    with pytest.raises(NotFoundException):
        table_service.query_context_data_with_jmespath(
            table_id="1", json_pointer_path="/nonexistent", query="users"
        )


def test_query_invalid_jmespath_syntax(table_service, mock_repository, sample_table_with_data):
    """测试无效的JMESPath语法"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    with pytest.raises(BusinessException) as exc_info:
        table_service.query_context_data_with_jmespath(
            table_id="1",
            json_pointer_path="",
            query="users[?age >",  # 语法错误
        )

    assert "JMESPath syntax error" in str(exc_info.value)


# ============= JMESPath 复杂查询测试 =============


def test_complex_query_multiline(table_service, mock_repository, sample_table_with_data):
    """测试复杂的多步骤查询"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    # 过滤产品，然后计算总价值
    result = table_service.query_context_data_with_jmespath(
        table_id="1",
        json_pointer_path="",
        query="products[?stock > `20`].{name: name, total_value: price}",
    )

    assert len(result) == 2
    assert any(p["name"] == "Mouse" for p in result)
    assert any(p["name"] == "Keyboard" for p in result)


def test_query_with_object_projection(table_service, mock_repository, sample_table_with_data):
    """测试对象投影"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="users[*].{username: name, user_age: age}"
    )

    assert len(result) == 3
    assert all("username" in u and "user_age" in u for u in result)
    assert result[0]["username"] == "Alice"


def test_query_nested_objects(table_service, mock_repository):
    """测试查询嵌套对象"""
    nested_table = Table(
        id="1",
        name="嵌套数据",
        project_id="1",
        description="测试嵌套对象",
        data={
            "company": {
                "departments": {
                    "engineering": {
                        "employees": [
                            {"name": "Alice", "role": "Engineer"},
                            {"name": "Bob", "role": "Manager"},
                        ]
                    },
                    "sales": {"employees": [{"name": "Charlie", "role": "Sales"}]},
                }
            }
        },
        created_at=datetime.now(UTC),
    )

    mock_repository.get_by_id.return_value = nested_table

    result = table_service.query_context_data_with_jmespath(
        table_id="1",
        json_pointer_path="",
        query="company.departments.engineering.employees[?role == 'Engineer'].name",
    )

    assert result == ["Alice"]


def test_query_with_contains(table_service, mock_repository, sample_table_with_data):
    """测试contains函数"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="users[?contains(name, 'li')].name"
    )

    assert set(result) == {"Alice", "Charlie"}


def test_query_with_starts_with(table_service, mock_repository, sample_table_with_data):
    """测试starts_with函数"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="products[?starts_with(name, 'K')].name"
    )

    assert result == ["Keyboard"]


# ============= JMESPath 查询边界情况测试 =============


def test_query_empty_array(table_service, mock_repository):
    """测试查询空数组"""
    empty_table = Table(
        id="1",
        name="空表格",
        project_id="1",
        description="空数据",
        data={"users": []},
        created_at=datetime.now(UTC),
    )

    mock_repository.get_by_id.return_value = empty_table

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="users[*].name"
    )

    assert result == []


def test_query_null_values(table_service, mock_repository):
    """测试查询包含null值的数据"""
    null_table = Table(
        id="1",
        name="包含null",
        project_id="1",
        description="测试null",
        data={
            "users": [
                {"name": "Alice", "age": 30},
                {"name": "Bob", "age": None},
                {"name": None, "age": 25},
            ]
        },
        created_at=datetime.now(UTC),
    )

    mock_repository.get_by_id.return_value = null_table

    result = table_service.query_context_data_with_jmespath(
        table_id="1", json_pointer_path="", query="users[?age != null].name"
    )

    # JMESPath 会过滤掉null值
    assert "Bob" not in result


def test_query_root_path_empty_string(table_service, mock_repository, sample_table_with_data):
    """测试根路径为空字符串的查询"""
    mock_repository.get_by_id.return_value = sample_table_with_data

    result = table_service.query_context_data_with_jmespath(
        table_id="1",
        json_pointer_path="",  # 根路径
        query="metadata",
    )

    assert result == {"version": "1.0", "author": "admin"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
