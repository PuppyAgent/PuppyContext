"""
Google Sheets Provider - Process Google Sheets imports.

Architecture:
- All sheets are stored in a single JSON file in the version tree
- Agent can query with jq: jq '.sheets[0].rows[] | select(.Column1 == "value")'
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.provider._base import ProviderDeps, ProviderSetup

import hashlib
import json
from datetime import datetime, timezone
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
from src.provider.oauth.google_sheets_service import GoogleSheetsOAuthService
from src.infra.s3.service import S3Service
from src.utils.logger import log_error


class GoogleSheetsProvider(BaseProvider):
    """Provider for Google Sheets imports - stores all data in single JSONB node."""

    SHEETS_API_URL = "https://sheets.googleapis.com/v4/spreadsheets"
    SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="google_sheets",
            display_name="Google Sheets",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
            default_trigger=TriggerMode.POLL,
            default_node_type="json",
            auth=AuthRequirement.OAUTH,
            oauth_type="sheets",
            oauth_ui_type="google_sheets",
            supported_sync_modes=("manual", "scheduled"),
            default_sync_mode="manual",
            creation_mode="direct",
            description="Sync spreadsheet data",
            accept_types=("folder",),
            icon_url="https://www.gstatic.com/images/branding/product/1x/sheets_2020q4_32dp.png",
        )

    def __init__(
        self,
        sheets_service: GoogleSheetsOAuthService,
        s3_service: S3Service,
        node_service: Any = None,
    ):
        self.node_service = node_service
        self.sheets_service = sheets_service
        self.s3_service = s3_service
        self.client = httpx.AsyncClient(timeout=60.0)

    async def fetch(self, config: dict, credentials: Credentials) -> FetchResult:
        """Fetch Google Sheets data using the unified fetch interface."""
        source = config.get("source") or {}
        spreadsheet_id = source.get("resource_id")
        if not spreadsheet_id:
            raise ValueError("source.resource_id is required for Google Sheets")

        access_token = credentials.access_token

        spreadsheet = await self._get_spreadsheet(access_token, spreadsheet_id)
        title = spreadsheet.get("properties", {}).get("title", "Untitled Spreadsheet")
        sheets_meta = spreadsheet.get("sheets", [])

        sheets_data = []
        total_rows = 0

        for idx, sheet in enumerate(sheets_meta):
            sheet_props = sheet.get("properties", {})
            sheet_title = sheet_props.get("title", f"Sheet{idx + 1}")
            sheet_id = sheet_props.get("sheetId", idx)

            try:
                values = await self._get_sheet_values(access_token, spreadsheet_id, sheet_title)
                sheet_data = self._format_sheet_data(sheet_title, sheet_id, values)
                sheets_data.append(sheet_data)
                total_rows += sheet_data.get("row_count", 0)
            except Exception as e:
                log_error(f"[Sheets fetch] Failed to process sheet {sheet_title}: {e}")
                sheets_data.append({
                    "name": sheet_title,
                    "sheet_id": sheet_id,
                    "error": str(e),
                    "headers": [],
                    "row_count": 0,
                    "rows": [],
                })

        content = {
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "source": "google_sheets",
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet_title": title,
            "spreadsheet_url": source.get("resource_url"),
            "sheet_count": len(sheets_data),
            "total_rows": total_rows,
            "sheets": sheets_data,
        }

        content_hash = hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]

        return FetchResult(
            content=content,
            content_hash=content_hash,
            node_type="json",
            node_name=source.get("resource_name") or title[:100],
            summary=f"Fetched {len(sheets_data)} sheets, {total_rows} rows from '{title}'",
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
            mime_type=self.SHEET_MIME_TYPE,
            icon="google_sheets",
            resource_type="spreadsheet",
            default_name="Untitled spreadsheet",
        )

    async def _get_spreadsheet(self, access_token: str, spreadsheet_id: str) -> dict:
        """Get spreadsheet metadata."""
        response = await self.client.get(
            f"{self.SHEETS_API_URL}/{spreadsheet_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "properties.title,sheets.properties"},
        )
        response.raise_for_status()
        return response.json()

    async def _get_sheet_values(
        self,
        access_token: str,
        spreadsheet_id: str,
        sheet_title: str,
    ) -> list[list]:
        """Get values from a specific sheet."""
        import urllib.parse
        encoded_title = urllib.parse.quote(sheet_title, safe='')

        response = await self.client.get(
            f"{self.SHEETS_API_URL}/{spreadsheet_id}/values/{encoded_title}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"valueRenderOption": "FORMATTED_VALUE"},
        )
        response.raise_for_status()
        return response.json().get("values", [])

    def _format_sheet_data(
        self,
        sheet_title: str,
        sheet_id: int,
        values: list[list],
    ) -> dict:
        """Format sheet data for JSONB storage."""
        if not values:
            return {
                "name": sheet_title,
                "sheet_id": sheet_id,
                "headers": [],
                "row_count": 0,
                "rows": [],
            }

        # First row is headers
        headers = [str(h) for h in values[0]] if values else []

        # Convert rows to list of dicts for easier querying
        rows = []
        max_rows = 1000  # Limit rows per sheet to prevent huge JSONB

        for row in values[1:max_rows + 1]:
            row_dict = {}
            for i, cell in enumerate(row):
                if i < len(headers):
                    row_dict[headers[i]] = str(cell) if cell else ""
                else:
                    row_dict[f"Column{i + 1}"] = str(cell) if cell else ""
            rows.append(row_dict)

        return {
            "name": sheet_title,
            "sheet_id": sheet_id,
            "headers": headers,
            "row_count": len(rows),
            "total_rows_in_source": len(values) - 1,
            "truncated": len(values) - 1 > max_rows,
            "rows": rows,
        }

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


def setup(deps: "ProviderDeps") -> "ProviderSetup":
    from src.provider._base import ProviderSetup
    from src.provider.oauth.google_sheets_service import GoogleSheetsOAuthService
    oauth_svc = GoogleSheetsOAuthService()
    return ProviderSetup(
        adapter=GoogleSheetsProvider(
            sheets_service=oauth_svc,
            s3_service=deps.s3_service,
            node_service=deps.node_service,
        ),
        oauth_bindings={"sheets": oauth_svc},
    )
