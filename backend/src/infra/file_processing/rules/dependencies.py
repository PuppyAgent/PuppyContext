"""
ETL Rules Repository Dependencies

Provides dependency injection functions for obtaining ETL rule repository instances.
"""

import logging

from fastapi import Depends

from src.infra.supabase.dependencies import get_supabase_client
from src.infra.file_processing.rules.repository_supabase import RuleRepositorySupabase
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser

logger = logging.getLogger(__name__)


def get_rule_repository(
    current_user: CurrentUser = Depends(get_current_user),
    supabase_client=Depends(get_supabase_client),
) -> RuleRepositorySupabase:
    """
    Get ETL rule repository instance.

    Args:
        current_user: Current user (extracted from token)
        supabase_client: Supabase client instance

    Returns:
        Rule repository instance
    """
    logger.debug(f"Creating rule repository for user: {current_user.user_id}")
    return RuleRepositorySupabase(
        supabase_client=supabase_client, created_by=current_user.user_id
    )
