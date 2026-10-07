"""ETL 规则存储兼容逻辑单元测试

当前项目使用 Supabase 存储规则，但为了避免 DB schema 变更，
postprocess 配置以兼容方式嵌入在 `json_schema` 字段中：
- Legacy: 纯 JSON Schema（包含 type）
- New: {"schema": <JSON Schema>, "_etl": {"postprocess_mode": "...", "postprocess_strategy": "..."}}
"""

from __future__ import annotations

from src.infra.file_processing.rules.schemas import build_rule_payload, parse_rule_payload


def test_parse_legacy_schema_defaults_to_llm():
    mode, strategy, schema = parse_rule_payload({"type": "object", "properties": {"a": {"type": "string"}}})
    assert mode == "llm"
    assert strategy is None
    assert schema["type"] == "object"


def test_build_and_parse_skip_mode_roundtrip():
    payload = build_rule_payload(
        json_schema=None,
        postprocess_mode="skip",
        postprocess_strategy=None,
    )
    mode, strategy, schema = parse_rule_payload(payload)
    assert mode == "skip"
    assert strategy is None
    # schema is minimal but valid
    assert schema["type"] == "object"


def test_build_and_parse_llm_mode_roundtrip_with_strategy():
    payload = build_rule_payload(
        json_schema={"type": "object", "properties": {"title": {"type": "string"}}},
        postprocess_mode="llm",
        postprocess_strategy="chunked-summarize",
    )
    mode, strategy, schema = parse_rule_payload(payload)
    assert mode == "llm"
    assert strategy == "chunked-summarize"
    assert schema["properties"]["title"]["type"] == "string"


