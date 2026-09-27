"""
Google Drive Provider - Process Google Drive file imports.

Imports files from Google Drive into content nodes.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.provider._base import ProviderDeps, ProviderSetup

import hashlib
import json
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
    ConfigField,
    SourceResource,
)
from src.provider.google_workspace.resources import (
    DRIVE_FILES_URL as GOOGLE_DRIVE_FILES_URL,
    GOOGLE_FOLDER_MIME_TYPE,
    list_drive_source_resources,
)
from src.provider.oauth.google_drive_service import GoogleDriveOAuthService
from src.infra.s3.service import S3Service


class GoogleDriveProvider(BaseProvider):
    """Provider for Google Drive imports."""

    DRIVE_FILES_URL = GOOGLE_DRIVE_FILES_URL
    DRIVE_EXPORT_URL = "https://www.googleapis.com/drive/v3/files/{file_id}/export"

    # Google Docs MIME types that can be exported
    EXPORT_MIME_TYPES = {
        "application/vnd.google-apps.document": "text/markdown",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }

    # Regular file types to download directly
    TEXT_MIME_TYPES = {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "text/html",
    }

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="google_drive",
            display_name="Google Drive",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
            default_trigger=TriggerMode.MANUAL,
            default_node_type="markdown",
            auth=AuthRequirement.OAUTH,
            oauth_type="drive",
            oauth_ui_type="google_drive",
            supported_sync_modes=("manual", "scheduled"),
            default_sync_mode="manual",
            creation_mode="direct",
            description="Sync files from Drive",
            accept_types=("folder",),
            icon_url="https://www.gstatic.com/images/branding/product/1x/drive_2020q4_32dp.png",
            ui_visible=False,
            config_fields=(
                ConfigField(key="max_results", label="Max files", type="number", default=50),
            ),
        )

    def __init__(
        self,
        drive_service: GoogleDriveOAuthService,
        s3_service: S3Service,
        node_service: Any = None,
    ):
        self.node_service = node_service
        self.drive_service = drive_service
        self.s3_service = s3_service
        self.client = httpx.AsyncClient(timeout=60.0)

    async def fetch(self, config: dict, credentials: Credentials) -> FetchResult:
        """Pull a JSON summary of folder/drive contents."""
        source = config.get("source") or {}
        options = config.get("options") or {}
        access_token = credentials.access_token
        resource_id = source.get("resource_id")

        if not resource_id:
            files = await self._list_recent_files(access_token, limit=options.get("max_results", 50))
            folder_name = "Google Drive"
        else:
            file_info = await self._get_file_info(access_token, resource_id)
            if file_info.get("mimeType") == GOOGLE_FOLDER_MIME_TYPE:
                files = await self._list_folder_files(access_token, resource_id)
                folder_name = file_info.get("name", "Google Drive Folder")
            else:
                files = [file_info]
                folder_name = file_info.get("name", "Google Drive File")

        content = {
            "source_type": "google_drive",
            "folder_name": folder_name,
            "total_files": len(files),
            "files": [
                {
                    "name": f.get("name"),
                    "mimeType": f.get("mimeType"),
                    "size": f.get("size"),
                    "modifiedTime": f.get("modifiedTime"),
                }
                for f in files
            ],
        }
        content_hash = hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]

        return FetchResult(
            content=content,
            content_hash=content_hash,
            node_type="json",
            node_name=folder_name,
            summary=f"Google Drive '{folder_name}' with {len(files)} files",
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
            icon="google_drive",
            resource_type=(
                lambda item: "drive_folder"
                if item.get("mimeType") == GOOGLE_FOLDER_MIME_TYPE
                else "drive_file"
            ),
        )

    async def _list_recent_files(
        self,
        access_token: str,
        limit: int = 50,
    ) -> list[dict]:
        """List recent files from Drive."""
        params = {
            "pageSize": min(limit, 100),
            "orderBy": "modifiedTime desc",
            "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
            "q": "trashed = false",
        }

        response = await self.client.get(
            self.DRIVE_FILES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        response.raise_for_status()
        return response.json().get("files", [])

    async def _list_folder_files(
        self,
        access_token: str,
        folder_id: str,
    ) -> list[dict]:
        """List files in a specific folder."""
        params = {
            "pageSize": 100,
            "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
            "q": f"'{folder_id}' in parents and trashed = false",
        }

        response = await self.client.get(
            self.DRIVE_FILES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        response.raise_for_status()
        return response.json().get("files", [])

    async def _get_file_info(self, access_token: str, file_id: str) -> dict:
        """Get info for a specific file."""
        params = {
            "fields": "id,name,mimeType,modifiedTime,size,webViewLink",
        }

        response = await self.client.get(
            f"{self.DRIVE_FILES_URL}/{file_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


def setup(deps: "ProviderDeps") -> "ProviderSetup":
    from src.provider._base import ProviderSetup
    from src.provider.oauth.google_drive_service import GoogleDriveOAuthService
    oauth_svc = GoogleDriveOAuthService()
    return ProviderSetup(
        adapter=GoogleDriveProvider(
            drive_service=oauth_svc,
            s3_service=deps.s3_service,
            node_service=deps.node_service,
        ),
        oauth_bindings={"drive": oauth_svc},
    )
