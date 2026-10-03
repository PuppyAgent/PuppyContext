"""
ETL Rule Repository Supabase 实现测试

测试 Supabase 存储的 ETL 规则仓库。
"""

import os
import pytest
from datetime import datetime, UTC

from src.infra.file_processing.rules.repository_supabase import RuleRepositorySupabase
from src.infra.file_processing.rules.schemas import RuleCreateRequest, RuleUpdateRequest
from src.infra.supabase.exceptions import SupabaseException
from src.infra.supabase.dependencies import get_supabase_client


_REAL_SUPABASE = os.getenv("SUPABASE_URL", "") and "localhost:54321" not in os.getenv("SUPABASE_URL", "")
if not _REAL_SUPABASE:
    pytest.skip("Skip Supabase-dependent tests (SUPABASE_URL/KEY not set)", allow_module_level=True)

@pytest.fixture
def repository():
    """创建测试用的 Repository 实例"""
    # 使用测试用户 ID
    return RuleRepositorySupabase(supabase_client=get_supabase_client(), user_id="1")


@pytest.fixture
def sample_rule_request():
    """创建示例规则请求"""
    return RuleCreateRequest(
        name="Test Invoice Extraction",
        description="Extract invoice data from documents",
        json_schema={
            "type": "object",
            "properties": {
                "invoice_number": {"type": "string"},
                "amount": {"type": "number"},
                "date": {"type": "string", "format": "date"},
            },
            "required": ["invoice_number", "amount"],
        },
        system_prompt="Extract invoice information accurately and completely.",
    )


class TestRuleRepositorySupabase:
    """测试 RuleRepositorySupabase 类"""

    def test_create_rule(self, repository, sample_rule_request):
        """测试创建规则"""
        rule = repository.create_rule(sample_rule_request)

        assert rule is not None
        assert rule.rule_id is not None
        assert rule.name == sample_rule_request.name
        assert rule.description == sample_rule_request.description
        assert rule.json_schema == sample_rule_request.json_schema
        assert rule.system_prompt == sample_rule_request.system_prompt
        assert rule.created_at is not None
        assert rule.updated_at is not None

        # 清理
        repository.delete_rule(rule.rule_id)

    def test_get_rule(self, repository, sample_rule_request):
        """测试获取规则"""
        # 先创建一个规则
        created_rule = repository.create_rule(sample_rule_request)

        # 获取规则
        rule = repository.get_rule(created_rule.rule_id)

        assert rule is not None
        assert rule.rule_id == created_rule.rule_id
        assert rule.name == created_rule.name
        assert rule.description == created_rule.description

        # 清理
        repository.delete_rule(rule.rule_id)

    def test_get_rule_not_found(self, repository):
        """测试获取不存在的规则"""
        rule = repository.get_rule("999999")
        assert rule is None

    def test_get_rule_invalid_id(self, repository):
        """测试获取无效 ID 的规则"""
        rule = repository.get_rule("invalid-id")
        assert rule is None

    def test_update_rule(self, repository, sample_rule_request):
        """测试更新规则"""
        # 先创建一个规则
        created_rule = repository.create_rule(sample_rule_request)

        # 更新规则
        update_request = RuleUpdateRequest(
            name="Updated Invoice Extraction",
            description="Updated description",
        )
        updated_rule = repository.update_rule(created_rule.rule_id, update_request)

        assert updated_rule is not None
        assert updated_rule.name == "Updated Invoice Extraction"
        assert updated_rule.description == "Updated description"
        assert updated_rule.json_schema == created_rule.json_schema
        assert updated_rule.updated_at > created_rule.updated_at

        # 清理
        repository.delete_rule(created_rule.rule_id)

    def test_update_rule_not_found(self, repository):
        """测试更新不存在的规则"""
        update_request = RuleUpdateRequest(name="New Name")
        result = repository.update_rule("999999", update_request)
        assert result is None

    def test_delete_rule(self, repository, sample_rule_request):
        """测试删除规则"""
        # 先创建一个规则
        created_rule = repository.create_rule(sample_rule_request)

        # 删除规则
        success = repository.delete_rule(created_rule.rule_id)
        assert success is True

        # 验证规则已被删除
        rule = repository.get_rule(created_rule.rule_id)
        assert rule is None

    def test_delete_rule_not_found(self, repository):
        """测试删除不存在的规则"""
        success = repository.delete_rule("999999")
        assert success is False

    def test_list_rules(self, repository, sample_rule_request):
        """测试列出规则"""
        # 创建几个测试规则
        rule1 = repository.create_rule(sample_rule_request)
        
        request2 = RuleCreateRequest(
            name="Test Receipt Extraction",
            description="Extract receipt data",
            json_schema={"type": "object", "properties": {"total": {"type": "number"}}},
        )
        rule2 = repository.create_rule(request2)

        # 列出规则
        rules = repository.list_rules(limit=10, offset=0)

        assert len(rules) >= 2
        rule_ids = [r.rule_id for r in rules]
        assert rule1.rule_id in rule_ids
        assert rule2.rule_id in rule_ids

        # 清理
        repository.delete_rule(rule1.rule_id)
        repository.delete_rule(rule2.rule_id)

    def test_list_rules_pagination(self, repository, sample_rule_request):
        """测试规则列表分页"""
        # 创建测试规则
        created_rules = []
        for i in range(5):
            request = RuleCreateRequest(
                name=f"Test Rule {i}",
                description=f"Description {i}",
                json_schema={"type": "object"},
            )
            rule = repository.create_rule(request)
            created_rules.append(rule)

        # 测试分页
        page1 = repository.list_rules(limit=2, offset=0)
        page2 = repository.list_rules(limit=2, offset=2)

        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].rule_id != page2[0].rule_id

        # 清理
        for rule in created_rules:
            repository.delete_rule(rule.rule_id)

    def test_count_rules(self, repository, sample_rule_request):
        """测试统计规则数量"""
        # 获取初始数量
        initial_count = repository.count_rules()

        # 创建新规则
        rule = repository.create_rule(sample_rule_request)

        # 验证数量增加
        new_count = repository.count_rules()
        assert new_count == initial_count + 1

        # 清理
        repository.delete_rule(rule.rule_id)

        # 验证数量恢复
        final_count = repository.count_rules()
        assert final_count == initial_count

    def test_user_isolation(self, sample_rule_request):
        """测试用户数据隔离"""
        # 创建两个不同用户的 Repository
        repo1 = RuleRepositorySupabase(user_id=1)
        repo2 = RuleRepositorySupabase(user_id=2)

        # 用户 1 创建规则
        rule1 = repo1.create_rule(sample_rule_request)

        # 用户 2 不应该能看到用户 1 的规则
        rule = repo2.get_rule(rule1.rule_id)
        assert rule is None

        # 用户 2 的规则列表中也不应该有用户 1 的规则
        rules = repo2.list_rules()
        rule_ids = [r.rule_id for r in rules]
        assert rule1.rule_id not in rule_ids

        # 清理
        repo1.delete_rule(rule1.rule_id)

    def test_admin_mode(self, sample_rule_request):
        """测试管理员模式（user_id=None）"""
        # 创建管理员 Repository
        admin_repo = RuleRepositorySupabase(user_id=None)
        user_repo = RuleRepositorySupabase(user_id=1)

        # 用户创建规则
        rule = user_repo.create_rule(sample_rule_request)

        # 管理员应该能看到所有用户的规则
        admin_rule = admin_repo.get_rule(rule.rule_id)
        assert admin_rule is not None
        assert admin_rule.rule_id == rule.rule_id

        # 清理
        admin_repo.delete_rule(rule.rule_id)


class TestRuleRepositoryIntegration:
    """集成测试"""

    def test_full_crud_workflow(self, repository, sample_rule_request):
        """测试完整的 CRUD 工作流"""
        # 1. 创建
        rule = repository.create_rule(sample_rule_request)
        assert rule is not None
        rule_id = rule.rule_id

        # 2. 读取
        fetched_rule = repository.get_rule(rule_id)
        assert fetched_rule is not None
        assert fetched_rule.name == sample_rule_request.name

        # 3. 更新
        update_request = RuleUpdateRequest(
            name="Updated Name",
            system_prompt="Updated prompt",
        )
        updated_rule = repository.update_rule(rule_id, update_request)
        assert updated_rule.name == "Updated Name"
        assert updated_rule.system_prompt == "Updated prompt"

        # 4. 列表中应该包含该规则
        rules = repository.list_rules()
        rule_ids = [r.rule_id for r in rules]
        assert rule_id in rule_ids

        # 5. 删除
        success = repository.delete_rule(rule_id)
        assert success is True

        # 6. 验证已删除
        deleted_rule = repository.get_rule(rule_id)
        assert deleted_rule is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

