"""
Default ETL Rules

Provides system default ETL transformation rules.
"""

import logging

from src.infra.file_processing.rules.repository_supabase import RuleRepositorySupabase
from src.infra.file_processing.rules.schemas import ETLRule, RuleCreateRequest

logger = logging.getLogger(__name__)


# Global default rule: skips LLM by default, only produces markdown pointer + necessary metadata
GLOBAL_DEFAULT_RULE_NAME = "global_default_etl_rule"
GLOBAL_DEFAULT_RULE_DESCRIPTION = "Built-in global default ETL rule (skips LLM by default), returns a stable JSON wrapper with OCR markdown pointer and metadata."

# In skip mode, json_schema is ignored; a minimal schema is provided here for storage and validation compatibility
GLOBAL_DEFAULT_RULE_SCHEMA = {"type": "object"}


def get_or_create_default_rule(
    rule_repository: RuleRepositorySupabase,
) -> ETLRule:
    """
    Get or create the default document parsing rule.

    If a rule with the same name already exists in the database, return the first one; otherwise create a new rule.

    Args:
        rule_repository: ETL rule repository instance

    Returns:
        Default document parsing rule
    """
    # Try to find an existing default rule
    existing_rules = rule_repository.list_rules(limit=100)

    for rule in existing_rules:
        if rule.name == GLOBAL_DEFAULT_RULE_NAME:
            logger.info(f"Found existing global default rule: {rule.rule_id}")
            return rule

    # If not found, create a new rule
    logger.info("Creating global default ETL rule (skip-llm)...")

    rule_request = RuleCreateRequest(
        name=GLOBAL_DEFAULT_RULE_NAME,
        description=GLOBAL_DEFAULT_RULE_DESCRIPTION,
        json_schema=GLOBAL_DEFAULT_RULE_SCHEMA,
        system_prompt=None,
        postprocess_mode="skip",
        postprocess_strategy=None,
    )

    rule = rule_repository.create_rule(rule_request)
    logger.info(f"Created default rule: {rule.rule_id}")

    return rule


def get_default_rule_id(rule_repository: RuleRepositorySupabase) -> int:
    """
    Get the default rule ID.

    Args:
        rule_repository: ETL rule repository instance

    Returns:
        Default rule ID (integer)
    """
    rule = get_or_create_default_rule(rule_repository)
    return int(rule.rule_id)
