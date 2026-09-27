"""
URL Provider - Process generic URL imports via Firecrawl.

Handles:
- Single page scraping
- Multi-page crawling (with crawl_options)
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.provider._base import ProviderDeps, ProviderSetup

import hashlib
import json
from typing import Any

from src.provider._base import (
    BaseProvider,
    ProviderSpec,
    Capability,
    AuthRequirement,
    TriggerMode,
    FetchResult,
    Credentials,
    ConfigField,
)
from src.provider.utils.url_parser import UrlParser


def setup(deps: "ProviderDeps") -> "ProviderSetup":
    from src.provider._base import ProviderSetup
    return ProviderSetup(
        adapter=UrlProvider(node_service=deps.node_service),
    )


class UrlProvider(BaseProvider):
    """Provider for generic URL imports using Firecrawl."""

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="url",
            display_name="Web Page",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
            default_trigger=TriggerMode.MANUAL,
            default_node_type="markdown",
            auth=AuthRequirement.NONE,
            supported_sync_modes=("manual", "scheduled"),
            default_sync_mode="manual",
            creation_mode="direct",
            description="Import content from a URL",
            accept_types=("folder",),
            config_fields=(
                ConfigField(
                    key="resource_url",
                    label="URL",
                    type="url",
                    required=True,
                    placeholder="https://example.com",
                ),
            ),
            icon="🌐",
        )

    def __init__(
        self,
        node_service: Any = None,
    ):
        self.node_service = node_service
        self.url_parser = UrlParser()

    async def fetch(self, config: dict, credentials: Credentials) -> FetchResult:
        """Scrape a URL via Firecrawl. No OAuth needed — credentials are ignored."""
        source = config.get("source") or {}
        options = config.get("options") or {}
        source_url = source.get("resource_url", "")
        if not source_url:
            raise ValueError("source.resource_url is required for URL fetch")

        url_parser = UrlParser()
        try:
            crawl_options = options.get("crawl_options")
            result = await url_parser.parse(source_url, crawl_options)

            if not result:
                raise ValueError(f"Failed to parse content from {source_url}")

            data = result.get("data", [])
            title = result.get("title", source_url)

            # Build markdown: use raw markdown from Firecrawl if available,
            # otherwise reconstruct from parsed sections
            raw_markdown = result.get("raw_markdown", "")
            if raw_markdown:
                markdown_content = raw_markdown
            else:
                markdown_parts = []
                for item in data:
                    item_title = item.get("title", "")
                    # Support multiple field names from different parsers
                    item_content = item.get("content", "") or item.get("item", "") or item.get("value", "")
                    if item_title and item_content:
                        markdown_parts.append(f"## {item_title}\n\n{item_content}")
                    elif item_title:
                        markdown_parts.append(f"## {item_title}")
                    elif item_content:
                        markdown_parts.append(item_content)
                markdown_content = "\n\n".join(markdown_parts)

            if not markdown_content.strip():
                markdown_content = f"# {title}\n\nNo content could be extracted from {source_url}."

            content_hash = hashlib.sha256(
                markdown_content.encode("utf-8")
            ).hexdigest()[:16]

            return FetchResult(
                content=markdown_content,
                content_hash=content_hash,
                node_type="markdown",
                node_name=title[:100],
                summary=f"Scraped {source_url} ({len(data)} sections)",
            )
        finally:
            await url_parser.close()
