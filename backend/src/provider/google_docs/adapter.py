"""
Google Docs Provider - Process Google Docs imports.

Imports Google Docs documents into content nodes as Markdown.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.provider._base import ProviderDeps, ProviderSetup

import hashlib
from typing import Any, Optional

import httpx
from src.provider._base import (
    BaseProvider,
    ProviderSpec,
    Capability,
    AuthRequirement,
    TriggerMode,
    FetchResult,
    Credentials,
    SourceResource,
)
from src.provider.google_workspace.resources import list_drive_source_resources
from src.provider.oauth.google_docs_service import GoogleDocsOAuthService
from src.infra.s3.service import S3Service
from src.utils.logger import log_error


class GoogleDocsProvider(BaseProvider):
    """Provider for Google Docs imports."""

    DOCS_API_URL = "https://docs.googleapis.com/v1/documents"
    DOC_MIME_TYPE = "application/vnd.google-apps.document"

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="google_docs",
            display_name="Google Docs",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
            default_trigger=TriggerMode.MANUAL,
            default_node_type="markdown",
            auth=AuthRequirement.OAUTH,
            oauth_type="docs",
            oauth_ui_type="google_docs",
            supported_sync_modes=("manual", "scheduled"),
            default_sync_mode="manual",
            creation_mode="direct",
            description="Sync documents",
            accept_types=("folder",),
            icon_url="https://www.gstatic.com/images/branding/product/1x/docs_2020q4_32dp.png",
        )

    def __init__(
        self,
        docs_service: GoogleDocsOAuthService,
        s3_service: S3Service,
        node_service: Any = None,
    ):
        self.node_service = node_service
        self.docs_service = docs_service
        self.s3_service = s3_service
        self.client = httpx.AsyncClient(timeout=60.0)

    async def fetch(self, config: dict, credentials: Credentials) -> FetchResult:
        """Pull a Google Doc and return as markdown."""
        source = config.get("source") or {}
        access_token = credentials.access_token
        doc_id = source.get("resource_id")
        if not doc_id:
            raise ValueError("source.resource_id is required for Google Docs")

        doc_content = await self._fetch_document(access_token, doc_id)
        if not doc_content:
            raise ValueError(f"Failed to fetch document: {doc_id}")

        title = doc_content.get("title", "Untitled Document")
        markdown_content = self._convert_to_markdown(doc_content)
        content_hash = hashlib.sha256(markdown_content.encode()).hexdigest()[:16]

        return FetchResult(
            content=markdown_content,
            content_hash=content_hash,
            node_type="markdown",
            node_name=f"{title}.md",
            summary=f"Google Doc '{title}'",
        )

    async def list_source_resources(
        self,
        credentials: Credentials,
        *,
        query: str = "",
        cursor: Optional[str] = None,
        resource_type: Optional[str] = None,
    ) -> tuple[list[SourceResource], Optional[str]]:
        return await list_drive_source_resources(
            self.client,
            credentials.access_token,
            query=query,
            cursor=cursor,
            mime_type=self.DOC_MIME_TYPE,
            icon="google_docs",
            resource_type="document",
            default_name="Untitled document",
        )

    async def _fetch_document(self, access_token: str, doc_id: str) -> Optional[dict]:
        """Fetch document content from Google Docs API."""
        try:
            response = await self.client.get(
                f"{self.DOCS_API_URL}/{doc_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            log_error(f"Failed to fetch Google Doc {doc_id}: {e}")
            return None

    def _convert_to_markdown(self, doc_content: dict) -> str:
        """Convert Google Docs content to Markdown."""
        markdown_lines = []

        body = doc_content.get("body", {})
        content = body.get("content", [])

        for element in content:
            if "paragraph" in element:
                paragraph = element["paragraph"]
                paragraph_style = paragraph.get("paragraphStyle", {})
                named_style = paragraph_style.get("namedStyleType", "NORMAL_TEXT")

                text_parts = []
                for elem in paragraph.get("elements", []):
                    if "textRun" in elem:
                        text_run = elem["textRun"]
                        text = text_run.get("content", "")
                        text_style = text_run.get("textStyle", {})

                        # Apply text formatting
                        if text_style.get("bold"):
                            text = f"**{text.strip()}**"
                        if text_style.get("italic"):
                            text = f"*{text.strip()}*"
                        if text_style.get("strikethrough"):
                            text = f"~~{text.strip()}~~"
                        if text_style.get("link"):
                            url = text_style["link"].get("url", "")
                            text = f"[{text.strip()}]({url})"

                        text_parts.append(text)

                line = "".join(text_parts).rstrip()

                # Apply heading styles
                if named_style == "HEADING_1":
                    line = f"# {line}"
                elif named_style == "HEADING_2":
                    line = f"## {line}"
                elif named_style == "HEADING_3":
                    line = f"### {line}"
                elif named_style == "HEADING_4":
                    line = f"#### {line}"
                elif named_style == "HEADING_5":
                    line = f"##### {line}"
                elif named_style == "HEADING_6":
                    line = f"###### {line}"

                markdown_lines.append(line)

            elif "table" in element:
                # Basic table support
                table = element["table"]
                rows = table.get("tableRows", [])

                for i, row in enumerate(rows):
                    cells = row.get("tableCells", [])
                    cell_texts = []

                    for cell in cells:
                        cell_content = cell.get("content", [])
                        cell_text = ""
                        for cell_elem in cell_content:
                            if "paragraph" in cell_elem:
                                for text_elem in cell_elem["paragraph"].get("elements", []):
                                    if "textRun" in text_elem:
                                        cell_text += text_elem["textRun"].get("content", "").strip()
                        cell_texts.append(cell_text)

                    markdown_lines.append("| " + " | ".join(cell_texts) + " |")

                    # Add header separator after first row
                    if i == 0:
                        markdown_lines.append("| " + " | ".join(["---"] * len(cell_texts)) + " |")

                markdown_lines.append("")

        return "\n".join(markdown_lines)

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


def setup(deps: "ProviderDeps") -> "ProviderSetup":
    from src.provider._base import ProviderSetup
    from src.provider.oauth.google_docs_service import GoogleDocsOAuthService
    oauth_svc = GoogleDocsOAuthService()
    return ProviderSetup(
        adapter=GoogleDocsProvider(
            docs_service=oauth_svc,
            s3_service=deps.s3_service,
            node_service=deps.node_service,
        ),
        oauth_bindings={"docs": oauth_svc},
    )
