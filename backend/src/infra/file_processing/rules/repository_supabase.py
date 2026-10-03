"""
ETL Rule Repository - Supabase Implementation

Manages storage and retrieval of ETL transformation rules (Supabase database implementation).
"""

import logging
import uuid
from datetime import UTC, datetime

from src.infra.supabase.exceptions import handle_supabase_error
from src.infra.file_processing.rules.schemas import (
    ETLRule,
    RuleCreateRequest,
    RuleUpdateRequest,
    build_rule_payload,
    parse_rule_payload,
)

logger = logging.getLogger(__name__)


def _parse_timestamp(
    timestamp_str: str | None, fallback: str | None = None
) -> datetime:
    """
    Safely parse a timestamp string.

    Args:
        timestamp_str: Timestamp string
        fallback: Fallback timestamp string

    Returns:
        Parsed datetime object
    """
    ts = timestamp_str or fallback
    if ts is None:
        return datetime.now(UTC)

    # Handle Supabase timestamp format (may have Z suffix)
    if isinstance(ts, str):
        ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


class RuleRepositorySupabase:
    """Repository for ETL transformation rules (Supabase storage)."""

    TABLE_NAME = "etl_rules"

    def __init__(self, supabase_client, org_id: str | None = None, created_by: str | None = None):
        """
        Initialize Supabase rule repository.

        Args:
            supabase_client: Supabase client instance
            org_id: Organization ID, used for filtering rules
            created_by: Creator user ID, used for recording the creator
        """
        self.supabase = supabase_client
        self.org_id = org_id
        self.created_by = created_by
        logger.info(f"RuleRepositorySupabase initialized for org_id: {org_id}")

    def create_rule(self, request: RuleCreateRequest) -> ETLRule:
        """
        Create a new ETL rule.

        Args:
            request: Rule creation request

        Returns:
            Created ETLRule

        Raises:
            SupabaseException: When database operation fails
        """
        # Generate unique rule ID (legacy: Supabase uses bigint primary key, only for tracking/compatibility)
        _ = str(uuid.uuid4())
        now = datetime.now(UTC)

        payload = build_rule_payload(
            json_schema=request.json_schema,
            postprocess_mode=request.postprocess_mode,
            postprocess_strategy=request.postprocess_strategy,
        )

        # Prepare insert data
        insert_data = {
            "name": request.name,
            "description": request.description,
            "json_schema": payload,
            "system_prompt": request.system_prompt or "",
            "created_by": self.created_by,
            "org_id": self.org_id,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

        try:
            # Insert into database
            response = (
                self.supabase.table(self.TABLE_NAME).insert(insert_data).execute()
            )

            if not response.data or len(response.data) == 0:
                raise Exception("Failed to create rule: no data returned")

            # Get the inserted record
            row = response.data[0]
            mode, strategy, schema = parse_rule_payload(row["json_schema"])

            rule = ETLRule(
                rule_id=str(row["id"]),
                name=row["name"],
                description=row["description"],
                json_schema=schema,
                postprocess_mode=mode,
                postprocess_strategy=strategy,
                system_prompt=row["system_prompt"],
                created_at=_parse_timestamp(row.get("created_at")),
                updated_at=_parse_timestamp(
                    row.get("updated_at"), row.get("created_at")
                ),
            )

            logger.info(f"Created rule: {rule.name} (id: {row['id']})")
            return rule

        except Exception as e:
            handle_supabase_error(e, "create ETL rule")

    def get_rule(self, rule_id: str) -> ETLRule | None:
        """
        Get rule by ID.

        Args:
            rule_id: Rule identifier

        Returns:
            ETLRule if found, None otherwise
        """
        try:
            # Convert rule_id to bigint
            try:
                id_int = int(rule_id)
            except ValueError:
                logger.warning(f"Invalid rule_id format: {rule_id}")
                return None

            query = self.supabase.table(self.TABLE_NAME).select("*").eq("id", id_int)

            if self.org_id is not None:
                query = query.eq("org_id", self.org_id)

            response = query.execute()

            if not response.data or len(response.data) == 0:
                logger.warning(f"Rule not found: {rule_id}")
                return None

            row = response.data[0]
            mode, strategy, schema = parse_rule_payload(row["json_schema"])

            # Build ETLRule from database record
            rule = ETLRule(
                rule_id=str(row["id"]),
                name=row.get("name", ""),
                description=row["description"],
                json_schema=schema,
                postprocess_mode=mode,
                postprocess_strategy=strategy,
                system_prompt=row["system_prompt"] or None,
                created_at=_parse_timestamp(row.get("created_at")),
                updated_at=_parse_timestamp(
                    row.get("updated_at"), row.get("created_at")
                ),
            )

            return rule

        except Exception as e:
            logger.error(f"Error getting rule {rule_id}: {e}")
            return None

    @staticmethod
    def _build_update_data(
        request: RuleUpdateRequest, existing_rule: ETLRule
    ) -> dict:
        """Build the update payload from request fields and existing rule state."""
        update_data: dict = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.description is not None:
            update_data["description"] = request.description
        if request.system_prompt is not None:
            update_data["system_prompt"] = request.system_prompt

        if (
            request.json_schema is not None
            or request.postprocess_mode is not None
            or request.postprocess_strategy is not None
        ):
            mode, strategy, schema = parse_rule_payload(existing_rule.json_schema)
            update_data["json_schema"] = build_rule_payload(
                json_schema=request.json_schema if request.json_schema is not None else schema,
                postprocess_mode=request.postprocess_mode or mode,
                postprocess_strategy=request.postprocess_strategy if request.postprocess_strategy is not None else strategy,
            )

        return update_data

    def update_rule(
        self, rule_id: str, request: RuleUpdateRequest
    ) -> ETLRule | None:
        """
        Update an existing rule.

        Args:
            rule_id: Rule identifier
            request: Update request

        Returns:
            Updated ETLRule if found, None otherwise

        Raises:
            SupabaseException: When database operation fails
        """
        # First check if the rule exists
        existing_rule = self.get_rule(rule_id)
        if not existing_rule:
            return None

        # Prepare update data
        update_data = self._build_update_data(request, existing_rule)

        if not update_data:
            # No fields to update
            return existing_rule

        # Add updated_at timestamp
        update_data["updated_at"] = datetime.now(UTC).isoformat()

        try:
            # Convert rule_id to bigint
            id_int = int(rule_id)

            query = (
                self.supabase.table(self.TABLE_NAME)
                .update(update_data)
                .eq("id", id_int)
            )

            if self.org_id is not None:
                query = query.eq("org_id", self.org_id)

            response = query.execute()

            if not response.data or len(response.data) == 0:
                logger.warning(f"Rule not found for update: {rule_id}")
                return None

            row = response.data[0]
            mode, strategy, schema = parse_rule_payload(row["json_schema"])

            # Build updated ETLRule
            rule = ETLRule(
                rule_id=str(row["id"]),
                name=row["name"],
                description=row["description"],
                json_schema=schema,
                postprocess_mode=mode,
                postprocess_strategy=strategy,
                system_prompt=row["system_prompt"] or None,
                created_at=_parse_timestamp(row.get("created_at")),
                updated_at=_parse_timestamp(
                    row.get("updated_at"), row.get("created_at")
                ),
            )

            logger.info(f"Updated rule: {rule_id}")
            return rule

        except Exception as e:
            handle_supabase_error(e, "update ETL rule")

    def delete_rule(self, rule_id: str) -> bool:
        """
        Delete a rule.

        Args:
            rule_id: Rule identifier

        Returns:
            True if deleted successfully, False if not found

        Raises:
            SupabaseException: When database operation fails
        """
        try:
            # Convert rule_id to bigint
            try:
                id_int = int(rule_id)
            except ValueError:
                logger.warning(f"Invalid rule_id format: {rule_id}")
                return False

            query = self.supabase.table(self.TABLE_NAME).delete().eq("id", id_int)

            if self.org_id is not None:
                query = query.eq("org_id", self.org_id)

            response = query.execute()

            if not response.data or len(response.data) == 0:
                logger.warning(f"Rule not found for deletion: {rule_id}")
                return False

            logger.info(f"Deleted rule: {rule_id}")
            return True

        except Exception as e:
            handle_supabase_error(e, "delete ETL rule")

    def list_rules(self, limit: int = 100, offset: int = 0) -> list[ETLRule]:
        """
        List all rules (with pagination).

        Args:
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of ETLRule objects
        """
        try:
            query = self.supabase.table(self.TABLE_NAME).select("*")

            if self.org_id is not None:
                query = query.eq("org_id", self.org_id)

            # Apply pagination
            query = query.range(offset, offset + limit - 1).order(
                "created_at", desc=True
            )

            response = query.execute()

            rules = []
            for row in response.data:
                mode, strategy, schema = parse_rule_payload(row["json_schema"])
                rule = ETLRule(
                    rule_id=str(row["id"]),
                    name=row.get("name", ""),
                    description=row["description"],
                    json_schema=schema,
                    postprocess_mode=mode,
                    postprocess_strategy=strategy,
                    system_prompt=row["system_prompt"] or None,
                    created_at=_parse_timestamp(row.get("created_at")),
                    updated_at=_parse_timestamp(
                        row.get("updated_at"), row.get("created_at")
                    ),
                )
                rules.append(rule)

            logger.info(f"Listed {len(rules)} rules (offset={offset}, limit={limit})")
            return rules

        except Exception as e:
            logger.error(f"Error listing rules: {e}")
            return []

    def count_rules(self) -> int:
        """
        Count total number of rules.

        Returns:
            Total number of rules
        """
        try:
            query = self.supabase.table(self.TABLE_NAME).select("id", count="exact")

            if self.org_id is not None:
                query = query.eq("org_id", self.org_id)

            response = query.execute()

            count = response.count if response.count is not None else 0
            return count

        except Exception as e:
            logger.error(f"Error counting rules: {e}")
            return 0
