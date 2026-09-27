"""Google Drive-backed resource listing for Google Workspace connectors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

import httpx

from src.provider._base import SourceResource


DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def escape_drive_query(value: str) -> str:
    """Escape a literal for Google Drive query strings."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def list_drive_source_resources(
    client: httpx.AsyncClient,
    access_token: str,
    *,
    query: str = "",
    cursor: Optional[str] = None,
    mime_type: Optional[str] = None,
    icon: Optional[str] = None,
    resource_type: str | Callable[[dict], str] = "drive_file",
    default_name: str = "Untitled",
    page_size: int = 25,
) -> tuple[list[SourceResource], Optional[str]]:
    q_parts = ["trashed = false"]
    if mime_type:
        q_parts.append(f"mimeType = '{escape_drive_query(mime_type)}'")
    search = query.strip()
    if search:
        q_parts.append(f"name contains '{escape_drive_query(search)}'")

    params = {
        "pageSize": page_size,
        "fields": (
            "nextPageToken,"
            "files(id,name,mimeType,webViewLink,modifiedTime,owners(emailAddress),size)"
        ),
        "orderBy": "modifiedTime desc",
        "q": " and ".join(q_parts),
    }
    if cursor:
        params["pageToken"] = cursor

    response = await client.get(
        DRIVE_FILES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
    )
    response.raise_for_status()
    payload = response.json()

    resources: list[SourceResource] = []
    for item in payload.get("files", []):
        item_id = item.get("id")
        if not item_id:
            continue
        item_type = resource_type(item) if callable(resource_type) else resource_type
        owner = (item.get("owners") or [{}])[0].get("emailAddress")
        metadata = {
            "mime_type": item.get("mimeType"),
            "owner": owner,
            "size": item.get("size"),
        }
        resources.append(
            SourceResource(
                id=item_id,
                type=item_type,
                name=item.get("name") or default_name,
                url=item.get("webViewLink"),
                subtitle=item.get("modifiedTime"),
                icon=icon,
                metadata={key: value for key, value in metadata.items() if value is not None},
            )
        )
    return resources, payload.get("nextPageToken")
