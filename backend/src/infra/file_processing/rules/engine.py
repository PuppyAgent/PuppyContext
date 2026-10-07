"""
ETL Rule Engine

Applies transformation rules to Markdown content using LLM.
"""

import json
import logging

import jsonschema
from jsonschema import ValidationError

from src.infra.llm.exceptions import LLMError
from src.infra.llm.service import LLMService
from src.infra.file_processing.rules.schemas import ETLRule, TransformationResult

logger = logging.getLogger(__name__)


class RuleEngine:
    """Engine for applying ETL transformation rules."""

    def __init__(self, llm_service: LLMService):
        """
        Initialize rule engine.

        Args:
            llm_service: LLM service for transformation
        """
        self.llm_service = llm_service
        logger.info("RuleEngine initialized")

    def _parse_and_validate(
        self, llm_response, rule: ETLRule
    ) -> tuple[TransformationResult | None, str | None]:
        """Parse LLM JSON and validate against schema. Returns (result, error)."""
        try:
            output_json = json.loads(llm_response.content)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from LLM: {e}")
            return None, f"Invalid JSON: {e!s}"

        try:
            jsonschema.validate(output_json, rule.json_schema)
            logger.info("JSON validation successful")
            return TransformationResult(
                success=True,
                output=output_json,
                error=None,
                llm_usage=llm_response.usage,
            ), None
        except ValidationError as e:
            logger.error(f"JSON Schema validation failed: {e}")
            return None, f"Schema validation failed: {e.message}"

    async def apply_rule(
        self,
        markdown_content: str,
        rule: ETLRule,
        max_retries: int = 2,
    ) -> TransformationResult:
        """
        Apply an ETL rule to Markdown content.

        Args:
            markdown_content: Input Markdown content
            rule: ETL rule to apply
            max_retries: Maximum number of retries on validation failure

        Returns:
            TransformationResult with transformed JSON or error
        """
        logger.info(f"Applying rule '{rule.name}' (rule_id: {rule.rule_id})")

        # Build user prompt
        user_prompt = self._build_prompt(markdown_content, rule.json_schema)

        # Try transformation with retries
        last_error: str | None = None
        for attempt in range(max_retries + 1):
            try:
                logger.info(f"Transformation attempt {attempt + 1}/{max_retries + 1}")

                llm_response = await self.llm_service.call_text_model(
                    prompt=user_prompt,
                    system_prompt=rule.system_prompt,
                    response_format="json_object",
                )

                result, error = self._parse_and_validate(llm_response, rule)
                if result is not None:
                    return result

                last_error = error
                if attempt < max_retries:
                    user_prompt += f"\n\nPrevious attempt failed: {last_error}. Please fix and return valid JSON matching the schema exactly."
                    continue
                break

            except LLMError as e:
                logger.error(f"LLM error during transformation: {e}")
                return TransformationResult(
                    success=False,
                    output=None,
                    error=f"LLM error: {e!s}",
                    llm_usage=None,
                )

        # All retries exhausted
        logger.error(f"Transformation failed after {max_retries + 1} attempts")
        return TransformationResult(
            success=False,
            output=None,
            error=last_error or "Unknown error",
            llm_usage=None,
        )

    def _build_prompt(self, markdown_content: str, json_schema: dict) -> str:
        """
        Build user prompt for LLM transformation.

        Args:
            markdown_content: Input Markdown
            json_schema: Target JSON Schema

        Returns:
            Formatted prompt string
        """
        schema_str = json.dumps(json_schema, indent=2)

        prompt = f"""Please extract and transform information from the following Markdown document according to the JSON Schema provided.

JSON Schema:
```json
{schema_str}
```

Markdown Document:
```markdown
{markdown_content}
```

Return a valid JSON object that strictly matches the schema. Do not include any additional fields not specified in the schema."""

        return prompt

    def validate_output(
        self, output: dict | list, json_schema: dict
    ) -> tuple[bool, str | None]:
        """
        Validate output against JSON Schema.

        Args:
            output: JSON output to validate (dict or list)
            json_schema: JSON Schema to validate against

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            jsonschema.validate(output, json_schema)
            return True, None
        except ValidationError as e:
            return False, e.message
